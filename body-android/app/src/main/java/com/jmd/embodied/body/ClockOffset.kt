package com.jmd.embodied.body

import kotlin.math.abs

/**
 * Clock-offset estimate from heartbeat round trips (protocol/v0.md section 6), the same algorithm as the
 * brain's brain/transport/clock.py. offset = remote clock - local clock.
 *
 * - Drift: once the best samples of at least 4 one-minute buckets span at least 4 minutes (buckets slowed
 *   by load ignored), a weighted least-squares slope gives [driftFitPpm] and offsets are projected to now.
 * - Estimate: the projected offset of the sample with the smallest rtt/2 + aging * age, where aging is
 *   50 ppm before a fit and 10 ppm after.
 * - Consistency: a sample whose interval (offset +/- rtt/2) cannot overlap the best's (projected +/- rtt/2 +
 *   50 ppm * age) means the older samples are wrong for this peer; they are dropped ([resets]).
 * - Stability: [maxChangeUs], the largest change within any 60 s window after a 60 s warm-up (limit 20 ms).
 */
class ClockOffset(
    private val horizon: Int = 900,
    private val agingPpm: Double = 50.0,
    private val fittedAgingPpm: Double = 10.0,
    private val historyCap: Int = 5000,
    private val bucketUs: Long = 60_000_000L,
) {
    data class Sample(val rttUs: Long, val offsetUs: Double, val atUs: Long)

    private val samples = ArrayDeque<Sample>()
    private val history = ArrayDeque<Pair<Long, Double>>()  // (local time, estimate)
    var best: Sample? = null
        private set
    var driftFitPpm: Double? = null
        private set
    var resets = 0
        private set
    var count = 0
        private set
    var nowUs = 0L
        private set
    val estimateUs: Double? get() = best?.let { project(it, nowUs) }
    val size: Int get() = samples.size

    fun project(s: Sample, now: Long): Double = s.offsetUs + (driftFitPpm ?: 0.0) * 1e-6 * (now - s.atUs)

    fun costUs(s: Sample, now: Long): Double =
        s.rttUs / 2.0 + (if (driftFitPpm == null) agingPpm else fittedAgingPpm) * 1e-6 * (now - s.atUs)

    private fun fit() {
        val buckets = HashMap<Long, Sample>()
        for (s in samples) {
            val k = Math.floorDiv(s.atUs, bucketUs)
            val b = buckets[k]
            if (b == null || s.rttUs < b.rttUs) buckets[k] = s
        }
        driftFitPpm = null
        if (buckets.size < 4) return
        val floor = buckets.values.minOf { it.rttUs }
        val good = buckets.values.filter { it.rttUs <= 2 * floor + 5000 }.sortedBy { it.atUs }
        if (good.size < 4 || good.last().atUs - good.first().atUs < 4 * bucketUs) return
        val w = good.map { 1.0 / Math.pow(it.rttUs / 2.0 + 1000, 2.0) }
        val sw = w.sum()
        val t0 = good.first().atUs
        val mx = good.indices.sumOf { w[it] * (good[it].atUs - t0) } / sw
        val my = good.indices.sumOf { w[it] * good[it].offsetUs } / sw
        val sxx = good.indices.sumOf { w[it] * Math.pow(good[it].atUs - t0 - mx, 2.0) }
        val sxy = good.indices.sumOf { w[it] * (good[it].atUs - t0 - mx) * (good[it].offsetUs - my) }
        if (sxx <= 0) return
        val ppm = sxy / sxx * 1e6
        if (abs(ppm) <= MAX_FIT_PPM) driftFitPpm = ppm
    }

    fun add(t0Local: Long, t1Remote: Long, t3Local: Long): Sample? {
        val rtt = t3Local - t0Local
        if (rtt < 0) return null
        val s = Sample(rtt, t1Remote - (t0Local + t3Local) / 2.0, t3Local)
        best?.let { b ->
            val p = project(b, t3Local)
            val bound = b.rttUs / 2.0 + agingPpm * 1e-6 * (t3Local - b.atUs)
            if (s.offsetUs + rtt / 2.0 < p - bound || s.offsetUs - rtt / 2.0 > p + bound) {
                samples.clear()
                driftFitPpm = null
                resets++
            }
        }
        samples.addLast(s)
        while (samples.size > horizon) samples.removeFirst()
        fit()
        nowUs = t3Local
        best = samples.minByOrNull { costUs(it, t3Local) }
        count++
        estimateUs?.let { history.addLast(t3Local to it) }  // always set here: best was just assigned
        while (history.size > historyCap) history.removeFirst()
        return s
    }

    /** Largest (max - min) of the estimate within any [windowS] window after [warmupS] from [sinceUs]. */
    fun maxChangeUs(sinceUs: Long = 0, warmupS: Double = 60.0, windowS: Double = 60.0): Double? {
        val all = history.filter { it.first >= sinceUs }
        if (all.isEmpty()) return null
        val start = all.first().first + (warmupS * 1e6).toLong()
        val h = all.filter { it.first >= start }
        val w = (windowS * 1e6).toLong()
        if (h.isEmpty() || h.last().first - h.first().first < w) return null
        var worst = 0.0
        var j = 0
        for (i in h.indices) {
            while (j < h.size && h[j].first - h[i].first <= w) j++
            val seg = h.subList(i, j).map { it.second }
            worst = maxOf(worst, seg.max() - seg.min())
        }
        return worst
    }

    /** Largest |estimate - least-squares line through the estimate| after [warmupS] from [sinceUs]
     * (same definition as the brain's max_dev_from_fit_us). Null with under a minute after the warm-up. */
    fun maxDevFromFitUs(sinceUs: Long = 0, warmupS: Double = 60.0): Double? {
        val all = history.filter { it.first >= sinceUs }
        if (all.isEmpty()) return null
        val start = all.first().first + (warmupS * 1e6).toLong()
        val h = all.filter { it.first >= start }
        if (h.size < 3 || h.last().first - h.first().first < 60_000_000L) return null
        val t0 = h.first().first
        val n = h.size
        val mx = h.sumOf { (it.first - t0).toDouble() } / n
        val my = h.sumOf { it.second } / n
        val sxx = h.sumOf { Math.pow(it.first - t0 - mx, 2.0) }
        val sxy = h.sumOf { (it.first - t0 - mx) * (it.second - my) }
        val slope = if (sxx > 0) sxy / sxx else 0.0
        return h.maxOf { abs(it.second - (my + slope * (it.first - t0 - mx))) }
    }

    fun clear() {
        samples.clear()
        history.clear()
        best = null
        driftFitPpm = null
    }

    companion object {
        const val MAX_FIT_PPM = 200.0
    }
}
