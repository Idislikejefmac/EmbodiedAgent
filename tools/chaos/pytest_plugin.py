"""Pytest fixtures for the chaos harness (loaded by tests/conftest.py through `pytest_plugins`).

    chaos_sandbox      session-wide sandbox (temporary data folder, canary token store, canary keys)
    chaos_monitors     run a test body under every invariant monitor; the test fails at teardown on
                       any violation (forbidden side effect, logged traceback, over-long or forged log
                       line, canary secret in a log record). Use it in any test that drives an entry point:

                           def test_something(chaos_monitors):
                               ...  # drive the entry point
                               # violations are checked when the test ends

    chaos_quick_report the quick chaos run, once per session (tests/chaos/test_chaos_runner.py uses it)
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from . import monitors, sandbox


@pytest.fixture(scope="session")
def chaos_sandbox(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sandbox.Sandbox]:
    sb = sandbox.create(tmp_path_factory.mktemp("chaos-sandbox"))
    try:
        yield sb
    finally:
        sandbox.destroy(sb)


@pytest.fixture
def chaos_monitors(chaos_sandbox: sandbox.Sandbox, tmp_path: Path) -> Iterator[monitors.CaseMonitors]:
    policy = monitors.SideEffectPolicy.make(write_roots=[chaos_sandbox.root, tmp_path],
                                            secret_roots=[chaos_sandbox.secrets_dir],
                                            protected_roots=sandbox.protected_roots())
    mon = monitors.CaseMonitors(policy=policy, canaries=dict(chaos_sandbox.canaries))
    mon.start()
    try:
        yield mon
    finally:
        found = mon.finish()
    if found:
        pytest.fail("chaos monitors: " + "; ".join(str(v) for v in found[:5]), pytrace=False)


@pytest.fixture(scope="session")
def chaos_quick_report(tmp_path_factory: pytest.TempPathFactory) -> Any:
    from . import runner

    return runner.run("quick", sandbox_parent=tmp_path_factory.mktemp("chaos-run"))
