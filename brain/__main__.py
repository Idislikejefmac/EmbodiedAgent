"""python -m brain [info|serve|probe|pair].

info (default): print the version, load config, and report which keys exist.
serve: run the protocol v0 WebSocket server (and keep the phone link up unless --no-phone-link).
probe: connect as a test body and run the echo test against a running server.
pair: give the phone body this brain's link token (debug builds of the body only).
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import logging.handlers
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
    for name, present in key_presence().items():
        print(f"key {name}: {'present' if present else 'missing'}")
    return 0


def data_dir(config: dict) -> Path:
    return REPO_ROOT / config.get("paths", {}).get("data_dir", "data")


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

    last = None
    while True:
        ok, message = await asyncio.to_thread(phone.ensure_link, port)
        if ok != last:
            (log.info if ok else log.warning)(message)
            last = ok
        await asyncio.sleep(PHONE_LINK_RECHECK_S)


def serve(args, config: dict) -> int:
    from .transport.auth import TokenStoreError, load_or_create_token

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
    try:
        token = load_or_create_token(data_dir(config))
    except TokenStoreError as e:
        log.error("link token store is not safe: %s", e)
        return 2
    asyncio.run(_serve(args, token, log))
    log.info("stopped")
    return 0


def probe(args, config: dict) -> int:
    from .transport.auth import load_or_create_token
    from .transport.client import TestBodyClient

    setup_logging(config, "probe")
    log = logging.getLogger("brain.probe")
    log.warning("probe connects as a body: the brain serves one body at a time, so a connected body is "
                "replaced (close 4001) and stops until Connect is pressed again")
    token = load_or_create_token(data_dir(config))

    async def go():
        url = f"ws://{args.host}:{args.port}/"
        async with TestBodyClient(url, token=token) as c:
            log.info("connected to %s session %s", url, c.brain_hello["payload"]["session_id"])
            res = await c.echo_test(args.count, args.bytes)
            log.info("echo result: %s ok=%s", res, res.ok)
            return res.ok

    return 0 if asyncio.run(go()) else 1


def pair(args, config: dict) -> int:
    from .body import phone
    from .transport.auth import TokenStoreError, load_or_create_token

    try:
        token = load_or_create_token(data_dir(config), rotate=args.rotate)
    except TokenStoreError as e:
        print(f"link token store is not safe: {e}")
        return 2
    ok, message = phone.pair(token)
    print(message)
    if args.rotate:
        print("token rotated: restart `python -m brain serve` so it uses the new token")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)
    if args.cmd == "serve":
        return serve(args, config)
    if args.cmd == "probe":
        return probe(args, config)
    if args.cmd == "pair":
        return pair(args, config)
    return info()


if __name__ == "__main__":
    raise SystemExit(main())
