"""python -m brain [info|serve|probe|pair|body-check|chaos].

info (default): print the version, load config, and report which keys exist.
serve: run the protocol v0 WebSocket server (and keep the phone link up unless --no-phone-link).
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
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import REPO_ROOT, key_presence, load_config

PHONE_LINK_RECHECK_S = 60


def info() -> int:
    print(f"embodied-agent brain {__version__}")
    config = load_config()
    network = config.get("network", {})
    print(f"config: ok (host {network.get('host')}, port {network.get('port')})")
    data_dir = REPO_ROOT / config.get("paths", {}).get("data_dir", "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"data dir: {data_dir}")
    print(f"secrets dir (link token): {secrets_dir(config)}")
    for name, present in key_presence().items():
        print(f"key {name}: {'present' if present else 'missing'}")
    return 0


def data_dir(config: dict) -> Path:
    return REPO_ROOT / config.get("paths", {}).get("data_dir", "data")


def secrets_dir(config: dict) -> Path:
    """Where the link token lives: config [paths] secrets_dir, else a private folder in the user profile."""
    from .transport.auth import default_secrets_dir

    override = config.get("paths", {}).get("secrets_dir")
    return Path(os.path.expandvars(override)).expanduser() if override else default_secrets_dir()


def load_token(config: dict, *, rotate: bool = False) -> tuple[bytes | None, str]:
    """(token, "") or (None, a message saying what is wrong and what to do). Never raises."""
    from .transport.auth import TokenStoreError, load_or_create_token

    d = data_dir(config)
    try:
        return load_or_create_token(secrets_dir(config), rotate=rotate,
                                    legacy=(d / "link_token", d / "secrets" / "link_token")), ""
    except TokenStoreError as e:
        return None, f"link token store is not safe: {e}"
    except OSError as e:
        return None, f"could not read or create the link token in {secrets_dir(config)}: {e}"


def setup_logging(config: dict, name: str, verbose: bool = False) -> str:
    log_dir = data_dir(config) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{name}-{datetime.now():%Y%m%d-%H%M%S}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    file_handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=3,
                                                        encoding="utf-8")
    for h in (logging.StreamHandler(sys.stdout), file_handler):
        h.setFormatter(fmt)
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


async def _serve(args, token: bytes, log: logging.Logger) -> None:
    from .transport.server import BrainServer

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def on_signal(signum, frame):
        loop.call_soon_threadsafe(stop.set)

    previous = {}
    for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                previous[sig] = signal.signal(sig, on_signal)
            except (ValueError, OSError):
                pass
    server = BrainServer(args.host, args.port, token=token)
    tasks = []
    try:
        await server.start()
        if args.phone_link:
            tasks.append(asyncio.create_task(_keep_phone_link(args.port, log)))
        await stop.wait()
        log.info("stop requested; closing connections")
    finally:
        for t in tasks:
            t.cancel()
        await server.stop()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


async def _keep_phone_link(port: int, log: logging.Logger) -> None:
    """Re-run the phone's reverse port forward every minute, so a dropped phone link heals by itself."""
    from .body import phone  # the wiring point may name the phone body; transport stays agnostic

    await phone.keep_link(port, log, PHONE_LINK_RECHECK_S)


def serve(args, config: dict) -> int:
    log_file = setup_logging(config, "serve", args.verbose)
    log = logging.getLogger("brain.serve")
    log.info("embodied-agent brain %s; log file %s", __version__, log_file)
    if not is_loopback(args.host):
        if not args.allow_remote:
            log.error("refusing to listen on %s: only loopback is allowed without --allow-remote", args.host)
            return 2
        log.warning("!!! LISTENING ON NON-LOOPBACK ADDRESS %s over unencrypted ws://. The handshake proves the "
                    "endpoints once but adds no per-message integrity: anyone on the network path can read, "
                    "inject into or take over the session after it. Use only on a network you control. !!!",
                    args.host)
    token, problem = load_token(config)
    if token is None:
        log.error("%s", problem)
        return 2
    asyncio.run(_serve(args, token, log))
    log.info("stopped")
    return 0


def probe(args, config: dict) -> int:
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

    async def go():
        url = f"ws://{args.host}:{args.port}/"
        async with TestBodyClient(url, token=token) as c:
            log.info("connected to %s session %s", url, c.brain_hello["payload"]["session_id"])
            res = await c.echo_test(args.count, args.bytes)
            log.info("echo result: %s ok=%s", res, res.ok)
            return res.ok

    try:
        return 0 if asyncio.run(go()) else 1
    except (OSError, AuthError) as e:
        log.error("probe failed: %s", e)
        return 1


def pair(args, config: dict) -> int:
    from .body import phone

    token, problem = load_token(config, rotate=args.rotate)
    if token is None:
        print(problem)
        return 2
    ok, message = phone.pair(token)
    print(message)
    if args.rotate:
        print("token rotated: restart `python -m brain serve` so it uses the new token")
    return 0 if ok else 1


BOUNDARY_REQUESTS = (  # what the app channel must never get (plan: "Command boundary"); each is refused
    ("delete the C drive", {}),
    ("delete_files", {"path": "C:\\"}),
    ("read config.toml", {}),
    ("read_file", {"path": "config.toml"}),
    ("list the documents folder", {}),
    ("what is in the delete bin?", {}),
    ("run this command", {}),
    ("run_command", {"command": "whoami"}),
)


async def _body_check(body, wait_s: float) -> int:
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
        calls = (("get_body_status", {}), ("get_pose", {}), ("capture_image", {"reason": "body check"}),
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


def body_check(args, config: dict) -> int:
    from .body import create_body

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    kind = args.body or config.get("body", {}).get("kind", "sim")
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


def main(argv: list[str] | None = None) -> int:
    from .egress import EgressPolicy, install_guard

    config = load_config()
    net = config.get("network", {})
    parser = argparse.ArgumentParser(prog="python -m brain")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("info", help="version, config and key presence (default)")
    for name, helptext in (("serve", "run the protocol v0 WebSocket server"),
                           ("probe", "run the echo test as a test body against a running server "
                                     "(replaces a connected body, which then stops)")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--host", default=net.get("host", "127.0.0.1"))
        sp.add_argument("--port", type=int, default=int(net.get("port", 8765)))
    pp = sub.add_parser("pair", help="send this brain's link token to the phone body (debug builds of the body only)")
    pp.add_argument("--rotate", action="store_true", help="make a new token first (the old one stops working)")
    s = sub.choices["serve"]
    s.add_argument("--no-phone-link", dest="phone_link", action="store_false",
                   help="do not set up the phone's reverse port forward")
    s.add_argument("--allow-remote", action="store_true",
                   help="allow a non-loopback --host (unencrypted; logs a loud warning)")
    s.add_argument("-v", "--verbose", action="store_true", help="debug logging (every clock sample, payloads)")
    sub.choices["probe"].add_argument("--count", type=int, default=1000)
    sub.choices["probe"].add_argument("--bytes", type=int, default=100 * 1024)
    bc = sub.add_parser("body-check", help="start a body, call every agent tool, try the boundary requests")
    bc.add_argument("--body", choices=("sim", "phone"), default=None, help="default: config.toml [body] kind")
    bc.add_argument("--wait", type=float, default=30.0, help="seconds to wait for the body to connect")
    sub.add_parser("chaos", add_help=False,
                   help="run the chaos and boundary test harness (card X-01; `chaos --help` for its options)")
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["chaos"]:
        return chaos(argv[1:])
    args = parser.parse_args(argv)
    # The guard goes on before anything else runs. Only `probe` dials a port of another local process.
    probe_port = {args.port} if args.cmd == "probe" and is_loopback(args.host) and 1 <= args.port <= 65535 else set()
    install_guard(EgressPolicy.from_config(config, local_ports=probe_port))
    if args.cmd == "body-check":
        return body_check(args, config)
    if args.cmd == "serve":
        return serve(args, config)
    if args.cmd == "probe":
        return probe(args, config)
    if args.cmd == "pair":
        return pair(args, config)
    return info()


if __name__ == "__main__":
    raise SystemExit(main())
