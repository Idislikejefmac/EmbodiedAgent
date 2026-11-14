"""Log hygiene: secrets, control characters, over-long lines and flooding in log output.

One set of rules, used three ways:
- on every log record a chaos case produces (tools/chaos/monitors.py);
- on structured events (`check_event`), the hook for the structured live event log of card X-02;
- on existing text log files (`scan_file`), by explicit command only, because logs are real data:
      python -m brain chaos --scan-logs [DIR]        (default data/logs)
  The report gives file names, line numbers and the kind of finding, never the text of a line.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brain.limits import LOG_RECORD_MAX_BYTES, PEER_LOG_LINES_PER_MIN_MAX

# Shapes of real secrets (E10 uses the same idea for the repository). Canaries are checked separately.
SECRET_PATTERNS = {
    "api_key": re.compile(r"\bsk-(?:ant|proj|live|test)[A-Za-z0-9_-]{8,}"),
    "hex_token_64": re.compile(r"(?<![0-9A-Fa-f])[0-9a-fA-F]{64}(?![0-9A-Fa-f])"),
    "env_assignment": re.compile(r"\b[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)\s*=\s*\S{6,}"),
    "bearer": re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._-]{16,}"),
    "url_password": re.compile(r"://[^/\s:@]+:[^/\s@]+@"),
}
CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")  # tab tolerated
LINE_PREFIX_ALLOWANCE = 200  # timestamp, level and logger name in a file line
_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}):\d{2}")


@dataclass(frozen=True)
class Finding:
    where: str  # "file:line" or "record n" or "event n"
    kind: str  # secret_canary | secret_<pattern> | control_char | over_long | flood

    def __str__(self) -> str:
        return f"{self.where}: {self.kind}"


def text_findings(text: str, where: str, *, canaries: Mapping[str, str] | None = None,
                  max_bytes: int = LOG_RECORD_MAX_BYTES, allow_newlines: bool = False) -> list[Finding]:
    out: list[Finding] = []
    low = text.lower()
    for label, secret in (canaries or {}).items():
        if secret and secret.lower() in low:
            out.append(Finding(where, f"secret_canary:{label}"))
    for name, pattern in SECRET_PATTERNS.items():
        if pattern.search(text):
            out.append(Finding(where, f"secret_{name}"))
    body = text.replace("\n", "") if allow_newlines else text
    if CONTROL.search(body):
        out.append(Finding(where, "control_char"))
    if len(text.encode("utf-8", "replace")) > max_bytes:
        out.append(Finding(where, "over_long"))
    return out


def check_event(event: Mapping[str, Any], where: str, canaries: Mapping[str, str] | None = None) -> list[Finding]:
    """A structured event (card X-02): plain JSON, bounded, no control characters in its strings, no secret."""
    try:
        text = json.dumps(event, ensure_ascii=False, allow_nan=False, default=str)
    except (TypeError, ValueError):
        return [Finding(where, "not_plain_json")]
    out = text_findings(text, where, canaries=canaries)
    stack: list[Any] = [event]
    while stack:
        v = stack.pop()
        if isinstance(v, Mapping):
            stack.extend(v.values())
        elif isinstance(v, (list, tuple)):
            stack.extend(v)
        elif isinstance(v, str) and CONTROL.search(v):
            out.append(Finding(where, "control_char"))
            break
    return out


def flood_findings(stamps: Iterable[str], where: str, limit: int = PEER_LOG_LINES_PER_MIN_MAX) -> list[Finding]:
    per_minute = Counter(stamps)
    return [Finding(f"{where} {minute}", f"flood:{n}_lines_per_minute") for minute, n in sorted(per_minute.items())
            if n > limit]


def scan_file(path: Path, canaries: Mapping[str, str] | None = None) -> list[Finding]:
    """A text log file written by brain/__main__.py setup_logging. Continuation lines of a traceback
    (lines without a timestamp) are counted as part of the record before them."""
    out: list[Finding] = []
    stamps: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return [Finding(path.name, f"unreadable:{type(e).__name__}")]
    for i, line in enumerate(lines, 1):
        m = _STAMP.match(line)
        if m:
            stamps.append(m.group(1))
        elif line.startswith("Traceback"):
            out.append(Finding(f"{path.name}:{i}", "traceback"))  # a crash, or an isolation point's report of one
        out += text_findings(line, f"{path.name}:{i}", canaries=canaries,
                             max_bytes=LOG_RECORD_MAX_BYTES + LINE_PREFIX_ALLOWANCE)
    out += flood_findings(stamps, path.name)
    return out


def scan_dir(folder: Path, canaries: Mapping[str, str] | None = None) -> tuple[int, list[Finding]]:
    files = sorted(p for p in folder.glob("*.log*") if p.is_file()) if folder.is_dir() else []
    findings: list[Finding] = []
    for f in files:
        findings += scan_file(f, canaries)
    return len(files), findings


def summarise(findings: list[Finding]) -> dict[str, int]:
    return dict(Counter(f.kind.split(":")[0] for f in findings))
