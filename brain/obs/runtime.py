"""The observability pieces one brain run needs, wired together: event files, data/health.json, data/state.json
and the supervised components that keep them going (used by `serve` and `demo-trace`)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..limits import OBS_HEALTH_INTERVAL_S
from .health import HEALTH_FILE, HealthBoard, write_snapshot
from .pipeline import EventLog
from .state import StateStore
from .supervisor import Supervisor, watch_thread

LOGS_DIR = "logs"


class ObsRuntime:
    def __init__(self, data_dir: Path, *, log: EventLog, board: HealthBoard, session_id: str = ""):
        self.data_dir = Path(data_dir)
        self.log = log
        self.board = board
        self.health_path = self.data_dir / HEALTH_FILE
        self.state = StateStore(self.data_dir, log.emit)
        self.session_id = session_id
        self.health_writes = 0
        self.health_failures = 0
        self._lost = (0, 0)  # (writer drops, per-type suppressions) already reported

    def start(self) -> None:
        """Start the event files and load (or start) the state file. Never raises for a file problem: a
        failing event file is reported by the writer, a bad state file is quarantined."""
        self.log.start_files(self.data_dir / LOGS_DIR)
        old = self.state.load()
        try:
            self.state.update(starts=old.starts + 1, session_id=self.session_id or old.session_id)
        except ValueError:  # swallowed: an id that is not acceptable keeps the old one; the counter still counts
            self.state.update(starts=old.starts + 1)
        self.state.save()
        self.board.session = self.session_id or None

    def write_health(self) -> bool:
        """Emit pending health changes, then write data/health.json atomically. Never raises."""
        for name, state, reason in self.board.take_changes():
            self.log.emit("health.change", f"{name} is {state}", source=name, state=state, reason=reason,
                          level="INFO" if state in ("up", "unknown") else "WARNING")
        stats = self.log.stats()
        lost = (stats["writer_dropped"], stats["suppressed"])
        if lost != self._lost and (lost[0] > self._lost[0] or lost[1] > self._lost[1]):
            self.log.emit("obs.dropped", "events were dropped or held back since the last snapshot",
                          writer_dropped=lost[0] - self._lost[0], suppressed=lost[1] - self._lost[1],
                          by_type=dict(self.log.suppressed))
        self._lost = lost
        st = self.state.state
        extra: dict[str, Any] = {
            "budget": {"guard": "placeholder until card P0-07 step 9", "requests_this_hour": st.requests_this_hour,
                       "spend_usd_session": st.spend_usd_session, "spend_usd_today": st.spend_usd_today,
                       "spend_usd_total": st.spend_usd_total},
            "state_file": {"starts": st.starts, "pending_requests": len(st.pending_request_ids),
                           "save_failures": self.state.save_failures},
        }
        try:
            snap = self.board.snapshot(data_dir=self.data_dir, obs_stats=self.log.stats(), extra=extra)
            write_snapshot(self.health_path, snap)
        except (OSError, TypeError, ValueError) as e:
            self.health_failures += 1
            if self.health_failures in (1, 10, 100, 1000):
                self.log.emit("obs.sink_failed", "data/health.json could not be written", file="health.json",
                              reason=type(e).__name__, failures=self.health_failures, level="WARNING")
            return False
        self.health_writes += 1
        return True

    async def health_loop(self) -> None:
        while True:
            self.write_health()
            await asyncio.sleep(OBS_HEALTH_INTERVAL_S)

    def supervise(self, sup: Supervisor) -> None:
        """Run the health writer and the event-writer watchdog as supervised components."""
        sup.start("health-writer", self.health_loop)

        def alive() -> bool:
            w = self.log.writer
            return w is None or w.alive or w.state == "stopped"

        def restart() -> None:
            self.log.ensure_writer()

        sup.start("event-writer-watch", lambda: watch_thread("event-writer", alive, restart, emit=self.log.emit))

    def stop(self) -> None:
        """Last health snapshot, save the state, drain and close the event files. Never raises."""
        self.write_health()
        self.state.save()
        self.log.stop_files()
