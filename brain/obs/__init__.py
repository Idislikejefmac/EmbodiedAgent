"""Observability for the brain (card X-02): structured events, the health panel and the crash barrier.

    from brain import obs
    obs.emit("tool.result", "get_pose ok", tool="get_pose", status="ok", ms=3.1)

`emit` never raises and never blocks; its fields are redacted and bounded at the source. See
docs/observability.md for the event types, how to watch them (`python -m brain watch`), the health
panel (`python -m brain health`) and the crash barrier.

This package imports nothing from the rest of the brain except brain.limits, so every module may use it
(demo.py, which drives SimBody through the tool registry, imports them inside its function).
"""

from __future__ import annotations

from typing import Any

from .events import EVENT_TYPES, correlate
from .health import HealthBoard
from .pipeline import Event, EventLog

LOG = EventLog()
HEALTH = HealthBoard()
LOG.add_sink(HEALTH.on_event)


def emit(etype: str, msg: str = "", /, *, level: str | None = None, **fields: Any) -> Event | None:
    """Record one event in the process's event log. Never raises, never blocks. `etype` must be a name in
    EVENT_TYPES (an unknown name is recorded as obs.bad_event). Field values are redacted and bounded."""
    return LOG.emit(etype, msg, level=level, **fields)


__all__ = ["EVENT_TYPES", "HEALTH", "LOG", "Event", "EventLog", "HealthBoard", "correlate", "emit"]
