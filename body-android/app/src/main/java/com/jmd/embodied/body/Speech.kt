package com.jmd.embodied.body

/**
 * The parts of speech output and the Poke button that need no Android classes, so they run in JVM unit tests:
 * the `speak_local` payload gate (protocol/v0.md 4.9), the bounded speech queue with its timeouts, the poke rate
 * limit and the capability decision for `hello`. The Android side (TextToSpeech, audio focus, threads) is
 * AndroidSpeech.kt.
 */

/** A validated `speak_local` request. The only way to get one is [parse]. */
class SpeakRequest private constructor(val utteranceId: String, val text: String, val interrupt: Boolean) {
    sealed interface Parsed {
        class Ok(val request: SpeakRequest) : Parsed
        class Bad(val reason: String) : Parsed
    }

    companion object {
        /** Schema: text minLength 1, maxLength 2000 (characters, i.e. code points). */
        const val MAX_TEXT_CHARS = 2000

        private val IDENT = Regex("^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$")

        /** The single gate: raw values from the payload (any type, possibly null) into an immutable request. */
        fun parse(utteranceId: Any?, text: Any?, interrupt: Any?): Parsed {
            if (utteranceId !is String || !IDENT.matches(utteranceId)) return Parsed.Bad("utterance_id is not an identifier")
            if (text !is String) return Parsed.Bad("text is not a string")
            // Cheap guard first: a code point is at most two chars, so more than 2 x the cap is certainly too long.
            if (text.isEmpty() || text.length > 2 * MAX_TEXT_CHARS) return Parsed.Bad("text must be 1 to $MAX_TEXT_CHARS characters")
            if (text.codePointCount(0, text.length) > MAX_TEXT_CHARS) return Parsed.Bad("text must be 1 to $MAX_TEXT_CHARS characters")
            if (text.any { it.code <= 0x1f || it.code == 0x7f }) return Parsed.Bad("text contains a control character")
            if (interrupt !is Boolean) return Parsed.Bad("interrupt is not true or false")
            return Parsed.Ok(SpeakRequest(utteranceId, text, interrupt))
        }
    }
}

/** At most one yes per [minIntervalMs]; everything else is dropped, never queued. */
class RateLimiter(private val minIntervalMs: Long) {
    init {
        require(minIntervalMs > 0) { "interval must be positive" }
    }

    private var lastMs: Long? = null

    fun tryAcquire(nowMs: Long): Boolean {
        val last = lastMs
        if (last != null && nowMs - last in 0 until minIntervalMs) return false
        lastMs = nowMs
        return true
    }
}

/** The text-to-speech engine as the controller sees it. Calls must return at once (the Android one posts). */
interface SpeechEngine {
    /** Start or queue [text] under [engineId]; false if the engine refused it at once. */
    fun speak(engineId: String, text: String, flush: Boolean): Boolean
    fun stop()
    fun shutdown()
    /** Called when speech starts or the queue empties (audio focus); may do nothing. */
    fun setActive(active: Boolean) {}
}

/** How the controller tells the link what happened to one request. Called from the speech thread. */
interface SpeechReply {
    fun ack()
    fun refuse(code: String, message: String)
}

/**
 * Owns the queue and the engine for one speech session. Single-threaded by contract: the Android wrapper calls
 * every method from one thread (the main thread), so there are no locks and the tests need none.
 *
 * Bounds: at most [MAX_QUEUED] requests (playing plus waiting); a head request must start within
 * [START_TIMEOUT_MS] and finish within [speechLimitMs]; a refused or timed-out request is dropped, never retried.
 * The engine is given one utterance at a time, so a request that never starts cannot hold the ones behind it.
 *
 * Answers (protocol section 9): `ack` when speech has started, or at once when the request waits behind speech
 * that is already playing (the brain waits only 5 s for an ack, and the bounded queue guarantees the text is
 * spoken or logged as dropped); `busy` for a full queue, an interrupted wait or a start timeout; `internal`
 * when the engine failed or is unusable; `not_implemented` when no speech session exists.
 */
class SpeechController(private val log: (String) -> Unit, private val maxQueued: Int = MAX_QUEUED) {
    enum class Status { IDLE, INITIALISING, READY, UNAVAILABLE }

    companion object {
        const val MAX_QUEUED = 8
        const val START_TIMEOUT_MS = 4_000L
        private const val SPEECH_BASE_MS = 15_000L
        private const val SPEECH_PER_CHAR_MS = 100L
        private const val SPEECH_MAX_MS = 120_000L

        /** How long one utterance may play before it is cut off as stuck. */
        fun speechLimitMs(chars: Int): Long = minOf(SPEECH_MAX_MS, SPEECH_BASE_MS + SPEECH_PER_CHAR_MS * chars)

        /** The capabilities for `hello`: speak_local only when the engine is usable (protocol 4.1). */
        fun helloCapabilities(base: List<String>, speechUsable: Boolean): List<String> =
            if (speechUsable) base + "speak_local" else base
    }

    private class Item(val request: SpeakRequest, val reply: SpeechReply) {
        var engineId = ""
        var submittedMs = 0L
        var startedMs = 0L
        var started = false
        var acked = false

        fun ack() {
            if (!acked) {
                acked = true
                reply.ack()
            }
        }

        fun refuse(code: String, message: String) {
            if (!acked) {
                acked = true  // answered either way: never answer one request twice
                reply.refuse(code, message)
            }
        }
    }

    var status = Status.IDLE
        private set
    var unavailableReason = ""
        private set
    private var engine: SpeechEngine? = null
    private var current: Item? = null
    private val waiting = ArrayDeque<Item>()
    private var counter = 0
    private var active = false

    /** True when speech is playing or waiting. */
    val busy: Boolean get() = current != null || waiting.isNotEmpty()
    val queued: Int get() = (if (current != null) 1 else 0) + waiting.size

    fun beginInit() {
        if (status == Status.IDLE || status == Status.UNAVAILABLE) {
            status = Status.INITIALISING
            unavailableReason = ""
        }
    }

    /** The engine finished starting: [engine] if it is usable, otherwise [failure] says why. */
    fun initFinished(engine: SpeechEngine?, failure: String) {
        if (status != Status.INITIALISING) {  // closed while starting: nobody wants this engine any more
            engine?.shutdown()
            return
        }
        if (engine == null) {
            status = Status.UNAVAILABLE
            unavailableReason = failure.take(200)
            log("speech unavailable: $unavailableReason")
        } else {
            this.engine = engine
            status = Status.READY
            log("speech engine ready")
        }
    }

    fun submit(request: SpeakRequest, reply: SpeechReply, nowMs: Long) {
        val eng = engine
        if (status != Status.READY || eng == null) {
            when (status) {
                Status.INITIALISING -> reply.refuse("busy", "the speech engine is still starting")
                Status.UNAVAILABLE -> reply.refuse("internal", "speech is unavailable: $unavailableReason")
                else -> reply.refuse("not_implemented", "no speech session")
            }
            return
        }
        val item = Item(request, reply)
        item.submittedMs = nowMs
        if (request.interrupt) {
            // Everything before it is dropped: the playing one is cut off, waiting ones are refused.
            val cur = current
            current = null
            cur?.refuse("busy", "interrupted")
            waiting.forEach { it.refuse("busy", "interrupted") }
            waiting.clear()
            if (cur != null) eng.stop()
        } else if (queued >= maxQueued) {
            log("speech queue full ($maxQueued): refused ${request.utteranceId}")
            reply.refuse("busy", "speech queue is full")
            return
        }
        if (current != null) item.ack()  // waits behind speech that is playing: accepted (see class comment)
        waiting.addLast(item)
        pump(nowMs)
    }

    private fun pump(nowMs: Long) {
        val eng = engine ?: return
        while (current == null) {
            val item = waiting.removeFirstOrNull() ?: break
            item.engineId = "e${++counter}"
            item.submittedMs = nowMs
            val ok = try {
                eng.speak(item.engineId, item.request.text, item.request.interrupt)
            } catch (e: RuntimeException) {
                log("speech engine threw on speak: ${e.javaClass.simpleName}")
                false
            }
            if (ok) {
                current = item
                setActive(true)
            } else {
                log("speech engine refused ${item.request.utteranceId}")
                item.refuse("internal", "the speech engine refused the text")
            }
        }
        if (current == null) setActive(false)
    }

    private fun setActive(on: Boolean) {
        if (on == active) return
        active = on
        try {
            engine?.setActive(on)
        } catch (e: RuntimeException) {
            log("speech audio focus failed: ${e.javaClass.simpleName}")
        }
    }

    fun onStart(engineId: String, nowMs: Long) {
        val cur = current ?: return
        if (cur.engineId != engineId) return
        cur.started = true
        cur.startedMs = nowMs
        log("speech started ${cur.request.utteranceId} (${cur.request.text.length} chars)")
        cur.ack()
    }

    fun onDone(engineId: String, nowMs: Long) {
        val cur = current ?: return
        if (cur.engineId != engineId) return
        log("speech finished ${cur.request.utteranceId}")
        cur.ack()  // an engine that never reported a start still spoke it
        current = null
        pump(nowMs)
    }

    fun onError(engineId: String, errorCode: Int, nowMs: Long) {
        val cur = current ?: return
        if (cur.engineId != engineId) return
        log("speech error ${cur.request.utteranceId} (engine code $errorCode)")
        cur.refuse("internal", "the speech engine failed (code $errorCode)")
        current = null
        pump(nowMs)
    }

    /** Call about twice a second while [busy]: times out speech that did not start or never ends. */
    fun tick(nowMs: Long) {
        val cur = current ?: return
        val stuckBeforeStart = !cur.started && nowMs - cur.submittedMs > START_TIMEOUT_MS
        val stuckPlaying = cur.started && nowMs - cur.startedMs > speechLimitMs(cur.request.text.length)
        if (!stuckBeforeStart && !stuckPlaying) return
        log("speech ${if (stuckPlaying) "ran too long" else "did not start"}: cut off ${cur.request.utteranceId}")
        current = null
        cur.refuse("busy", "speech did not start in ${START_TIMEOUT_MS / 1000} s")
        try {
            engine?.stop()
        } catch (e: RuntimeException) {
            log("speech stop failed: ${e.javaClass.simpleName}")
        }
        pump(nowMs)
    }

    /** Stop everything and release the engine. Safe to call at any time, any number of times. */
    fun close() {
        val cur = current
        current = null
        cur?.refuse("busy", "speech stopped")
        waiting.forEach { it.refuse("busy", "speech stopped") }
        waiting.clear()
        setActive(false)
        val eng = engine
        engine = null
        status = Status.IDLE
        try {
            eng?.stop()
            eng?.shutdown()
        } catch (e: RuntimeException) {
            log("speech shutdown failed: ${e.javaClass.simpleName}")
        }
    }
}
