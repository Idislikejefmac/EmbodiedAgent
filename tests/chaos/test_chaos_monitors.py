"""Every monitor proven to fire on a planted violation, and to stay quiet on clean code (plan card X-01
step 3; T-X01-030 to T-X01-034, T-X01-043). Each planted violation runs as a real chaos case through
`runner.run_case`, so the proof covers the path every experiment uses."""

import asyncio
import logging
import socket
import subprocess
import threading
import tracemalloc

import pytest

from brain import egress
from brain.config import REPO_ROOT
from tools.chaos import log_hygiene, monitors, runner, sandbox
from tools.chaos.api import Case, CaseClass, Experiment, Violation, failure, invalid, not_applicable, valid

pytestmark = pytest.mark.chaos
log = logging.getLogger("brain.selftest")


def run_one(sb, check, *, case=None, allow_processes=()):
    case = case or valid("toy", None)
    exp = Experiment("X-01.selftest.toy", "X-01", ("tool.speak",), "a toy experiment for the monitor self-tests",
                     "sandbox", {CaseClass.VALID: lambda g: [case], CaseClass.INVALID: lambda g: [],
                                 CaseClass.HOSTILE: not_applicable("monitor self-test only"),
                                 CaseClass.FAILURE: not_applicable("monitor self-test only")}, check,
                     frozenset(allow_processes))
    policy = runner._policy(sb, exp, False, False)
    return runner.run_case(exp, case, "X-01.selftest.toy/valid/0", 1, sb, "quick", policy)


def invariants(result):
    return {inv for inv, _ in result.violations}


def test_clean_code_raises_no_alarm(chaos_sandbox):
    def clean(case, ctx):
        (ctx.sandbox.fresh_dir("ok") / "f.txt").write_text("fine", encoding="utf-8")
        log.info("an ordinary line")
        return {"ok": True}

    r = run_one(chaos_sandbox, clean)
    assert r.status == "pass", r.violations


@pytest.mark.parametrize("plant, invariant", [
    ("write_outside", "no_forbidden_side_effect"),
    ("read_real_data", "no_forbidden_side_effect"),
    ("read_secrets_directly", "no_forbidden_side_effect"),
    ("start_process", "no_forbidden_side_effect"),
    ("connect_non_loopback", "no_forbidden_side_effect"),
    ("import_forbidden", "no_forbidden_side_effect"),
    ("bind_wildcard", "no_forbidden_side_effect"),
])
def test_side_effect_audit_fires(chaos_sandbox, monkeypatch, tmp_path, plant, invariant):
    outside = REPO_ROOT / "data" / "chaos-selftest-must-not-exist.txt"

    def bad(case, ctx):
        if plant == "write_outside":
            (tmp_path / "x.txt").write_text("x")  # tmp_path is outside the sandbox
        elif plant == "read_real_data":
            outside.read_text()
        elif plant == "read_secrets_directly":
            (ctx.sandbox.secrets_dir / "link_token").read_text()
        elif plant == "start_process":
            subprocess.run(["cmd", "/c", "echo", "x"], capture_output=True)
        elif plant == "connect_non_loopback":
            monkeypatch.setattr(egress, "_check_address", lambda *a, **k: None)  # as if the guard had a gap
            ctx.monitors.check_destination(socket.AF_INET, ("192.0.2.1", 9))
        elif plant == "import_forbidden":
            import ctypes  # noqa: F401  (from project code: a test file under the repository)
        elif plant == "bind_wildcard":
            with socket.socket() as s:
                s.bind(("0.0.0.0", 0))
        return "done"

    r = run_one(chaos_sandbox, bad)
    assert r.status == "fail" and invariant in invariants(r), r.violations
    assert not outside.exists() and not (tmp_path / "x.txt").exists()  # refused, not just reported


def test_the_token_store_may_read_the_secrets_folder(chaos_sandbox):
    from brain import __main__ as cli

    def through_store(case, ctx):
        token, problem = cli.load_token(ctx.sandbox.config)
        ctx.expect(token == ctx.sandbox.token, "fail_closed", problem)
        return "ok"

    assert run_one(chaos_sandbox, through_store).status == "pass"


def test_a_process_the_experiment_allows_is_not_an_alarm(chaos_sandbox):
    def icacls(case, ctx):
        subprocess.run([r"C:\Windows\System32\icacls.exe", str(ctx.sandbox.tmp_dir)], capture_output=True)
        return "ok"

    assert run_one(chaos_sandbox, icacls, allow_processes=("icacls.exe",)).status == "pass"


@pytest.mark.parametrize("plant", ["raise", "task_never_retrieved", "thread_dies", "system_exit", "logged_traceback",
                                   "masked_by_isolation_point"])
def test_crash_detector_fires(chaos_sandbox, plant):
    async def bad(case, ctx):
        if plant == "raise":
            return {}["odd input"]
        if plant == "task_never_retrieved":
            async def die():
                raise KeyError("silent")
            t = asyncio.get_running_loop().create_task(die())
            await asyncio.sleep(0.01)
            del t
            import gc
            gc.collect()
        if plant == "thread_dies":
            th = threading.Thread(target=lambda: 1 / 0)
            th.start()
            th.join()
        if plant == "system_exit":
            raise SystemExit(3)
        if plant == "logged_traceback":
            try:
                1 / 0
            except ZeroDivisionError:
                log.exception("something broke")
        if plant == "masked_by_isolation_point":
            logging.getLogger("brain.agent.tools").error("tool registry failed handling 'x'")
        return "done"

    r = run_one(chaos_sandbox, bad)
    assert r.status == "fail" and "no_crash" in invariants(r), (plant, r.violations)


def test_an_injected_fault_may_be_logged_by_its_isolation_point(chaos_sandbox):
    def isolated(case, ctx):
        try:
            raise RuntimeError("injected")
        except RuntimeError:
            log.exception("handler failed (expected)")
        return "ok"

    r = run_one(chaos_sandbox, isolated, case=failure("injected", None, "N14", isolated_fault=True))
    assert r.status == "pass", r.violations


@pytest.mark.parametrize("plant", ["long_line", "many_lines", "queue", "hang", "memory", "leaked_tasks"])
def test_resource_monitor_fires(chaos_sandbox, plant):
    async def bad(case, ctx):
        if plant == "long_line":
            log.info("keys %r", list(range(3000)))
        if plant == "many_lines":
            for i in range(200):
                log.info("line %d", i)
        if plant == "queue":
            ctx.monitors.observe_queue("planted", 300, 256)
        if plant == "hang":
            await asyncio.sleep(30)
        if plant == "memory":
            blob = [bytearray(1024 * 1024) for _ in range(300)]  # 300 MiB held at once
            del blob
        if plant == "leaked_tasks":
            for _ in range(40):
                asyncio.get_running_loop().create_task(asyncio.sleep(5))
        return "done"

    case = valid("toy", None, timeout_s=0.5) if plant == "hang" else None
    started = not tracemalloc.is_tracing()
    if plant == "memory" and started:
        tracemalloc.start(1)
    try:
        r = run_one(chaos_sandbox, bad, case=case)
    finally:
        if plant == "memory" and started:
            tracemalloc.stop()
    assert "bounded_resources" in invariants(r), (plant, r.violations)
    assert r.status in ("fail", "timeout")


@pytest.mark.parametrize("where", ["log", "exception", "output", "traceback"])
def test_secret_scan_fires_on_a_canary(chaos_sandbox, where):
    def leak(case, ctx):
        secret = ctx.sandbox.canaries["ANTHROPIC_API_KEY"]
        if where == "log":
            log.info("using key %s", secret)
        if where == "exception":
            raise ValueError(f"bad key {secret}")
        if where == "traceback":
            try:
                raise ValueError(f"bad key {secret}")
            except ValueError:
                log.warning("failed", exc_info=True)
        return {"echo": secret}

    r = run_one(chaos_sandbox, leak)
    assert "no_secret_in_output" in invariants(r), (where, r.violations)
    assert chaos_sandbox.canaries["ANTHROPIC_API_KEY"] not in repr(r)  # the report masks it


def test_log_forgery_monitor_fires(chaos_sandbox):
    def forge(case, ctx):
        log.info("peer said %s", "ok\nINFO brain: forged line")
        return "done"

    assert "validate_before_use" in invariants(run_one(chaos_sandbox, forge))


def test_structured_event_sink_is_checked(chaos_sandbox):
    """The hook for card X-02's structured event log: events go through the same hygiene rules."""
    def events(case, ctx):
        monitors.structured_sink({"event": "ok", "n": 1})
        monitors.structured_sink({"event": "leak", "token": ctx.sandbox.token_hex})
        monitors.structured_sink({"event": "forged\nline"})
        monitors.structured_sink({"event": "big", "x": "y" * 5000})
        return "done"

    found = invariants(run_one(chaos_sandbox, events))
    assert {"no_secret_in_output", "validate_before_use", "bounded_resources"} <= found
    assert monitors.active() is None
    monitors.structured_sink({"outside": "a case"})  # does nothing outside a case


def test_the_chaos_monitors_fixture_fails_a_test_that_leaks(chaos_monitors, chaos_sandbox):
    """The reusable fixture: violations inside a test body are collected; here one is planted and then
    cleared so this test passes while proving the fixture saw it."""
    log.info("token %s", chaos_sandbox.token_hex)
    log_hygiene_kinds = [f.kind for f in log_hygiene.text_findings(chaos_monitors.logs.records[-1].message, "r",
                                                                    canaries=chaos_sandbox.canaries)]
    assert any(k.startswith("secret_canary") for k in log_hygiene_kinds)
    chaos_monitors.logs.records.clear()


def test_log_hygiene_scans_text_log_files(tmp_path, chaos_sandbox):
    good = tmp_path / "serve-1.log"
    good.write_text("2026-11-13 10:00:00,000 INFO brain.serve: started\n", encoding="utf-8")
    bad = tmp_path / "serve-2.log"
    lines = ["2026-11-13 10:00:00,000 INFO x: " + "k" * 3000,
             "2026-11-13 10:00:01,000 INFO x: key sk-ant-api03-abcdefghijklmnop",
             "2026-11-13 10:00:02,000 INFO x: bell \x07",
             "Traceback (most recent call last):"]
    lines += ["2026-11-13 10:01:%02d,000 INFO x: tick" % (i % 60) for i in range(80)]
    bad.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert log_hygiene.scan_file(good) == []
    kinds = log_hygiene.summarise(log_hygiene.scan_file(bad))
    assert {"over_long", "secret_api_key", "control_char", "traceback", "flood"} <= set(kinds), kinds
    n, findings = log_hygiene.scan_dir(tmp_path)
    assert n == 2 and findings


def test_monitors_are_off_between_cases(chaos_sandbox, tmp_path):
    run_one(chaos_sandbox, lambda case, ctx: "ok")
    (tmp_path / "after.txt").write_text("writes outside a case are not the harness's business")
    assert monitors.active() is None


def test_sandbox_never_points_at_real_data(chaos_sandbox):
    roots = [str(p).lower() for p in sandbox.protected_roots()]
    assert not any(str(chaos_sandbox.root).lower().startswith(r) for r in roots)
    assert chaos_sandbox.config.egress.allowed_hosts == ()  # the typed Config since H-05
