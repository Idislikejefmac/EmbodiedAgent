"""Test-wide setup.

Network policy (docs/testing CONVENTIONS C8): the whole suite runs under the egress guard with a
LOOPBACK-ONLY policy: no outside destination at all, whatever config.toml allows. (Until X-01 the suite
used config.toml's allowlist, so a real model call from a test would have gone out once a key was in
.env.) Loopback to ports this process listens on, which the transport tests use, still works. A test
that must reach a real vendor carries @pytest.mark.real_vendor; such tests are never collected into a
normal run (they need their own process, policy and spend cap; the guard cannot be widened).

Markers (registered in pyproject.toml): chaos, slow (skipped unless --run-slow), real_vendor (never
run here), gate(name, limit=, edge=) for E1b gate-edge tests, trace(ids..., promises=, risks=) for
traceability (tools/trace.py).
"""

import json
import os
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
sys.path.insert(0, str(TESTS))  # so tests can import body_contract
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))  # so tests can import tools.chaos

from brain import egress  # noqa: E402
from tools.chaos.pytest_plugin import chaos_monitors, chaos_quick_report, chaos_sandbox  # noqa: E402,F401

try:  # Hypothesis: deterministic in a normal run, more examples with HYPOTHESIS_PROFILE=chaos-full
    from hypothesis import settings

    settings.register_profile("chaos-quick", derandomize=True, database=None, deadline=None, max_examples=150,
                              print_blob=True)
    settings.register_profile("chaos-full", derandomize=True, database=None, deadline=None, max_examples=2000,
                              print_blob=True)
    settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "chaos-quick"))
except ImportError:  # the property tests then fail loudly at import (no silent skip)
    pass


def pytest_addoption(parser):
    parser.addoption("--run-slow", action="store_true", help="also run tests marked slow (chaos --full)")
    parser.addoption("--trace-out", metavar="PATH", help="write collected test ids, markers and docstrings "
                                                         "as JSON for tools/trace.py")


def pytest_collection_modifyitems(config, items):
    keep, drop = [], []
    for item in items:
        if item.get_closest_marker("real_vendor") or (item.get_closest_marker("slow")
                                                       and not config.getoption("--run-slow")):
            drop.append(item)
        else:
            keep.append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
    out = config.getoption("--trace-out")
    if out:
        rows = []
        for item in keep + drop:
            marks = [{"name": m.name, "args": [str(a) for a in m.args],
                      "kwargs": {k: [str(x) for x in v] if isinstance(v, (list, tuple)) else str(v)
                                 for k, v in m.kwargs.items()}}
                     for m in item.iter_markers() if m.name in ("trace", "gate", "chaos", "slow", "real_vendor")]
            doc = (getattr(item, "function", None).__doc__ or "").strip().splitlines() if hasattr(item, "function") else []
            rows.append({"node": item.nodeid, "markers": marks, "doc": doc[0] if doc else "",
                         "selected": item in keep})
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(rows, indent=1), encoding="utf-8")


@pytest.fixture(scope="session", autouse=True)
def egress_guard():
    egress.install_guard(egress.EgressPolicy())  # loopback only (C8)
    yield
