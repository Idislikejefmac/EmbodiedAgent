package com.jmd.embodied.body

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Hardening H-21: no forced unwrap (`!!`) in the product code, and the clock history is still filled without one. */
class NullSafetyTest {
    @Test
    fun noForcedUnwrapInMainSources() {
        val root = File("src/main/java")
        assertTrue("run from the app module directory", root.isDirectory)
        val offenders = root.walkTopDown().filter { it.extension == "kt" }.flatMap { f ->
            f.readLines().mapIndexedNotNull { i, line ->
                if (line.contains("!!")) "${f.name}:${i + 1}: ${line.trim()}" else null
            }
        }.toList()
        assertEquals(emptyList<String>(), offenders)
    }

    @Test
    fun clockHistoryStillFeedsTheStabilityFigures() {
        val c = ClockOffset()
        var t = 0L
        repeat(130) {  // one sample every 2 s, a steady offset of 1 s: about 4 minutes of history
            c.add(t, t + 1_000_000 + 500, t + 1_000)
            t += 2_000_000
        }
        assertNotNull(c.estimateUs)
        val change = c.maxChangeUs()
        assertNotNull(change)  // would be null if add() had stopped recording the history
        assertTrue(change!! < 1_000.0)
    }
}
