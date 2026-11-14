"""What must survive a restart (card X-02 step 8): data/state.json, written atomically, checked on load.

Held today: the run's session id, how many times the brain has started, request and spend counters
(placeholders until card P0-07 step 9's spend guard fills them) and pending request ids. It is small
and never holds secrets, prompts or personal data.

- **Atomic**: written to a temporary file and os.replace'd (brain/obs/files.py), so a kill or power cut
  mid-write leaves the old file or the new one, never half of one (a stray temporary file is ignored).
- **Checked on load**: a file over STATE_FILE_MAX_BYTES, not UTF-8 JSON, of another schema version, or with
  any field of the wrong type or range is QUARANTINED (renamed `state.corrupt-<time>.json`, at most
  STATE_QUARANTINE_KEPT kept), reported with a `state.quarantined` event, and replaced by defaults. Loading
  never raises and never refuses to start: losing a counter is safer than not starting. (The spend guard of
  P0-07 must therefore treat a reset counter conservatively, for example by also reading its request log.)
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ..limits import SPEND_CAP_SANITY_MAX, STATE_FILE_MAX_BYTES, STATE_PENDING_MAX, STATE_QUARANTINE_KEPT
from .files import atomic_write_text, read_capped

STATE_FILE = "state.json"
STATE_SCHEMA = 1
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SPEND_TOTAL_SANITY_MAX = SPEND_CAP_SANITY_MAX * 1000  # money held in the file: far above any real cap


@dataclass(frozen=True)
class BrainState:
    session_id: str = ""
    starts: int = 0  # how many times the brain has started with this file
    hour_start: float = 0.0  # wall time (epoch s) the request-per-hour counter began
    requests_this_hour: int = 0
    spend_usd_session: float = 0.0
    spend_usd_today: float = 0.0
    spend_usd_total: float = 0.0
    day: str = ""  # YYYY-MM-DD of spend_usd_today
    pending_request_ids: tuple[str, ...] = field(default=())
    saved_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["pending_request_ids"] = list(self.pending_request_ids)
        return {"schema": STATE_SCHEMA, **d}


def _problem(raw: object) -> str | None:
    """Why `raw` is not a valid state document, or None. Field names only, never values."""
    if not isinstance(raw, dict):
        return "not a JSON object"
    if raw.get("schema") != STATE_SCHEMA:
        return "unknown schema version"
    known = set(BrainState.__dataclass_fields__) | {"schema"}
    if set(raw) - known:
        return "unknown fields"
    for name in ("session_id", "day"):
        v = raw.get(name, "")
        if not isinstance(v, str) or (v and not _ID.match(v)):
            return f"{name}: not an identifier"
    for name in ("starts", "requests_this_hour"):
        v = raw.get(name, 0)
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 10**9:
            return f"{name}: not a count"
    for name, high in (("spend_usd_session", SPEND_TOTAL_SANITY_MAX), ("spend_usd_today", SPEND_TOTAL_SANITY_MAX),
                       ("spend_usd_total", SPEND_TOTAL_SANITY_MAX), ("hour_start", 1e11), ("saved_at", 1e11)):
        v = raw.get(name, 0.0)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= high:
            return f"{name}: not a number in range"
    ids = raw.get("pending_request_ids", [])
    if not isinstance(ids, list) or len(ids) > STATE_PENDING_MAX or not all(
            isinstance(i, str) and _ID.match(i) for i in ids):
        return f"pending_request_ids: not a list of at most {STATE_PENDING_MAX} identifiers"
    return None


class StateStore:
    """Loads and saves data/state.json. `emit` is the event function (brain.obs.emit) or None."""

    def __init__(self, data_dir: Path, emit: Any = None):
        self.path = Path(data_dir) / STATE_FILE
        self._emit = emit
        self.state = BrainState()
        self.save_failures = 0

    def _event(self, etype: str, msg: str, **fields: Any) -> None:
        if self._emit is not None:
            self._emit(etype, msg, **fields)

    def load(self) -> BrainState:
        """Read the file; on any problem quarantine it and use defaults. Never raises."""
        data, why = read_capped(self.path, STATE_FILE_MAX_BYTES)
        if data is None:
            if why == "missing":
                self._event("state.loaded", "no state file yet; starting fresh", fresh=True)
            else:
                self._quarantine(why)
            self.state = BrainState()
            return self.state
        try:
            raw = json.loads(data.decode("utf-8"))
            problem = _problem(raw)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raw, problem = None, "not valid UTF-8 JSON (perhaps cut short by a crash)"
        if problem is not None or not isinstance(raw, dict):
            self._quarantine(problem or "not a JSON object")
            self.state = BrainState()
            return self.state
        raw.pop("schema", None)
        raw["pending_request_ids"] = tuple(raw.get("pending_request_ids", ()))
        for name in ("hour_start", "spend_usd_session", "spend_usd_today", "spend_usd_total", "saved_at"):
            raw[name] = float(raw.get(name, 0.0))
        self.state = BrainState(**raw)
        self._event("state.loaded", "state file read", starts=self.state.starts,
                    pending=len(self.state.pending_request_ids))
        return self.state

    def _quarantine(self, why: str) -> None:
        target = self.path.with_name(f"state.corrupt-{datetime.now():%Y%m%d-%H%M%S}-{time.monotonic_ns() % 10**6}.json")
        moved = True
        try:
            self.path.replace(target)
        except OSError:
            moved = False
        self._event("state.quarantined", "state file refused; defaults used", problem=why,
                    moved_to=target.name if moved else None)
        try:
            old = sorted(self.path.parent.glob("state.corrupt-*.json"))
            for extra in old[: max(0, len(old) - STATE_QUARANTINE_KEPT)]:
                extra.unlink()
        except OSError:  # swallowed: pruning old quarantined copies is housekeeping; the next load retries
            pass

    def update(self, **changes: Any) -> BrainState:
        """A new state with `changes` applied (checked like a loaded file; a bad change raises ValueError)."""
        new = replace(self.state, **changes)
        problem = _problem(new.to_json())
        if problem is not None:
            raise ValueError(f"refusing a state change: {problem}")
        self.state = new
        return new

    def save(self) -> bool:
        """Write the state atomically. Returns False (and reports) on failure; never raises."""
        self.state = replace(self.state, saved_at=time.time())
        try:
            atomic_write_text(self.path, json.dumps(self.state.to_json(), indent=1))
        except OSError as e:
            self.save_failures += 1
            if self.save_failures == 1 or self.save_failures % 60 == 0:
                self._event("state.save_failed", "state file could not be written",
                            reason=e.strerror or type(e).__name__, failures=self.save_failures)
            return False
        return True
