"""Crash reports: what main() writes when something unexpected reaches the outermost level, and what the
launcher writes when the brain process dies (card X-02 step 7).

A report is a small JSON file in data/crash/ (at most CRASH_FILES_KEPT kept): when, which command, the
exception type, a cleaned and shortened message, the frames as `file:line in function` (never local
values), and the last events from the ring buffer (already redacted). It holds no secrets: the message
goes through the same redaction as an event.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from ..limits import CRASH_FILES_KEPT
from .files import atomic_write_text
from .redact import clean_text

CRASH_DIR = "crash"
FRAMES_KEPT = 20
EVENTS_KEPT = 30


def frames_of(exc: BaseException) -> list[str]:
    try:
        tb = traceback.extract_tb(exc.__traceback__)[-FRAMES_KEPT:]
        return [clean_text(f"{f.filename.replace(chr(92), '/').split('/brain/')[-1]}:{f.lineno} in {f.name}", 160)
                for f in tb]
    except Exception:  # broad-except: a broken traceback must not stop the crash report
        return []


def write_report(data_dir: Path, kind: str, details: Mapping[str, Any],
                 recent_events: Sequence[Mapping[str, Any]] | None = None) -> Path | None:
    """Write data/crash/<kind>-<time>.json and prune old reports. Returns the path, or None if it could not
    be written (the caller still prints its one line). Never raises."""
    folder = Path(data_dir) / CRASH_DIR
    path = folder / f"{kind}-{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}-{time.monotonic_ns() % 10**6:06d}.json"
    report = {"v": 1, "kind": kind, "when": time.time(), "when_text": datetime.now().isoformat(timespec="seconds"),
              "pid": os.getpid(), **dict(details), "recent_events": list(recent_events or [])[-EVENTS_KEPT:]}
    try:
        atomic_write_text(path, json.dumps(report, ensure_ascii=False, indent=1, default=str))
    except (OSError, TypeError, ValueError):
        return None
    try:
        old = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for extra in old[: max(0, len(old) - CRASH_FILES_KEPT)]:
            extra.unlink()
    except OSError:  # swallowed: pruning old reports is housekeeping; the next report retries
        pass
    return path


def exception_details(exc: BaseException, command: str) -> dict[str, Any]:
    return {"command": clean_text(command, 40), "exception": type(exc).__name__,
            "message": clean_text(str(exc), 300) if _str_ok(exc) else "<unprintable>", "frames": frames_of(exc)}


def _str_ok(exc: BaseException) -> bool:
    try:
        str(exc)
        return True
    except Exception:  # broad-except: str() of a hostile exception may raise anything
        return False
