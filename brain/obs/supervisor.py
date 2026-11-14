"""The crash barrier (card X-02 step 7): no long-lived task or thread of the brain dies silently.

- `run_with_restarts(name, factory)` runs one component (a coroutine factory). An unhandled exception is
  caught, reported as `supervisor.crashed` (type and where, never values) and logged with its traceback in
  the text log; the component restarts after a backoff that doubles from SUPERVISOR_BACKOFF_INITIAL_S up to
  SUPERVISOR_BACKOFF_MAX_S. After SUPERVISOR_RESTARTS_MAX failures within SUPERVISOR_WINDOW_S it is stopped
  and reported (`supervisor.gave_up`), never restarted forever. A component that raises
  `FatalComponentError` (a security refusal at start) is never retried and never falls back to a weaker
  mode (`supervisor.fatal`). Cancellation is never swallowed: shutdown always works, also while a component
  is failing or waiting out its backoff.
- `Supervisor` holds the components of one brain run (`serve`, `demo-trace`) and stops them all.
- `watch_task(task, name)` reports a background task that ends with an exception (instead of "Task
  exception was never retrieved" at some later garbage collection). It does not restart it; the owner does.
- `watch_thread(...)` restarts a thread that died (the event writer), with the same bounds.

What this cannot do (docs/observability.md): survive an operating-system kill, a power cut, an
out-of-memory kill of the whole process or an interpreter defect. For those, `serve --supervised`
(brain/obs/launcher.py) restarts the process, and data/state.json keeps what must survive.
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..limits import (SUPERVISOR_BACKOFF_INITIAL_S, SUPERVISOR_BACKOFF_MAX_S, SUPERVISOR_RESTARTS_MAX,
                      SUPERVISOR_STOP_TIMEOUT_S, SUPERVISOR_WINDOW_S)

log = logging.getLogger("brain.obs.supervisor")

Factory = Callable[[], Awaitable[None]]
Emit = Callable[..., Any]


class FatalComponentError(Exception):
    """A component refuses to run for a security reason (fail closed). Never retried, never weakened."""


@dataclass(frozen=True)
class Policy:
    max_failures: int = SUPERVISOR_RESTARTS_MAX
    window_s: float = SUPERVISOR_WINDOW_S
    backoff_initial_s: float = SUPERVISOR_BACKOFF_INITIAL_S
    backoff_max_s: float = SUPERVISOR_BACKOFF_MAX_S

    def backoff(self, failures: int) -> float:
        return float(min(self.backoff_max_s, self.backoff_initial_s * 2 ** max(0, failures - 1)))


DEFAULT_POLICY = Policy()


def where(exc: BaseException) -> str:
    """`file.py:123 in func` of the innermost frame: where it broke, with no values. Never raises."""
    try:
        frames = traceback.extract_tb(exc.__traceback__)
        if not frames:
            return "?"
        f = frames[-1]
        return f"{f.filename.replace(chr(92), '/').rsplit('/', 1)[-1]}:{f.lineno} in {f.name}"
    except Exception:  # broad-except: describing a broken traceback must not raise
        return "?"


def _default_emit() -> Emit:
    from . import emit

    return emit


async def run_with_restarts(name: str, factory: Factory, *, policy: Policy = DEFAULT_POLICY,
                            emit: Emit | None = None, restart_on_return: bool = False, quiet: bool = False,
                            sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> str:
    """Run `factory()` until it returns ("done"), cannot be retried ("fatal"), or failed too often
    ("failed"). With `restart_on_return`, returning counts as a failure (a server that should run
    forever). `quiet` (short-lived components such as a stream producer) reports only failures and
    restarts, not the first start or a normal end. CancelledError always propagates."""
    em = emit or _default_emit()
    failures: deque[float] = deque()
    attempt = 0
    while True:
        attempt += 1
        if not quiet or attempt > 1:
            em("supervisor.started", f"{name} started", component=name, attempt=attempt)
        problem: str | None = None
        try:
            await factory()
        except asyncio.CancelledError:
            if not quiet:
                em("supervisor.stopped", f"{name} cancelled", component=name, how="cancelled")
            raise
        except FatalComponentError as e:
            em("supervisor.fatal", f"{name} refused to run; not retried", component=name,
               reason=str(e)[:200])
            log.error("component %s refused to run (fail closed): %s", name, e)
            return "fatal"
        except Exception as e:  # broad-except: the crash barrier; reported with context, then restarted or stopped
            problem = type(e).__name__
            em("supervisor.crashed", f"{name} raised {problem}", component=name, error=problem, where=where(e),
               attempt=attempt)
            log.error("component %s failed (%s at %s); contained", name, problem, where(e), exc_info=e)
        else:
            if not restart_on_return:
                if not quiet:
                    em("supervisor.stopped", f"{name} finished", component=name, how="done")
                return "done"
            me = asyncio.current_task()
            if me is not None and me.cancelling():  # it swallowed our cancellation: stopping, not a failure
                em("supervisor.stopped", f"{name} ended after a stop request", component=name, how="cancelled")
                raise asyncio.CancelledError
            problem = "returned"
            em("supervisor.crashed", f"{name} returned when it should keep running", component=name,
               error="returned", where="-", attempt=attempt)
        now = time.monotonic()
        failures.append(now)
        while failures and now - failures[0] > policy.window_s:
            failures.popleft()
        if len(failures) >= policy.max_failures:
            em("supervisor.gave_up", f"{name} failed {len(failures)} times in {policy.window_s:g} s; stopped",
               component=name, failures=len(failures), window_s=policy.window_s, last_error=problem)
            log.error("component %s failed %d times in %g s; stopped (not restarted)", name, len(failures),
                      policy.window_s)
            return "failed"
        delay = policy.backoff(len(failures))
        em("supervisor.restart", f"{name} restarts in {delay:g} s", component=name, attempt=attempt + 1,
           delay_s=delay, failures=len(failures))
        await sleep(delay)


class Supervisor:
    """The supervised components of one brain run. `critical_ended` is set when a critical component can
    no longer run: it refused for a security reason (`exit_code` 2, fail closed) or failed too often
    (`exit_code` 1, so `serve --supervised` restarts the whole process within its own bounds). Other
    components that give up are stopped and reported while the rest keeps running."""

    def __init__(self, *, policy: Policy = DEFAULT_POLICY, emit: Emit | None = None):
        self.policy = policy
        self._emit = emit
        self.tasks: dict[str, asyncio.Task[str]] = {}
        self.results: dict[str, str] = {}
        self.critical_ended = asyncio.Event()
        self.exit_code = 0
        self.reason = ""

    def start(self, name: str, factory: Factory, *, critical: bool = False, restart_on_return: bool = True,
              policy: Policy | None = None) -> asyncio.Task[str]:
        if name in self.tasks and not self.tasks[name].done():
            raise ValueError(f"component {name!r} is already running")

        async def runner() -> str:
            result = await run_with_restarts(name, factory, policy=policy or self.policy, emit=self._emit,
                                             restart_on_return=restart_on_return)
            self.results[name] = result
            if critical and result in ("fatal", "failed"):
                self.exit_code = 2 if result == "fatal" else 1
                self.reason = f"{name} {'refused to run (fail closed)' if result == 'fatal' else 'failed too often'}"
                self.critical_ended.set()
            return result

        task = asyncio.create_task(runner(), name=f"supervised:{name}")
        self.tasks[name] = task
        return task

    def states(self) -> dict[str, str]:
        return {n: (self.results.get(n, "ended") if t.done() else "running") for n, t in self.tasks.items()}

    async def stop(self, timeout: float = SUPERVISOR_STOP_TIMEOUT_S) -> list[str]:
        """Cancel every component and wait up to `timeout` for them. Returns the names that did not end
        in time (they are abandoned, and reported). Never raises (except the caller's own cancellation)."""
        pending = [t for t in self.tasks.values() if not t.done()]
        for t in pending:
            t.cancel()
        if not pending:
            return []
        done, still = await asyncio.wait(pending, timeout=timeout)
        for t in done:
            if not t.cancelled() and t.exception() is not None:  # retrieve it; run_with_restarts already reported
                log.error("supervised task %s ended with %s", t.get_name(), type(t.exception()).__name__)
        stuck = sorted(t.get_name() for t in still)
        if stuck:
            em = self._emit or _default_emit()
            em("supervisor.gave_up", "components did not stop in time; abandoned", component="shutdown",
               stuck=stuck)
        return stuck


def watch_task(task: asyncio.Task[Any], name: str, emit: Emit | None = None) -> asyncio.Task[Any]:
    """Report `task` if it ends with an exception (and retrieve that exception, so asyncio does not log a
    traceback later). Cancellation and a normal end are silent."""

    def done(t: asyncio.Task[Any]) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            (emit or _default_emit())("supervisor.crashed", f"{name} ended with {type(exc).__name__}",
                                      component=name, error=type(exc).__name__, where=where(exc))
            log.error("background task %s failed (%s at %s)", name, type(exc).__name__, where(exc))

    task.add_done_callback(done)
    return task


async def watch_thread(name: str, alive: Callable[[], bool], restart: Callable[[], None], *,
                       policy: Policy = DEFAULT_POLICY, period_s: float = 1.0, emit: Emit | None = None) -> None:
    """Check a thread every `period_s`; restart it when it has died, within the policy's bounds. Returns
    (and reports) when it died too often."""
    em = emit or _default_emit()
    deaths: deque[float] = deque()
    while True:
        await asyncio.sleep(period_s)
        if alive():
            continue
        now = time.monotonic()
        deaths.append(now)
        while deaths and now - deaths[0] > policy.window_s:
            deaths.popleft()
        em("supervisor.crashed", f"thread {name} died", component=name, error="thread_died", where="-")
        if len(deaths) >= policy.max_failures:
            em("supervisor.gave_up", f"thread {name} died {len(deaths)} times; not restarted", component=name,
               failures=len(deaths))
            return
        restart()
        em("supervisor.restart", f"thread {name} restarted", component=name, attempt=len(deaths) + 1, delay_s=0)
