"""Bounded log text for peer-supplied data (H-08; docs/testing/00 Appendix A: LOG_RECORD_MAX_BYTES).

A peer can make a list or a dict as long as a frame allows. These helpers keep one log line short: a count
plus the first few items, each shortened and shown with %r (so it cannot forge a line), and `CapRecordSize`
is a last line of defence that cuts any record, whatever put the text there.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from itertools import islice

from .limits import LOG_PEER_ITEM_MAX_CHARS, LOG_PEER_KEYS_SHOWN, LOG_RECORD_MAX_BYTES

TRUNCATED = "...[truncated]"


def short_items(items: Iterable[object], total: int, *, shown: int = LOG_PEER_KEYS_SHOWN,
                item_chars: int = LOG_PEER_ITEM_MAX_CHARS) -> str:
    """`N: ['a', 'b', 'c'] and N-3 more`: the count and the first few items, each cut and shown with %r."""
    first = [repr(str(i)[:item_chars]) + ("..." if len(str(i)) > item_chars else "") for i in islice(items, shown)]
    more = f" and {total - len(first)} more" if total > len(first) else ""
    return f"{total}: [{', '.join(first)}]{more}"


def short_repr(value: object, max_chars: int = 300) -> str:
    """repr(value), cut to max_chars. Never raises (a peer-built object whose repr fails is named by type)."""
    try:
        text = repr(value)
    except Exception:  # broad-except: repr of a hostile object may raise anything; logging must not
        return f"<{type(value).__name__}>"
    return text if len(text) <= max_chars else text[:max_chars] + "..."


class CapRecordSize(logging.Filter):
    """Cut any record's message to `max_bytes` of UTF-8 (LOG_RECORD_MAX_BYTES). Attach it to every handler:
    a logger's own filters do not see records from its children."""

    def __init__(self, max_bytes: int = LOG_RECORD_MAX_BYTES):
        super().__init__()
        self.max_bytes = max_bytes

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # broad-except: a bad format string or argument must not break logging
            text = f"{record.msg!r}"[: self.max_bytes]
        raw = text.encode("utf-8", "replace")
        if len(raw) > self.max_bytes:
            keep = max(0, self.max_bytes - len(TRUNCATED.encode()))
            text = raw[:keep].decode("utf-8", "ignore") + TRUNCATED
            record.msg, record.args = text, ()
        return True
