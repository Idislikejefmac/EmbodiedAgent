"""The egress chokepoint: the only way the brain may reach anything outside this process (card P0-06 step 9).

Plan, "Command boundary": `config.toml` `[egress] allowed_hosts` lists the destinations Jeff has
approved; everything else is denied. Adding a destination is a console edit of that file. Nothing the
agent can call reads or changes it.

Two layers, both in this file:

1. **By construction.** This is the only module under `brain/` allowed to import a network client
   library or a vendor SDK (tests/test_egress.py lints the imports; `brain/transport/` may import the
   WebSocket library for the loopback link to the body, and nothing else). A new outbound client is
   therefore built here, where it must call `policy.check(host, port)` for its fixed destination.
2. **At run time.** `install_guard(policy)` makes this process refuse, in the socket layer, any
   name lookup or connection that is not allowed, whichever library attempts it. So a client that
   was wired up wrongly, or a library that phones home, fails with `EgressDenied` instead of sending.
   `python -m brain` installs the guard before it does anything else; it is installed once and
   cannot be widened afterwards, and it tests itself as it is installed.

What is allowed:

- a destination on the allowlist: exact host name (or IP address), lower case, optionally
  "host:port"; the port defaults to 443; no wildcards. An address is reachable only if a lookup of a
  listed name returned it within the last 10 minutes, and only on that name's ports;
- **loopback, but only to a port this process itself is listening on** (the body link, the event
  loop's own socket pair), or one named in the policy's `local_ports` (the port `python -m brain
  probe` dials). Loopback to any other port is refused: a local proxy or forwarder would otherwise
  carry traffic anywhere while the guard saw only "127.0.0.1".

For the same reason `install_guard` removes the proxy environment variables (HTTP_PROXY and the like)
from this process and says so in the log. Proxy settings in the Windows registry (which the standard
library's `urllib` reads) are not changed; a client that follows them is stopped at its connection
to the proxy, whether the proxy is local or remote.

What the guard cannot see: code that opens sockets outside Python's socket module (a C extension with
its own networking, direct calls into `_overlapped` or `ctypes`, or a child process). The import lint
keeps those out of the brain. The process cannot be made to filter by host name from outside (a
firewall rule filters addresses, not names), so this in-process layer is the enforcement.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import sys
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any

from . import obs
from .config import Config
from .limits import PORT_MAX, PORT_MIN
from .limits import RESOLVED_TTL_S as RESOLVED_TTL_S  # how long a learned address stays reachable (H-11)

log = logging.getLogger("brain.egress")

DEFAULT_PORT = 443
PROXY_VARIABLES = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "WS_PROXY", "WSS_PROXY",
                             "NO_PROXY"})
SELF_TEST_ADDRESS = ("192.0.2.1", 9)  # RFC 5737 documentation address: never a real destination
_HOST = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
_LOCAL_NAMES = frozenset({"localhost"})

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class EgressDenied(PermissionError):
    """An outbound connection or name lookup was refused because the destination is not allowed."""


def _as_ip(host: str) -> IPAddress | None:
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])  # drop an IPv6 zone
    except ValueError:
        return None
    return ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip


def _clean(host: object) -> str:
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    return str(host).strip().rstrip(".").lower()


def is_local(host: object) -> bool:
    """True for loopback addresses and "localhost": traffic that never leaves this machine."""
    h = _clean(host)
    ip = _as_ip(h)
    return ip.is_loopback if ip is not None else h in _LOCAL_NAMES


@dataclass(frozen=True)
class EgressPolicy:
    """The allowlist: a set of (host, port), plus loopback ports of other local processes that may be
    dialled. Both empty means no egress at all."""

    allowed: frozenset[tuple[str, int]] = frozenset()
    local_ports: frozenset[int] = frozenset()

    @classmethod
    def from_hosts(cls, entries: list[str], *, local_ports: frozenset[int] | set[int] = frozenset()) -> EgressPolicy:
        allowed = set()
        for entry in entries:
            if not isinstance(entry, str):
                raise ValueError(f"egress entry {entry!r} must be a string")
            text = entry.strip().lower()
            host, port = text, DEFAULT_PORT
            if text.startswith("["):  # [ipv6]:port
                host, _, rest = text[1:].partition("]")
                port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else DEFAULT_PORT if not rest else -1
            elif text.count(":") == 1:
                host, _, port_text = text.partition(":")
                port = int(port_text) if port_text.isdigit() else -1
            ip = _as_ip(host)
            if not PORT_MIN <= port <= PORT_MAX or (ip is None and not _HOST.match(host)):
                raise ValueError(f"egress entry {entry!r} must be an exact host name or IP address, optionally "
                                 f"with :port (no scheme, path or wildcard)")
            if is_local(host):
                raise ValueError(f"egress entry {entry!r}: loopback is not egress; it is governed by local_ports")
            allowed.add((str(ip) if ip is not None else host, port))
        ports = frozenset(local_ports)
        if any(isinstance(p, bool) or not isinstance(p, int) or not PORT_MIN <= p <= PORT_MAX for p in ports):
            raise ValueError("local_ports must be port numbers")
        return cls(frozenset(allowed), ports)

    @classmethod
    def from_config(cls, config: Config | dict[str, Any], *,
                    local_ports: frozenset[int] | set[int] = frozenset()) -> EgressPolicy:
        """From the typed Config of brain/config.py (its [egress] list is already checked), or from a raw
        config dict (tests, tools). A missing section or list means deny everything."""
        if isinstance(config, Config):
            return cls.from_hosts(list(config.egress.allowed_hosts), local_ports=local_ports)
        section = config.get("egress", {}) if isinstance(config, dict) else None
        if not isinstance(section, dict):
            raise ValueError("[egress] must be a table")
        hosts = section.get("allowed_hosts", [])
        if not isinstance(hosts, list):
            raise ValueError("[egress] allowed_hosts must be a list")
        return cls.from_hosts(hosts, local_ports=local_ports)

    def allows(self, host: object, port: int) -> bool:
        """Is this destination allowed by the policy alone? (The guard also allows loopback to ports
        this process is listening on.)"""
        h = _clean(host)
        if is_local(h):
            return port in self.local_ports
        ip = _as_ip(h)
        return ((str(ip) if ip is not None else h), port) in self.allowed

    def allows_name(self, host: object) -> bool:
        """May this name be looked up at all? (It is listed, on some port.)"""
        h = _clean(host)
        return is_local(h) or any(h == a for a, _ in self.allowed)

    def ports_for(self, host: object) -> frozenset[int]:
        h = _clean(host)
        return frozenset(p for a, p in self.allowed if a == h)

    def check(self, host: object, port: int = DEFAULT_PORT) -> None:
        """Raise EgressDenied (and log) unless the destination is on the allowlist.
        Every outbound client built in this module calls this before it is created."""
        if not self.allows(host, port):
            _deny(f"{_clean(host)!r} port {port}")


_quiet = threading.local()  # set while the guard tests itself, so the test does not fill the log


def _deny(what: str) -> None:
    if not getattr(_quiet, "on", False):
        log.warning("EGRESS DENIED: %s is not allowed by the [egress] allowlist", what)
        obs.emit("egress.denied", "outbound connection or lookup refused", what=what)
    raise EgressDenied(f"egress to {what} is not allowed by the [egress] allowlist in config.toml")


# ---- the process-wide guard --------------------------------------------------------------------------

_policy: EgressPolicy | None = None  # None: guard not active
_installed = False
_lock = threading.Lock()
_resolved: dict[str, dict[int, float]] = {}  # address -> {port: expiry}, from lookups of allowlisted names
_bound: weakref.WeakSet[socket.socket] = weakref.WeakSet()  # sockets this process has bound
_real_getaddrinfo = socket.getaddrinfo
_INET = (socket.AF_INET, socket.AF_INET6)


def _own_ports() -> set[int]:
    """The ports this process is itself bound to right now."""
    ports = set()
    for sock in list(_bound):
        try:
            if sock.fileno() != -1 and sock.family in _INET:
                ports.add(int(sock.getsockname()[1]))
        except OSError:
            continue  # closed, or its bind has not happened yet
    return ports


def _check_name(policy: EgressPolicy, host: object, port: object = None) -> None:
    """Before a name is looked up: the name must be listed, and if the lookup says which port it is
    for, on that port. An unlisted name is therefore never sent to a DNS server."""
    if host is None or host == "" or host == b"":
        return  # the wildcard address, used for listening
    h = _clean(host)
    if _as_ip(h) is not None or is_local(h):
        return  # an address needs no lookup; connecting to it is checked separately
    if not policy.allows_name(h):
        _deny(f"name lookup of {h!r}")
    if isinstance(port, (bytes, str)) and port.isdigit():
        port = int(port)
    if isinstance(port, int) and port and port not in policy.ports_for(h):
        _deny(f"{h!r} port {port}")


def _check_address(policy: EgressPolicy, family: int, address: object) -> None:
    if family not in _INET:
        if family == getattr(socket, "AF_UNIX", object()):
            return  # a local socket file
        _deny(f"a socket of address family {family}")
    if not isinstance(address, tuple) or len(address) < 2:
        _deny(f"address {address!r:.80}")
        return
    host, port = _clean(address[0]), address[1]
    if is_local(host):
        if port in policy.local_ports or port in _own_ports():
            return
        _deny(f"loopback port {port} (not a port this process listens on; a local proxy could forward anywhere)")
    ip = _as_ip(host)
    if ip is None:  # connect() given a name resolves it without a separate lookup event
        if not policy.allows(host, port):
            _deny(f"{host!r} port {port}")
        return
    if policy.allows(str(ip), port):
        return
    with _lock:
        if _resolved.get(str(ip), {}).get(port, 0.0) > time.monotonic():
            return
    _deny(f"{ip} port {port}")


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if not event.startswith("socket."):
        return
    if event == "socket.bind":  # remembered even while no policy is active, so own ports are always known
        try:
            _bound.add(args[0])
        except TypeError:  # swallowed: an unhashable address in a bind event cannot be remembered; nothing to do
            pass
        return
    policy = _policy
    if policy is None:
        return
    if event == "socket.getaddrinfo":
        _check_name(policy, args[0], args[1])
    elif event == "socket.gethostbyname":
        _check_name(policy, args[0])
    elif event == "socket.gethostbyaddr":
        if not is_local(args[0]):
            _deny(f"reverse lookup of {_clean(args[0])!r}")
    elif event == "socket.getnameinfo":
        sockaddr = args[0]
        if not (isinstance(sockaddr, tuple) and sockaddr and is_local(sockaddr[0])):
            _deny(f"reverse lookup of {sockaddr!r:.80}")
    elif event in ("socket.connect", "socket.sendto"):
        _check_address(policy, args[0].family, args[1])
    elif event == "socket.sendmsg" and args[1] is not None:
        _check_address(policy, args[0].family, args[1])


def _getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    """socket.getaddrinfo, remembering which addresses an allowlisted name resolved to, so that the
    connection that follows (which names only the address) can be recognised."""
    result = _real_getaddrinfo(host, port, *args, **kwargs)  # raises the audit event; unlisted names stop there
    policy = _policy
    if policy is not None and host:
        ports = policy.ports_for(host)
        if ports:
            expiry = time.monotonic() + RESOLVED_TTL_S
            with _lock:
                for stale in [a for a, p in _resolved.items() if all(t <= time.monotonic() for t in p.values())]:
                    del _resolved[stale]
                for info in result:
                    ip = _as_ip(str(info[4][0]))
                    if ip is not None:
                        _resolved.setdefault(str(ip), {}).update(dict.fromkeys(ports, expiry))
    return result


def _guard_windows_event_loop() -> None:
    """asyncio's Windows event loop connects and sends through overlapped I/O, which raises no audit
    event, so its two outbound calls are checked here."""
    from asyncio import windows_events

    proactor: Any = windows_events.IocpProactor
    real_connect, real_sendto = proactor.connect, proactor.sendto

    def connect(self: Any, conn: Any, address: Any) -> Any:
        if _policy is not None:
            _check_address(_policy, conn.family, address)
        return real_connect(self, conn, address)

    def sendto(self: Any, conn: Any, buf: Any, flags: int = 0, addr: Any = None) -> Any:
        if _policy is not None and addr is not None:
            _check_address(_policy, conn.family, addr)
        return real_sendto(self, conn, buf, flags, addr)

    proactor.connect, proactor.sendto = connect, sendto


def strip_proxy_environment() -> list[str]:
    """Remove proxy settings from this process's environment (and so from its children's). Returns the
    names removed, never their values (a proxy URL can hold a password)."""
    removed = [name for name in list(os.environ) if name.upper() in PROXY_VARIABLES]
    for name in removed:
        del os.environ[name]
    return sorted(removed)


def self_test() -> list[str]:
    """Check that the guard really stops a connection, on both paths the brain uses: a blocking socket
    and the asyncio event loop. Returns what got through (empty when the guard works). Nothing is
    sent when it works; the target is a documentation address that routes nowhere."""
    failures: list[str] = []

    async def through_event_loop() -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setblocking(False)
            try:
                await asyncio.wait_for(asyncio.get_running_loop().sock_connect(sock, SELF_TEST_ADDRESS), 0.5)
            except EgressDenied:
                return
            # broad-except: any other error means the guard did not refuse; recorded as a self-test failure
            except Exception as e:
                failures.append(f"asyncio sock_connect was not refused ({type(e).__name__})")
            else:
                failures.append("asyncio sock_connect was not refused")

    def run() -> None:
        _quiet.on = True
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setblocking(False)
                try:
                    sock.connect(SELF_TEST_ADDRESS)
                except EgressDenied:  # swallowed: this is the refusal the self-test expects
                    pass
                except OSError as e:
                    failures.append(f"blocking connect was not refused ({type(e).__name__})")
                else:
                    failures.append("blocking connect was not refused")
            asyncio.run(through_event_loop())
        # broad-except: the self-test itself broke, which is a failure too (reported, never raised)
        except Exception as e:
            failures.append(f"self-test could not run ({type(e).__name__}: {e})")
        finally:
            _quiet.on = False

    thread = threading.Thread(target=run, name="egress-self-test")  # its own thread: no event loop is running there
    thread.start()
    thread.join()
    return failures


def _activate(policy: EgressPolicy) -> None:
    global _policy, _installed
    with _lock:
        if not _installed:
            sys.addaudithook(_audit)
            socket.getaddrinfo = _getaddrinfo
            if sys.platform == "win32":
                _guard_windows_event_loop()
            _installed = True
        _resolved.clear()
        _policy = policy


def install_guard(policy: EgressPolicy) -> None:
    """Enforce `policy` for every socket this process opens from now on. One-shot: it cannot be
    removed, and calling it again with a different policy raises (the same policy again is a no-op),
    so nothing running later can widen it. Also removes proxy environment variables, and tests
    itself: if a connection to a documentation address is not refused, it raises RuntimeError rather
    than let the brain run with a gap."""
    if not isinstance(policy, EgressPolicy):
        raise TypeError("install_guard needs an EgressPolicy")
    if _policy is not None:
        if policy == _policy:
            return
        raise RuntimeError("the egress guard is already installed with a different policy; it cannot be changed")
    removed = strip_proxy_environment()
    _activate(policy)
    failures = self_test()
    obs.emit("startup.self_test", "egress guard self-test " + ("failed" if failures else "passed"), test="egress_guard",
             ok=not failures, failures=failures, destinations=len(policy.allowed),
             level="ERROR" if failures else "INFO")
    if failures:
        raise RuntimeError("the egress guard failed its self-test: " + "; ".join(failures))
    log.info("egress guard on: %s%s%s", ", ".join(f"{h}:{p}" for h, p in sorted(policy.allowed)) or "no destinations",
             f"; local ports {sorted(policy.local_ports)}" if policy.local_ports else "",
             f"; removed proxy settings {removed} from the environment" if removed else "")
    if removed:
        log.warning("proxy environment variables %s were set and have been removed for this process", removed)


def guard_active() -> bool:
    return _policy is not None
