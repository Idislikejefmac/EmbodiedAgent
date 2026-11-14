"""Bounds on what a connected peer can make the brain do with VALID frames (H-09, H-04; DEC-104).

Three pieces, one per connection, used by the brain server's receive loop:

- `FloodGuard`: a token bucket per message type (PEER_MSG_MAX_PER_S by default; heartbeats and echoes
  have their own rates). A message over its type's rate is not handled: heartbeat and echo requests are
  answered `busy` (not fatal), anything else is dropped and counted. More than PEER_EXCESS_MAX_PER_MIN
  messages over the limit within one minute is sustained flooding: the link is closed (a fatal `busy`
  error, then close code 1002). Answers to the brain's own requests are never limited.
- `HandlerBudget`: the queue in front of the brain's message handler is bounded in messages
  (HANDLER_QUEUE) AND in bytes (HANDLER_QUEUE_MAX_BYTES), so a slow handler cannot make the queue hold
  256 x 8 MiB; beyond either bound the body gets `busy`.
- `PeerLogLimit`: at most PEER_LOG_LINES_PER_MIN_MAX log lines per minute per connection; the rest are
  suppressed and counted, and one "N suppressed" line says so (S9, T-P0-05-047).

`size_problem` gives the per-type size caps (an event or an echo must be small; images and microphone
chunks have their own caps), checked before a message is queued.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable, Mapping

from ..limits import (AUDIO_IN_MAX_CHUNK_BYTES, ECHO_TEST_MAX_BYTES, EVENT_MAX_BYTES, HANDLER_QUEUE,
                      HANDLER_QUEUE_MAX_BYTES, MAX_IMAGE_BYTES, PEER_ECHO_MAX_PER_S, PEER_EXCESS_MAX_PER_MIN,
                      PEER_HEARTBEAT_MAX_PER_S, PEER_LOG_LINES_PER_MIN_MAX, PEER_MSG_BURST, PEER_MSG_MAX_PER_S)

WINDOW_S = 60.0  # "per minute" in the limits above
Clock = Callable[[], float]

# (refill per second, bucket size) by message type; every other type gets DEFAULT_RATE.
DEFAULT_RATE = (float(PEER_MSG_MAX_PER_S), float(PEER_MSG_BURST))
TYPE_RATES: Mapping[str, tuple[float, float]] = {
    "heartbeat": (float(PEER_HEARTBEAT_MAX_PER_S), float(PEER_HEARTBEAT_MAX_PER_S)),
    "echo": (float(PEER_ECHO_MAX_PER_S), float(PEER_ECHO_MAX_PER_S)),
}
ANSWERED_WHEN_LIMITED = frozenset({"heartbeat", "echo"})  # requests the peer waits on: answered `busy`

# Largest message per type: (what is measured, bytes). "text" is the JSON frame, "data" the binary frame.
TYPE_MAX_BYTES: Mapping[str, tuple[str, int]] = {
    "event": ("text", EVENT_MAX_BYTES),
    "echo": ("data", ECHO_TEST_MAX_BYTES),
    "image": ("data", MAX_IMAGE_BYTES),
    "audio_in": ("data", AUDIO_IN_MAX_CHUNK_BYTES),
}


def size_problem(mtype: str, text_bytes: int, data_bytes: int) -> str | None:
    """Why a message of this type is too large to handle, or None."""
    cap = TYPE_MAX_BYTES.get(mtype)
    if cap is None:
        return None
    what, limit = cap
    size = text_bytes if what == "text" else data_bytes
    return f"{mtype} {what} of {size} bytes is over the {limit}-byte limit" if size > limit else None


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: float, now: float):
        self.rate, self.burst = rate_per_s, burst
        self.tokens, self.last = burst, now

    def take(self, now: float) -> bool:
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class FloodGuard:
    """Per-type rate limit for valid frames from one peer (DEC-104). Not thread-safe; one per connection."""

    def __init__(self, *, rates: Mapping[str, tuple[float, float]] = TYPE_RATES,
                 default: tuple[float, float] = DEFAULT_RATE, excess_max_per_min: int = PEER_EXCESS_MAX_PER_MIN,
                 clock: Clock = time.monotonic):
        self._rates, self._default, self._clock = rates, default, clock
        self._buckets: dict[str, TokenBucket] = {}
        self.excess_max_per_min = excess_max_per_min
        self.limited: Counter[str] = Counter()  # messages over the limit, by type, since the connection began
        self._window_start = clock()
        self._window_excess = 0

    def admit(self, mtype: str) -> bool:
        """True if this message is within its type's rate. A refusal is counted."""
        now = self._clock()
        bucket = self._buckets.get(mtype)
        if bucket is None:
            rate, burst = self._rates.get(mtype, self._default)
            bucket = self._buckets[mtype] = TokenBucket(rate, burst, now)
        if bucket.take(now):
            return True
        self.limited[mtype] += 1
        if now - self._window_start >= WINDOW_S:
            self._window_start, self._window_excess = now, 0
        self._window_excess += 1
        return False

    @property
    def flooding(self) -> bool:
        """Sustained excess: more than excess_max_per_min messages over the limit in the current minute."""
        return self._window_excess > self.excess_max_per_min


class HandlerBudget:
    """Messages and bytes waiting for the handler. `reserve` before queueing, `release` after handling."""

    def __init__(self, max_messages: int = HANDLER_QUEUE, max_bytes: int = HANDLER_QUEUE_MAX_BYTES):
        self.max_messages, self.max_bytes = max_messages, max_bytes
        self.messages = 0
        self.bytes = 0
        self.peak_bytes = 0

    def reserve(self, size: int) -> bool:
        if self.messages + 1 > self.max_messages or self.bytes + size > self.max_bytes:
            return False
        self.messages += 1
        self.bytes += size
        self.peak_bytes = max(self.peak_bytes, self.bytes)
        return True

    def release(self, size: int) -> None:
        self.messages = max(0, self.messages - 1)
        self.bytes = max(0, self.bytes - size)


class PeerLogLimit(logging.Filter):
    """A filter on one connection's logger: at most `max_per_min` records per minute pass, the last slot
    of each minute being kept for the "suppressed" summary. Records logged with
    extra={"log_limit_exempt": True} (the one-per-session summaries) always pass."""

    def __init__(self, logger: logging.Logger, max_per_min: int = PEER_LOG_LINES_PER_MIN_MAX,
                 clock: Clock = time.monotonic):
        super().__init__()
        self.logger, self.max_per_min, self._clock = logger, max_per_min, clock
        self._window_start = clock()
        self._count = 0
        self.suppressed = 0  # in the current minute
        self.suppressed_total = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "log_limit_exempt", False):
            return True
        now = self._clock()
        if now - self._window_start >= WINDOW_S:
            self.flush()
            self._window_start, self._count = now, 0
        if self._count < self.max_per_min - 1:  # one slot is kept for the summary line
            self._count += 1
            return True
        self.suppressed += 1
        self.suppressed_total += 1
        return False

    def flush(self) -> None:
        """Log the "N suppressed" line for the current minute, if anything was suppressed."""
        if self.suppressed:
            n, self.suppressed = self.suppressed, 0
            self.logger.warning("%d log lines from this connection suppressed in the last minute (limit %d per "
                                "minute)", n, self.max_per_min, extra={"log_limit_exempt": True})
