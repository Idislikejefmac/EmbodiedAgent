package com.jmd.embodied.body

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.fail
import org.junit.Test

class BinaryFrameTest {
    // Same vector as protocol/v0.md section 5.1 and tests/test_binary_frame.py.
    private val golden = "00 04 00 00 00 00 00 07 00 00 01 1f 71 fb 04 cb 00 00 00 05 68 65 6c 6c 6f"

    private fun hex(b: ByteArray) = b.joinToString(" ") { "%02x".format(it) }

    @Test
    fun goldenVectorPacksAndUnpacks() {
        val bytes = BinaryFrame("echo", 7, 1234567890123L, "hello".toByteArray()).pack()
        assertEquals(golden, hex(bytes))
        val f = BinaryFrame.unpack(bytes)
        assertEquals("echo", f.type)
        assertEquals(7L, f.id)
        assertEquals(1234567890123L, f.ts)
        assertEquals(0, f.flags)
        assertArrayEquals("hello".toByteArray(), f.data)
    }

    @Test
    fun roundTripsMaxIdAnd100kB() {
        val data = ByteArray(102400) { (it % 251).toByte() }
        for (type in BinaryFrame.TYPE_CODES.keys) {
            val f = BinaryFrame.unpack(BinaryFrame(type, BinaryFrame.MAX_ID, Protocol.MAX_TS, data).pack())
            assertEquals(type, f.type)
            assertEquals(BinaryFrame.MAX_ID, f.id)
            assertEquals(Protocol.MAX_TS, f.ts)
            assertArrayEquals(data, f.data)
        }
    }

    @Test
    fun rejectsMalformedFrames() {
        val good = BinaryFrame("echo", 1, 0, "abc".toByteArray()).pack()
        val cases = mapOf(
            good.copyOf(3) to "bad_binary",
            good.copyOf(good.size - 1) to "bad_binary",
            good.copyOf(good.size + 1) to "bad_binary",
            good.copyOf().also { it[0] = 1 } to "unsupported_version",
            good.copyOf().also { it[1] = 9 } to "bad_binary",
            good.copyOf().also { it[8] = 0x80.toByte() } to "bad_binary",  // negative timestamp
        )
        for ((frame, code) in cases) {
            try {
                BinaryFrame.unpack(frame)
                fail("expected $code")
            } catch (e: BinaryFrame.FrameException) {
                assertEquals(code, e.code)
            }
        }
    }

    @Test
    fun clockOffsetPicksMinRttSample() {
        val c = ClockOffset()
        c.add(0, 1_001_000, 2_000)
        c.add(10_000, 1_011_000, 62_000)
        assertEquals(1_000_000.0, c.estimateUs!!, 0.001)
        assertEquals(2_000L, c.best!!.rttUs)
    }
}
