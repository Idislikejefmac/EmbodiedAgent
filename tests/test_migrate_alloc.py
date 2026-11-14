"""Regression: reading a small archive must not reserve memory for the size LIMIT.

The chaos full run found that `f.read(ARCHIVE_MAX_BYTES + 1)` made Python allocate 8 GiB up front, even for an
empty or tiny file (BufferedReader.read(n) allocates n bytes first). T-P0-04-024 family, H-01.
"""

import importlib.util
import tracemalloc
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "migrate_alloc_under_test", Path(__file__).resolve().parent.parent / "tools" / "migrate.py"
)
assert _spec is not None and _spec.loader is not None
migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate)

ALLOWED_PEAK_BYTES = 64 * 1024 * 1024  # generous: a few KiB are read; the old code asked for 8 GiB


@pytest.mark.parametrize("size", [0, 1, 6, 1024])
def test_reading_a_small_archive_does_not_allocate_the_limit(tmp_path, size):
    f = tmp_path / "a.eamig"
    f.write_bytes(b"x" * size)
    tracemalloc.start()
    try:
        blob = migrate._read_archive(f)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert blob == b"x" * size
    assert peak < ALLOWED_PEAK_BYTES, f"peak {peak} bytes while reading {size} bytes"


def test_the_decrypt_path_refuses_a_tiny_bad_archive_without_a_big_allocation(tmp_path):
    f = tmp_path / "bad.eamig"
    f.write_bytes(b"not an archive")
    tracemalloc.start()
    try:
        with pytest.raises(ValueError):
            migrate.decrypt_archive(f, "a-long-test-passphrase")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < ALLOWED_PEAK_BYTES
