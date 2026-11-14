"""`python -m brain serve --supervised`: the process-level watchdog (card X-02 step 7).

A small parent process starts `python -m brain serve ...` (the same arguments, without --supervised) as a
child, waits for it, and decides by its exit code:

    0        a clean stop                                  -> the launcher ends too (exit 0)
    2        a deliberate fail-closed refusal to start     -> never restarted (exit 2): restarting would
             (bad config, unsafe token store, self-test)      only repeat the refusal, or worse, wear it down
    other    an unexpected death                           -> recorded (event + crash file), restarted after
             (1: a crash main() caught, or a critical         a backoff, at most LAUNCHER_RESTARTS_MAX times
             component that failed too often; or a kill,      in LAUNCHER_WINDOW_S; then the launcher gives up
             an out-of-memory end, a power-cut restart)

Ctrl+C reaches both processes (same console): the child stops cleanly, the launcher waits for it (up to
LAUNCHER_STOP_TIMEOUT_S, then ends it) and exits 0. Nothing runs after the launcher ends.

This module starts a process, so it is one of the few allowed to import subprocess (tests/test_egress.py).
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..limits import (LAUNCHER_BACKOFF_INITIAL_S, LAUNCHER_BACKOFF_MAX_S, LAUNCHER_RESTARTS_MAX, LAUNCHER_STOP_TIMEOUT_S,
                      LAUNCHER_WINDOW_S)
from .crash import write_report

EXIT_OK = 0
EXIT_REFUSED = 2  # fail closed: config, token store, egress self-test, a security refusal of a component
EXIT_FAILED = 1  # a failure; also what main() returns when it caught something unexpected (crash report written)
WINDOWS_CTRL_C_EXIT = 0xC000013A  # STATUS_CONTROL_C_EXIT: the process was ended by Ctrl+C or Ctrl+Break


def meaning(code: int) -> str:
    if code == EXIT_OK:
        return "clean stop"
    if code == EXIT_REFUSED:
        return "refused to start (fail closed); not restarted"
    if code == EXIT_FAILED:
        return "failed or crashed (exit code 1; see data/crash and data/logs)"
    if code in (WINDOWS_CTRL_C_EXIT, -2):
        return "ended by Ctrl+C"
    if code < 0:
        return f"killed by signal {-code}"
    return f"ended unexpectedly (exit code {code})"


def child_command(argv: Sequence[str]) -> list[str]:
    """The child's command line: this interpreter, `-m brain`, the same arguments without --supervised."""
    return [sys.executable, "-m", "brain", *[a for a in argv if a != "--supervised"]]


def run_launcher(command: Sequence[str], *, data_dir: Path, emit: Callable[..., Any],
                 restarts_max: int = LAUNCHER_RESTARTS_MAX, window_s: float = LAUNCHER_WINDOW_S,
                 backoff_initial_s: float = LAUNCHER_BACKOFF_INITIAL_S,
                 backoff_max_s: float = LAUNCHER_BACKOFF_MAX_S, stop_timeout_s: float = LAUNCHER_STOP_TIMEOUT_S,
                 sleep: Callable[[float], None] = time.sleep, out: Callable[[str], None] = print) -> int:
    """Run `command` under the policy in the module docstring. Returns the launcher's exit code."""
    deaths: deque[float] = deque()
    attempt = 0
    while True:
        attempt += 1
        emit("launcher.start", "starting the brain process", attempt=attempt)
        try:
            proc = subprocess.Popen(list(command))
        except OSError as e:
            emit("launcher.gave_up", "cannot start the brain process", reason=e.strerror or type(e).__name__)
            out(f"launcher: cannot start the brain process ({e.strerror or type(e).__name__})")
            return 1
        try:
            code = proc.wait()
        except KeyboardInterrupt:
            code = _stop_child(proc, stop_timeout_s)
            emit("launcher.exit", "stopped by Ctrl+C", code=code, meaning="stopped by Ctrl+C", pid=proc.pid)
            return EXIT_OK
        what = meaning(code)
        emit("launcher.exit", f"brain process ended: {what}", code=code, meaning=what, pid=proc.pid,
             level="INFO" if code in (EXIT_OK, EXIT_REFUSED) else "WARNING")
        if code in (EXIT_OK, WINDOWS_CTRL_C_EXIT, -2):
            return EXIT_OK
        if code == EXIT_REFUSED:
            out("launcher: the brain refused to start (fail closed); not restarting. See the message above.")
            return EXIT_REFUSED
        report = write_report(data_dir, "child-exit", {"command": "serve", "exit_code": code, "meaning": what,
                                                       "attempt": attempt})
        now = time.monotonic()
        deaths.append(now)
        while deaths and now - deaths[0] > window_s:
            deaths.popleft()
        if len(deaths) > restarts_max:
            emit("launcher.gave_up", f"the brain process died {len(deaths)} times in {window_s:g} s; giving up",
                 deaths=len(deaths), window_s=window_s)
            out(f"launcher: the brain died {len(deaths)} times in {window_s:g} s; giving up"
                + (f" (last report: {report.name})" if report else ""))
            return 1
        delay = float(min(backoff_max_s, backoff_initial_s * 2 ** (len(deaths) - 1)))
        emit("launcher.restart", f"restarting in {delay:g} s", delay_s=delay, deaths=len(deaths))
        out(f"launcher: the brain process {what}; restarting in {delay:g} s"
            + (f" (report: {report.name})" if report else ""))
        try:
            sleep(delay)
        except KeyboardInterrupt:
            return EXIT_OK


def _stop_child(proc: subprocess.Popen[Any], timeout: float) -> int:
    try:
        return proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait(timeout)
    except KeyboardInterrupt:
        proc.kill()
        return proc.wait(timeout)
