package com.jmd.embodied.body

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ApplicationInfo
import android.os.BatteryManager
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import kotlin.math.min
import kotlin.random.Random

/**
 * The body's protocol v0 link to the brain (protocol/v0.md): authenticated handshake, heartbeats, liveness,
 * reconnect with backoff, clock offset, command allowlist, backpressure and the echo test.
 *
 * Lifecycle (section 7): the link runs only between an explicit start() and stop(). It stops by itself when it
 * has gone [UNREACHABLE_STOP_MS] without a stable link (one that stayed up at least 10 s), when the brain shut
 * down (1001) and did not return within 60 s, or when an authenticated brain replaces it with another body
 * (4001). Authentication failures before the handshake completes count as failed attempts (backoff, then the
 * same give-up), so an unauthenticated peer cannot stop the body with one message. A CPU wake lock is held only
 * while connected, with a timeout that each heartbeat reply renews.
 *
 * All protocol state lives on one executor thread, so a JSON message and its binary frame are always sent
 * back to back and ids increase in wire order.
 */
object BrainLink {
    const val TAG = "EmbodiedLink"
    const val DEFAULT_HEARTBEAT_MS = 2000L
    const val DEFAULT_LIVENESS_MS = 6000L
    const val UNREACHABLE_STOP_MS = 3 * 60_000L
    const val MAX_ECHO_COUNT = 10_000
    const val MAX_ECHO_BYTES = 1024 * 1024
    private const val TICK_MS = 250L
    private const val PUMP_MS = 20L
    private const val HANDSHAKE_TIMEOUT_MS = 10_000L
    private const val STABLE_LINK_MS = 10_000L
    private const val GOING_AWAY_GRACE_MS = 60_000L
    private const val WAKE_LOCK_MS = 30_000L
    private const val CLOSE_GRACE_MS = 1_000L
    private const val MAX_PEER_ERRORS_PER_MIN = 20
    private const val CLOCK_INFO_EVERY = 30
    private const val ECHO_WINDOW = 4
    private const val ECHO_STALL_MS = 15_000L

    enum class State { STOPPED, NOT_PAIRED, CONNECTING, CONNECTED, WAITING }
    private enum class Phase { OPENING, HELLO_SENT, AUTH_SENT, READY }

    interface Listener {
        fun onState(state: State, detail: String)
        fun onLog(line: String)
        fun onEcho(running: Boolean) {}
    }

    @Volatile var state = State.STOPPED; private set
    @Volatile var detail = ""; private set
    @Volatile var echoRunning = false; private set
    val isRunning: Boolean get() = state != State.STOPPED && state != State.NOT_PAIRED
    private val listeners = CopyOnWriteArrayList<Listener>()
    fun addListener(l: Listener) = listeners.add(l)
    fun removeListener(l: Listener) = listeners.remove(l)

    private val exec = Executors.newSingleThreadScheduledExecutor { r -> Thread(r, "brain-link") }
    private val client = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.SECONDS)  // liveness and the handshake timeout are ours (tick)
        .writeTimeout(15, TimeUnit.SECONDS)
        .build()
    private val bootId = "b-" + Random.nextBytes(4).joinToString("") { "%02x".format(it) }

    // ---- state below is touched only on the exec thread ----
    private var appContext: Context? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var host = "127.0.0.1"
    private var port = 8765
    private var token: ByteArray? = null
    private var started = false
    private var gen = 0
    private var ws: WebSocket? = null
    private var sendQueue: SendQueue? = null
    private var phase = Phase.OPENING
    private var helloId = 0L
    private var authId = 0L
    private var transcript = mutableListOf<String>()
    private var lastSentText = ""
    private var connectStartMs = 0L
    private var connectedSinceMs = 0L
    private var stable = false
    private var nextId = 1L
    private var lastSendTs = 0L
    private var peerLastId = 0L
    private var idGaps = 0
    private var lastRxMs = 0L
    private var lastHeartbeatMs = 0L
    private var attempts = 0
    private var downSinceMs = 0L
    private var lastFailure = ""
    private var goingAwayUntilMs = 0L
    private var unreachableStopMs = UNREACHABLE_STOP_MS
    private var reconnectTask: ScheduledFuture<*>? = null
    private var tickTask: ScheduledFuture<*>? = null
    private var pumpTask: ScheduledFuture<*>? = null
    private val pendingHeartbeats = LinkedHashMap<Long, Long>()  // heartbeat id -> our own send stamp (t0)
    private val clock = ClockOffset()
    private var sessionClockStartUs = 0L
    private var brainBootId: String? = null
    private var heartbeatReplies = 0
    private var heartbeatMs = DEFAULT_HEARTBEAT_MS
    private var livenessMs = DEFAULT_LIVENESS_MS
    private var maxBinary = BinaryFrame.MAX_DATA.toLong()
    private var pendingHeader: JSONObject? = null
    private var sessionId = ""
    private var echo: EchoRun? = null
    private val peerErrors = ArrayDeque<Long>()

    // ---- public API (any thread) ----

    fun start(context: Context, host: String, port: Int) = post {
        val ctx = context.applicationContext
        appContext = ctx
        if (wakeLock == null) {
            wakeLock = ctx.getSystemService(PowerManager::class.java)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "EmbodiedBody:link").apply { setReferenceCounted(false) }
        }
        token = LinkSettings.loadToken(ctx)
        if (token == null) {
            stopInternal("not paired with a brain: run `python -m brain pair` on the laptop", State.NOT_PAIRED)
            return@post
        }
        val changed = host != this.host || port != this.port
        this.host = host
        this.port = port
        if (!started || changed) {
            started = true
            downSinceMs = SystemClock.elapsedRealtime()
            lastFailure = ""
            goingAwayUntilMs = 0
            attempts = 0
            log("target ws://$host:$port/ (boot_id $bootId; gives up after ${unreachableStopMs / 1000} s without a stable link)")
            drop("target set", reconnectNow = true)
        }
    }

    fun stop(reason: String) = post { stopInternal(reason) }

    fun reconnect() = post { if (started) drop("reconnect requested", reconnectNow = true) }

    fun runEcho(count: Int, bytes: Int) = post { startEcho(count, bytes) }

    /** Debug builds only: shorten the give-up time for the next run (5 s to 3 min); reset by stop. */
    fun setUnreachableStopMs(context: Context, ms: Long) = post {
        val debuggable = (context.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) != 0
        if (debuggable) {
            unreachableStopMs = ms.coerceIn(5_000L, UNREACHABLE_STOP_MS)
            log("give-up time set to ${unreachableStopMs / 1000} s (debug)")
        }
    }

    // ---- executor helpers: an exception in a task is logged, never silently ends the link ----

    private fun guarded(block: () -> Unit) = Runnable {
        try {
            block()
        } catch (t: Throwable) {
            Log.e(TAG, "link task failed", t)
        }
    }

    private fun post(block: () -> Unit) = exec.execute(guarded(block))

    // ---- connection lifecycle ----

    private fun connect() {
        if (!started) return
        val myGen = ++gen
        phase = Phase.OPENING
        connectStartMs = SystemClock.elapsedRealtime()
        lastRxMs = connectStartMs
        setState(State.CONNECTING, "ws://$host:$port/")
        sendQueue = SendQueue({ ws?.queueSize() ?: 0L })
        val request = Request.Builder().url("ws://$host:$port/").build()
        ws = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) =
                post { if (myGen == gen) onOpen() }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val rx = Protocol.nowUs()
                post { if (myGen == gen) onText(text, rx) }
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                val data = bytes.toByteArray()
                post { if (myGen == gen) onBinary(data) }
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
                post { if (myGen == gen) onBrainClosed(code, reason) }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) =
                post { if (myGen == gen) drop("failure: ${t.javaClass.simpleName}: ${t.message}") }
        })
        // Scheduled from connect, not onOpen, so a stalled TCP or WebSocket handshake also times out.
        tickTask = exec.scheduleWithFixedDelay(guarded { if (myGen == gen) tick() }, TICK_MS, TICK_MS, TimeUnit.MILLISECONDS)
    }

    private fun onOpen() {
        nextId = 1
        peerLastId = 0
        idGaps = 0
        pendingHeader = null
        pendingHeartbeats.clear()
        heartbeatReplies = 0
        peerErrors.clear()
        heartbeatMs = DEFAULT_HEARTBEAT_MS
        livenessMs = DEFAULT_LIVENESS_MS
        maxBinary = BinaryFrame.MAX_DATA.toLong()
        lastRxMs = SystemClock.elapsedRealtime()
        val payload = JSONObject()
            .put("role", "body")
            .put("software", "embodied-body/${BuildConfigInfo.versionName(appContext)}")
            .put("versions", JSONArray(listOf(Protocol.VERSION)))
            .put("boot_id", bootId)
            .put("capabilities", JSONArray(listOf("event", "echo")))
            .put("commands", JSONArray(Protocol.IMPLEMENTED_COMMANDS))
            .put("auth", JSONObject().put("nonce", LinkAuth.newNonce()))
        helloId = sendCritical("hello", payload) ?: return
        transcript = mutableListOf(LinkAuth.digest(lastSentText))
        phase = Phase.HELLO_SENT
    }

    /** Close the current socket (politely if [closeCode] is given) and forget per-connection state. */
    private fun teardown(closeCode: Int?, reason: String) {
        gen++ // ignore any further callbacks from the old socket
        if (phase == Phase.READY) logClockSummary()
        val old = ws
        ws = null
        if (old != null) {
            if (closeCode != null) {
                old.close(closeCode, reason.take(120))  // queued after anything already sent, e.g. our error
                exec.schedule(guarded { old.cancel() }, CLOSE_GRACE_MS, TimeUnit.MILLISECONDS)
            } else {
                old.cancel()
            }
        }
        phase = Phase.OPENING
        pendingHeader = null
        pendingBrainHello = null
        transcript = mutableListOf()
        tickTask?.cancel(false)
        tickTask = null
        pumpTask?.cancel(false)
        pumpTask = null
        reconnectTask?.cancel(false)
        reconnectTask = null
        sendQueue?.clear()
        sendQueue = null
        echo?.let { finishEcho(it, "link down: $reason") }
        releaseWake()
    }

    /** Tear down the current connection and schedule the next attempt, or give up. */
    private fun drop(reason: String, reconnectNow: Boolean = false, closeCode: Int? = null) {
        if (!started) return
        val wasConnected = phase == Phase.READY
        val wasStable = wasConnected && stable
        teardown(closeCode, reason)
        stable = false
        val now = SystemClock.elapsedRealtime()
        if (wasConnected) log("disconnected: $reason")
        // The give-up clock runs from the end of the last stable link: a brain that accepts and then always
        // closes within 10 s does not keep the body trying forever.
        if (wasStable || downSinceMs == 0L) downSinceMs = now
        if (!reconnectNow) lastFailure = reason
        val down = now - downSinceMs
        if (down >= unreachableStopMs) {
            stopInternal("no stable link to the brain for ${down / 1000} s (last: $lastFailure); press Connect to try again")
            return
        }
        if (goingAwayUntilMs != 0L && now >= goingAwayUntilMs) {
            stopInternal("brain shut down (1001) and did not come back within ${GOING_AWAY_GRACE_MS / 1000} s")
            return
        }
        val delay = if (reconnectNow) 0L else backoffMs()
        if (!reconnectNow) attempts++
        if (!wasConnected && !reconnectNow) Log.d(TAG, "connect attempt failed ($reason); retry in $delay ms")
        setState(State.WAITING, if (reconnectNow) reason else "retry in $delay ms ($reason)")
        reconnectTask = exec.schedule(guarded { reconnectTask = null; connect() }, delay, TimeUnit.MILLISECONDS)
    }

    private fun stopInternal(reason: String, final: State = State.STOPPED, closeCode: Int = Protocol.CLOSE_NORMAL) {
        val wasStarted = started
        started = false
        teardown(closeCode, "body stopped")
        stable = false
        downSinceMs = 0
        unreachableStopMs = UNREACHABLE_STOP_MS  // a debug override lasts one run
        if (wasStarted || state != final) log("${if (final == State.NOT_PAIRED) "not paired" else "stopped"}: $reason")
        setState(final, reason)
    }

    private fun onBrainClosed(code: Int, reason: String) {
        val r = Protocol.quote(reason)
        when {
            // Only an authenticated brain may retire this body; before that it is just a failed attempt.
            code == Protocol.CLOSE_REPLACED && phase == Phase.READY ->
                stopInternal("another body connected to the brain and replaced this one (4001)")
            code == Protocol.CLOSE_GOING_AWAY && phase == Phase.READY -> {
                if (goingAwayUntilMs == 0L) goingAwayUntilMs = SystemClock.elapsedRealtime() + GOING_AWAY_GRACE_MS
                drop("brain going away (1001 $r)")
            }
            code == Protocol.CLOSE_AUTH_FAILED -> drop("authentication refused (1008 $r): pairing may be wrong")
            else -> drop("brain closed: $code $r")
        }
    }

    /** 0.5 s, 1 s, 2 s, then 4 s, each with up to 20 % jitter (section 7). */
    private fun backoffMs(): Long {
        val base = min(500L shl min(attempts, 3), 4000L)
        return (base * (1.0 + Random.nextDouble(-0.2, 0.2))).toLong()
    }

    private fun tick() {
        val now = SystemClock.elapsedRealtime()
        if (phase != Phase.READY) {
            if (now - connectStartMs > HANDSHAKE_TIMEOUT_MS) drop("no handshake within ${HANDSHAKE_TIMEOUT_MS / 1000} s")
            return
        }
        val silent = now - lastRxMs
        if (silent > livenessMs) {
            drop("no valid frame for $silent ms (liveness)")
            return
        }
        // Only a link that stayed up resets the backoff and the give-up clock.
        if (!stable && now - connectedSinceMs >= STABLE_LINK_MS) {
            stable = true
            attempts = 0
            downSinceMs = 0
        }
        if (now - lastHeartbeatMs >= heartbeatMs) {
            lastHeartbeatMs = now
            val payload = JSONObject()
                .put("offset_us", clock.estimateUs?.toLong() ?: JSONObject.NULL)
                .put("rtt_us", clock.best?.rttUs ?: JSONObject.NULL)
            sendCritical("heartbeat", payload)?.let {
                pendingHeartbeats[it] = lastSendTs
                while (pendingHeartbeats.size > 16) pendingHeartbeats.remove(pendingHeartbeats.keys.first())
            }
        }
    }

    // ---- sending ----

    /** Write one message and its binary frame (if [data] is given) to the socket now. Returns the id. */
    private fun write(type: String, payload: JSONObject, replyTo: Long?, data: ByteArray?, tsBrain: Long?,
                      ts: Long? = null): Long? {
        val w = ws ?: return null
        val id = nextId
        val stamp = ts ?: Protocol.nowUs()
        val text = Protocol.envelope(type, id, replyTo, stamp, tsBrain, payload).toString()
        if (Protocol.textTooLarge(text)) {
            Log.e(TAG, "not sending $type: over ${Protocol.MAX_TEXT_BYTES} bytes")
            return null
        }
        nextId++
        lastSendTs = stamp
        lastSentText = text
        var ok = w.send(text)
        if (ok && data != null && data.isNotEmpty()) ok = w.send(BinaryFrame(type, id, stamp, data).pack().toByteString())
        if (!ok) {
            val g = gen
            post { if (g == gen) drop("send failed (socket closing or queue full)") }
            return null
        }
        return id
    }

    /** hello, auth, heartbeat, ack, error, event (SendQueue.CRITICAL): small, written at once, never dropped. */
    private fun sendCritical(type: String, payload: JSONObject, replyTo: Long? = null, tsBrain: Long? = null): Long? {
        require(type in SendQueue.CRITICAL) { "$type is bulk" }
        var id: Long? = null
        val sq = sendQueue ?: return null
        sq.critical(SendQueue.Item(type, 0) { id = write(type, payload, replyTo, null, tsBrain); id != null })
        return id
    }

    /** image, audio_in, pose, echo: through the backpressure queue; may be dropped (oldest first). */
    private fun sendBulk(type: String, payload: JSONObject, data: ByteArray?, replyTo: Long? = null, ts: Long? = null,
                         onWritten: (Long) -> Unit = {}, onDropped: () -> Unit = {}): Boolean {
        require(type !in SendQueue.CRITICAL) { "$type is critical" }
        val sq = sendQueue ?: return false
        if (ws == null) return false
        val item = SendQueue.Item(type, (data?.size ?: 0) + 512L, onDropped) {
            val id = write(type, payload, replyTo, data, null, ts)
            if (id != null) onWritten(id)
            id != null
        }
        sq.offer(item) { Log.w(TAG, "backpressure: dropped queued ${it.type} (${it.bytes} bytes; ${sq.dropped} dropped so far)") }
        if (sq.pendingCount > 0 && pumpTask == null) {
            val myGen = gen
            pumpTask = exec.scheduleWithFixedDelay(guarded {
                val q = sendQueue
                if (myGen != gen || q == null) return@guarded
                q.pump()
                if (q.pendingCount == 0) {
                    pumpTask?.cancel(false)
                    pumpTask = null
                }
            }, PUMP_MS, PUMP_MS, TimeUnit.MILLISECONDS)
        }
        return true
    }

    private fun sendError(e: Protocol.ProtocolException) {
        Log.w(TAG, "refused: ${e.code} (reply_to=${e.replyTo}, fatal=${e.fatal}${if (e.silent) ", not answered" else ""}): ${Protocol.quote(e.message)}")
        if (!e.silent) {
            sendCritical("error", JSONObject().put("code", e.code).put("message", e.message ?: "")
                .put("fatal", e.fatal), replyTo = e.replyTo)
        }
        if (e.fatal) {
            drop("fatal ${e.code}", closeCode = if (e.code == "unauthorized") Protocol.CLOSE_AUTH_FAILED else Protocol.CLOSE_PROTOCOL_ERROR)
        }
    }

    /** Count a protocol error caused by the brain; more than 20 a minute makes it fatal. */
    private fun peerFault(e: Protocol.ProtocolException): Protocol.ProtocolException {
        val now = SystemClock.elapsedRealtime()
        peerErrors.addLast(now)
        while (peerErrors.isNotEmpty() && now - peerErrors.first() > 60_000) peerErrors.removeFirst()
        if (peerErrors.size > MAX_PEER_ERRORS_PER_MIN && !e.fatal) {
            return Protocol.ProtocolException(e.code, "too many protocol errors (${peerErrors.size} in 1 min); last: ${e.message}",
                true, e.replyTo)
        }
        return e
    }

    // ---- receiving ----

    private fun onText(text: String, rxUs: Long) {
        val header = pendingHeader
        if (header != null) {
            pendingHeader = null
            sendError(Protocol.ProtocolException("bad_binary", "text frame where a binary frame was due", true,
                Protocol.optLong(header, "id")))
            return
        }
        try {
            if (Protocol.textTooLarge(text)) throw Protocol.ProtocolException("too_large", "text frame too large", true)
            val msg = try {
                JSONObject(text)
            } catch (e: Exception) {
                throw Protocol.ProtocolException("bad_json", "not a JSON object")
            } catch (e: StackOverflowError) {
                throw Protocol.ProtocolException("bad_json", "nested too deeply")
            }
            Protocol.optLong(msg, "id")?.let {
                if (it != peerLastId + 1) {
                    idGaps++
                    Log.w(TAG, "id gap: expected ${peerLastId + 1}, got $it")
                }
                peerLastId = it
            }
            Protocol.validateFromBrain(msg)
            lastRxMs = SystemClock.elapsedRealtime()
            if (Protocol.hasBinary(msg)) {
                pendingHeader = msg
                return
            }
            handle(msg, null, rxUs, text)
        } catch (e: Protocol.ProtocolException) {
            sendError(peerFault(e))
        }
    }

    private fun onBinary(frame: ByteArray) {
        val header = pendingHeader
        if (header == null) {
            sendError(peerFault(Protocol.ProtocolException("bad_sequence", "binary frame of ${frame.size} bytes was not due; dropped")))
            return
        }
        pendingHeader = null
        val id = header.getLong("id")
        try {
            val f = BinaryFrame.unpack(frame)
            val want = header.getJSONObject("payload").getLong("byte_length")
            if (f.type != header.getString("type") || f.id != id || f.ts != Protocol.optLong(header, "ts_brain") ||
                f.data.size.toLong() != want) {
                throw Protocol.ProtocolException("bad_binary", "binary frame does not match message $id", true, id)
            }
            lastRxMs = SystemClock.elapsedRealtime()
            handle(header, f.data, Protocol.nowUs(), "")
        } catch (e: BinaryFrame.FrameException) {
            sendError(Protocol.ProtocolException(e.code, e.message ?: "", true, id))
        } catch (e: Protocol.ProtocolException) {
            sendError(e)
        }
    }

    private fun handle(msg: JSONObject, data: ByteArray?, rxUs: Long, raw: String) {
        val type = msg.getString("type")
        val id = msg.getLong("id")
        val replyTo = Protocol.optLong(msg, "reply_to")
        val payload = msg.getJSONObject("payload")
        when (phase) {
            Phase.OPENING -> return
            Phase.HELLO_SENT -> {
                when {
                    type == "error" -> handshakeError(payload)
                    type == "hello" && replyTo == helloId -> onBrainHello(payload, raw)
                    else -> sendError(Protocol.ProtocolException("bad_sequence", "expected hello reply, got $type", true, id))
                }
                return
            }
            Phase.AUTH_SENT -> {
                when {
                    type == "error" -> handshakeError(payload)
                    type == "auth" && replyTo == authId -> onBrainAuth(id, payload)
                    else -> sendError(Protocol.ProtocolException("bad_sequence", "expected the brain's auth, got $type", true, id))
                }
                return
            }
            Phase.READY -> {}
        }
        when (type) {
            "heartbeat" -> if (replyTo == null) {
                sendCritical("heartbeat", JSONObject(), replyTo = id, tsBrain = Protocol.optLong(msg, "ts_brain"))
            } else {
                val t0 = pendingHeartbeats.remove(replyTo) ?: return  // our own stamp, not the echoed copy
                val t1 = Protocol.optLong(msg, "ts_brain") ?: return
                val resets = clock.resets
                val s = clock.add(t0, t1, rxUs) ?: return
                if (clock.resets != resets) Log.w(TAG, "clock: new sample inconsistent with the estimate; dropped the older samples")
                renewWake()
                heartbeatReplies++
                val estimate = clock.estimateUs
                val bestRtt = clock.best?.rttUs
                if (estimate != null && bestRtt != null) {
                    val line = "clock offset_ms=%.3f best_rtt_ms=%.2f rtt_ms=%.2f drift_ppm=%s (brain minus body)".format(
                        estimate / 1000, bestRtt / 1000.0, s.rttUs / 1000.0,
                        clock.driftFitPpm?.let { "%.1f".format(it) } ?: "none")
                    if (heartbeatReplies % CLOCK_INFO_EVERY == 1) Log.i(TAG, line) else Log.d(TAG, line)
                }
            }
            "echo" -> if (replyTo == null) {
                val bytes = data ?: ByteArray(0)
                if (Protocol.crc32(bytes) != payload.getLong("crc32")) {
                    sendError(Protocol.ProtocolException("bad_binary", "echo crc32 mismatch", replyTo = id))
                } else {
                    sendBulk("echo", JSONObject(payload.toString()), data, replyTo = id)
                }
            } else {
                echo?.let { onEchoReply(it, replyTo, payload, data) }
            }
            "command" -> handleCommand(id, payload)
            "error" -> {
                Log.w(TAG, "brain error ${Protocol.quote(payload.optString("code"))} (reply_to=$replyTo): ${Protocol.quote(payload.optString("message"))}")
                echo?.let { run -> if (replyTo != null && run.pending.remove(replyTo) != null) { run.corrupt++; pumpEcho(run) } }
            }
            "ack" -> Log.i(TAG, "ack for $replyTo")
            "hello", "auth" -> sendError(Protocol.ProtocolException("bad_sequence", "$type after the handshake ignored", replyTo = id))
            else -> sendError(Protocol.ProtocolException("not_implemented", "$type not implemented by this body", replyTo = id))
        }
    }

    /** An error before the handshake completes is a failed attempt, never a reason to stop (S5). */
    private fun handshakeError(payload: JSONObject) {
        val code = payload.optString("code")
        log("brain refused the handshake: ${Protocol.quote(code)} ${Protocol.quote(payload.optString("message"))}")
        drop(if (code == "unauthorized") "authentication refused by the brain: pairing may be wrong" else "brain error $code during handshake")
    }

    private fun onBrainHello(payload: JSONObject, raw: String) {
        val tok = token ?: return stopInternal("not paired", State.NOT_PAIRED)
        transcript.add(LinkAuth.digest(raw))
        pendingBrainHello = payload  // applied only after the brain's proof checks out (onBrainAuth)
        authId = sendCritical("auth", JSONObject().put("mac", LinkAuth.proof(tok, "body", transcript))) ?: return
        transcript.add(LinkAuth.digest(lastSentText))
        phase = Phase.AUTH_SENT
    }

    private var pendingBrainHello: JSONObject? = null

    private fun onBrainAuth(id: Long, payload: JSONObject) {
        val tok = token ?: return stopInternal("not paired", State.NOT_PAIRED)
        if (!LinkAuth.check(tok, "brain", transcript, payload.optString("mac"))) {
            // Not our brain (or the token changed). Counted as a failed attempt: the real brain may come back
            // (adb reverse restored), and an impostor must not be able to stop the body.
            sendError(Protocol.ProtocolException("unauthorized", "the brain failed authentication", true, id))
            return
        }
        val hello = pendingBrainHello ?: return
        pendingBrainHello = null
        sessionId = hello.getString("session_id")
        heartbeatMs = hello.optLong("heartbeat_ms", DEFAULT_HEARTBEAT_MS).coerceIn(100L, 60_000L)
        livenessMs = hello.optLong("liveness_timeout_ms", DEFAULT_LIVENESS_MS).coerceIn(300L, 300_000L)
        maxBinary = hello.optLong("max_binary_bytes", BinaryFrame.MAX_DATA.toLong()).coerceIn(0L, BinaryFrame.MAX_DATA.toLong())
        val bid = hello.optString("boot_id").ifEmpty { null }
        if (bid == null || bid != brainBootId) {
            clock.clear()
            brainBootId = bid
        } else if (clock.size > 0) {
            Log.i(TAG, "same brain run ($bid): keeping the clock estimate (${clock.size} samples)")
        }
        onReady()
    }

    private fun onReady() {
        val now = SystemClock.elapsedRealtime()
        phase = Phase.READY
        connectedSinceMs = now
        lastHeartbeatMs = now
        lastRxMs = now
        goingAwayUntilMs = 0
        sessionClockStartUs = Protocol.nowUs()
        val took = if (downSinceMs > 0) now - downSinceMs else -1
        renewWake()
        log("connected: session $sessionId (authenticated)${if (took >= 0) "; link restored $took ms after it went down" else ""}")
        setState(State.CONNECTED, "session $sessionId")
    }

    private fun logClockSummary() {
        val estimate = clock.estimateUs
        if (clock.best == null || estimate == null) return
        val change = clock.maxChangeUs(sinceUs = sessionClockStartUs)
        val dev = clock.maxDevFromFitUs(sinceUs = sessionClockStartUs)
        log("clock summary: samples=$heartbeatReplies max_change_60s_ms=${change?.let { "%.3f".format(it / 1000) } ?: "n/a (under 2 min)"} " +
            "max_dev_from_fit_ms=${dev?.let { "%.3f".format(it / 1000) } ?: "n/a (under 2 min)"} " +
            "(limits 20, after 60 s warm-up) drift_ppm=${clock.driftFitPpm?.let { "%.1f".format(it) } ?: "none"} " +
            "estimate_ms=${"%.3f".format(estimate / 1000)} resets=${clock.resets}")
    }

    /** Command boundary, body side: validateFromBrain already refused unknown names and bad args. */
    private fun handleCommand(id: Long, payload: JSONObject) {
        when (val name = payload.getString("name")) {
            "get_status" -> {
                val result = JSONObject()
                appContext?.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))?.let { b ->
                    val level = b.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
                    val scale = b.getIntExtra(BatteryManager.EXTRA_SCALE, 100)
                    if (level >= 0 && scale > 0) result.put("battery_pct", level * 100 / scale)
                    result.put("charging", b.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0)
                }
                sendCritical("ack", JSONObject().put("status", "ok").put("result", result), replyTo = id)
                log("command get_status answered")
            }
            else -> sendError(Protocol.ProtocolException("not_implemented", "$name is allowlisted but not built yet", replyTo = id))
        }
    }

    // ---- wake lock: held only while connected, self-expiring ----

    private fun renewWake() {
        wakeLock?.acquire(WAKE_LOCK_MS)
    }

    private fun releaseWake() {
        wakeLock?.let { if (it.isHeld) it.release() }
    }

    // ---- echo test ----

    private class EchoRun(val count: Int, val size: Int, val gapsAtStart: Int) {
        var sent = 0
        var queued = 0
        var received = 0
        var corrupt = 0
        var dropped = 0
        val pending = LinkedHashMap<Long, Triple<Int, String, ByteArray>>()
        val rng = Random(1)
        val startMs = SystemClock.elapsedRealtime()
        var lastProgressMs = startMs
        var watchdog: ScheduledFuture<*>? = null
    }

    private fun startEcho(count: Int, bytes: Int) {
        if (phase != Phase.READY) {
            log("echo test not started: not connected")
            return
        }
        if (echo != null) {
            log("echo test already running")
            return
        }
        val n = count.coerceIn(1, MAX_ECHO_COUNT)
        val size = bytes.coerceIn(0, min(MAX_ECHO_BYTES.toLong(), maxBinary).toInt())
        val run = EchoRun(n, size, idGaps)
        echo = run
        setEcho(true)
        log("echo test start: $n x ($size bytes + JSON)")
        run.watchdog = exec.scheduleWithFixedDelay(guarded {
            if (echo === run && SystemClock.elapsedRealtime() - run.lastProgressMs > ECHO_STALL_MS) {
                finishEcho(run, "no reply for ${ECHO_STALL_MS / 1000} s")
            }
        }, 1, 1, TimeUnit.SECONDS)
        pumpEcho(run)
    }

    private fun pumpEcho(run: EchoRun) {
        if (echo !== run) return
        if (run.received + run.corrupt + run.dropped >= run.count) {
            finishEcho(run, "done")
            return
        }
        while (run.sent < run.count && run.pending.size + run.queued < ECHO_WINDOW) {
            val seq = run.sent
            val data = run.rng.nextBytes(run.size)
            val text = "echo $seq ${Random.nextLong().toString(16)}"
            val payload = JSONObject().put("seq", seq).put("text", text).put("byte_length", data.size)
                .put("crc32", Protocol.crc32(data))
            run.sent++
            run.queued++
            val ok = sendBulk("echo", payload, data,
                onWritten = { id -> run.queued--; run.pending[id] = Triple(seq, text, data) },
                onDropped = { run.queued--; run.dropped++ })
            if (!ok) {
                run.sent--
                run.queued--
                return
            }
        }
    }

    private fun onEchoReply(run: EchoRun, replyTo: Long?, payload: JSONObject, data: ByteArray?) {
        val want = run.pending.remove(replyTo) ?: return
        val got = data ?: ByteArray(0)
        val intact = payload.optInt("seq", -1) == want.first && payload.optString("text") == want.second &&
            payload.optLong("crc32") == Protocol.crc32(want.third) && got.contentEquals(want.third)
        if (intact) run.received++ else run.corrupt++
        run.lastProgressMs = SystemClock.elapsedRealtime()
        if ((run.received + run.corrupt) % 100 == 0) Log.i(TAG, "echo progress ${run.received + run.corrupt}/${run.count}")
        pumpEcho(run)
    }

    private fun finishEcho(run: EchoRun, why: String) {
        if (echo !== run) return
        echo = null
        setEcho(false)
        run.watchdog?.cancel(false)
        val lost = run.count - run.received - run.corrupt - run.dropped
        val secs = (SystemClock.elapsedRealtime() - run.startMs) / 1000.0
        val gaps = idGaps - run.gapsAtStart
        val ok = lost == 0 && run.corrupt == 0 && run.dropped == 0 && run.received == run.count && gaps == 0
        log("ECHO RESULT sent=${run.sent} received=${run.received} lost=$lost corrupt=${run.corrupt} " +
            "dropped=${run.dropped} id_gaps=$gaps bytes_each=${run.size} seconds=%.2f ok=$ok ($why)".format(secs))
        if (phase == Phase.READY && ws != null) {
            sendCritical("event", JSONObject().put("name", "echo_test_result").put("data", JSONObject()
                .put("sent", run.sent).put("received", run.received).put("lost", lost).put("corrupt", run.corrupt)
                .put("dropped", run.dropped).put("id_gaps", gaps).put("bytes_each", run.size).put("seconds", secs)
                .put("ok", ok)))
        }
    }

    // ---- helpers: a throwing listener must never break the link thread ----

    private fun setState(s: State, d: String) {
        state = s
        detail = d
        listeners.forEach { l -> safely { l.onState(s, d) } }
    }

    private fun setEcho(running: Boolean) {
        echoRunning = running
        listeners.forEach { l -> safely { l.onEcho(running) } }
    }

    private fun log(line: String) {
        Log.i(TAG, line)
        listeners.forEach { l -> safely { l.onLog(line) } }
    }

    private inline fun safely(block: () -> Unit) {
        try {
            block()
        } catch (t: Throwable) {
            Log.e(TAG, "listener failed", t)
        }
    }
}

private object BuildConfigInfo {
    fun versionName(ctx: Context?): String = try {
        ctx?.packageManager?.getPackageInfo(ctx.packageName, 0)?.versionName ?: "0"
    } catch (e: Exception) {
        "0"
    }
}
