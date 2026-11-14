"""The event pipeline (card X-02 step 2): it can never harm the brain.

    emit() -> redaction -> per-type limit -> ring buffer (memory) -> in-process sinks (health, chaos)
                                                                  +-> bounded queue -> writer thread -> data/logs/events-*.jsonl

- `emit()` never raises and never blocks: it builds one small dict, takes an uncontended lock for a
  sequence number, appends to a deque and does a non-blocking put. Its cost is measured by a test
  (OBS_EMIT_BUDGET_US).
- **Bounded everywhere.** The ring holds OBS_RING_EVENTS; the writer queue OBS_WRITER_QUEUE_MAX (beyond it
  events are dropped and counted); each type may burst OBS_TYPE_BURST then OBS_TYPE_RATE_PER_S (the excess
  is counted, and the next event of that type says how many were suppressed). Crash events are never
  limited, so a crash cannot hide behind a flood.
- **A failing sink is swallowed, counted and reported once.** The writer thread catches every error of the
  file (disk full, locked, read-only folder, permission denied), reports it once as `obs.sink_failed`
  (which the health panel shows as log_sink down), drops events while it is down, and retries every
  OBS_SINK_RETRY_S. An in-process sink that raises is counted and, after three failures, removed.
- **Rotation-safe on Windows.** Files are never renamed: when one reaches OBS_FILE_MAX_BYTES the writer
  starts `events-<run>-<n+1>.jsonl` and deletes the oldest beyond OBS_FILES_MAX (a delete that fails
  because a viewer has the file open is retried at the next rotation).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..limits import (OBS_EVENT_MAX_BYTES, OBS_FILE_MAX_BYTES, OBS_FILES_MAX, OBS_MESSAGE_MAX_CHARS, OBS_RING_EVENTS,
                      OBS_SINK_RETRY_S, OBS_TYPE_BURST, OBS_TYPE_RATE_PER_S, OBS_WRITER_CLOSE_TIMEOUT_S,
                      OBS_WRITER_QUEUE_MAX)
from .events import EVENT_TYPES, UNLIMITED_TYPES, LEVELS, SCHEMA_VERSION, SESSION, TURN
from .redact import clean_fields, clean_text

log = logging.getLogger("brain.obs")

Event = dict[str, Any]
Sink = Callable[[Mapping[str, Any]], None]
EVENT_FILE_PREFIX = "events-"
EVENT_FILE_SUFFIX = ".jsonl"
SINK_FAILURES_BEFORE_REMOVAL = 3


def event_line(event: Mapping[str, Any]) -> str:
    """The event as one JSON line (no newline). Never raises for an event built by EventLog."""
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str)


def run_stamp() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}"


class EventWriter:
    """The writer thread: queued lines to rotating JSONL files in one folder. See the module docstring."""

    def __init__(self, folder: Path, *, run: str, report: Callable[[str, str], None] | None = None,
                 max_bytes: int = OBS_FILE_MAX_BYTES, max_files: int = OBS_FILES_MAX,
                 retry_s: float = OBS_SINK_RETRY_S, queue_max: int = OBS_WRITER_QUEUE_MAX):
        self.folder = Path(folder)
        self.run = run
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.retry_s = retry_s
        self.queue: queue.Queue[str] = queue.Queue(maxsize=queue_max)
        self.state = "starting"  # starting | up | down | stopped
        self.reason = ""
        self.failures = 0  # times the file failed
        self.dropped = 0  # events lost: queue full, or the file was down
        self.written = 0
        self.prune_failures = 0
        self.current: Path | None = None
        self._report = report
        self._index = 0
        self._size = 0
        self._fh: Any = None
        self._retry_at = 0.0
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    # ---- the producer side (any thread) ----

    def offer(self, line: str) -> None:
        try:
            self.queue.put_nowait(line)
        except queue.Full:
            self.dropped += 1

    @property
    def depth(self) -> int:
        return self.queue.qsize()

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self._stop.clear()
        self.thread = threading.Thread(target=self._run, name="obs-event-writer", daemon=True)
        self.thread.start()

    @property
    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def stop(self, timeout: float = OBS_WRITER_CLOSE_TIMEOUT_S) -> None:
        """Drain what is queued (up to `timeout`), close the file. Safe to call twice."""
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout)
        self._close()
        self.state = "stopped"

    # ---- the thread ----

    def _run(self) -> None:
        while True:
            try:
                batch = [self.queue.get(timeout=0.2)]
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                while len(batch) < 500:
                    batch.append(self.queue.get_nowait())
            except queue.Empty:  # swallowed: the batch is simply complete
                pass
            try:
                self._write(batch)
            except Exception as e:  # broad-except: the writer thread must never die from a file problem
                self._failed(f"{type(e).__name__}: {e}", len(batch))

    def _write(self, batch: list[str]) -> None:
        now = time.monotonic()
        if self._fh is None and self.state == "down" and now < self._retry_at:
            self.dropped += len(batch)
            return
        for i, line in enumerate(batch):
            if self._fh is None:
                try:
                    self._open_next()
                except OSError as e:
                    self._failed(f"{type(e).__name__}: {e}", len(batch) - i)
                    return
            data = line + "\n"
            self._fh.write(data)
            self._size += len(data.encode("utf-8"))
            self.written += 1
            if self._size >= self.max_bytes:  # the next line starts the next file (nothing is ever renamed)
                self._fh.flush()
                self._close()
                self._index += 1
        if self._fh is not None:
            self._fh.flush()
        if self.state != "up":
            was_down = self.state == "down"
            self.state, self.reason = "up", ""
            if was_down and self._report is not None:
                self._report("up", f"{self.dropped} events dropped while it was down")

    def _open_next(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        if self._index == 0:
            self._index = 1
        path = self.folder / f"{EVENT_FILE_PREFIX}{self.run}-{self._index:04d}{EVENT_FILE_SUFFIX}"
        self._fh = open(path, "a", encoding="utf-8", newline="\n")
        self._size = path.stat().st_size
        self.current = path
        self._prune()

    def _prune(self) -> None:
        """Delete the oldest event files beyond max_files (all runs). A file a viewer holds open cannot be
        deleted on Windows: that is counted and retried at the next rotation."""
        files = sorted(self.folder.glob(f"{EVENT_FILE_PREFIX}*{EVENT_FILE_SUFFIX}"))
        for old in files[: max(0, len(files) - self.max_files)]:
            if old == self.current:
                continue
            try:
                old.unlink()
            except OSError:
                self.prune_failures += 1

    def _close(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:  # swallowed: closing a file on a failed disk; the failure was already reported
                pass

    def _failed(self, reason: str, lost: int) -> None:
        self.failures += 1
        self.dropped += lost
        self._close()
        self._retry_at = time.monotonic() + self.retry_s
        first = self.state != "down"
        self.state, self.reason = "down", clean_text(reason, 160)
        if first and self._report is not None:
            self._report("down", self.reason)


class EventLog:
    """The process's event log: emit(), the ring buffer, sinks and the optional file writer."""

    def __init__(self, *, ring: int = OBS_RING_EVENTS, min_level: str = "INFO",
                 burst: float = OBS_TYPE_BURST, rate_per_s: float = OBS_TYPE_RATE_PER_S):
        self.ring: deque[Event] = deque(maxlen=ring)
        self.min_level = LEVELS[min_level]
        self.burst = float(burst)
        self.rate = float(rate_per_s)
        self.sinks: list[Sink] = []
        self.writer: EventWriter | None = None
        self.counts: Counter[str] = Counter()  # emitted, suppressed, sink_errors, emit_errors, reentrant, bad_type
        self.suppressed: Counter[str] = Counter()  # per type, since its last event that got through
        self._sink_failures: Counter[int] = Counter()
        self._buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._local = threading.local()
        self._seq = 0

    # ---- emit ----

    def emit(self, etype: str, msg: str = "", /, *, level: str | None = None, **fields: Any) -> Event | None:
        """Record one event. Returns it (or None if it was filtered, limited or could not be built).
        Never raises (cancellation and interpreter exit aside) and never blocks."""
        local = self._local
        if getattr(local, "busy", False):  # a sink emitting from inside emit: dropped, counted
            self.counts["reentrant"] += 1
            return None
        local.busy = True
        try:
            return self._emit(etype, msg, level, fields)
        except Exception:  # broad-except: emit() must never raise into the code that called it
            self.counts["emit_errors"] += 1
            return None
        finally:
            local.busy = False

    def _emit(self, etype: str, msg: str, level: str | None, fields: dict[str, Any]) -> Event | None:
        spec = EVENT_TYPES.get(etype) if isinstance(etype, str) else None
        if spec is None:
            self.counts["bad_type"] += 1
            fields = {"asked_type": etype if isinstance(etype, str) else f"<{type(etype).__name__}>"}
            etype, msg, level = "obs.bad_event", "emit() was called with an unknown event type", None
            spec = EVENT_TYPES[etype]
        lvl = level if level is not None and level in LEVELS else spec.level
        if LEVELS[lvl] < self.min_level:
            return None
        limited = etype not in UNLIMITED_TYPES
        now = time.monotonic()
        with self._lock:
            if limited and not self._admit(etype, now):
                self.counts["suppressed"] += 1
                self.suppressed[etype] += 1
                return None
            self._seq += 1
            seq = self._seq
            held = self.suppressed.pop(etype, 0)
        f = clean_fields(fields) if fields else {}
        if held:
            f["suppressed_before"] = held
        event: Event = {
            "v": SCHEMA_VERSION, "seq": seq,
            "ts": datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "mono": round(now, 6), "level": lvl, "comp": spec.component, "type": etype,
            "session": SESSION.get(), "turn": TURN.get(),
            "msg": clean_text(msg if isinstance(msg, str) else f"<{type(msg).__name__}>", OBS_MESSAGE_MAX_CHARS),
            "f": f,
        }
        line = event_line(event)
        if len(line) > OBS_EVENT_MAX_BYTES // 4 and len(line.encode("utf-8")) > OBS_EVENT_MAX_BYTES:
            event["f"] = {"_cut": True, "_fields": [clean_text(str(k), 40) for k in list(f)[:8]]}
            event["msg"] = clean_text(event["msg"], 120)
            line = event_line(event)
        self.counts["emitted"] += 1
        self.ring.append(event)
        for sink in list(self.sinks):
            try:
                sink(event)
            except Exception:  # broad-except: a failing sink is counted and, if it keeps failing, removed
                self._sink_failed(sink)
        writer = self.writer
        if writer is not None:
            writer.offer(line)
        return event

    def _admit(self, etype: str, now: float) -> bool:
        """Per-type token bucket (OBS_TYPE_BURST, OBS_TYPE_RATE_PER_S). Called under the lock."""
        b = self._buckets.get(etype)
        if b is None:
            self._buckets[etype] = [self.burst - 1.0, now]
            return True
        tokens = min(self.burst, b[0] + (now - b[1]) * self.rate)
        b[1] = now
        if tokens >= 1.0:
            b[0] = tokens - 1.0
            return True
        b[0] = tokens
        return False

    def _sink_failed(self, sink: Sink) -> None:
        self.counts["sink_errors"] += 1
        key = id(sink)
        self._sink_failures[key] += 1
        if self._sink_failures[key] >= SINK_FAILURES_BEFORE_REMOVAL and sink in self.sinks:
            self.sinks.remove(sink)
            log.error("an event sink failed %d times and was removed", SINK_FAILURES_BEFORE_REMOVAL)

    # ---- sinks and files ----

    def add_sink(self, sink: Sink) -> None:
        if sink not in self.sinks:
            self.sinks.append(sink)
            self._sink_failures.pop(id(sink), None)

    def remove_sink(self, sink: Sink) -> None:
        if sink in self.sinks:
            self.sinks.remove(sink)

    def start_files(self, folder: Path, *, run: str | None = None) -> EventWriter:
        """Start writing events to `folder` (events already in the ring are written first)."""
        self.stop_files()
        writer = EventWriter(folder, run=run or run_stamp(), report=self._writer_report)
        for event in list(self.ring):
            writer.offer(event_line(event))
        self.writer = writer
        writer.start()
        self.emit("obs.started", "writing events", folder=Path(folder).name, prefix=f"{EVENT_FILE_PREFIX}{writer.run}")
        return writer

    def ensure_writer(self) -> bool:
        """Restart the writer thread if it died (the supervisor calls this). True if it had to."""
        w = self.writer
        if w is None or w.alive or w.state == "stopped":
            return False
        w.start()
        return True

    def stop_files(self, timeout: float = OBS_WRITER_CLOSE_TIMEOUT_S) -> None:
        w, self.writer = self.writer, None
        if w is not None:
            w.stop(timeout)

    def _writer_report(self, state: str, reason: str) -> None:
        if state == "down":
            log.error("the event file cannot be written (%s); events stay in memory only until it recovers", reason)
            self.emit("obs.sink_failed", "event file cannot be written; retrying", reason=reason)
        else:
            self.emit("obs.sink_recovered", "event file can be written again", detail=reason)

    # ---- reading ----

    def stats(self) -> dict[str, Any]:
        w = self.writer
        return {"emitted": self.counts["emitted"], "suppressed": self.counts["suppressed"],
                "emit_errors": self.counts["emit_errors"], "sink_errors": self.counts["sink_errors"],
                "ring": len(self.ring), "writer_state": "off" if w is None else w.state,
                "writer_reason": "" if w is None else w.reason, "writer_queue": 0 if w is None else w.depth,
                "writer_dropped": 0 if w is None else w.dropped, "writer_failures": 0 if w is None else w.failures,
                "writer_file": None if w is None or w.current is None else w.current.name}

    def recent(self, n: int = 50) -> list[Event]:
        return list(self.ring)[-n:]

    def reset_limits(self) -> None:
        """Forget the per-type buckets (tests, and each chaos case, start from a full burst)."""
        with self._lock:
            self._buckets.clear()
            self.suppressed.clear()
