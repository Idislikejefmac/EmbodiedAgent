"""python -m brain [info|serve|probe|pair|body-check|chaos|watch|health|demo-trace].

info (default): print the version, load config, and report which keys exist.
serve: run the protocol v0 WebSocket server (and keep the phone link up unless --no-phone-link), under the
    crash barrier (brain/obs/supervisor.py), writing events to data/logs/events-*.jsonl, the health panel
    to data/health.json and its state to data/state.json. --supervised runs it under the process launcher,
    which restarts it if it dies unexpectedly (never after a fail-closed refusal, exit code 2).
watch: show the event files (live with --follow), filtered; --health shows the health panel live.
health: print the health panel (data/health.json), saying STALE if the brain stopped writing it.
demo-trace: a scripted conversation against SimBody that shows a complete trace with no hardware.
probe: connect as a test body and run the echo test against a running server.
pair: give the phone body this brain's link token (debug builds of the body only).
body-check: start a body (--body sim or phone), call every agent tool through the registry, and try the
    command-boundary requests. With --body sim it needs no phone and no network.
chaos: the chaos and boundary test harness (tools/chaos/README.md): --quick, --full, --seed N.

Every command runs under the egress guard (brain/egress.py): only the destinations in config.toml
[egress] can be reached, plus loopback ports this process listens on. `probe` may also dial the
loopback port it is given; `probe --host <another machine>` is refused unless that host and port are
in [egress].
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import logging.handlers
import math
import signal
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any

from . import __version__, obs
from .config import Config, ConfigError, as_config, key_presence, load_config
from .config import REPO_ROOT as REPO_ROOT  # re-exported for the harness and tests
from .limits import (BODY_CHECK_WAIT_MAX_S, ECHO_TEST_MAX_BYTES, ECHO_TEST_MAX_COUNT, LOG_FILE_BACKUPS,
                     LOG_FILE_MAX_BYTES, PORT_MAX, PORT_MIN)
from .logsafe import CapRecordSize
from .obs.events import COMPONENTS, LEVELS

if TYPE_CHECKING:
    from .body.base import Body

AnyConfig = Config | Mapping[str, Any] | None  # a checked Config, or a raw table that goes through the gate
EXIT_REFUSED = 2  # a deliberate fail-closed refusal (bad config, unsafe token store, a security self-test)
EXIT_CRASH = 1  # something unexpected reached main(); a crash report was written (1: "failed", as tools expect)
_run: dict[str, Any] = {"data_dir": None, "command": "?"}  # what the outermost crash barrier needs to know


def info() -> int:
    print(f"embodied-agent brain {__version__}")
    config = as_config(load_config())
    print(f"config: ok (host {config.network.host}, port {config.network.port})")
    print(f"data dir: {data_dir(config)}")  # read-only: info never creates it
    print(f"secrets dir (link token): {secrets_dir(config)}")
    for name, present in key_presence().items():
        print(f"key {name}: {'present' if present else 'missing'}")
    return 0


def data_dir(config: AnyConfig) -> Path:
    """config [paths] data_dir, confined to the project by the config gate (H-05)."""
    return as_config(config).paths.data_dir


def secrets_dir(config: AnyConfig) -> Path:
    """Where the link token lives: config [paths] secrets_dir, else a private folder in the user profile."""
    from .transport.auth import default_secrets_dir

    override = as_config(config).paths.secrets_dir
    return override if override is not None else default_secrets_dir()


def load_token(config: AnyConfig, *, rotate: bool = False) -> tuple[bytes | None, str]:
    """(token, "") or (None, a message saying what is wrong and what to do). Never raises."""
    from .transport.auth import TokenStoreError, load_or_create_token

    try:
        cfg = as_config(config)
    except ConfigError as e:
        return None, str(e)
    d = data_dir(cfg)
    try:
        return load_or_create_token(secrets_dir(cfg), rotate=rotate,
                                    legacy=(d / "link_token", d / "secrets" / "link_token")), ""
    except TokenStoreError as e:
        return None, f"link token store is not safe: {e}"
    except OSError as e:
        return None, f"could not read or create the link token in {secrets_dir(cfg)}: {e}"


class NoTracebackFormatter(logging.Formatter):
    """The console shows one line per record: a contained failure's traceback goes to the log file only."""

    def format(self, record: logging.LogRecord) -> str:
        saved = record.exc_info, record.exc_text
        record.exc_info, record.exc_text = None, None
        try:
            text = super().format(record)
        finally:
            record.exc_info, record.exc_text = saved
        return text + (" (traceback in the log file)" if saved[0] else "")


def setup_logging(config: AnyConfig, name: str, verbose: bool = False) -> str:
    log_dir = data_dir(config) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{name}-{datetime.now():%Y%m%d-%H%M%S}.log"
    fmt_text = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    fmt = logging.Formatter(fmt_text)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    file_handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=LOG_FILE_MAX_BYTES,
                                                        backupCount=LOG_FILE_BACKUPS, encoding="utf-8")
    console = logging.StreamHandler(sys.stdout)
    for h, f in ((console, NoTracebackFormatter(fmt_text)), (file_handler, fmt)):
        h.setFormatter(f)
        h.addFilter(CapRecordSize())  # no record over LOG_RECORD_MAX_BYTES (H-08)
        root.addHandler(h)
    logging.getLogger("websockets").setLevel(logging.INFO)
    return str(log_file)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def _serve(args: argparse.Namespace, token: bytes, log: logging.Logger, rt: Any = None) -> int:
    """Run the server and its helpers as supervised components until a stop signal, or until a critical
    component can no longer run. Returns the exit code (0, or 1 / 2 from the supervisor)."""
    from .obs.supervisor import FatalComponentError, Supervisor
    from .transport.server import BrainServer

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def on_signal(signum: int, frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(stop.set)

    previous = {}
    for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                previous[sig] = signal.signal(sig, on_signal)
            except (ValueError, OSError):  # swallowed: no handler for this signal here (not the main thread, or Windows)
                pass

    async def transport() -> None:
        server = BrainServer(args.host, args.port, token=token)
        try:
            server.check_library()
        except RuntimeError as e:  # a security precondition: never run with a weaker frame limit
            raise FatalComponentError(str(e)) from None
        try:
            await server.start()
            await asyncio.Future()  # runs until cancelled
        finally:
            await server.stop()

    sup = Supervisor()
    try:
        sup.start("transport", transport, critical=True)
        if args.phone_link:
            sup.start("phone-link", lambda: _keep_phone_link(args.port, log))
        if rt is not None:
            rt.supervise(sup)
        waiters = {asyncio.create_task(stop.wait()), asyncio.create_task(sup.critical_ended.wait())}
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
        if sup.critical_ended.is_set():
            log.error("stopping: %s", sup.reason)
        else:
            log.info("stop requested; closing connections")
    finally:
        await sup.stop()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return sup.exit_code if sup.critical_ended.is_set() else 0


async def _keep_phone_link(port: int, log: logging.Logger) -> None:
    """Re-run the phone's reverse port forward every minute, so a dropped phone link heals by itself."""
    from .body import phone  # the wiring point may name the phone body; transport stays agnostic

    await phone.keep_link(port, log)  # every ADB_LINK_RECHECK_S (brain/limits.py)


def serve(args: argparse.Namespace, config: Config, argv: list[str] | None = None) -> int:
    if getattr(args, "supervised", False):
        return serve_supervised(config, argv or [])
    log_file = setup_logging(config, "serve", args.verbose)
    logging.raiseExceptions = False  # a log file on a failing disk must not print tracebacks to the console
    log = logging.getLogger("brain.serve")
    log.info("embodied-agent brain %s; log file %s", __version__, log_file)
    if not is_loopback(args.host):
        if not args.allow_remote:
            log.error("refusing to listen on %s: only loopback is allowed without --allow-remote", args.host)
            obs.emit("startup.refused", "non-loopback listen address without --allow-remote", reason="not_loopback")
            return EXIT_REFUSED
        log.warning("!!! LISTENING ON NON-LOOPBACK ADDRESS %s over unencrypted ws://. The handshake proves the "
                    "endpoints once but adds no per-message integrity: anyone on the network path can read, "
                    "inject into or take over the session after it. Use only on a network you control. !!!",
                    args.host)
    token, problem = load_token(config)
    if token is None:
        log.error("%s", problem)
        obs.emit("startup.refused", "the link token store is not usable; not starting", reason="token_store")
        return EXIT_REFUSED
    from .obs.runtime import ObsRuntime
    from .transport.server import new_session_id

    rt = ObsRuntime(data_dir(config), log=obs.LOG, board=obs.HEALTH, session_id="serve-" + new_session_id()[2:])
    rt.start()
    try:
        code = asyncio.run(_serve(args, token, log, rt))
    finally:
        rt.stop()
    log.info("stopped (exit code %d)", code)
    return code


def serve_supervised(config: Config, argv: list[str]) -> int:
    """`serve --supervised`: the launcher process (brain/obs/launcher.py) runs `serve` as a child."""
    from .obs.launcher import child_command, run_launcher

    d = data_dir(config)
    obs.LOG.start_files(d / "logs")
    try:
        return run_launcher(child_command(argv), data_dir=d, emit=obs.emit)
    finally:
        obs.LOG.stop_files()


def watch(args: argparse.Namespace, config: Config) -> int:
    """`watch`: the event files (or, with --health, the health panel), once or live."""
    from .obs.events import LEVELS
    from .obs.viewer import Filter, health_view, is_event_file, parse_since
    from .obs.viewer import watch as watch_events

    d = data_dir(config)
    if args.health:
        try:
            return health_view(d / "health.json", follow=True)
        except KeyboardInterrupt:
            return 0
    source = Path(args.source) if args.source else d / "logs"
    if args.source and source.is_file() and not is_event_file(source):
        print("watch reads only event files (events-*.jsonl) or a folder of them")
        return 2
    try:
        since = parse_since(args.since) if args.since else None
    except ValueError as e:
        print(e)
        return 2
    flt = Filter(components=frozenset(args.component or ()), min_level=LEVELS[args.level],
                 types=tuple(args.type or ()), session=args.session, turn=args.turn, since=since)
    try:
        counts = watch_events(source, flt, detail=args.detail, follow=args.follow)
    except KeyboardInterrupt:
        return 0
    except ValueError as e:
        print(e)
        return 2
    if not args.follow:
        print(f"-- {counts.shown} event(s) shown from {len(counts.files)} file(s)"
              + (f"; {counts.skipped_bad} bad line(s) skipped" if counts.skipped_bad else ""))
    return 0


def health(args: argparse.Namespace, config: Config) -> int:
    from .obs.viewer import health_view

    return health_view(data_dir(config) / "health.json")


def demo_trace(args: argparse.Namespace, config: Config) -> int:
    from .obs.demo import run_demo

    logging.basicConfig(level=logging.CRITICAL, stream=sys.stderr)  # the event trace is the output
    result = asyncio.run(run_demo(unavailable=args.unavailable, data_dir=None if args.no_files else data_dir(config),
                                  detail=args.detail))
    print(f"-- demo-trace: {len(result.events)} events, {result.turns} turns"
          + ("" if args.no_files else f"; replay with `python -m brain watch`, panel with `python -m brain health`"))
    return 0


def probe(args: argparse.Namespace, config: Config) -> int:
    from .transport.auth import AuthError
    from .transport.client import TestBodyClient

    setup_logging(config, "probe")
    log = logging.getLogger("brain.probe")
    log.warning("probe connects as a body: the brain serves one body at a time, so a connected body is "
                "replaced (close 4001) and stops until Connect is pressed again")
    token, problem = load_token(config)
    if token is None:
        log.error("%s", problem)
        return 2

    async def go() -> bool:
        url = f"ws://{args.host}:{args.port}/"
        async with TestBodyClient(url, token=token) as c:
            hello = c.brain_hello or {}
            log.info("connected to %s session %s", url, hello.get("payload", {}).get("session_id"))
            res = await c.echo_test(args.count, args.bytes)
            log.info("echo result: %s ok=%s", res, res.ok)
            return bool(res.ok)

    try:
        ok = asyncio.run(go())
    except (OSError, AuthError) as e:
        log.error("probe failed: %s", e)
        obs.emit("probe.result", "probe could not run", ok=False, error=type(e).__name__)
        return 1
    obs.emit("probe.result", "echo probe finished", ok=ok, count=args.count, bytes_each=args.bytes)
    return 0 if ok else 1


def pair(args: argparse.Namespace, config: Config) -> int:
    from .body import phone

    token, problem = load_token(config, rotate=args.rotate)
    if token is None:
        print(problem)
        return 2
    ok, message = phone.pair(token)
    obs.emit("pair.result", "pairing finished", ok=ok, rotated=bool(args.rotate))
    print(message)
    if args.rotate:
        print("token rotated: restart `python -m brain serve` so it uses the new token")
    return 0 if ok else 1


BOUNDARY_REQUESTS: tuple[tuple[str, dict[str, Any]], ...] = (  # what the app channel must never get (plan: "Command boundary"); each is refused
    ("delete the C drive", {}),
    ("delete_files", {"path": "C:\\"}),
    ("read config.toml", {}),
    ("read_file", {"path": "config.toml"}),
    ("list the documents folder", {}),
    ("what is in the delete bin?", {}),
    ("run this command", {}),
    ("run_command", {"command": "whoami"}),
)


async def _body_check(body: Body, wait_s: float) -> int:
    """Drive a body through the tool registry exactly as the agent loop will. Prints one line per call."""
    from .agent.body_tools import register_body_tools
    from .agent.tools import APP, CONSOLE, ToolRegistry

    registry = ToolRegistry()
    register_body_tools(registry, body)
    failures = 0
    async with body:
        print(f"body: {body.name}; waiting up to {wait_s:g} s for it to connect")
        if not await body.wait_connected(wait_s):
            print("body: NOT CONNECTED (status is still asked for, and must say so)")
        print(f"body: capabilities {sorted(c.value for c in body.capabilities)}; clock {body.clock_sync().to_json()}")
        calls: tuple[tuple[str, dict[str, Any]], ...] = (("get_body_status", {}), ("get_pose", {}), ("capture_image", {"reason": "body check"}),
                 ("speak", {"text": "Body check."}), ("stop", {}), ("listen", {}), ("who_is_present", {}),
                 ("navigate_to", {"waypoint": "kitchen"}))
        for name, args in calls:
            r = await registry.call(name, args, CONSOLE)
            shown = r.content if r.ok else f"{r.message}{f' ({r.detail})' if r.detail else ''}"
            print(f"tool {name}: {r.status}/{r.code} {shown}")
            failures += r.status in ("error", "refused") or (name == "get_body_status" and not r.ok)
        for name, args in BOUNDARY_REQUESTS:
            r = await registry.call(name, args, APP)
            print(f"boundary {name!r}: {r.status}/{r.code}")
            failures += r.status != "refused"
    print(f"phone body module loaded: {'brain.body.phone' in sys.modules}")
    print(f"body-check: {'ok' if not failures else f'{failures} problem(s)'}")
    return 0 if not failures else 1


def body_check(args: argparse.Namespace, config: Config) -> int:
    from .body import create_body

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    kind = args.body or config.body.kind
    token = None
    if kind == "phone":
        token, problem = load_token(config)
        if token is None:
            print(problem)
            return 2
    try:
        body = create_body(kind, config, token=token)
    except ValueError as e:
        print(e)
        return 2
    return asyncio.run(_body_check(body, args.wait))


def chaos(argv: list[str]) -> int:
    """The chaos harness (tools/chaos, card X-01). It runs under a stricter guard than the brain itself:
    loopback only, no [egress] destination at all, installed before the harness is even imported."""
    from .egress import EgressPolicy, install_guard

    install_guard(EgressPolicy())
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))  # the harness lives in tools/, outside the brain package
    from tools.chaos.runner import main as chaos_main

    return chaos_main(argv)


def _int_in_range(low: int, high: int, what: str) -> Callable[[str], int]:
    """argparse type: a whole number from low to high, else a clean `error:` line and exit code 2."""
    def convert(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{what} must be a whole number, not {text[:40]!r}") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"{what} must be from {low} to {high}, not {text[:40]}")
        return value
    return convert


def _float_in_range(low: float, high: float, what: str) -> Callable[[str], float]:
    """argparse type: a finite number from low to high (NaN, infinity and out-of-range values are refused)."""
    def convert(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{what} must be a number, not {text[:40]!r}") from None
        if not math.isfinite(value) or not low <= value <= high:
            raise argparse.ArgumentTypeError(f"{what} must be a finite number from {low:g} to {high:g}, "
                                             f"not {text[:40]}")
        return value
    return convert


def build_parser(config: AnyConfig) -> argparse.ArgumentParser:
    net = as_config(config).network
    port_type = _int_in_range(PORT_MIN, PORT_MAX, "a port")
    parser = argparse.ArgumentParser(prog="python -m brain")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("info", help="version, config and key presence (default)")
    for name, helptext in (("serve", "run the protocol v0 WebSocket server"),
                           ("probe", "run the echo test as a test body against a running server "
                                     "(replaces a connected body, which then stops)")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--host", default=net.host)
        sp.add_argument("--port", type=port_type, default=net.port,
                        help=f"TCP port, {PORT_MIN} to {PORT_MAX} (well-known ports are not refused)")
    pp = sub.add_parser("pair", help="send this brain's link token to the phone body (debug builds of the body only)")
    pp.add_argument("--rotate", action="store_true", help="make a new token first (the old one stops working)")
    s = sub.choices["serve"]
    s.add_argument("--no-phone-link", dest="phone_link", action="store_false",
                   help="do not set up the phone's reverse port forward")
    s.add_argument("--allow-remote", action="store_true",
                   help="allow a non-loopback --host (unencrypted; logs a loud warning)")
    s.add_argument("-v", "--verbose", action="store_true", help="debug logging (every clock sample, payloads)")
    s.add_argument("--supervised", action="store_true",
                   help="run under the launcher: restart the brain if it dies unexpectedly (bounded; never after "
                        "a fail-closed refusal)")
    sub.choices["probe"].add_argument("--count", type=_int_in_range(1, ECHO_TEST_MAX_COUNT, "--count"),
                                         default=1000, help=f"echoes, 1 to {ECHO_TEST_MAX_COUNT}")
    sub.choices["probe"].add_argument("--bytes", type=_int_in_range(0, ECHO_TEST_MAX_BYTES, "--bytes"),
                                         default=100 * 1024, help=f"bytes per echo, 0 to {ECHO_TEST_MAX_BYTES}")
    bc = sub.add_parser("body-check", help="start a body, call every agent tool, try the boundary requests")
    bc.add_argument("--body", choices=("sim", "phone"), default=None, help="default: config.toml [body] kind")
    bc.add_argument("--wait", type=_float_in_range(0.0, BODY_CHECK_WAIT_MAX_S, "--wait"), default=30.0,
                    help=f"seconds to wait for the body to connect, 0 to {BODY_CHECK_WAIT_MAX_S:g}")
    sub.add_parser("chaos", add_help=False,
                   help="run the chaos and boundary test harness (card X-01; `chaos --help` for its options)")
    w = sub.add_parser("watch", help="show the event files (live with --follow); --health shows the health panel")
    w.add_argument("--component", action="append", choices=COMPONENTS, help="only this component (repeatable)")
    w.add_argument("--level", choices=tuple(LEVELS), default="DEBUG", help="minimum level")
    w.add_argument("--type", action="append", help="event type or glob such as 'tool.*' (repeatable)")
    w.add_argument("--session", help="only events of this session id")
    w.add_argument("--turn", help="only events of this turn id")
    w.add_argument("--since", help="only events since: 10s, 5m, 2h, 1d, or a time like 2026-11-13T10:00")
    w.add_argument("--detail", action="store_true", help="every field of every event")
    w.add_argument("-f", "--follow", action="store_true", help="keep showing new events as they arrive")
    w.add_argument("--health", action="store_true", help="show the health panel, refreshed every second")
    w.add_argument("--source", help="a folder of event files, or one events-*.jsonl file (default: data/logs)")
    sub.add_parser("health", help="print the health panel (data/health.json); says STALE if the brain stopped")
    d = sub.add_parser("demo-trace", help="a scripted conversation against SimBody: a complete trace, no hardware")
    d.add_argument("--unavailable", action="store_true",
                   help="start with the body link, camera and microphone unavailable")
    d.add_argument("--no-files", action="store_true", help="print only; do not write data/logs or data/health.json")
    d.add_argument("--detail", action="store_true", help="every field of every event")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Every command starts here, behind the outermost crash barrier.

    A config.toml or .env problem stops the brain with ONE clear message on stderr and exit code 2 (fail
    closed, H-05). Anything unexpected that reaches this level prints ONE line and where the details are,
    writes a crash report (data/crash/), and exits with EXIT_CRASH: never a raw traceback. Ctrl+C ends
    quietly. SystemExit (argparse's own messages and --help) passes through unchanged."""
    try:
        return _main(argv)
    except ConfigError as e:
        obs.emit("config.error", "configuration refused; not starting", problem=str(e)[:200])
        print(f"config error: {e}", file=sys.stderr)
        print("the brain did not start; fix the file and run it again", file=sys.stderr)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def _crashed(exc: BaseException) -> int:
    from .obs.crash import exception_details, write_report

    obs.emit("process.crash", f"unexpected {type(exc).__name__} reached main()", error=type(exc).__name__,
             command=_run["command"])
    folder = _run["data_dir"]
    if folder is None:
        try:
            folder = as_config(None).paths.data_dir
        except ConfigError:
            folder = REPO_ROOT / "data"
    report = write_report(folder, "crash", exception_details(exc, str(_run["command"])), obs.LOG.recent(30))
    if logging.getLogger().handlers:  # the log file keeps the traceback (the console handler shows one line)
        logging.getLogger("brain").critical("unexpected %s reached main()", type(exc).__name__, exc_info=exc)
    print(f"the brain stopped unexpectedly ({type(exc).__name__}); "
          + (f"details in {report}" if report else "the crash report could not be written; see data/logs"),
          file=sys.stderr)
    return EXIT_CRASH


def _main(argv: list[str] | None) -> int:
    config = as_config(load_config())
    _run["data_dir"] = config.paths.data_dir
    parser = build_parser(config)
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["chaos"]:
        _run["command"] = "chaos"
        return _barrier(lambda: chaos(argv[1:]))
    args = parser.parse_args(argv)
    _run["command"] = args.cmd or "info"
    return _barrier(lambda: _run_command(args, config, argv))


def _barrier(run: Callable[[], int]) -> int:
    """The outermost crash barrier around a command (after its arguments are parsed): anything unexpected
    becomes one line, a crash report and EXIT_CRASH."""
    try:
        return run()
    except (ConfigError, KeyboardInterrupt):
        raise  # main() answers these
    except Exception as e:  # broad-except: the outermost crash barrier: one line, a crash report, no traceback
        return _crashed(e)


def _run_command(args: argparse.Namespace, config: Config, argv: list[str]) -> int:
    from .egress import EgressPolicy, install_guard

    obs.emit("config.loaded", "config passed the gate", file=Path(config.source).name, body_kind=config.body.kind)
    # The guard goes on before anything else runs. Only `probe` dials a port of another local process.
    probe_port = ({args.port} if args.cmd == "probe" and is_loopback(args.host) and PORT_MIN <= args.port <= PORT_MAX
                  else set())
    install_guard(EgressPolicy.from_config(config, local_ports=probe_port))
    if args.cmd in ("watch", "health"):  # read-only viewers: no start/stop events of their own
        return watch(args, config) if args.cmd == "watch" else health(args, config)
    obs.emit("brain.start", f"{_run['command']} starting", command=_run["command"], version=__version__)
    code = _command(args, config, argv)
    obs.emit("brain.stop", f"{_run['command']} ended", command=_run["command"], exit_code=code)
    return code


def _command(args: argparse.Namespace, config: Config, argv: list[str]) -> int:
    if args.cmd == "body-check":
        return body_check(args, config)
    if args.cmd == "serve":
        return serve(args, config, argv)
    if args.cmd == "probe":
        return probe(args, config)
    if args.cmd == "pair":
        return pair(args, config)
    if args.cmd == "demo-trace":
        return demo_trace(args, config)
    return info()


if __name__ == "__main__":
    raise SystemExit(main())
