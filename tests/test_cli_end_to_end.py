"""Every CLI command in a CHILD PROCESS with hostile argv (X-01 stage 1b, item 2).

For each command (info, serve, probe, pair is left out: it would call adb; body-check --body sim, chaos, and
tools/migrate.py export, verify and restore) the argv is empty, very long, holds control characters, starts
with `-`, repeats a flag, sits at a numeric limit, or is path-like. Every child must: exit with 0, 1 or 2; print
no traceback; finish in bounded time; leave the repository's data/ folder as it was; and open no network
connection outside loopback (the children run under the egress guard; the one that dials a non-loopback host
must be refused before it connects).

What cannot be expressed: a NUL character and non-UTF-16 text cannot be put in a Windows command line at all
(CreateProcess and subprocess refuse them); those are covered in-process by the chaos experiments.

The children read a sandbox config.toml (the wrapper below points brain.config.DEFAULT_CONFIG at it), so the log
files `serve` and `probe` write land in the sandbox's data folder, never in the repository's.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from brain.config import REPO_ROOT
from brain.transport.server import BrainServer

pytestmark = pytest.mark.chaos

TIMEOUT_S = 30.0
WRAPPER = f"""\
import sys
sys.path.insert(0, {str(REPO_ROOT)!r})
from pathlib import Path
import brain.config as c
c.DEFAULT_CONFIG = Path(sys.argv[1])
from brain import __main__ as m
raise SystemExit(m.main(sys.argv[2:]))
"""
MIGRATE_WRAPPER = f"""import getpass, runpy, sys
sys.path.insert(0, {str(REPO_ROOT)!r})
getpass.getpass = lambda prompt="": sys.stdin.readline().rstrip("\\n")  # Windows getpass needs a console; tests feed a pipe
sys.argv = [{str(REPO_ROOT / "tools" / "migrate.py")!r}, *sys.argv[1:]]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
LONG = "x" * 20_000  # under the 32 KiB Windows command-line limit
CONTROL = "a\x01\x07\x1b[2Jb"


@dataclass
class Run:
    name: str
    code: int | None
    out: str
    err: str
    seconds: float


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    out = {}
    for p in root.rglob("*") if root.exists() else []:
        if p.is_file():
            st = p.stat()
            out[str(p)] = (st.st_size, st.st_mtime_ns)
    return out


class Env:
    def __init__(self, sandbox):  # type: ignore[no-untyped-def]
        self.sb = sandbox
        self.script = sandbox.tmp_dir / "run_brain.py"
        self.script.write_text(WRAPPER, encoding="utf-8")
        self.migrate = sandbox.tmp_dir / "run_migrate.py"
        self.migrate.write_text(MIGRATE_WRAPPER, encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items() if not k.upper().endswith("_PROXY")}
        self.env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        self.port = 0

    def brain(self, *argv: str, stdin: str | None = None) -> list[str]:
        return [sys.executable, str(self.script), str(self.sb.config_path), *argv]

    def run(self, name: str, cmd: list[str], stdin: str | None = None) -> Run:
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=TIMEOUT_S, cwd=self.sb.tmp_dir,
                               env=self.env, encoding="utf-8", errors="replace")
            return Run(name, p.returncode, p.stdout, p.stderr, time.monotonic() - t0)
        except subprocess.TimeoutExpired:
            return Run(name, None, "", "timeout", time.monotonic() - t0)


class LoopbackServer:
    """A BrainServer in a thread, with the sandbox's canary token, for `probe` to dial."""

    def __init__(self, token: bytes):
        self.token = token
        self.port = 0
        self.ready = threading.Event()
        self.stop_event: asyncio.Event | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        async def go() -> None:
            self.loop = asyncio.get_running_loop()
            self.stop_event = asyncio.Event()
            server = BrainServer("127.0.0.1", 0, token=self.token)
            await server.start()
            self.port = server.port
            self.ready.set()
            await self.stop_event.wait()
            await server.stop()

        asyncio.run(go())

    def __enter__(self) -> LoopbackServer:
        self.thread.start()
        self.ready.wait(10)
        return self

    def __exit__(self, *exc: object) -> None:
        if self.loop is not None and self.stop_event is not None:
            self.loop.call_soon_threadsafe(self.stop_event.set)
        self.thread.join(10)


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _serve_briefly(env: Env) -> Run:
    """serve on a free loopback port for about 3 s, then a clean stop (CTRL_BREAK): must exit 0."""
    port = _free_port()
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    t0 = time.monotonic()
    p = subprocess.Popen(env.brain("serve", "--no-phone-link", "--port", str(port)), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, cwd=env.sb.tmp_dir, env=env.env, creationflags=flags, encoding="utf-8", errors="replace")
    try:
        time.sleep(3.0)
        alive = p.poll() is None
        if alive:
            p.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        out, err = p.communicate(timeout=20)
        return Run("serve_short_life", p.returncode if alive else -99, out, err, time.monotonic() - t0)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        return Run("serve_short_life", None, out, err + "\ntimeout", time.monotonic() - t0)


def _cases(env: Env, server_port: int) -> dict[str, tuple[list[str], str | None]]:
    b = env.brain
    mig = [sys.executable, str(env.migrate)]
    root = env.sb.root
    cases: dict[str, tuple[list[str], str | None]] = {
        "info": (b("info"), None), "info_default": (b(), None),
        "info_unknown_flag": (b("info", "--bogus"), None), "info_empty_arg": (b("info", ""), None),
        "info_long_arg": (b("info", LONG), None), "info_control_chars": (b("info", CONTROL), None),
        "info_dash_value": (b("info", "-x"), None), "info_repeated": (b("info", "info", "info"), None),
        "unknown_command": (b("frobnicate"), None), "command_empty": (b(""), None), "command_long": (b(LONG), None),
        "command_pathlike": (b("..\\..\\Windows\\System32"), None), "help": (b("--help"), None),
        "serve_help": (b("serve", "--help"), None),
        "serve_port_zero": (b("serve", "--port", "0"), None), "serve_port_over": (b("serve", "--port", "65536"), None),
        "serve_port_negative": (b("serve", "--port", "-1"), None), "serve_port_text": (b("serve", "--port", "abc"), None),
        "serve_port_empty": (b("serve", "--port", ""), None), "serve_port_float": (b("serve", "--port", "1e3"), None),
        "serve_port_huge": (b("serve", "--port", "9" * 5000), None), "serve_port_missing": (b("serve", "--port"), None),
        "serve_host_nonloopback_refused": (b("serve", "--host", "0.0.0.0", "--no-phone-link"), None),
        "serve_host_name_refused": (b("serve", "--host", "example.org", "--no-phone-link"), None),
        "serve_host_dash": (b("serve", "--host", "-x"), None), "serve_unknown_flag": (b("serve", "--shell"), None),
        "serve_host_long": (b("serve", "--host", LONG, "--no-phone-link"), None),
        "probe_count_zero": (b("probe", "--count", "0"), None), "probe_count_over": (b("probe", "--count", "10001"), None),
        "probe_count_text": (b("probe", "--count", "x"), None), "probe_bytes_negative": (b("probe", "--bytes", "-1"), None),
        "probe_bytes_over": (b("probe", "--bytes", "1048577"), None), "probe_port_zero": (b("probe", "--port", "0"), None),
        "probe_host_outside_refused": (b("probe", "--host", "example.org", "--port", "9", "--count", "1", "--bytes", "1"), None),
        "probe_host_ip_outside_refused": (b("probe", "--host", "192.0.2.1", "--port", "9", "--count", "1", "--bytes", "1"), None),
        "probe_port_closed": (b("probe", "--port", str(_free_port()), "--count", "1", "--bytes", "1"), None),
        "probe_ok_minimum": (b("probe", "--port", str(server_port), "--count", "1", "--bytes", "0"), None),
        "probe_ok_small": (b("probe", "--port", str(server_port), "--count", "5", "--bytes", "100"), None),
        "probe_repeated_port": (b("probe", "--port", "1", "--port", str(server_port), "--count", "1", "--bytes", "1"), None),
        "probe_host_long": (b("probe", "--host", LONG, "--count", "1", "--bytes", "1"), None),
        "body_check_sim": (b("body-check", "--body", "sim", "--wait", "0"), None),
        "body_check_wait_nan": (b("body-check", "--body", "sim", "--wait", "nan"), None),
        "body_check_wait_negative": (b("body-check", "--body", "sim", "--wait", "-1"), None),
        "body_check_wait_huge": (b("body-check", "--body", "sim", "--wait", "1e9"), None),
        "body_check_wait_empty": (b("body-check", "--body", "sim", "--wait", ""), None),
        "body_check_body_bogus": (b("body-check", "--body", "toaster"), None),
        "body_check_body_empty": (b("body-check", "--body", ""), None),
        "body_check_repeated": (b("body-check", "--body", "sim", "--body", "sim", "--wait", "0", "--wait", "0"), None),
        "chaos_check_manifest": (b("chaos", "--check-manifest"), None), "chaos_unknown_flag": (b("chaos", "--nope"), None),
        "chaos_seed_bad": (b("chaos", "--seed", "-1"), None), "chaos_seed_text": (b("chaos", "--seed", LONG), None),
        "chaos_only_control": (b("chaos", "--list", "--only", CONTROL), None), "chaos_help": (b("chaos", "--help"), None),
        "chaos_both_modes": (b("chaos", "--quick", "--full"), None),
        "chaos_scan_logs_missing": (b("chaos", "--scan-logs", str(root / "no-such-logs")), None),
        "chaos_manifest_stub": (b("chaos", "--manifest-stub"), None),
        "migrate_help": (mig + ["--help"], None), "migrate_no_command": (mig, None),
        "migrate_unknown": (mig + ["explode"], None), "migrate_verify_missing_arg": (mig + ["verify"], None),
        "migrate_verify_no_such_file": (mig + ["verify", str(root / "nothing.eamig")], "a-long-passphrase-1\n"),
        "migrate_verify_empty_file": (mig + ["verify", str(root / "empty.eamig")], "a-long-passphrase-1\n"),
        "migrate_verify_garbage_file": (mig + ["verify", str(root / "garbage.eamig")], "a-long-passphrase-1\n"),
        "migrate_verify_stdin_closed": (mig + ["verify", str(root / "garbage.eamig")], ""),
        "migrate_verify_pathlike": (mig + ["verify", "..\\..\\..\\Windows\\win.ini"], "x\n"),
        "migrate_verify_long": (mig + ["verify", LONG], "x\n"),
        "migrate_export_short_passphrase": (mig + ["export", str(root / "short.eamig"), "--root", str(root)], "abc\nabc\n"),
        "migrate_export_mismatch": (mig + ["export", str(root / "mm.eamig"), "--root", str(root)], "a-long-passphrase-1\nother-phrase-22\n"),
        "migrate_export_ok": (mig + ["export", str(root / "ok.eamig"), "--root", str(root)], "a-long-passphrase-1\na-long-passphrase-1\n"),
        "migrate_export_into_missing_dir": (mig + ["export", str(root / "nodir" / "x" / "y.eamig"), "--root", str(root)],
                                            "a-long-passphrase-1\na-long-passphrase-1\n"),
        "migrate_export_root_missing": (mig + ["export", str(root / "r.eamig"), "--root", str(root / "no-root")],
                                        "a-long-passphrase-1\na-long-passphrase-1\n"),
        "migrate_export_repeated_force": (mig + ["export", str(root / "ok.eamig"), "--force", "--force", "--root", str(root)],
                                          "a-long-passphrase-1\na-long-passphrase-1\n"),
        "migrate_restore_no_dest": (mig + ["restore", str(root / "ok.eamig")], None),
        "migrate_restore_wrong_passphrase": (mig + ["restore", str(root / "ok.eamig"), "--dest", str(root / "dest1")], "wrong-passphrase-zz\n"),
        "migrate_restore_dest_is_file": (mig + ["restore", str(root / "ok.eamig"), "--dest", str(root / "garbage.eamig")],
                                         "a-long-passphrase-1\n"),
        "migrate_restore_dash_dest": (mig + ["restore", str(root / "ok.eamig"), "--dest", "-x"], "a-long-passphrase-1\n"),
        "migrate_verify_ok": (mig + ["verify", str(root / "ok.eamig")], "a-long-passphrase-1\n"),
    }
    return cases


@pytest.fixture(scope="module")
def runs(chaos_sandbox) -> Iterator[dict[str, Run]]:  # type: ignore[no-untyped-def]
    env = Env(chaos_sandbox)
    root = chaos_sandbox.root
    (root / "empty.eamig").write_bytes(b"")
    (root / "garbage.eamig").write_bytes(os.urandom(300))
    before = _snapshot(REPO_ROOT / "data")
    results: dict[str, Run] = {}
    server_port = 0
    with LoopbackServer(chaos_sandbox.token) as server:
        server_port = server.port
        cases = _cases(env, server.port)
        order = list(cases)
        # export must run before restore and verify use its archive: run the migrate chain serially first
        serial = [n for n in order if n.startswith("migrate_export_ok")]
        for n in serial:
            results[n] = env.run(n, *cases[n][:1], stdin=cases[n][1])
        # probes that reach the server replace one another (one body at a time), so they run one after the other
        probes = [n for n in order if n.startswith("probe_ok") or n == "probe_repeated_port"]
        rest = [n for n in order if n not in results and n not in probes
                and n not in ("migrate_restore_wrong_passphrase", "migrate_verify_ok")]
        with ThreadPoolExecutor(max_workers=6) as pool:
            for r in pool.map(lambda n: env.run(n, cases[n][0], cases[n][1]), rest):
                results[r.name] = r
        for n in probes + ["migrate_restore_wrong_passphrase", "migrate_verify_ok"]:
            results[n] = env.run(n, cases[n][0], cases[n][1])
        results["serve_short_life"] = _serve_briefly(env)
    # Another session may be using the real data/ folder at the same time, so a plain before/after comparison would
    # blame our children for its files. A child's file is recognised by what it mentions: the sandbox folder or the
    # loopback port of this test's server.
    after = _snapshot(REPO_ROOT / "data")
    mine = []
    for name in sorted(set(after) - set(before) | {k for k in before if before.get(k) != after.get(k)}):
        try:
            text = Path(name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if str(chaos_sandbox.root) in text or f"127.0.0.1:{server_port}" in text:
            mine.append(name)
    results["__data_changed__"] = Run("data", 0, "", "" if not mine else "a child wrote into the real data folder: " + ", ".join(mine[:5]), 0)
    yield results


def test_every_child_exits_cleanly_with_no_traceback_in_bounded_time(runs: dict[str, Run]) -> None:
    bad = []
    for name, r in runs.items():
        if name == "__data_changed__":
            continue
        text = r.out + r.err
        if r.code not in (0, 1, 2):
            bad.append(f"{name}: exit {r.code}")
        elif "Traceback (most recent call last)" in text:
            bad.append(f"{name}: traceback: {text[text.index('Traceback'):][:300]!r}")
        elif r.seconds > TIMEOUT_S:
            bad.append(f"{name}: {r.seconds:.0f} s")
    assert not bad, "\n".join(bad)


def test_the_repositorys_data_folder_is_untouched(runs: dict[str, Run]) -> None:
    assert runs["__data_changed__"].err == ""


@pytest.mark.parametrize("name, code", [
    ("info", 0), ("info_default", 0), ("serve_port_zero", 2), ("serve_port_over", 2), ("serve_port_text", 2),
    ("serve_host_nonloopback_refused", 2), ("serve_short_life", 0), ("probe_count_zero", 2), ("probe_bytes_over", 2),
    ("probe_ok_minimum", 0), ("probe_ok_small", 0), ("probe_port_closed", 1), ("body_check_sim", 0),
    ("body_check_wait_nan", 2), ("body_check_body_bogus", 2), ("chaos_unknown_flag", 2), ("chaos_both_modes", 2),
    ("migrate_export_ok", 0), ("migrate_verify_ok", 0), ("migrate_export_mismatch", 1), ("migrate_verify_garbage_file", 1),
    ("migrate_restore_wrong_passphrase", 1), ("unknown_command", 2), ("migrate_unknown", 2),
])
def test_the_expected_exit_code(runs: dict[str, Run], name: str, code: int) -> None:
    r = runs[name]
    assert r.code == code, f"{name}: exit {r.code}\nstdout: {r.out[-400:]}\nstderr: {r.err[-400:]}"


def test_refusals_print_a_usage_or_an_error_line(runs: dict[str, Run]) -> None:
    for name in ("serve_port_zero", "probe_count_zero", "body_check_wait_nan", "chaos_unknown_flag", "unknown_command"):
        assert "usage" in runs[name].err.lower() or "error" in runs[name].err.lower(), name


def test_a_dial_outside_loopback_is_refused_before_any_connection(runs: dict[str, Run]) -> None:
    for name in ("probe_host_outside_refused", "probe_host_ip_outside_refused"):
        r = runs[name]
        assert r.code in (1, 2), name
        assert "refus" in (r.out + r.err).lower() or "egress" in (r.out + r.err).lower() or "not allowed" in (r.out + r.err).lower(), \
            f"{name}: {(r.out + r.err)[-300:]!r}"


def test_no_secret_in_any_output(runs: dict[str, Run], chaos_sandbox) -> None:  # type: ignore[no-untyped-def]
    secrets_ = [v for v in chaos_sandbox.canaries.values() if v]
    for name, r in runs.items():
        for s in secrets_:
            assert s not in r.out + r.err, f"{name} printed a canary secret"


@pytest.mark.xfail(strict=True, reason="X01B-09: probe --host with a 20,000-character name ends in the crash barrier (UnicodeError from "
                   "the DNS label encoder, not caught by probe); it should be refused with a clear message and exit code 2")
def test_probe_with_an_absurd_host_name_is_refused_cleanly_not_by_the_crash_barrier(runs: dict[str, Run]) -> None:
    r = runs["probe_host_long"]
    assert r.code == 2 and "stopped unexpectedly" not in r.out + r.err and "UnicodeEncodeError" not in r.out + r.err
