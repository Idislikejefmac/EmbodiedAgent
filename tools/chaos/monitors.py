"""Invariant monitors: every chaos case runs under all of them (plan card X-01 step 3).

- **No crash**: the check raising anything but `Violation`, an exception an event loop could not
  deliver ("Task exception was never retrieved"), an exception in another thread, an unraisable
  exception, `SystemExit`. Masked crashes too: a log record from a declared isolation point
  (`MASKED_CRASH_MARKERS`) means something below it raised.
- **No forbidden side effect** (`sys.addaudithook`, plus a wrapper of `builtins.__import__` and of
  asyncio's Windows proactor connect, which raises no audit event): a write, delete, rename or
  directory change outside the sandbox; any read of the real data folder, the real `.env` or the real
  secrets folder; a read of any secrets folder except through the token store (`brain/transport/auth.py`);
  a process start not allowed by the experiment; a socket to anything but a loopback port this process
  itself listens on; a bind to a non-loopback address; a name lookup; a forbidden import from project
  code. Forbidden effects are recorded AND refused (a `ChaosBlocked` error is raised into the code that
  tried), so a misbehaving case cannot damage real data while it is being reported.
- **Bounded resources**: wall time (the runner's timeout), Python heap peak (`tracemalloc`), log
  record size (`LOG_RECORD_MAX_BYTES`), log lines per case (`PEER_LOG_LINES_PER_MIN_MAX`), queue depth
  (`observe_queue`), leaked tasks.
- **No secret in output**: canary secrets planted in the sandbox (token store, `.env`, environment)
  must never appear in a log record, an exception, a case's output or the results file.
- **Log forging**: a log message carrying a raw control character (a forged line).

Known blind spots (R-FND-35): effects made outside Python's audit points (a C extension's own
networking or file I/O, `ctypes` calls, child processes' own effects); abandoned threads from an
earlier case that act during a later one are attributed to the later one.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
import os
import re
import sys
import threading
import tracemalloc
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any

from brain import egress
from brain.config import REPO_ROOT
from brain.limits import LOG_RECORD_MAX_BYTES, PEER_LOG_LINES_PER_MIN_MAX

from . import log_hygiene
from . import thresholds as T
from .api import Violation

PROCESS_EVENTS = frozenset({"subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn",
                            "os.startfile", "os.fork", "os.forkpty", "pty.spawn", "webbrowser.open"})
PATH_EVENTS = {  # event -> indexes of the path arguments it changes
    "os.remove": (0,), "os.rmdir": (0,), "os.mkdir": (0,), "shutil.rmtree": (0,), "os.rename": (0, 1),
    "os.chmod": (0,), "os.truncate": (0,), "os.link": (0, 1), "os.symlink": (0, 1), "shutil.copyfile": (1,),
    "shutil.copytree": (1,), "shutil.move": (1,), "os.utime": (0,), "os.chown": (0,),
}
NET_EVENTS = frozenset({"socket.connect", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo",
                        "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo", "socket.bind"})
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

# Modules project code may not import while a case runs (beyond the files the egress lint allows).
FORBIDDEN_IMPORTS = frozenset({
    "subprocess", "multiprocessing", "ctypes", "_ctypes", "pty", "pexpect", "cffi", "_posixsubprocess",
    "pickle", "marshal", "shelve", "webbrowser", "anthropic", "openai", "requests", "httpx", "urllib3", "aiohttp",
    "ftplib", "smtplib", "telnetlib", "paramiko", "socketserver", "_overlapped", "_winapi", "winreg",
})
IMPORT_ALLOWED_FILES = {  # repo-relative file -> modules it may import (mirrors tests/test_egress.py ALLOWED)
    "brain/egress.py": None,  # None: anything
    "brain/transport/auth.py": {"subprocess", "ctypes"},
    "brain/body/phone.py": {"subprocess"},
}
TOKEN_STORE_FILE = "brain/transport/auth.py"  # the only code allowed to read a secrets folder
HARNESS_DIR = "tools/chaos/"

# Log records that mean an isolation point caught an exception from below (a crash, masked).
MASKED_CRASH_MARKERS = (
    "tool registry failed handling",  # brain/agent/tools.py ToolRegistry.call catch-all
    "handler failed on",  # brain/transport/server.py _dispatch_loop
    "link listener failed",  # brain/transport/server.py _notify
)
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")  # tab is tolerated; CR, LF and the rest are not


class ChaosBlocked(PermissionError):
    """The harness refused a forbidden side effect while a case was running."""


def _rel(path: str) -> str:
    try:
        return Path(path).resolve().relative_to(REPO_ROOT).as_posix()
    except (ValueError, OSError):
        return ""


def _norm(p: object) -> str | None:
    if isinstance(p, int) or p is None:
        return None
    try:
        return os.path.normcase(os.path.abspath(os.fsdecode(p)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


@dataclass(frozen=True)
class SideEffectPolicy:
    """What a case may touch. Built by the runner from the sandbox and the experiment."""

    write_roots: tuple[str, ...]
    secret_roots: tuple[str, ...]  # readable only through the token store
    protected_roots: tuple[str, ...]  # never touched (real data, real .env, real secrets)
    allow_processes: frozenset[str] = frozenset()
    extra_local_ports: frozenset[int] = frozenset()

    @classmethod
    def make(cls, write_roots: Iterable[Path | str], secret_roots: Iterable[Path | str] = (),
             protected_roots: Iterable[Path | str] = (), allow_processes: Iterable[str] = (),
             extra_local_ports: Iterable[int] = ()) -> SideEffectPolicy:
        def n(items: Iterable[Path | str]) -> tuple[str, ...]:
            return tuple(x for x in (_norm(i) for i in items) if x)

        return cls(n(write_roots), n(secret_roots), n(protected_roots),
                   frozenset(p.lower() for p in allow_processes), frozenset(extra_local_ports))


@dataclass
class Record:
    level: int
    logger: str
    message: str
    exc_text: str


class LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.records: list[Record] = []
        self.count = 0
        self.format_errors = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1
        if len(self.records) >= T.CASE_LOG_RECORDS_HARD_MAX:
            return
        try:
            message = record.getMessage()
        except Exception:  # broad-except: a record that cannot be formatted is itself a finding
            self.format_errors += 1
            message = f"<unformattable record from {record.name}>"
        exc_text = ""
        if record.exc_info:
            exc_text = logging.Formatter().formatException(record.exc_info)
        self.records.append(Record(record.levelno, record.name, message, exc_text))


@dataclass
class CaseMonitors:
    """The monitors for one case. `start()` opens the window, `finish()` closes it and returns the
    violations found (the check's own Violation is added by the runner)."""

    policy: SideEffectPolicy
    canaries: dict[str, str] = field(default_factory=dict)  # label -> secret text
    max_log_lines: int = PEER_LOG_LINES_PER_MIN_MAX
    allow_isolated_traceback: bool = False  # the case injected the fault an isolation point reports
    structured_count: int = 0
    violations: list[Violation] = field(default_factory=list)
    logs: LogCapture = field(default_factory=LogCapture)
    effects: list[str] = field(default_factory=list)  # every forbidden effect seen, as text
    _mem_start: int = 0
    _saved_level: int = logging.WARNING
    _saved_import: Any = None
    _saved_hooks: tuple[Any, Any] = (None, None)

    # ---- window ----

    def start(self) -> None:
        _install_hooks_once()
        root = logging.getLogger()
        self._saved_level = root.level
        root.setLevel(logging.INFO)
        root.addHandler(self.logs)
        self._saved_hooks = (threading.excepthook, sys.unraisablehook)
        threading.excepthook = self._thread_exception
        sys.unraisablehook = self._unraisable
        if tracemalloc.is_tracing():
            tracemalloc.reset_peak()
            self._mem_start = tracemalloc.get_traced_memory()[0]
        global _ACTIVE
        _ACTIVE = self

    def finish(self) -> list[Violation]:
        global _ACTIVE
        _ACTIVE = None
        root = logging.getLogger()
        root.removeHandler(self.logs)
        root.setLevel(self._saved_level)
        threading.excepthook, sys.unraisablehook = self._saved_hooks
        if tracemalloc.is_tracing():
            peak = tracemalloc.get_traced_memory()[1] - self._mem_start
            if peak > T.CASE_MEMORY_PEAK_MAX_BYTES:
                self.violation("bounded_resources", f"Python heap grew by {peak} bytes in one case "
                                                    f"(limit {T.CASE_MEMORY_PEAK_MAX_BYTES})")
        self._check_logs()
        return self.violations

    # ---- recording ----

    def violation(self, invariant: str, message: str) -> None:
        if len(self.violations) < 50:
            self.violations.append(Violation(invariant, message[:500]))

    def forbidden(self, message: str, *, block: bool = True) -> None:
        self.effects.append(message)
        self.violation("no_forbidden_side_effect", message)
        if block:
            raise ChaosBlocked(f"chaos harness refused: {message}")

    def messages(self) -> list[str]:
        return [r.message for r in self.logs.records]

    def observe_queue(self, name: str, depth: int, bound: int) -> None:
        """For checks: report a queue's depth; more than `bound` is a resource violation."""
        if depth > bound:
            self.violation("bounded_resources", f"queue {name} holds {depth} items (bound {bound})")

    def loop_exception(self, loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        self.violation("no_crash", f"event loop: {context.get('message', '')}: "
                                   f"{type(exc).__name__ if exc else ''}: {str(exc)[:200] if exc else ''}")

    def _thread_exception(self, args: threading.ExceptHookArgs) -> None:
        self.violation("no_crash", f"exception in thread {getattr(args.thread, 'name', '?')}: "
                                   f"{args.exc_type.__name__}: {str(args.exc_value)[:200]}")

    def _unraisable(self, u: Any) -> None:
        self.violation("no_crash", f"unraisable exception: {type(u.exc_value).__name__}: {str(u.exc_value)[:200]}")

    # ---- checks run at the end of the window ----

    def _check_logs(self) -> None:
        recs = self.logs.records
        if self.logs.count > self.max_log_lines:
            self.violation("bounded_resources", f"{self.logs.count} log records in one case "
                                                f"(limit {self.max_log_lines}, PEER_LOG_LINES_PER_MIN_MAX)")
        if self.logs.format_errors:
            self.violation("no_crash", f"{self.logs.format_errors} log record(s) could not be formatted")
        seen: set[str] = set()
        for i, r in enumerate(recs):
            findings = log_hygiene.text_findings(r.message, f"record {i} ({r.logger})", canaries=self.canaries)
            findings += log_hygiene.text_findings(r.exc_text, f"record {i} traceback", canaries=self.canaries,
                                                  max_bytes=1 << 30, allow_newlines=True) if r.exc_text else []
            for f in findings:
                kind = f.kind.split(":")[0]
                if kind in seen:
                    continue
                seen.add(kind)
                if kind.startswith("secret"):
                    self.violation("no_secret_in_output", f"{f}: a secret appeared in the log")
                elif kind == "control_char":
                    self.violation("validate_before_use", f"log record from {r.logger} carries a raw control "
                                                          f"character (a forged line): {r.message[:120]!r}")
                elif kind == "over_long":
                    size = len(r.message.encode("utf-8", "replace"))
                    self.violation("bounded_resources", f"log record of {size} bytes from {r.logger} "
                                                        f"(LOG_RECORD_MAX_BYTES {LOG_RECORD_MAX_BYTES}): "
                                                        f"{r.message[:120]!r}...")
        for r in recs:
            last = r.exc_text.strip().splitlines()[-1][:200] if r.exc_text else ""
            if any(r.message.startswith(m) or m in r.message[:80] for m in MASKED_CRASH_MARKERS):
                self.violation("no_crash", f"an isolation point caught a crash: {r.message[:160]!r}: {last}")
                break
            if r.exc_text and not self.allow_isolated_traceback:
                self.violation("no_crash", f"a traceback was logged by {r.logger}: {r.message[:120]!r}: {last}")
                break

    # ---- structured events (card X-02's live event log will feed these through `structured_sink`) ----

    def on_structured_event(self, event: Any) -> None:
        self.structured_count += 1
        for f in log_hygiene.check_event(event, f"event {self.structured_count}", self.canaries)[:1]:
            kind = f.kind.split(":")[0]
            invariant = ("no_secret_in_output" if kind.startswith("secret") else "bounded_resources"
                         if kind == "over_long" else "validate_before_use")
            self.violation(invariant, f"structured event: {f}")
        if self.structured_count == self.max_log_lines + 1:
            self.violation("bounded_resources", f"more than {self.max_log_lines} structured events in one case")

    # ---- audit events ----

    def on_event(self, event: str, args: tuple[Any, ...]) -> None:
        if event == "open":
            self._open(args)
        elif event in PATH_EVENTS:
            for i in PATH_EVENTS[event]:
                if i < len(args):
                    self._change(event, args[i])
        elif event in PROCESS_EVENTS:
            self._process(event, args)
        elif event in NET_EVENTS:
            self._net(event, args)

    def _open(self, args: tuple[Any, ...]) -> None:
        path = _norm(args[0]) if args else None
        if path is None:
            return
        mode, flags = (args[1] if len(args) > 1 else None), (args[2] if len(args) > 2 else 0)
        write = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            not isinstance(mode, str) and isinstance(flags, int) and bool(flags & _WRITE_FLAGS))
        if any(_within(path, r) for r in self.policy.protected_roots):
            self.forbidden(f"opened real data {path!r} ({'write' if write else 'read'})")
        if any(_within(path, r) for r in self.policy.secret_roots) and not _called_from(TOKEN_STORE_FILE):
            self.forbidden(f"read the secrets folder outside the token store: {path!r}")
        if write and not any(_within(path, r) for r in self.policy.write_roots):
            self.forbidden(f"opened {path!r} for writing outside the sandbox")

    def _change(self, event: str, raw: object) -> None:
        path = _norm(raw)
        if path is None:
            return
        if any(_within(path, r) for r in self.policy.protected_roots) or not any(
                _within(path, r) for r in self.policy.write_roots):
            self.forbidden(f"{event} on {path!r} outside the sandbox")

    def _process(self, event: str, args: tuple[Any, ...]) -> None:
        name = ""
        if event == "subprocess.Popen" and args:
            exe, argv = args[0], args[1] if len(args) > 1 else None
            first = exe or (argv if isinstance(argv, (str, bytes)) else (argv[0] if argv else ""))
            text = os.fsdecode(first).lower()[:400] if first else ""
            name = text[:80]
            for allowed in self.policy.allow_processes:  # the program itself, not an argument that names it
                if re.match(r'^\s*"?(?:[^"\s]*[\\/])?' + re.escape(allowed) + r'(?:"|\s|$)', text):
                    return
        self.forbidden(f"{event} started a process {name!r}".rstrip())

    def _net(self, event: str, args: tuple[Any, ...]) -> None:
        if event in ("socket.getaddrinfo", "socket.gethostbyname"):
            host = args[0] if args else None
            if host in (None, "", b"") or egress.is_local(host) or egress._as_ip(egress._clean(host)) is not None:
                return
            if egress._policy is not None and not egress._policy.allows_name(host):
                return  # the guard refuses this lookup before it is made (whichever hook runs first)
            self.forbidden(f"name lookup of {egress._clean(host)!r}")
        elif event in ("socket.gethostbyaddr", "socket.getnameinfo"):
            target = args[0] if args else None
            host = target[0] if isinstance(target, tuple) and target else target
            if host is not None and egress.is_local(host):
                return
            if egress._policy is not None:
                return  # the guard refuses every reverse lookup of a non-local address
            self.forbidden(f"reverse lookup of {str(host)[:60]!r}")
        elif event == "socket.bind":
            address = args[1] if len(args) > 1 else None
            if isinstance(address, tuple) and address and not egress.is_local(address[0]):
                self.forbidden(f"bound a socket to non-loopback address {address[0]!r}")
        else:  # connect, sendto, sendmsg
            sock, address = (args[0], args[1]) if len(args) > 1 else (None, None)
            if address is None:
                return
            self.check_destination(getattr(sock, "family", None), address)

    def check_destination(self, family: object, address: object) -> None:
        if not isinstance(address, tuple) or len(address) < 2:
            return  # a local socket file or an unconnected send
        policy = egress._policy
        if policy is not None:  # whatever the guard refuses never leaves; not this monitor's finding
            saved = getattr(egress._quiet, "on", False)
            egress._quiet.on = True
            try:
                egress._check_address(policy, family, address)  # type: ignore[arg-type]
            except egress.EgressDenied:
                return
            finally:
                egress._quiet.on = saved
        host, port = address[0], address[1]
        if egress.is_local(host):
            own = egress._own_ports()
            local = policy.local_ports if policy is not None else frozenset()
            if port in own or port in local or port in self.policy.extra_local_ports:
                return
            self.forbidden(f"connection to loopback port {port}, which this process does not listen on "
                           "(a local proxy could forward it anywhere)")
        else:
            self.forbidden(f"connection to non-loopback {egress._clean(host)!r} port {port}")

    def on_import(self, name: str, importer: str) -> None:
        top = name.split(".")[0]
        if top not in FORBIDDEN_IMPORTS:
            return
        rel = _rel(importer)
        if not rel or rel.startswith(HARNESS_DIR):
            return  # the standard library, a dependency, or the harness itself
        allowed = IMPORT_ALLOWED_FILES.get(rel, set())
        if allowed is None or top in allowed:
            return
        self.forbidden(f"{rel} imported {top}", block=False)


def scan_secrets(text: str, canaries: dict[str, str]) -> list[str]:
    """Labels of the canary secrets that occur in `text` (case-insensitive for hex)."""
    if not text or not canaries:
        return []
    low = text.lower()
    return sorted(label for label, secret in canaries.items() if secret and secret.lower() in low)


def _called_from(rel_file: str) -> bool:
    suffix = os.path.normcase(os.path.join(*rel_file.split("/")))
    f: FrameType | None = sys._getframe(2)
    for _ in range(60):
        if f is None:
            return False
        if os.path.normcase(f.f_code.co_filename).endswith(suffix):
            return True
        f = f.f_back
    return False


_THIS_FILE = os.path.normcase(os.path.abspath(__file__))


def _importer_file() -> str:
    f: FrameType | None = sys._getframe(2)
    for _ in range(60):
        if f is None:
            return ""
        name = f.f_code.co_filename
        if not name.startswith("<frozen") and "importlib" not in name and os.path.normcase(name) != _THIS_FILE:
            return name
        f = f.f_back
    return ""


# ---- the process-wide hooks (installed once, active only while a case runs) ----------------------------

_ACTIVE: CaseMonitors | None = None
_installed = False
_inside = threading.local()
_real_import = builtins.__import__


def active() -> CaseMonitors | None:
    return _ACTIVE


def structured_sink(event: Any) -> None:
    """The hook for structured events (card X-02): the live event log calls its sinks with each event
    (a JSON-able mapping); register this function as a sink and every chaos case checks the stream for
    secrets, control characters, size and flooding. Outside a case it does nothing."""
    mon = _ACTIVE
    if mon is not None:
        mon.on_structured_event(event)


def _hook(event: str, args: tuple[Any, ...]) -> None:
    mon = _ACTIVE
    if mon is None or getattr(_inside, "on", False):
        return
    if event not in PATH_EVENTS and event not in PROCESS_EVENTS and event not in NET_EVENTS and event != "open":
        return
    _inside.on = True
    try:
        mon.on_event(event, args)
    finally:
        _inside.on = False


def _import(name: str, globals: Any = None, locals: Any = None, fromlist: Any = (), level: int = 0) -> Any:
    mon = _ACTIVE
    if mon is not None and level == 0 and not getattr(_inside, "on", False):
        _inside.on = True
        try:
            mon.on_import(name, _importer_file())
            for sub in fromlist or ():
                if isinstance(sub, str):
                    mon.on_import(f"{name}.{sub}" if name else sub, _importer_file())
        finally:
            _inside.on = False
    return _real_import(name, globals, locals, fromlist, level)


def _install_hooks_once() -> None:
    global _installed
    if _installed:
        return
    sys.addaudithook(_hook)
    builtins.__import__ = _import
    if sys.platform == "win32":
        from asyncio import windows_events

        proactor: Any = windows_events.IocpProactor
        real_connect = proactor.connect

        def connect(self: Any, conn: Any, address: Any) -> Any:
            mon = _ACTIVE
            if mon is not None and not getattr(_inside, "on", False):
                _inside.on = True
                try:
                    mon.check_destination(conn.family, address)
                finally:
                    _inside.on = False
            return real_connect(self, conn, address)

        proactor.connect = connect
    _installed = True
