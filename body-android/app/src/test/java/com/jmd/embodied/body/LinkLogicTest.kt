package com.jmd.embodied.body

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import kotlin.math.abs
import kotlin.random.Random

class LinkLogicTest {
    // ---- authentication: same known-answer vectors as tests/test_transport_server.py (checked with .NET too) ----

    private val token = ByteArray(32) { it.toByte() }
    private val a = "aa".repeat(32)
    private val b = "bb".repeat(32)
    private val c = "cc".repeat(32)

    @Test
    fun hmacProofMatchesThePythonBrain() {
        assertEquals("361f76624d4ad34c13145c8beb4d0af5485f0bfa6c9ec01147b51af80f0d1715",
            LinkAuth.proof(token, "body", listOf(a, b)))
        assertEquals("00e75be5aa0f7100fedccde85f835bb4cd24f399bc4fae1d260c1bff6ab1819e",
            LinkAuth.proof(token, "brain", listOf(a, b, c)))
        assertEquals("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824", LinkAuth.digest("hello"))
    }

    @Test
    fun proofIsRoleAndTranscriptBound() {
        val tr = listOf(a, b)
        val brainMac = LinkAuth.proof(token, "brain", tr)
        assertTrue(LinkAuth.check(token, "brain", tr, brainMac))
        assertFalse(LinkAuth.check(token, "body", tr, brainMac))  // no reflection
        assertFalse(LinkAuth.check(token, "brain", listOf(a, c), brainMac))  // other hello, other proof
        assertFalse(LinkAuth.check(ByteArray(32), "brain", tr, brainMac))  // rogue brain: other token
        assertFalse(LinkAuth.check(token, "brain", tr, null))
        assertEquals(64, LinkAuth.newNonce().length)
        assertTrue(LinkAuth.newNonce() != LinkAuth.newNonce())
    }

    @Test
    fun tokenParsing() {
        assertTrue(LinkAuth.parseToken(LinkAuth.hex(token))!!.contentEquals(token))
        assertNull(LinkAuth.parseToken("abc"))
        assertNull(LinkAuth.parseToken("zz".repeat(32)))
        assertNull(LinkAuth.parseToken(null))
    }

    // ---- clock ----

    /** Heartbeats every 2 s with random queueing between clocks drifting at [ppm]. Returns the true offset. */
    private fun drifting(cl: ClockOffset, ppm: Double, minutes: Int, startUs: Long = 0): (Long) -> Double {
        val rng = Random(1)
        val off = { t: Long -> 5_000_000.0 + ppm * 1e-6 * t }
        var t = startUs
        repeat(minutes * 30) {
            val up = 10_000 + (-15_000 * Math.log(1 - rng.nextDouble())).toLong()
            val down = 10_000 + (-15_000 * Math.log(1 - rng.nextDouble())).toLong()
            cl.add(t, (t + up + off(t + up)).toLong(), t + up + down)
            t += 2_000_000
        }
        return off
    }

    @Test
    fun clockOffsetHoldsThroughALongBurstOfDelayedSamples() {
        val cl = ClockOffset()
        val off = 5_000_000L
        var t = 0L
        repeat(30) { cl.add(t, off + t + 10_000, t + 20_000); t += 2_000_000 }
        val idle = cl.estimateUs!!
        repeat(60) { i ->
            cl.add(t, off + t + 10_000, t + 300_000 + i * 17_500L)
            t += 2_000_000
        }
        assertTrue(abs(cl.estimateUs!! - idle) < 20_000)
        assertEquals(off.toDouble(), cl.estimateUs!!, 1.0)
    }

    @Test
    fun driftIsFittedAndStabilityIsMeasuredPerMinute() {
        val cl = ClockOffset()
        val off = drifting(cl, -28.6, 20)
        assertEquals(-28.6, cl.driftFitPpm!!, 3.0)
        assertTrue(abs(cl.estimateUs!! - off(cl.nowUs)) < 5_000)
        assertTrue(cl.maxChangeUs()!! < 20_000)
    }

    @Test
    fun consistencyCheckDropsADifferentPeer() {
        val cl = ClockOffset()
        drifting(cl, 0.0, 5)
        val t = cl.nowUs + 2_000_000
        cl.add(t, t + 10_000 + 250_000_000, t + 20_000)
        assertEquals(1, cl.resets)
        assertEquals(250_000_000.0, cl.estimateUs!!, 20_000.0)
    }

    @Test
    fun stabilityNeedsTwoMinutesOfData() {
        val cl = ClockOffset()
        drifting(cl, 0.0, 1)
        assertNull(cl.maxChangeUs())
        drifting(cl, 0.0, 2, startUs = cl.nowUs + 2_000_000)
        assertNotNull(cl.maxChangeUs())
    }

    // ---- backpressure with a stalled reader ----

    @Test
    fun stalledReaderNeverGetsMoreThanTheCapAndOldestBulkIsDropped() {
        var socketQueued = 0L  // the peer never reads: nothing ever leaves the socket queue
        val written = mutableListOf<String>()
        val dropped = mutableListOf<String>()
        val q = SendQueue({ socketQueued }, socketCap = 1_000_000, pendingCap = 2_000_000)
        fun bulk(name: String, size: Long) = SendQueue.Item("image", size, { dropped += name }) {
            socketQueued += size; written += name; true
        }
        for (i in 0 until 20) q.offer(bulk("img$i", 300_000))
        assertEquals(listOf("img0", "img1", "img2"), written)  // 3 x 300 kB fit under 1 MB
        assertTrue(socketQueued <= 1_000_000)
        assertTrue(q.pendingBytes <= 2_000_000)
        assertEquals("img3", dropped.first())  // the oldest waiting bulk goes first
        while (q.pendingCount > 0) {
            socketQueued = 0
            q.pump()
        }
        assertEquals("img19", written.last())
        assertEquals(20, written.size + dropped.size)
    }

    @Test
    fun criticalMessagesPassAStalledSocketAndQueuedBulk() {
        var socketQueued = 5_000_000L  // already over the cap: bulk must wait
        val order = mutableListOf<String>()
        val q = SendQueue({ socketQueued }, socketCap = 1_000_000, pendingCap = 4_000_000)
        q.offer(SendQueue.Item("image", 100_000) { order += "image"; true })
        assertEquals(1, q.pendingCount)
        assertTrue(q.critical(SendQueue.Item("heartbeat", 200) { order += "heartbeat"; true }))
        assertEquals(listOf("heartbeat"), order)  // written at once, ahead of the waiting image
        socketQueued = 0
        q.pump()
        assertEquals(listOf("heartbeat", "image"), order)
        // Every message type the body sends is either critical or bulk, never both.
        val bodyTypes = setOf("hello", "auth", "heartbeat", "pose", "tracking_state", "image", "audio_in", "event",
            "ack", "error", "echo")
        assertTrue(bodyTypes.containsAll(SendQueue.CRITICAL))
    }

    @Test
    fun oneLargeItemPassesAnEmptyQueue() {
        var queued = 0L
        val q = SendQueue({ queued }, socketCap = 1_000, pendingCap = 1_000)
        var sent = false
        q.offer(SendQueue.Item("image", 8_000_000) { sent = true; queued += 8_000_000; true })
        assertTrue(sent)
    }

    // ---- validation order and limits ----

    private fun envelope(vararg extra: Pair<String, Any?>): JSONObject {
        val o = JSONObject("""{"v":0,"type":"heartbeat","id":1,"reply_to":null,"ts_body":null,"ts_brain":5,"payload":{}}""")
        for ((k, v) in extra) o.put(k, v ?: JSONObject.NULL)
        return o
    }

    private fun codeOf(o: JSONObject): String? = try {
        Protocol.validateFromBrain(o); null
    } catch (e: Protocol.ProtocolException) {
        e.code
    }

    @Test
    fun versionIsCheckedBeforeUnknownEnvelopeFields() {
        assertEquals("unsupported_version", codeOf(envelope("v" to 1, "extra" to 1)))
        assertEquals("bad_envelope", codeOf(envelope("extra" to 1)))
        assertNull(codeOf(envelope()))
    }

    @Test
    fun brainMustStampItsOwnTimestamp() {
        assertEquals("bad_envelope", codeOf(envelope("ts_brain" to null)))
        assertEquals("bad_envelope", codeOf(envelope("ts_brain" to -1)))
    }

    @Test
    fun anInvalidErrorIsNeverAnswered() {
        try {
            Protocol.validateFromBrain(envelope("type" to "error", "payload" to JSONObject()))
            fail("expected a refusal")
        } catch (e: Protocol.ProtocolException) {
            assertTrue(e.silent)
        }
    }

    @Test
    fun brainHelloAndAuthAreChecked() {
        val hello = JSONObject().put("role", "brain").put("software", "x").put("session_id", "s-1")
            .put("capabilities", JSONArray())
        assertEquals("bad_payload", codeOf(envelope("type" to "hello", "payload" to hello)))  // no nonce
        hello.put("auth", JSONObject().put("nonce", a))
        assertNull(codeOf(envelope("type" to "hello", "payload" to hello)))
        hello.put("role", "body")
        assertEquals("bad_payload", codeOf(envelope("type" to "hello", "payload" to hello)))
        assertEquals("bad_payload", codeOf(envelope("type" to "auth", "reply_to" to 2, "payload" to JSONObject())))
        assertNull(codeOf(envelope("type" to "auth", "reply_to" to 2, "payload" to JSONObject().put("mac", b))))
    }

    @Test
    fun errorCodesAreTheSpecsAndMessagesHaveNoControlCharacters() {
        try {
            Protocol.ProtocolException("no_such_code", "x")
            fail("unknown code accepted")
        } catch (e: IllegalArgumentException) {
        }
        assertEquals("a?b", Protocol.ProtocolException("internal", "a\nb").message)
        assertEquals("\"x\\ny\"", Protocol.quote("x\ny"))
    }

    @Test
    fun textCapCountsUtf8Bytes() {
        assertFalse(Protocol.textTooLarge("a".repeat(Protocol.MAX_TEXT_BYTES)))
        val accented = "é".repeat(40_000)  // 40,000 chars, 80,000 bytes
        assertTrue(accented.length < Protocol.MAX_TEXT_BYTES)
        assertTrue(Protocol.textTooLarge(accented))
        assertEquals(4, Protocol.utf8Length("😀"))
        assertEquals(3, Protocol.utf8Length("€"))
    }

    @Test
    fun commandsOutsideTheAllowlistAreRefused() {
        val cmd = JSONObject().put("name", "run_shell").put("args", JSONObject())
        assertEquals("unknown_command", codeOf(envelope("type" to "command", "payload" to cmd)))
        val smuggled = JSONObject().put("name", "get_status").put("args", JSONObject().put("path", "/"))
        assertEquals("bad_args", codeOf(envelope("type" to "command", "payload" to smuggled)))
    }
}
