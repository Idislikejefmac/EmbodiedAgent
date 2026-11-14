"""The health panel (card X-02 step 5): one status per source, so a missing camera is visible, not silent.

Each source has a state (up, degraded, down, unknown), a reason in words, and the age of the last thing
heard from it. The board is fed by the event stream itself (it is a sink of the event log), so code
reports health simply by emitting its normal events; nothing calls the board directly except the
computed sources (disk, queues, log sink), which the snapshot measures when it is taken.

The running brain writes the snapshot to data/health.json about once a second, atomically (a reader sees
the old file or the new one). `python -m brain health` prints it, and says STALE when it is older than
OBS_HEALTH_STALE_S (the brain is stopped, or stuck).

A state change becomes a `health.change` event, emitted at the next snapshot (never from inside the
sink, which runs inside emit()).
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..limits import OBS_DISK_LOW_BYTES, OBS_DISK_MIN_BYTES, OBS_HEALTH_FILE_MAX_BYTES, OBS_HEALTH_STALE_S
from .files import atomic_write_text, read_capped
from .redact import clean_text

STATES = ("up", "degraded", "down", "unknown")
HEALTH_FILE = "health.json"
DROP_MEMORY_S = 60.0  # a queue that dropped items shows degraded for this long after the last drop

# Source -> (state, reason) before anything is heard from it.
DEFAULTS: dict[str, tuple[str, str]] = {
    "body_link": ("unknown", "no body has connected yet"),
    "camera": ("unknown", "not built: no camera path in the brain yet (card P1-01), and no body has reported one"),
    "microphone": ("unknown", "not built: no microphone path in the brain yet (card P2-02), and no body has "
                              "reported one"),
    "speaker": ("unknown", "not built: no speech output yet (cards P0-07, P2-01), and no body has reported one"),
    "clock": ("unknown", "no clock samples yet (needs a body link)"),
    "battery": ("unknown", "no body status reading yet"),
    "temperature": ("unknown", "no body status reading yet"),
    "model_vendor": ("unknown", "not built: the agent loop and its model calls are card P0-07"),
    "speech_vendors": ("unknown", "not built: speech to text and text to speech are cards P2-01 and P2-02"),
    "queues": ("up", "no drops"),
    "budget": ("unknown", "placeholder: the interim spend guard is card P0-07 step 9"),
    "disk": ("unknown", "not measured yet"),
    "log_sink": ("unknown", "event files are not being written (only `serve` and `demo-trace` write them)"),
    "config": ("unknown", "not loaded yet"),
    "supervisor": ("unknown", "no supervised components"),
}
CAPABILITY_SOURCES = {"camera": ("image",), "microphone": ("audio_in",), "speaker": ("audio_out", "speak_text")}


@dataclass
class Source:
    state: str
    reason: str
    seen: float | None = None  # time.monotonic() of the last thing heard; None: never
    changed: float = field(default_factory=time.monotonic)


class HealthBoard:
    """The current health of every source. Thread-safe; `on_event` is an event-log sink."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self.sources: dict[str, Source] = {k: Source(s, r) for k, (s, r) in DEFAULTS.items()}
        self.components: dict[str, dict[str, Any]] = {}
        self.counters: dict[str, int] = {"queue_busy": 0, "stream_drops": 0}
        self._last_drop = -1e9
        self._pending: dict[str, tuple[str, str]] = {}
        self.session: str | None = None

    # ---- updates ----

    def set(self, name: str, state: str, reason: str, *, seen: bool = True) -> None:
        if state not in STATES:
            state = "unknown"
        now = self._clock()
        with self._lock:
            src = self.sources.setdefault(name, Source("unknown", ""))
            reason = clean_text(reason, 160)
            if src.state != state:
                src.changed = now
                self._pending[name] = (state, reason)  # coalesced: the latest state per source until the next flush
            src.state, src.reason = state, reason
            if seen:
                src.seen = now

    def seen(self, name: str) -> None:
        with self._lock:
            src = self.sources.get(name)
            if src is not None:
                src.seen = self._clock()

    def take_changes(self) -> list[tuple[str, str, str]]:
        """State changes since the last call, at most one per source (its latest state)."""
        with self._lock:
            pending, self._pending = self._pending, {}
        return [(name, state, reason) for name, (state, reason) in pending.items()]

    def on_event(self, event: Mapping[str, Any]) -> None:
        """Map one event to the sources it says something about. Unknown types are ignored."""
        etype = event.get("type")
        f = event.get("f") or {}
        handler = _HANDLERS.get(str(etype))
        if handler is not None:
            handler(self, f, event)

    def component(self, name: str, state: str, reason: str = "") -> None:
        with self._lock:
            self.components[name] = {"state": state, "reason": clean_text(reason, 160), "since": time.time()}
        states = [c["state"] for c in self.components.values()]
        if any(s in ("failed", "fatal") for s in states):
            bad = sorted(n for n, c in self.components.items() if c["state"] in ("failed", "fatal"))
            self.set("supervisor", "down", f"stopped after repeated failures: {', '.join(bad)}")
        elif any(s == "backing_off" for s in states):
            self.set("supervisor", "degraded", "a component is restarting")
        else:
            self.set("supervisor", "up", f"{len(states)} component(s) supervised")

    # ---- the snapshot ----

    def snapshot(self, *, data_dir: Path | None = None, obs_stats: Mapping[str, Any] | None = None,
                 extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The panel as plain JSON. Measures the computed sources (disk, queues, log sink) first."""
        if data_dir is not None:
            self._measure_disk(data_dir)
        if obs_stats is not None:
            self._measure_pipeline(obs_stats)
        now = self._clock()
        with self._lock:
            sources = {name: {"state": s.state, "reason": s.reason,
                              "age_s": None if s.seen is None else round(now - s.seen, 1),
                              "for_s": round(now - s.changed, 1)}
                       for name, s in self.sources.items()}
            components = {k: dict(v) for k, v in self.components.items()}
            counters = dict(self.counters)
        return {"v": 1, "written_at": time.time(), "written": datetime.now(UTC).isoformat(timespec="seconds"),
                "session": self.session, "sources": sources, "components": components, "counters": counters,
                "obs": dict(obs_stats or {}), **dict(extra or {})}

    def _measure_disk(self, data_dir: Path) -> None:
        try:
            free = shutil.disk_usage(data_dir if data_dir.exists() else data_dir.parent).free
        except OSError as e:
            self.set("disk", "unknown", f"cannot measure free space ({e.strerror or type(e).__name__})")
            return
        gib = free / 1024**3
        state = "up" if free >= OBS_DISK_LOW_BYTES else "degraded" if free >= OBS_DISK_MIN_BYTES else "down"
        self.set("disk", state, f"{gib:.1f} GiB free under the data folder")

    def _measure_pipeline(self, st: Mapping[str, Any]) -> None:
        ws = st.get("writer_state")
        if ws == "up":
            self.set("log_sink", "up", f"writing {st.get('writer_file')}")
        elif ws == "down":
            self.set("log_sink", "down", f"{st.get('writer_reason')}; {st.get('writer_dropped')} events dropped")
        elif ws == "starting":
            self.set("log_sink", "up", "starting")
        drops = int(st.get("writer_dropped") or 0)
        recent = self._clock() - self._last_drop < DROP_MEMORY_S
        with self._lock:
            busy, stream = self.counters["queue_busy"], self.counters["stream_drops"]
        text = (f"event queue {st.get('writer_queue', 0)} waiting, {drops} dropped; handler queue full {busy} "
                f"times; stream items dropped {stream}")
        self.set("queues", "degraded" if recent or (drops and ws == "up") else "up", text)

    def note_drop(self, counter: str, n: int = 1) -> None:
        with self._lock:
            self.counters[counter] = self.counters.get(counter, 0) + n
            self._last_drop = self._clock()


# ---- event -> source mapping ------------------------------------------------------------------------------


def _caps(board: HealthBoard, f: Mapping[str, Any], event: Mapping[str, Any]) -> None:
    caps = f.get("capabilities")
    caps = [c for c in caps if isinstance(c, str)] if isinstance(caps, list) else []
    connected = f.get("connected", True)
    for source, needs in CAPABILITY_SOURCES.items():
        if any(c in caps for c in needs):
            board.set(source, "up", f"the body offers {'/'.join(n for n in needs if n in caps)}")
        elif connected is False:
            board.set(source, "down", "the body link is down", seen=False)
        else:
            board.set(source, "down", f"the body has no {' or '.join(needs)} capability", seen=False)


def _link_up(board: HealthBoard, f: Mapping[str, Any], event: Mapping[str, Any]) -> None:
    board.set("body_link", "up", f"body authenticated ({f.get('software', 'unknown software')})")
    board.session = event.get("session") if isinstance(event.get("session"), str) else board.session


def _link_down(board: HealthBoard, f: Mapping[str, Any], event: Mapping[str, Any]) -> None:
    if f.get("active"):
        board.set("body_link", "down", f"link closed: {f.get('reason') or 'connection ended'}")
        board.set("clock", "unknown", "no body link", seen=False)


def _body_link(board: HealthBoard, f: Mapping[str, Any], event: Mapping[str, Any]) -> None:
    if f.get("connected"):
        board.set("body_link", "up", f"the {f.get('body', 'body')} adapter reaches its body")
    else:
        board.set("body_link", "down", f"the {f.get('body', 'body')} adapter cannot reach its body")


def _status(board: HealthBoard, f: Mapping[str, Any], event: Mapping[str, Any]) -> None:
    if not f.get("connected"):
        for s in ("battery", "temperature"):
            board.set(s, "unknown", "the body is not connected", seen=False)
        return
    if f.get("stale"):
        for s in ("battery", "temperature"):
            board.set(s, "degraded", "the body did not answer the status request (stale)")
        return
    pct, temp = f.get("battery_pct"), f.get("temperature_c")
    if isinstance(pct, (int, float)):
        board.set("battery", "up" if pct >= 20 else "degraded" if pct >= 5 else "down",
                  f"{pct:.0f}%{' charging' if f.get('charging') else ''}")
    else:
        board.set("battery", "unknown", "the body reports no battery level")
    if isinstance(temp, (int, float)):
        board.set("temperature", "up" if temp < 45 else "degraded" if temp < 55 else "down", f"{temp:.1f} C")
    else:
        board.set("temperature", "unknown", "the body reports no temperature")


def _clock(board: HealthBoard, f: Mapping[str, Any], event: Mapping[str, Any]) -> None:
    board.set("clock", "up", f"offset {f.get('offset_ms')} ms, best rtt {f.get('best_rtt_ms')} ms")


def _seen_or_up(source: str, what: str) -> Callable[[HealthBoard, Mapping[str, Any], Mapping[str, Any]], None]:
    def handler(board: HealthBoard, f: Mapping[str, Any], event: Mapping[str, Any]) -> None:
        if board.sources[source].state != "up":
            board.set(source, "up", what)
        else:
            board.seen(source)
    return handler


def _vendor(source: str) -> Callable[[HealthBoard, Mapping[str, Any], Mapping[str, Any]], None]:
    def handler(board: HealthBoard, f: Mapping[str, Any], event: Mapping[str, Any]) -> None:
        ok = f.get("ok", True)
        board.set(source, "up" if ok else "degraded", "last call ok" if ok else f"last call failed: {f.get('code')}")
    return handler


def _component(state: str) -> Callable[[HealthBoard, Mapping[str, Any], Mapping[str, Any]], None]:
    def handler(board: HealthBoard, f: Mapping[str, Any], event: Mapping[str, Any]) -> None:
        name = f.get("component")
        if isinstance(name, str):
            board.component(name, state, str(event.get("msg", "")))
    return handler


def _drop(counter: str) -> Callable[[HealthBoard, Mapping[str, Any], Mapping[str, Any]], None]:
    def handler(board: HealthBoard, f: Mapping[str, Any], event: Mapping[str, Any]) -> None:
        n = f.get("new_drops", 1)
        board.note_drop(counter, n if isinstance(n, int) and n > 0 else 1)
    return handler


Handler = Callable[[HealthBoard, Mapping[str, Any], Mapping[str, Any]], None]
_HANDLERS: dict[str, Handler] = {
    "link.up": _link_up,
    "link.down": _link_down,
    "body.link": _body_link,
    "body.capabilities": _caps,
    "body.status": _status,
    "body.frame": _seen_or_up("camera", "stills are arriving"),
    "body.audio": _seen_or_up("microphone", "audio is arriving"),
    "body.speak": _seen_or_up("speaker", "the body accepted speech"),
    "clock.estimate": _clock,
    "clock.reset": lambda b, f, e: b.set("clock", "degraded", "a sample disagreed; the estimate restarted"),
    "model.call_end": _vendor("model_vendor"),
    "speech.stt": _vendor("speech_vendors"),
    "speech.tts": _vendor("speech_vendors"),
    "budget.check": lambda b, f, e: b.set("budget", "up", f"headroom {f.get('headroom_usd')} USD"),
    "budget.stop": lambda b, f, e: b.set("budget", "down", f"stopped by the {f.get('cap')} cap"),
    "config.loaded": lambda b, f, e: b.set("config", "up", f"{f.get('file')} passed the gate"),
    "config.error": lambda b, f, e: b.set("config", "down", "refused; the brain does not start"),
    "obs.sink_failed": lambda b, f, e: b.set("log_sink", "down", str(f.get("reason", "write failed"))),
    "obs.sink_recovered": lambda b, f, e: b.set("log_sink", "up", "writing again"),
    "queue.busy": _drop("queue_busy"),
    "queue.drop": _drop("stream_drops"),
    "supervisor.started": _component("running"),
    "supervisor.crashed": _component("crashed"),
    "supervisor.restart": _component("backing_off"),
    "supervisor.gave_up": _component("failed"),
    "supervisor.fatal": _component("fatal"),
    "supervisor.stopped": _component("stopped"),
}


# ---- the file ------------------------------------------------------------------------------------------------


def write_snapshot(path: Path, snapshot: Mapping[str, Any]) -> None:
    """Atomically replace data/health.json. Raises OSError (the caller counts it and retries next tick)."""
    atomic_write_text(path, json.dumps(snapshot, ensure_ascii=False, indent=1, default=str))


def read_snapshot(path: Path) -> tuple[dict[str, Any] | None, str]:
    """(snapshot, "") or (None, why). Checks size and shape before use. Never raises."""
    data, problem = read_capped(path, OBS_HEALTH_FILE_MAX_BYTES)
    if data is None:
        return None, problem
    try:
        snap = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, "not valid JSON (written by another program, or damaged)"
    if not isinstance(snap, dict) or not isinstance(snap.get("sources"), dict):
        return None, "not a health snapshot"
    return snap, ""


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def format_panel(snap: Mapping[str, Any], *, now: float | None = None) -> str:
    """The panel as text. Every string from the file is cleaned again (the file could have been edited)."""
    now = time.time() if now is None else now
    written = _num(snap.get("written_at"))
    age = None if written is None else now - written
    head = f"brain health, written {clean_text(str(snap.get('written', '?')), 40)}"
    if age is not None:
        head += f" ({age:.1f} s ago)"
    session = snap.get("session")
    if isinstance(session, str):
        head += f", session {clean_text(session, 40)}"
    lines = [head]
    if age is None or age > OBS_HEALTH_STALE_S:
        lines.append(f"STALE: the snapshot is {'of unknown age' if age is None else f'{age:.0f} s old'} "
                     f"(more than {OBS_HEALTH_STALE_S:g} s): the brain is stopped or stuck; what follows is "
                     "the last thing it reported")
    lines.append(f"  {'SOURCE':<15} {'STATE':<9} {'SEEN':>8}  REASON")
    sources = snap.get("sources")
    for name, s in (sources.items() if isinstance(sources, dict) else []):
        if not isinstance(s, dict):
            continue
        state = clean_text(str(s.get("state", "?")), 9)
        seen = _num(s.get("age_s"))
        lines.append(f"  {clean_text(str(name), 15):<15} {state:<9} {'-' if seen is None else f'{seen:.1f} s':>8}  "
                     f"{clean_text(str(s.get('reason', '')), 160)}")
    comps = snap.get("components")
    if isinstance(comps, dict) and comps:
        lines.append("  components: " + ", ".join(
            f"{clean_text(str(k), 40)}={clean_text(str(v.get('state', '?')), 12) if isinstance(v, dict) else '?'}"
            for k, v in list(comps.items())[:20]))
    obs = snap.get("obs")
    if isinstance(obs, dict) and obs:
        lines.append(f"  events: emitted {obs.get('emitted')}, suppressed {obs.get('suppressed')}, writer "
                     f"{clean_text(str(obs.get('writer_state')), 12)}, dropped {obs.get('writer_dropped')}")
    budget = snap.get("budget")
    if isinstance(budget, dict):
        lines.append("  budget: " + ", ".join(f"{clean_text(str(k), 30)}={clean_text(str(v), 30)}"
                                              for k, v in list(budget.items())[:8]))
    return "\n".join(lines)
