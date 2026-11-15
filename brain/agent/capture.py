"""Development prompt capture (card P0-07 step 10; DEC-025, Jeff 13 Nov 2026).

While config.toml [logging] full_prompts is true, the full request and the full response (or the failure) of
every model call are appended to a PRIVATE capture file, so an unexpected outcome can be traced to its root
cause. What holds:

- **Where.** `data/capture/prompts-<start>-<pid>-<n>.jsonl` (data/ is git-ignored). Files are created with
  owner-only permissions where the platform honours them (POSIX mode 0600); on Windows a file takes the
  access list of the data folder, which is the developer's own account folder.
- **Never in the event stream.** Events carry only `prompt.capture_ref`: the record id, its kind and its
  size in bytes. The general log never holds a prompt either.
- **No secret.** The API key is not part of what is captured (it travels in a header the SDK adds, which is
  never captured); as a second wall every record is scanned before it is written: the known key values given
  to the capture and anything shaped like a key or token (brain/obs/redact.py SECRET_SHAPES) become
  `<redacted:secret>`.
- **No pixels.** An image block is recorded as its media type, byte count and SHA-256, never its data.
- **Bounded.** One record at most CAPTURE_RECORD_MAX_BYTES (a longer one is cut and marked); one file at
  most CAPTURE_FILE_MAX_BYTES; all files together at most [logging] capture_max_mb (oldest deleted first);
  files older than capture_max_days deleted.
- **Never breaks the agent.** A capture that cannot be written is reported once (a `prompt.capture_ref`
  event with ok false) and the agent goes on.

It records whatever the microphone hears (once speech exists) and whatever is typed: Jeff-only for now.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .. import obs
from ..limits import CAPTURE_FILE_MAX_BYTES, CAPTURE_RECORD_MAX_BYTES
from ..obs.redact import REDACTED_SECRET, SECRET_SHAPES
from .model import AnthropicModel, ModelFailure, ModelReply, ModelRequest

log = logging.getLogger("brain.agent.capture")

CAPTURE_DIR = "capture"
FILE_PREFIX = "prompts-"
DAY_S = 86_400.0


def _image_summary(block: dict[str, Any]) -> dict[str, Any]:
    source = block.get("source")
    data = source.get("data") if isinstance(source, dict) else None
    out: dict[str, Any] = {"type": "image", "media_type": source.get("media_type") if isinstance(source, dict) else None}
    if isinstance(data, str):
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raw = data.encode("utf-8", "replace")
        out.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()[:16])
    return out


def _strip_images(value: Any, depth: int = 0) -> Any:
    """A copy with every image block replaced by its summary (no pixels in the capture)."""
    if depth > 32:
        return "<nested too deeply>"
    if isinstance(value, dict):
        if value.get("type") == "image":
            return _image_summary(value)
        return {k: _strip_images(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_strip_images(v, depth + 1) for v in value]
    return value


class PromptCapture:
    """Appends capture records. `secrets` are exact strings that must never be written (the API key)."""

    def __init__(self, data_dir: Path, *, enabled: bool, max_mb: int, max_days: int, secrets: Iterable[str] = (),
                 now: Callable[[], float] = time.time):
        self.folder = Path(data_dir) / CAPTURE_DIR
        self.enabled = bool(enabled)
        self.max_bytes = max(1, int(max_mb)) * 1024 * 1024
        self.max_days = max(1, int(max_days))
        self._secrets = sorted({s for s in secrets if isinstance(s, str) and len(s) >= 8}, key=len, reverse=True)
        self._now = now
        self._file: Path | None = None
        self._file_bytes = 0
        self._n = 0
        self._started = time.strftime("%Y%m%d-%H%M%S")
        self.records = 0
        self.failures = 0

    def _clean(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, REDACTED_SECRET)
        return SECRET_SHAPES.sub(REDACTED_SECRET, text)

    def _next_file(self) -> Path:
        self._n += 1
        self._file_bytes = 0
        self._file = self.folder / f"{FILE_PREFIX}{self._started}-{os.getpid()}-{self._n}.jsonl"
        return self._file

    def _write(self, line: bytes) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self._file if self._file is not None and self._file_bytes + len(line) <= CAPTURE_FILE_MAX_BYTES \
            else self._next_file()
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        self._file_bytes += len(line)

    def prune(self) -> int:
        """Delete capture files older than max_days, then the oldest until the folder is within max_mb.
        Returns how many were deleted. Never raises."""
        deleted = 0
        try:
            files = sorted(self.folder.glob(f"{FILE_PREFIX}*.jsonl"), key=lambda p: p.stat().st_mtime)
            now = self._now()
            keep: list[tuple[Path, int]] = []
            for f in files:
                st = f.stat()
                if now - st.st_mtime > self.max_days * DAY_S and f != self._file:
                    f.unlink()
                    deleted += 1
                else:
                    keep.append((f, st.st_size))
            total = sum(size for _, size in keep)
            for f, size in keep:
                if total <= self.max_bytes:
                    break
                if f == self._file:
                    continue
                f.unlink()
                total -= size
                deleted += 1
        except OSError as e:
            log.warning("pruning the prompt capture folder failed (%s)", e.strerror or type(e).__name__)
        return deleted

    def _record(self, kind: str, call_id: str, payload: dict[str, Any]) -> str | None:
        """Append one record (kind: request, response or error); return its id, or None when capture is off or
        failed. Emits `prompt.capture_ref` with the id and byte count only. Never raises."""
        if not self.enabled:
            return None
        self.records += 1
        rid = f"c-{self._started}-{os.getpid()}-{self.records}"
        try:
            body = json.dumps({"id": rid, "ts": round(self._now(), 3), "kind": kind, "call": call_id,
                               "payload": _strip_images(payload)}, ensure_ascii=False, default=str)
            body = self._clean(body)
            raw = body.encode("utf-8", "replace")
            if len(raw) > CAPTURE_RECORD_MAX_BYTES:
                cut = raw[: CAPTURE_RECORD_MAX_BYTES - 200].decode("utf-8", "ignore")
                body = json.dumps({"id": rid, "kind": kind, "call": call_id, "cut_at_bytes": CAPTURE_RECORD_MAX_BYTES,
                                   "original_bytes": len(raw), "head": self._clean(cut)}, ensure_ascii=False)
                raw = body.encode("utf-8", "replace")
            self._write(raw + b"\n")
        except (OSError, TypeError, ValueError, RecursionError) as e:
            self.failures += 1
            if self.failures == 1:
                log.warning("the prompt capture could not be written (%s); the agent goes on without it",
                            getattr(e, "strerror", None) or type(e).__name__)
            obs.emit("prompt.capture_ref", "prompt capture failed", record=rid, kind=kind, ok=False,
                     failures=self.failures, level="WARNING" if self.failures == 1 else "DEBUG")
            return None
        if self.records % 50 == 1:
            self.prune()
        obs.emit("prompt.capture_ref", f"{kind} captured", record=rid, kind=kind, call=call_id, bytes=len(raw) + 1,
                 ok=True)
        return rid

    def request(self, call_id: str, request: ModelRequest) -> str | None:
        """Capture the full request exactly as it is sent (the Messages API parameters)."""
        return self._record("request", call_id, AnthropicModel.params(request)) if self.enabled else None

    def reply(self, call_id: str, reply: ModelReply) -> str | None:
        """Capture the full checked reply: its content blocks, stop reason and usage."""
        if not self.enabled:
            return None
        return self._record("response", call_id, {
            "stop_reason": reply.stop_reason, "content": list(reply.blocks), "usage": vars(reply.usage),
            "events": reply.events, "dropped": reply.dropped, "text_cut": reply.text_cut})

    def failure(self, call_id: str, failure: ModelFailure) -> str | None:
        """Capture a failed call: its code, status and the usage known (no vendor text exists to capture)."""
        if not self.enabled:
            return None
        return self._record("error", call_id, {"code": failure.code, "status": failure.status,
                                               "retryable": failure.retryable,
                                               "usage": vars(failure.usage) if failure.usage else None})
