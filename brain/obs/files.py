"""Small file helpers for the observability files: atomic replace and capped, checked reads.

Side effects: `atomic_write_text` writes `<path>` (through a temporary file in the same folder) and nothing
else; the callers keep every such file under the configured data folder.
"""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` so a reader sees the old file or the new one, never half of one: write a
    temporary file in the same folder, flush it to disk, then os.replace it over `path`. Raises OSError
    (the temporary file is removed); the caller decides what a failure means."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:  # swallowed: the temporary file may not exist; the original error is what matters
            pass
        raise


def read_capped(path: Path, max_bytes: int) -> tuple[bytes | None, str]:
    """(bytes, "") or (None, why): a missing, unreadable or larger-than-max_bytes file is refused before it is
    parsed. Never raises."""
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes + 1)
    except FileNotFoundError:
        return None, "missing"
    except OSError as e:
        return None, f"unreadable ({e.strerror or type(e).__name__})"
    if len(data) > max_bytes:
        return None, f"larger than {max_bytes} bytes"
    return data, ""
