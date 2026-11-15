package com.jmd.embodied.body

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** speak_local validation, the bounded speech queue (with a fake engine), the poke limiter and the hello capability. */
class SpeechTest {
    private fun ok(id: Any? = "u-1", text: Any? = "Hello", interrupt: Any? = false) =
        SpeakRequest.parse(id, text, interrupt) is SpeakRequest.Parsed.Ok

    // ---- payload gate ----

    @Test
    fun validRequestParses() {
        val p = SpeakRequest.parse("u-13", "Hello, I can hear you.", true) as SpeakRequest.Parsed.Ok
        assertEquals("u-13", p.request.utteranceId)
        assertEquals("Hello, I can hear you.", p.request.text)
        assertTrue(p.request.interrupt)
    }

    @Test
    fun textLengthBoundaries() {
        assertTrue(ok(text = "a"))
        assertTrue(ok(text = "a".repeat(2000)))
        assertFalse(ok(text = "a".repeat(2001)))
        assertFalse(ok(text = ""))
        assertFalse(ok(text = "a".repeat(1_000_000)))
        // the cap counts characters (code points), like the schema: 2000 emoji (4000 UTF-16 units) pass, 2001 do not
        assertTrue(ok(text = "😀".repeat(2000)))
        assertFalse(ok(text = "😀".repeat(2001)))
    }

    @Test
    fun controlCharactersAreRefused() {
        for (c in listOf(0x00, 0x07, 0x09, 0x0a, 0x0d, 0x1b, 0x1f, 0x7f)) {
            assertFalse("U+%04X".format(c), ok(text = "a${c.toChar()}b"))
        }
        assertFalse(ok(text = "hello\n"))
        assertTrue(ok(text = "café 你好 \u0080  "))  // not in the schema's control set
    }

    @Test
    fun wrongTypesAndMissingValuesAreRefused() {
        assertFalse(ok(text = null))
        assertFalse(ok(text = 5))
        assertFalse(ok(text = JSONObject.NULL))
        assertFalse(ok(id = null))
        assertFalse(ok(id = 7))
        assertFalse(ok(id = 1.5))
        assertFalse(ok(interrupt = null))
        assertFalse(ok(interrupt = "false"))
        assertFalse(ok(interrupt = 0))
        assertFalse(ok(interrupt = JSONObject.NULL))
    }

    @Test
    fun utteranceIdPattern() {
        assertTrue(ok(id = "a"))
        assertTrue(ok(id = "u-12_x.y"))
        assertTrue(ok(id = "a".repeat(64)))
        assertFalse(ok(id = "a".repeat(65)))
        assertFalse(ok(id = ""))
        assertFalse(ok(id = ".hidden"))
        assertFalse(ok(id = "a/b"))
        assertFalse(ok(id = "a b"))
        assertFalse(ok(id = "u-1\n"))
    }

    @Test
    fun protocolGateRefusesWithBadPayloadAndNeverThrows() {
        fun msg(payload: String) = JSONObject("""{"v":0,"type":"speak_local","id":5,"reply_to":null,"ts_body":null,"ts_brain":1,"payload":$payload}""")
        Protocol.validateFromBrain(msg("""{"utterance_id":"u-1","text":"hi","interrupt":false}"""))
        for (bad in listOf("""{"utterance_id":"u-1","text":"hi"}""", "{}",
            """{"utterance_id":"u-1","text":"hi","interrupt":null}""",
            """{"utterance_id":"u-1","text":"h\u0000i","interrupt":false}""",
            """{"utterance_id":"u-1","text":null,"interrupt":false}""")) {
            try {
                Protocol.validateFromBrain(msg(bad))
                throw AssertionError("accepted $bad")
            } catch (e: Protocol.ProtocolException) {
                assertEquals("bad_payload", e.code)
                assertEquals(5L, e.replyTo)
                assertFalse(e.fatal)
            }
        }
    }

    // ---- capability decision ----

    @Test
    fun speakLocalOfferedOnlyWhenUsable() {
        val base = listOf("event", "echo")
        assertEquals(listOf("event", "echo"), SpeechController.helloCapabilities(base, false))
        assertEquals(listOf("event", "echo", "speak_local"), SpeechController.helloCapabilities(base, true))
    }

    // ---- poke limiter ----

    @Test
    fun pokeLimiterAllowsOneEveryThreeSecondsAndNeverQueues() {
        val l = RateLimiter(3_000)
        assertTrue(l.tryAcquire(10_000))
        assertFalse(l.tryAcquire(10_001))
        assertFalse(l.tryAcquire(12_999))
        assertTrue(l.tryAcquire(13_000))
        var yes = 0
        for (t in 13_001L..16_000L step 1) if (l.tryAcquire(t)) yes++  // a flood of 3,000 taps in 3 s
        assertEquals(1, yes)
        assertFalse(l.tryAcquire(16_001))
    }

    // ---- speech controller with a fake engine ----

    private class FakeEngine(var accept: Boolean = true) : SpeechEngine {
        val spoken = mutableListOf<Triple<String, String, Boolean>>()
        var stops = 0
        var shutdowns = 0
        var focus = mutableListOf<Boolean>()
        override fun speak(engineId: String, text: String, flush: Boolean): Boolean {
            if (accept) spoken += Triple(engineId, text, flush)
            return accept
        }
        override fun stop() { stops++ }
        override fun shutdown() { shutdowns++ }
        override fun setActive(active: Boolean) { focus += active }
    }

    private class Reply : SpeechReply {
        var acks = 0
        val refusals = mutableListOf<String>()
        override fun ack() { acks++ }
        override fun refuse(code: String, message: String) { refusals += code }
    }

    private fun req(id: String, text: String = "hi", interrupt: Boolean = false) =
        (SpeakRequest.parse(id, text, interrupt) as SpeakRequest.Parsed.Ok).request

    private fun ready(engine: FakeEngine = FakeEngine(), max: Int = SpeechController.MAX_QUEUED): Pair<SpeechController, FakeEngine> {
        val c = SpeechController({}, max)
        c.beginInit()
        c.initFinished(engine, "")
        return c to engine
    }

    @Test
    fun ackWhenSpeechStartsThenFinishes() {
        val (c, e) = ready()
        val r = Reply()
        c.submit(req("u-1", "hello"), r, 0)
        assertEquals(1, e.spoken.size)
        assertFalse(e.spoken[0].third)  // QUEUE_ADD by default
        assertEquals(0, r.acks)
        c.onStart(e.spoken[0].first, 100)
        assertEquals(1, r.acks)
        c.onDone(e.spoken[0].first, 900)
        assertFalse(c.busy)
        assertEquals(1, r.acks)
        assertTrue(r.refusals.isEmpty())
        assertEquals(listOf(true, false), e.focus)
    }

    @Test
    fun initFailurePathsRefuseWithProtocolCodes() {
        val failed = SpeechController({})
        failed.beginInit()
        failed.initFinished(null, "no voice data")
        assertEquals(SpeechController.Status.UNAVAILABLE, failed.status)
        val r = Reply()
        failed.submit(req("u-1"), r, 0)
        assertEquals(listOf("internal"), r.refusals)
        assertEquals(0, r.acks)

        val starting = SpeechController({})
        starting.beginInit()
        val r2 = Reply()
        starting.submit(req("u-1"), r2, 0)
        assertEquals(listOf("busy"), r2.refusals)

        val never = SpeechController({})
        val r3 = Reply()
        never.submit(req("u-1"), r3, 0)
        assertEquals(listOf("not_implemented"), r3.refusals)
    }

    @Test
    fun engineThatArrivesAfterCloseIsShutDown() {
        val c = SpeechController({})
        c.beginInit()
        c.close()
        val e = FakeEngine()
        c.initFinished(e, "")
        assertEquals(1, e.shutdowns)
        assertEquals(SpeechController.Status.IDLE, c.status)
    }

    @Test
    fun engineRefusingTheTextAnswersInternal() {
        val (c, e) = ready(FakeEngine(accept = false))
        val r = Reply()
        c.submit(req("u-1"), r, 0)
        assertEquals(listOf("internal"), r.refusals)
        assertFalse(c.busy)
        assertTrue(e.focus.isEmpty())  // audio focus was never taken
    }

    @Test
    fun engineErrorBeforeStartIsRefusedAfterStartOnlyLogged() {
        val (c, e) = ready()
        val a = Reply()
        c.submit(req("u-1"), a, 0)
        c.onError(e.spoken[0].first, 3, 10)
        assertEquals(listOf("internal"), a.refusals)
        val b = Reply()
        c.submit(req("u-2"), b, 20)
        c.onStart(e.spoken[1].first, 30)
        c.onError(e.spoken[1].first, 3, 40)
        assertEquals(1, b.acks)
        assertTrue(b.refusals.isEmpty())  // already acked: never answered twice
        assertFalse(c.busy)
    }

    @Test
    fun waitingRequestsAreAckedAtOnceAndSpokenInOrderOneAtATime() {
        val (c, e) = ready()
        val a = Reply(); val b = Reply()
        c.submit(req("u-1", "one"), a, 0)
        c.submit(req("u-2", "two"), b, 1)
        assertEquals(1, e.spoken.size)  // the engine holds one utterance at a time
        assertEquals(1, b.acks)         // behind speech that is playing: accepted at once
        c.onStart(e.spoken[0].first, 5)
        c.onDone(e.spoken[0].first, 6)
        assertEquals(2, e.spoken.size)
        assertEquals("two", e.spoken[1].second)
        c.onStart(e.spoken[1].first, 7)
        assertEquals(1, b.acks)
        assertEquals(1, a.acks)
    }

    @Test
    fun queueIsBoundedAndRefusesBeyondTheLimit() {
        val (c, e) = ready(max = 3)
        val replies = (1..5).map { Reply() }
        replies.forEachIndexed { i, r -> c.submit(req("u-$i"), r, i.toLong()) }
        assertEquals(3, c.queued)
        assertTrue(replies[3].refusals == listOf("busy") && replies[4].refusals == listOf("busy"))
        assertTrue(replies.take(3).all { it.refusals.isEmpty() })
        assertEquals(1, e.spoken.size)
    }

    @Test
    fun floodNeverGrowsPastTheNamedBound() {
        val (c, _) = ready()
        repeat(10_000) { c.submit(req("u-1"), Reply(), it.toLong()) }
        assertEquals(SpeechController.MAX_QUEUED, c.queued)
    }

    @Test
    fun interruptFlushesEverythingBeforeIt() {
        val (c, e) = ready()
        val a = Reply(); val b = Reply(); val n = Reply()
        c.submit(req("u-1", "one"), a, 0)
        c.onStart(e.spoken[0].first, 1)
        c.submit(req("u-2", "two"), b, 2)
        c.submit(req("u-3", "three", interrupt = true), n, 3)
        assertEquals(1, e.stops)
        assertTrue(b.refusals.isEmpty())  // it was acked on arrival, so the drop is only logged
        assertEquals(1, b.acks)
        val last = e.spoken.last()
        assertEquals("three", last.second)
        assertTrue(last.third)  // QUEUE_FLUSH
        assertEquals(1, c.queued)
        c.onError(e.spoken[0].first, -1, 4)  // the old utterance's stop callback is ignored
        assertEquals(1, c.queued)
    }

    @Test
    fun speechThatNeverStartsIsCutOffAndTheNextOneRuns() {
        val (c, e) = ready()
        val a = Reply(); val b = Reply()
        c.submit(req("u-1"), a, 0)
        c.tick(SpeechController.START_TIMEOUT_MS)
        assertTrue(a.refusals.isEmpty())  // not over the limit yet
        c.submit(req("u-2"), b, 10)
        c.tick(SpeechController.START_TIMEOUT_MS + 1)
        assertEquals(listOf("busy"), a.refusals.take(1))
        assertEquals(1, e.stops)
        assertEquals(2, e.spoken.size)  // the second one was handed to the engine
    }

    @Test
    fun speechThatNeverEndsIsCutOff() {
        val (c, e) = ready()
        c.submit(req("u-1", "hello"), Reply(), 0)
        c.onStart(e.spoken[0].first, 10)
        c.tick(10 + SpeechController.speechLimitMs(5))
        assertTrue(c.busy)
        c.tick(11 + SpeechController.speechLimitMs(5))
        assertFalse(c.busy)
        assertEquals(1, e.stops)
        assertEquals(120_000L, SpeechController.speechLimitMs(2000).coerceAtMost(120_000L))
    }

    @Test
    fun closeReleasesTheEngineAndAnswersEverythingPending() {
        val (c, e) = ready()
        val a = Reply(); val b = Reply()
        c.submit(req("u-1"), a, 0)
        c.submit(req("u-2"), b, 1)
        c.close()
        c.close()  // idempotent
        assertEquals(1, e.shutdowns)
        assertEquals(SpeechController.Status.IDLE, c.status)
        assertEquals(listOf("busy"), a.refusals)
        assertFalse(c.busy)
        val r = Reply()
        c.submit(req("u-3"), r, 5)  // after close: refused, engine untouched
        assertEquals(listOf("not_implemented"), r.refusals)
        assertEquals(1, e.spoken.size)
    }

    @Test
    fun throwingEngineNeverEscapesTheController() {
        val bad = object : SpeechEngine {
            override fun speak(engineId: String, text: String, flush: Boolean): Boolean = throw IllegalStateException("boom")
            override fun stop() {}
            override fun shutdown() {}
        }
        val d = SpeechController({})
        d.beginInit()
        d.initFinished(bad, "")
        val r = Reply()
        d.submit(req("u-1"), r, 0)
        assertEquals(listOf("internal"), r.refusals)
        assertFalse(d.busy)
    }
}
