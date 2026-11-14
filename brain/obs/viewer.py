"""`python -m brain watch`: the live viewer and replayer of the event files (card X-02 step 4).

It reads ONLY event files (`events-*.jsonl` in the logs folder, or a folder or file given on the command
line whose names match that pattern) and, with --health, the health snapshot. It opens no socket and
reads nothing else, so it cannot be used to look at any other file (a test checks every file it opens).
The live view is the same files followed as they grow: no network listener is needed.

- Filters: --component, --level (minimum), --type (glob), --session, --turn, --since (10s, 5m, 2h, or a
  time such as 2026-11-13T10:00).
- Formats: one compact line per event (default), or --detail with every field.
- --follow keeps reading as events arrive, across file rotation and several writers at once (the
  launcher and the brain each write their own file): every poll it reads the new bytes of each event file.
- The files are treated as untrusted: a line that is too long, not JSON or not an event is skipped and
  counted, and every string is cleaned again before it reaches the terminal (no control characters).
"""

from __future__ import annotations

import fnmatch
import json
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..limits import OBS_VIEW_LINE_MAX_BYTES, OBS_VIEW_POLL_S
from .events import LEVELS
from .health import format_panel, read_snapshot
from .pipeline import EVENT_FILE_PREFIX, EVENT_FILE_SUFFIX
from .redact import clean_text

EVENT_FILE_NAME = re.compile(r"^events-[A-Za-z0-9_.-]{1,100}\.jsonl$")
_SINCE = re.compile(r"^(\d{1,6})\s*([smhd])$")


def is_event_file(path: Path) -> bool:
    return bool(EVENT_FILE_NAME.match(path.name))


def event_files(folder: Path) -> list[Path]:
    """The event files of a folder, oldest name first. Only names matching events-*.jsonl."""
    try:
        return sorted(p for p in folder.glob(f"{EVENT_FILE_PREFIX}*{EVENT_FILE_SUFFIX}") if is_event_file(p))
    except OSError:
        return []


def parse_since(text: str, now: float | None = None) -> float:
    """--since: `10s`, `5m`, `2h`, `1d` ago, or an ISO time (local time unless it says otherwise). Returns
    epoch seconds. Raises ValueError with a plain message."""
    now = time.time() if now is None else now
    m = _SINCE.match(text.strip())
    if m:
        unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        return now - int(m.group(1)) * unit
    try:
        when = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("--since must be like 10s, 5m, 2h, 1d or a time like 2026-11-13T10:00") from None
    return when.timestamp()


def _epoch(ts: object) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class Filter:
    components: frozenset[str] = frozenset()
    min_level: int = 0
    types: tuple[str, ...] = ()
    session: str | None = None
    turn: str | None = None
    since: float | None = None

    def matches(self, e: Mapping[str, Any]) -> bool:
        if self.components and e.get("comp") not in self.components:
            return False
        if LEVELS.get(str(e.get("level")), 0) < self.min_level:
            return False
        if self.types and not any(fnmatch.fnmatchcase(str(e.get("type", "")), t) for t in self.types):
            return False
        if self.session is not None and e.get("session") != self.session:
            return False
        if self.turn is not None and e.get("turn") != self.turn:
            return False
        if self.since is not None:
            t = _epoch(e.get("ts"))
            if t is None or t < self.since:
                return False
        return True


@dataclass
class Counts:
    shown: int = 0
    skipped_bad: int = 0  # too long, not JSON, not an event
    files: set[str] = field(default_factory=set)


def parse_line(raw: bytes) -> dict[str, Any] | None:
    """One event line, or None if it is too long, not UTF-8 JSON, or not an event object."""
    if len(raw) > OBS_VIEW_LINE_MAX_BYTES:
        return None
    try:
        e = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(e, dict) or not isinstance(e.get("type"), str) or not isinstance(e.get("f", {}), dict):
        return None
    return e


def _fmt_value(v: object) -> str:
    if isinstance(v, str):
        return clean_text(v, 120)
    try:
        return clean_text(json.dumps(v, ensure_ascii=False, default=str), 200)
    except (TypeError, ValueError, RecursionError):
        return "?"


def format_event(e: Mapping[str, Any], *, detail: bool = False) -> str:
    """A compact line, or with `detail` a block with every field. Every string is cleaned."""
    ts = e.get("ts")
    clock = clean_text(ts[11:23], 12) if isinstance(ts, str) and len(ts) >= 23 else "??:??:??.???"
    level = clean_text(str(e.get("level", "?")), 8)
    comp = clean_text(str(e.get("comp", "?")), 10)
    etype = clean_text(str(e.get("type", "?")), 24)
    corr = [clean_text(str(e[k]), 32) for k in ("session", "turn") if isinstance(e.get(k), str)]
    msg = clean_text(str(e.get("msg", "")), 160)
    raw_f = e.get("f")
    f: dict[str, Any] = raw_f if isinstance(raw_f, dict) else {}
    if not detail:
        fields = " ".join(f"{clean_text(str(k), 30)}={_fmt_value(v)}" for k, v in list(f.items())[:10])
        return (f"{clock} {level:<7} {comp:<10} {etype:<22} {'[' + '|'.join(corr) + '] ' if corr else ''}"
                f"{msg}{'  ' + fields if fields else ''}")
    lines = [f"{clean_text(str(ts), 40)}  {level}  {comp}  {etype}  seq={_fmt_value(e.get('seq'))} "
             f"mono={_fmt_value(e.get('mono'))}",
             f"    session={_fmt_value(e.get('session'))} turn={_fmt_value(e.get('turn'))}",
             f"    {msg}"]
    lines += [f"    {clean_text(str(k), 40)}: {_fmt_value(v)}" for k, v in f.items()]
    return "\n".join(lines)


def read_events(files: Iterable[Path], counts: Counts | None = None) -> Iterator[dict[str, Any]]:
    """Every event of the files, in file order. Unreadable files and bad lines are skipped (counted)."""
    c = counts if counts is not None else Counts()
    for path in files:
        if not is_event_file(path):
            continue
        try:
            with open(path, "rb") as fh:
                c.files.add(path.name)
                for raw in fh:
                    e = parse_line(raw.rstrip(b"\r\n"))
                    if e is None:
                        c.skipped_bad += 1 if raw.strip() else 0
                        continue
                    yield e
        except OSError:
            c.skipped_bad += 1


class Follower:
    """Reads what was appended to each event file of a folder since the last poll (reopening each file per
    poll, so the writer can rotate and prune; nothing is renamed). Partial last lines wait for the next poll."""

    def __init__(self, folder: Path, *, from_start: bool = True):
        self.folder = folder
        self.offsets: dict[str, int] = {}
        self.partial: dict[str, bytes] = {}
        if not from_start:
            for p in event_files(folder):
                try:
                    self.offsets[p.name] = p.stat().st_size
                except OSError:
                    continue

    def poll(self, counts: Counts | None = None) -> list[dict[str, Any]]:
        c = counts if counts is not None else Counts()
        out: list[dict[str, Any]] = []
        for path in event_files(self.folder):
            start = self.offsets.get(path.name, 0)
            try:
                with open(path, "rb") as fh:
                    fh.seek(start)
                    data = fh.read(4 * 1024 * 1024)
            except OSError:
                continue
            if not data:
                continue
            self.offsets[path.name] = start + len(data)
            c.files.add(path.name)
            data = self.partial.pop(path.name, b"") + data
            *lines, rest = data.split(b"\n")
            if rest:
                self.partial[path.name] = rest[: OBS_VIEW_LINE_MAX_BYTES + 1]
            for raw in lines:
                e = parse_line(raw.rstrip(b"\r"))
                if e is None:
                    c.skipped_bad += 1 if raw.strip() else 0
                    continue
                out.append(e)
        out.sort(key=lambda e: (str(e.get("ts", "")), e.get("seq") if isinstance(e.get("seq"), int) else 0))
        return out


def watch(source: Path, flt: Filter, *, detail: bool = False, follow: bool = False,
          out: Callable[[str], None] = print, poll_s: float = OBS_VIEW_POLL_S,
          keep_going: Callable[[], bool] = lambda: True) -> Counts:
    """Print the events of `source` (an event-file folder, or one event file) that pass `flt`; with
    `follow`, keep printing new ones until `keep_going()` is false or Ctrl+C."""
    counts = Counts()
    if source.is_file():
        if not is_event_file(source):
            raise ValueError("watch reads only event files (events-*.jsonl)")
        for e in read_events([source], counts):
            if flt.matches(e):
                out(format_event(e, detail=detail))
                counts.shown += 1
        return counts
    follower = Follower(source)
    while True:
        for e in follower.poll(counts):
            if flt.matches(e):
                out(format_event(e, detail=detail))
                counts.shown += 1
        if not follow or not keep_going():
            return counts
        time.sleep(poll_s)


def health_view(path: Path, *, follow: bool = False, out: Callable[[str], None] = print,
                period_s: float = 1.0, keep_going: Callable[[], bool] = lambda: True) -> int:
    """Print the health panel from `path` (data/health.json), once or every `period_s`. 0 if it was read."""
    while True:
        snap, problem = read_snapshot(path)
        if snap is None:
            out(f"no health snapshot ({problem}): is the brain running? Start it with `python -m brain serve`.")
            code = 1
        else:
            out(format_panel(snap))
            code = 0
        if not follow or not keep_going():
            return code
        out("")
        time.sleep(period_s)
