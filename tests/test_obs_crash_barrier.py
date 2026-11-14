"""Card X-02 steps 6 and 7 and the acceptance test: the crash barrier (supervised tasks, threads, connections and
stream producers; the process launcher; main()'s outermost barrier) and the scripted demo with no hardware."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect as ws_connect

from brain import obs
from brain.limits import (LAUNCHER_BACKOFF_INITIAL_S, LAUNCHER_BACKOFF_MAX_S, LAUNCHER_RESTARTS_MAX,
                          LAUNCHER_STOP_TIMEOUT_S, LAUNCHER_WINDOW_S, SUPERVISOR_BACKOFF_INITIAL_S,
                          SUPERVISOR_BACKOFF_MAX_S, SUPERVISOR_RESTARTS_MAX, SUPERVISOR_STOP_TIMEOUT_S,
                          SUPERVISOR_WINDOW_S)
from brain.obs.launcher import child_command, run_launcher
from brain.obs.supervisor import (DEFAULT_POLICY, FatalComponentError, Policy, Supervisor, run_with_restarts,
                                  watch_task, watch_thread)
from brain.transport.server import BrainServer
from fakes import TOKEN, body, wait_for

FAST = Policy(max_failures=3, window_s=60.0, backoff_initial_s=0.01, backoff_max_s=0.04)
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def events():  # type: ignore[no-untyped-def]
    got: list[dict[str, Any]] = []
    obs.LOG.reset_limits()
    obs.LOG.add_sink(got.append)
    try:
        yield got
    finally:
        obs.LOG.remove_sink(got.append)


def types(events: list[dict[str, Any]]) -> list[str]:
    return [e["type"] for e in events]


# ---- the supervisor --------------------------------------------------------------------------------------------


def test_the_default_policy_is_the_named_limits():
    assert DEFAULT_POLICY == Policy(SUPERVISOR_RESTARTS_MAX, SUPERVISOR_WINDOW_S, SUPERVISOR_BACKOFF_INITIAL_S,
                                    SUPERVISOR_BACKOFF_MAX_S)
    assert [DEFAULT_POLICY.backoff(n) for n in (1, 2, 3, 10)] == [0.5, 1.0, 2.0, SUPERVISOR_BACKOFF_MAX_S]
    assert SUPERVISOR_STOP_TIMEOUT_S == 5.0


def test_a_crash_is_contained_reported_and_restarted_then_recovers(events):  # type: ignore[no-untyped-def]
    n = {"runs": 0}

    async def flaky() -> None:
        n["runs"] += 1
        if n["runs"] < 3:
            raise RuntimeError("injected")

    assert asyncio.run(run_with_restarts("flaky", flaky, policy=FAST)) == "done"
    assert n["runs"] == 3
    t = types(events)
    assert t.count("supervisor.crashed") == 2 and t.count("supervisor.restart") == 2 and "supervisor.stopped" in t
    crashed = next(e for e in events if e["type"] == "supervisor.crashed")
    assert crashed["f"]["error"] == "RuntimeError" and "test_obs_crash_barrier.py" in crashed["f"]["where"]


def test_a_component_that_keeps_failing_is_stopped_and_reported_not_restarted_forever(events):  # type: ignore[no-untyped-def]
    n = {"runs": 0}

    async def broken() -> None:
        n["runs"] += 1
        raise ValueError("always")

    assert asyncio.run(run_with_restarts("broken", broken, policy=FAST)) == "failed"
    assert n["runs"] == FAST.max_failures and types(events).count("supervisor.gave_up") == 1


def test_a_security_refusal_is_never_retried(events):  # type: ignore[no-untyped-def]
    n = {"runs": 0}

    async def refuse() -> None:
        n["runs"] += 1
        raise FatalComponentError("frame limit cannot be enforced")

    async def go() -> Supervisor:
        sup = Supervisor(policy=FAST)
        sup.start("transport", refuse, critical=True)
        await asyncio.wait_for(sup.critical_ended.wait(), 2)
        await sup.stop()
        return sup

    sup = asyncio.run(go())
    assert n["runs"] == 1 and sup.exit_code == 2 and "supervisor.fatal" in types(events)


def test_a_critical_component_that_fails_too_often_ends_the_run_with_exit_code_1():
    async def broken() -> None:
        raise OSError("port in use")

    async def go() -> Supervisor:
        sup = Supervisor(policy=FAST)
        sup.start("transport", broken, critical=True)
        await asyncio.wait_for(sup.critical_ended.wait(), 2)
        await sup.stop()
        return sup

    assert asyncio.run(go()).exit_code == 1


def test_the_rest_keeps_running_and_stop_works_while_a_component_is_failing(events):  # type: ignore[no-untyped-def]
    slow = Policy(max_failures=1000, window_s=60.0, backoff_initial_s=0.01, backoff_max_s=0.02)
    ticks = {"n": 0}

    async def healthy() -> None:
        while True:
            ticks["n"] += 1
            await asyncio.sleep(0.01)

    async def crashing() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("injected, forever")

    async def go() -> tuple[float, dict[str, str]]:
        sup = Supervisor(policy=slow)
        sup.start("healthy", healthy)
        sup.start("crashing", crashing)
        await asyncio.sleep(0.3)
        before = ticks["n"]
        t0 = time.monotonic()
        stuck = await sup.stop(timeout=1.0)
        assert stuck == [] and before > 5
        return time.monotonic() - t0, sup.states()

    took, states = asyncio.run(go())
    assert took < 0.5 and set(states.values()) == {"ended"}
    assert types(events).count("supervisor.crashed") >= 5


def test_stop_returns_even_if_a_component_ignores_cancellation(events):  # type: ignore[no-untyped-def]
    async def stubborn() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(0.5)

    async def go() -> list[str]:
        sup = Supervisor(policy=FAST)
        sup.start("stubborn", stubborn)
        await asyncio.sleep(0.05)
        return await sup.stop(timeout=0.1)

    assert asyncio.run(go()) == ["supervised:stubborn"]


def test_watch_task_reports_a_background_task_that_dies(events):  # type: ignore[no-untyped-def]
    async def go() -> None:
        async def boom() -> None:
            raise KeyError("x")

        t = watch_task(asyncio.create_task(boom()), "link.heartbeat")
        await asyncio.sleep(0.01)
        assert t.done()

    asyncio.run(go())
    e = next(e for e in events if e["type"] == "supervisor.crashed")
    assert e["f"]["component"] == "link.heartbeat" and e["f"]["error"] == "KeyError"


def test_watch_thread_restarts_a_dead_writer_thread_within_bounds(tmp_path: Path):
    log = obs.EventLog()
    w = log.start_files(tmp_path)
    restarts = {"n": 0}

    def restart() -> None:
        restarts["n"] += 1
        log.ensure_writer()

    async def go() -> None:
        w.stop()  # the thread ends: the watchdog must notice and start it again
        w.state = "up"
        task = asyncio.create_task(watch_thread("event-writer", lambda: w.alive, restart, policy=FAST,
                                                period_s=0.01, emit=log.emit))
        await asyncio.sleep(0.1)
        task.cancel()

    asyncio.run(go())
    log.stop_files()
    assert restarts["n"] >= 1 and any(e["type"] == "supervisor.restart" for e in log.ring)


def test_a_stream_producer_that_crashes_is_restarted_and_the_stream_goes_on(events):  # type: ignore[no-untyped-def]
    from brain.body.streams import Subscription, spawn_stream

    runs = {"n": 0}

    async def produce(stream: Subscription[int]) -> None:
        runs["n"] += 1
        stream.push(runs["n"])
        if runs["n"] == 1:
            raise RuntimeError("producer bug")
        await asyncio.sleep(10)

    async def go() -> list[int]:
        s = spawn_stream(4, produce, name="test")
        got = [await asyncio.wait_for(anext(s), 2), await asyncio.wait_for(anext(s), 2)]
        await s.aclose()
        return got

    assert asyncio.run(go()) == [1, 2]
    assert "supervisor.crashed" in types(events) and "supervisor.restart" in types(events)


# ---- per-connection crash barrier ----------------------------------------------------------------------------


def test_a_connection_handler_that_raises_is_contained_and_the_server_keeps_serving(events, monkeypatch):  # type: ignore[no-untyped-def]
    real = BrainServer._connection
    calls = {"n": 0}

    async def failing_once(self: BrainServer, ws: Any, session_id: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected connection fault")
        await real(self, ws, session_id)

    monkeypatch.setattr(BrainServer, "_connection", failing_once)

    async def go() -> None:
        server = await BrainServer("127.0.0.1", 0, token=TOKEN).start()
        url = f"ws://127.0.0.1:{server.port}/"
        try:
            async with ws_connect(url) as ws:
                await asyncio.wait_for(ws.wait_closed(), 3)  # this connection is dropped
            async with body(url) as c:  # the next one works
                assert c.brain_hello is not None
                assert await wait_for(lambda: bool(server.links))
        finally:
            await server.stop()

    asyncio.run(asyncio.wait_for(go(), 20))
    crashed = [e for e in events if e["type"] == "supervisor.crashed"]
    assert crashed and crashed[0]["f"]["component"] == "link.connection" and crashed[0]["session"].startswith("s-")
    assert "link.up" in types(events) and "link.auth" in types(events)


def test_a_failing_link_listener_is_reported_and_the_session_goes_on(events):  # type: ignore[no-untyped-def]
    def bad_listener(link: Any) -> None:
        raise ValueError("listener bug")

    async def go() -> None:
        server = await BrainServer("127.0.0.1", 0, token=TOKEN, on_connect=bad_listener).start()
        try:
            async with body(f"ws://127.0.0.1:{server.port}/") as c:
                assert c.brain_hello is not None
        finally:
            await server.stop()

    asyncio.run(asyncio.wait_for(go(), 20))
    e = next(e for e in events if e["type"] == "supervisor.crashed")
    assert e["f"]["component"] == "link.listener"
    assert [x["f"]["active"] for x in events if x["type"] == "link.down"] == [True]


# ---- the process launcher (serve --supervised) ---------------------------------------------------------------


def _child(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _launch(tmp_path: Path, command: list[str], **kw: Any) -> tuple[int, list[dict[str, Any]], list[str]]:
    got: list[dict[str, Any]] = []
    printed: list[str] = []
    code = run_launcher(command, data_dir=tmp_path, emit=lambda t, m="", **f: got.append({"type": t, **f}),
                        sleep=lambda s: None, out=printed.append, **kw)
    return code, got, printed


def test_the_launcher_policy_is_the_named_limits():
    assert (LAUNCHER_RESTARTS_MAX, LAUNCHER_WINDOW_S, LAUNCHER_BACKOFF_INITIAL_S, LAUNCHER_BACKOFF_MAX_S,
            LAUNCHER_STOP_TIMEOUT_S) == (5, 600.0, 1.0, 60.0, 10.0)
    assert child_command(["serve", "--supervised", "--no-phone-link"])[1:] == ["-m", "brain", "serve", "--no-phone-link"]


def test_a_clean_stop_is_not_restarted(tmp_path: Path):
    code, got, _ = _launch(tmp_path, _child("raise SystemExit(0)"))
    assert code == 0 and [g["type"] for g in got] == ["launcher.start", "launcher.exit"]


def test_a_fail_closed_refusal_is_never_restarted(tmp_path: Path):
    code, got, printed = _launch(tmp_path, _child("raise SystemExit(2)"))
    assert code == 2 and [g["type"] for g in got] == ["launcher.start", "launcher.exit"]
    assert "not restarting" in printed[0]
    assert not (tmp_path / "crash").exists()


def test_an_unexpected_death_is_recorded_and_restarted_with_bounds(tmp_path: Path):
    code, got, printed = _launch(tmp_path, _child("raise SystemExit(7)"), restarts_max=2)
    t = [g["type"] for g in got]
    assert code == 1 and t.count("launcher.start") == 3 and t.count("launcher.restart") == 2
    assert t[-1] == "launcher.gave_up"
    reports = sorted((tmp_path / "crash").glob("child-exit-*.json"))
    assert reports and json.loads(reports[-1].read_text(encoding="utf-8"))["exit_code"] == 7
    delays = [g["delay_s"] for g in got if g["type"] == "launcher.restart"]
    assert delays == [LAUNCHER_BACKOFF_INITIAL_S, 2 * LAUNCHER_BACKOFF_INITIAL_S]


def test_a_child_that_recovers_after_a_crash_ends_the_launcher_cleanly(tmp_path: Path):
    marker = tmp_path / "ran-once"
    code = f"import pathlib, sys\np = pathlib.Path({str(marker)!r})\nif p.exists(): sys.exit(0)\np.write_text('x')\nsys.exit(1)"
    rc, got, _ = _launch(tmp_path, _child(code))
    assert rc == 0 and [g["type"] for g in got].count("launcher.start") == 2


# ---- main(): the outermost barrier, in a child process ---------------------------------------------------------

WRAPPER = textwrap.dedent("""\
    import sys
    sys.path.insert(0, {repo!r})
    from pathlib import Path
    import brain.config as c
    c.DEFAULT_CONFIG = Path(sys.argv[1])
    from brain import __main__ as m
    if sys.argv[2] == "--inject":
        def boom(args, config, argv):
            raise RuntimeError("unexpected failure holding sk-ant-api03-CANARYcanaryCANARYcanary0123")
        m._command = boom
        sys.argv.pop(2)
    raise SystemExit(m.main(sys.argv[2:]))
""")


@pytest.fixture
def sandbox_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"  # relative paths resolve under the folder that holds it: data/ is in tmp_path
    cfg.write_text('[paths]\ndata_dir = "data"\n[body]\nkind = "sim"\n', encoding="utf-8")
    return cfg


def _run_brain(tmp_path: Path, cfg: Path, *argv: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(WRAPPER.format(repo=str(REPO)), encoding="utf-8")
    return subprocess.run([sys.executable, str(wrapper), str(cfg), *argv], capture_output=True, text=True,
                          timeout=timeout, cwd=tmp_path)


def test_an_unexpected_exception_prints_one_line_writes_a_crash_report_and_no_traceback(tmp_path, sandbox_config):  # type: ignore[no-untyped-def]
    r = _run_brain(tmp_path, sandbox_config, "--inject", "info")
    assert r.returncode == 1, r.stdout + r.stderr
    text = r.stdout + r.stderr
    assert "Traceback" not in text and "stopped unexpectedly (RuntimeError)" in r.stderr
    assert len([line for line in r.stderr.splitlines() if line.strip()]) == 1
    reports = list((tmp_path / "data" / "crash").glob("crash-*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["exception"] == "RuntimeError" and report["command"] == "info" and report["frames"]
    assert "CANARY" not in reports[0].read_text(encoding="utf-8")  # the secret-shaped text was redacted


def test_demo_trace_watch_and_health_from_the_command_line(tmp_path, sandbox_config):  # type: ignore[no-untyped-def]
    r = _run_brain(tmp_path, sandbox_config, "demo-trace", "--unavailable")
    assert r.returncode == 0, r.stderr
    assert "turn.start" in r.stdout and "body_link       down" in r.stdout and "Traceback" not in r.stdout + r.stderr
    w = _run_brain(tmp_path, sandbox_config, "watch", "--type", "turn.*")
    assert w.returncode == 0 and w.stdout.count("turn.start") == 4 and w.stdout.count("turn.end") == 4, w.stdout
    h = _run_brain(tmp_path, sandbox_config, "health")
    assert h.returncode == 0 and "camera" in h.stdout and "down" in h.stdout
    state = json.loads((tmp_path / "data" / "state.json").read_text(encoding="utf-8"))
    assert state["starts"] == 1
    bad = _run_brain(tmp_path, sandbox_config, "watch", "--source", str(sandbox_config))
    assert bad.returncode == 2 and "only event files" in bad.stdout


# ---- the acceptance scenario: no link, no camera, no microphone ---------------------------------------------


def test_acceptance_a_scripted_conversation_with_link_camera_and_microphone_unavailable(tmp_path: Path):
    from brain.obs.demo import run_demo
    from brain.obs.health import read_snapshot

    lines: list[str] = []
    result = asyncio.run(run_demo(unavailable=True, data_dir=tmp_path, out=lines.append))
    t = [e["type"] for e in result.events]
    # A complete trace: every turn starts and ends, every tool call has its result or refusal, in order.
    assert t.count("turn.start") == t.count("turn.end") == 4
    assert t.count("tool.call") == t.count("tool.result") + t.count("tool.refused") == 12
    for turn in ("t-1", "t-2", "t-3", "t-4"):
        mine = [e["type"] for e in result.events if e.get("turn") == turn]
        assert mine[0] == "turn.start" and mine[-1] == "turn.end" and "channel.decision" in mine, turn
    # Readable: one line per event, no control characters.
    event_lines = lines[: len(result.events)]
    assert all("\n" not in line and all(ord(c) >= 32 for c in line) for line in event_lines)
    assert all(ord(c) >= 32 for line in "\n".join(lines).splitlines() for c in line)  # the panel too
    # A correct health panel: what is missing says so, with a reason.
    snap, _ = read_snapshot(tmp_path / "health.json")
    assert snap is not None
    for source in ("body_link", "camera", "microphone"):
        assert snap["sources"][source]["state"] == "down" and snap["sources"][source]["reason"], source
    assert snap["sources"]["log_sink"]["state"] == "up"
    # The injected fault was contained: crashed, restarted, and no other crash.
    crashes = [e for e in result.events if e["type"] in ("supervisor.crashed", "tool.failed", "process.crash")]
    assert crashes and all(e["f"].get("component") == "flaky-component" for e in crashes)
    files = list((tmp_path / "logs").glob("events-*.jsonl"))
    assert files and sum(1 for _ in files[0].open(encoding="utf-8")) >= len(result.events)


def test_a_failing_log_sink_never_changes_behaviour(tmp_path: Path):
    """The same scripted session gives the same tool outcomes whether the event files can be written or not."""
    from brain.obs.demo import run_demo

    def outcomes(data_dir: Path) -> list[Any]:
        r = asyncio.run(run_demo(unavailable=True, data_dir=data_dir, out=None, pace=0.3))
        return [e["f"]["outcomes"] for e in r.events if e["type"] == "turn.end"]

    good = outcomes(tmp_path / "good")
    blocked = tmp_path / "blocked"
    blocked.write_text("not a folder", encoding="utf-8")
    assert outcomes(blocked) == good and len(good) == 4


def test_crash_reports_are_bounded_and_hold_no_secret(tmp_path: Path):
    from brain.limits import CRASH_FILES_KEPT
    from brain.obs.crash import exception_details, write_report

    secret = "sk-ant-api03-" + "Q" * 40
    for i in range(CRASH_FILES_KEPT + 5):
        try:
            raise ValueError(f"boom {i} {secret} \x1b[2J")
        except ValueError as e:
            path = write_report(tmp_path, "crash", exception_details(e, "serve"))
        assert path is not None
    reports = list((tmp_path / "crash").glob("*.json"))
    assert len(reports) == CRASH_FILES_KEPT
    text = "".join(p.read_text(encoding="utf-8") for p in reports)
    assert secret not in text and "\x1b" not in text
    blocked = tmp_path / "blocked"
    blocked.write_text("x", encoding="utf-8")
    assert write_report(blocked, "crash", {"command": "serve"}) is None  # never raises
