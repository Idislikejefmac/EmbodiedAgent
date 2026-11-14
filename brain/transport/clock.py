"""Clock-offset estimation from heartbeat round trips (protocol/v0.md section 6)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ..limits import CLOCK_HISTORY_MAX
from ..limits import CLOCK_MAX_FIT_PPM as MAX_FIT_PPM  # the limit lives in brain/limits.py (H-11; renamed there)


@dataclass(frozen=True)
class OffsetSample:
    rtt_us: int
    offset_us: float  # remote clock minus local clock
    at_us: int  # local receive time (t3)


class ClockOffsetEstimator:
    """Aged minimum-RTT filter with a fitted drift, over a long horizon of NTP-style samples.

    One sample: local send time t0, the peer's stamp t1, local receive time t3.
    rtt = t3 - t0, offset = t1 - (t0 + t3) / 2; the true offset lies within offset +/- rtt / 2.

    Drift: the two clocks run at slightly different rates (S25 Edge vs the laptop: about -28.6 ppm). Once
    there are enough good samples (the best sample in each of at least 4 one-minute buckets spanning at
    least 4 minutes, buckets slowed by load ignored), a weighted least-squares slope gives drift_fit_ppm,
    and every sample's offset is projected to "now" along it.

    Estimate: the projected offset of the sample with the smallest error bound
        rtt / 2 + aging_ppm * 1e-6 * age
    where aging_ppm is 50 before a drift fit exists and 10 after (the fit's residual). An old short-RTT
    sample therefore outlives a burst of samples delayed by load, and fresh samples take over as it ages.

    Consistency: if a new sample's interval (offset +/- rtt/2) cannot overlap the current best's
    (projected offset +/- rtt/2 + 50 ppm * age), the older samples are wrong for this peer (a bogus sample,
    or another body reusing a boot_id) and are dropped (`resets` counts this).
    """

    def __init__(self, horizon: int = 900, aging_ppm: float = 50.0, fitted_aging_ppm: float = 10.0,
                 history_cap: int = CLOCK_HISTORY_MAX, bucket_s: float = 60.0):
        self._samples: deque[OffsetSample] = deque(maxlen=horizon)  # 900 x 2 s = 30 minutes
        self.aging_ppm = aging_ppm
        self.fitted_aging_ppm = fitted_aging_ppm
        self.bucket_us = int(bucket_s * 1e6)
        self.history: deque[tuple[int, float]] = deque(maxlen=history_cap)  # (local time, estimate); 5,000 = 2.8 h, as on the body
        self.best: OffsetSample | None = None
        self.drift_fit_ppm: float | None = None
        self.count = 0
        self.resets = 0
        self.now_us = 0

    # ---- model ---------------------------------------------------------------------------------

    def project(self, s: OffsetSample, now_us: int) -> float:
        return s.offset_us + (self.drift_fit_ppm or 0.0) * 1e-6 * (now_us - s.at_us)

    def cost_us(self, s: OffsetSample, now_us: int) -> float:
        ppm = self.aging_ppm if self.drift_fit_ppm is None else self.fitted_aging_ppm
        return s.rtt_us / 2 + ppm * 1e-6 * (now_us - s.at_us)

    def _fit(self) -> None:
        buckets: dict[int, OffsetSample] = {}
        for s in self._samples:
            k = s.at_us // self.bucket_us
            if k not in buckets or s.rtt_us < buckets[k].rtt_us:
                buckets[k] = s
        if len(buckets) < 4:
            self.drift_fit_ppm = None
            return
        floor = min(b.rtt_us for b in buckets.values())
        good = [b for b in buckets.values() if b.rtt_us <= 2 * floor + 5000]
        if len(good) < 4 or max(b.at_us for b in good) - min(b.at_us for b in good) < 4 * self.bucket_us:
            self.drift_fit_ppm = None
            return
        w = [1.0 / (b.rtt_us / 2 + 1000) ** 2 for b in good]
        sw = sum(w)
        t0 = good[0].at_us
        mx = sum(wi * (b.at_us - t0) for wi, b in zip(w, good)) / sw
        my = sum(wi * b.offset_us for wi, b in zip(w, good)) / sw
        sxx = sum(wi * (b.at_us - t0 - mx) ** 2 for wi, b in zip(w, good))
        sxy = sum(wi * (b.at_us - t0 - mx) * (b.offset_us - my) for wi, b in zip(w, good))
        ppm = sxy / sxx * 1e6 if sxx > 0 else None
        self.drift_fit_ppm = ppm if ppm is not None and abs(ppm) <= MAX_FIT_PPM else None

    def add(self, t0_local: int, t1_remote: int, t3_local: int) -> OffsetSample | None:
        """Add one sample. Returns it, or None if it is impossible (negative RTT)."""
        rtt = t3_local - t0_local
        if rtt < 0:
            return None
        sample = OffsetSample(rtt, t1_remote - (t0_local + t3_local) / 2, t3_local)
        if self.best is not None:
            p = self.project(self.best, t3_local)
            bound = self.best.rtt_us / 2 + self.aging_ppm * 1e-6 * (t3_local - self.best.at_us)
            if sample.offset_us + rtt / 2 < p - bound or sample.offset_us - rtt / 2 > p + bound:
                self._samples.clear()
                self.drift_fit_ppm = None
                self.resets += 1
        self._samples.append(sample)
        self._fit()
        self.now_us = t3_local
        self.best = min(self._samples, key=lambda s: self.cost_us(s, t3_local))
        self.count += 1
        estimate = self.estimate_us
        if estimate is not None:
            self.history.append((t3_local, estimate))
        return sample

    @property
    def estimate_us(self) -> float | None:
        return self.project(self.best, self.now_us) if self.best else None

    def __len__(self) -> int:
        return len(self._samples)

    # ---- stability (section 6): after a warm-up, the change within any window --------------------

    def max_change_us(self, *, since_us: int = 0, warmup_s: float = 60.0, window_s: float = 60.0) -> float | None:
        """Largest (max - min) of the estimate within any `window_s` window, ignoring the first `warmup_s`
        after `since_us`. None if there is not a full window of data yet."""
        h = [(t, e) for t, e in self.history if t >= since_us]
        if not h:
            return None
        start = h[0][0] + warmup_s * 1e6
        h = [(t, e) for t, e in h if t >= start]
        if not h or h[-1][0] - h[0][0] < window_s * 1e6:
            return None
        worst, j = 0.0, 0
        w = window_s * 1e6
        for i in range(len(h)):
            while j < len(h) and h[j][0] - h[i][0] <= w:
                j += 1
            seg = [e for _, e in h[i:j]]
            worst = max(worst, max(seg) - min(seg))
        return worst

    def max_dev_from_fit_us(self, *, since_us: int = 0, warmup_s: float = 60.0) -> float | None:
        """Largest |estimate - straight line| after `warmup_s`, where the line is the least-squares fit of the
        estimate against time over that same period (so steady drift does not count, wander does).
        None if there is under a minute of data after the warm-up."""
        h = [(t, e) for t, e in self.history if t >= since_us]
        if not h:
            return None
        start = h[0][0] + warmup_s * 1e6
        h = [(t, e) for t, e in h if t >= start]
        if len(h) < 3 or h[-1][0] - h[0][0] < 60e6:
            return None
        t0 = h[0][0]
        n = len(h)
        mx = sum(t - t0 for t, _ in h) / n
        my = sum(e for _, e in h) / n
        sxx = sum((t - t0 - mx) ** 2 for t, _ in h)
        sxy = sum((t - t0 - mx) * (e - my) for t, e in h)
        slope = sxy / sxx if sxx > 0 else 0.0
        return max(abs(e - (my + slope * (t - t0 - mx))) for t, e in h)

    def spread_us(self, *, since_us: int = 0, skip: int = 0) -> float | None:
        """Max minus min of all estimates since `since_us` (includes real drift; for information only)."""
        h = [e for t, e in self.history if t >= since_us][skip:]
        return (max(h) - min(h)) if h else None
