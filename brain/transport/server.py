"""The brain's WebSocket server for protocol v0. Body-agnostic: any paired body that speaks v0 may connect."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from http import HTTPStatus

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .. import __version__
from . import auth
from . import protocol as p
from .clock import ClockOffsetEstimator
from .link import Link, Received

log = logging.getLogger("brain.transport.server")

MessageHandler = Callable[[Link, Received], Awaitable[None]]

BRAIN_CAPABILITIES = ["event", "echo"]
HANDLER_QUEUE = 256  # messages waiting for on_message; beyond this the body gets `busy`
DIAGNOSTIC_EVENTS = {"echo_test_result"}  # only counters; logged in full. Other event data is not.
MAX_PENDING_HANDSHAKES = 4  # connections not yet authenticated; more get HTTP 503
PRE_AUTH_MAX_BYTES = 16 * 1024  # largest frame accepted before authentication (hello and auth are ~1 kB)
MAX_FRAME_BYTES = p.MAX_BINARY_BYTES + p.BINARY_HEADER.size


def new_session_id() -> str:
    return f"s-{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


class BrainServer:
    """Accepts one paired body at a time: authenticated handshake, heartbeats, echo, requests.

    Other messages go to `on_message`, which runs in its own task per connection (in order), so a slow
    handler never delays heartbeats and a handler may await `link.request(...)`.
    Policy (section 1): one active body. A newly authenticated body replaces the previous one, which is
    closed with code 4001. At most MAX_PENDING_HANDSHAKES unauthenticated connections at a time, each
    limited to PRE_AUTH_MAX_BYTES per frame.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8765, *, token: bytes,
                 on_message: MessageHandler | None = None,
                 heartbeat_s: float = p.HEARTBEAT_MS / 1000, liveness_s: float = p.LIVENESS_TIMEOUT_MS / 1000,
                 hello_timeout_s: float = 10.0, send_timeout_s: float = 30.0):
        if len(token) < 16:
            raise ValueError("the link token must be at least 16 bytes")
        self.host = host
        self.port = port
        self._token = token
        self.on_message = on_message
        self.heartbeat_s = heartbeat_s
        self.liveness_s = liveness_s
        self.hello_timeout_s = hello_timeout_s
        self.send_timeout_s = send_timeout_s
        self.boot_id = f"r-{secrets.token_hex(4)}"
        self.links: dict[str, Link] = {}  # at most one: the active body
        self.pending_handshakes = 0
        self._clocks: OrderedDict[str, ClockOffsetEstimator] = OrderedDict()  # by body boot_id
        self._server: Server | None = None

    async def start(self) -> BrainServer:
        self._server = await serve(
            self._handle, self.host, self.port,
            max_size=MAX_FRAME_BYTES,
            max_queue=4,  # at most a few frames (32 MiB worst case) buffered per connection
            ping_interval=None,  # liveness comes from protocol heartbeats
            compression=None,
            origins=[None],  # refuse any request with an Origin header: web pages may not act as a body
            process_request=self._process_request,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("listening on ws://%s:%d/ (protocol v%d, brain boot_id %s)", self.host, self.port,
                 p.PROTOCOL_VERSION, self.boot_id)
        return self

    def _process_request(self, connection: ServerConnection, request):
        if self.pending_handshakes >= MAX_PENDING_HANDSHAKES:
            log.warning("refusing a connection from %s: %d handshakes already pending",
                        connection.remote_address, self.pending_handshakes)
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "too many pending handshakes\n")
        return None

    async def stop(self) -> None:
        """Close every connection with 1001 (going away) and wait for the handlers to log their summaries."""
        if self._server is not None:
            self._server.close(close_connections=True)
            await self._server.wait_closed()
            self._server = None
            log.info("server stopped")

    async def serve_forever(self) -> None:
        await self.start()
        try:
            await asyncio.Future()
        finally:
            await self.stop()

    def _clock_for(self, boot_id: str | None) -> ClockOffsetEstimator:
        """Keep the clock estimate across reconnects of the same body run (same boot_id)."""
        if boot_id is None:
            return ClockOffsetEstimator()
        est = self._clocks.pop(boot_id, None) or ClockOffsetEstimator()
        self._clocks[boot_id] = est
        while len(self._clocks) > 8:
            self._clocks.popitem(last=False)
        return est

    async def _handle(self, ws: ServerConnection) -> None:
        session_id = new_session_id()
        slog = logging.getLogger(f"brain.transport.{session_id}")
        link = Link(ws, p.BRAIN, log=slog, heartbeat_s=self.heartbeat_s, liveness_s=self.liveness_s,
                    send_timeout_s=self.send_timeout_s)
        slog.info("connection from %s", ws.remote_address)
        dispatcher = None
        self.pending_handshakes += 1
        ws.protocol.max_message_size = PRE_AUTH_MAX_BYTES
        try:
            try:
                ok = await self._handshake(link, session_id)
            finally:
                self.pending_handshakes -= 1
            if not ok:
                return
            ws.protocol.max_message_size = MAX_FRAME_BYTES
            old = list(self.links.items())
            self.links = {session_id: link}
            for old_id, old_link in old:
                slog.info("replacing the previous body connection %s", old_id)
                await old_link.close(p.CLOSE_REPLACED, "replaced by a newer body connection")
            link.start_background()
            queue: asyncio.Queue[Received] = asyncio.Queue(HANDLER_QUEUE)
            dispatcher = asyncio.create_task(self._dispatch_loop(link, queue))
            while True:
                try:
                    r = await link.recv()
                except p.ProtocolError as e:
                    if await link.refuse(e):
                        await link.close(p.CLOSE_PROTOCOL_ERROR, e.code)
                        return
                    continue
                if await link.handle_common(r):
                    continue
                if r.type in ("hello", "auth"):
                    await link.refuse(p.ProtocolError("bad_sequence", f"{r.type} after the handshake ignored",
                                                      reply_to=r.msg["id"]))
                    continue
                try:
                    queue.put_nowait(r)
                except asyncio.QueueFull:
                    await link.refuse(p.ProtocolError("busy", "brain handler queue full; message dropped",
                                                      reply_to=r.msg["id"]), peer_fault=False)
        except ConnectionClosed as e:
            slog.info("connection closed: code=%s reason=%r", e.rcvd.code if e.rcvd else None,
                      e.rcvd.reason if e.rcvd else "")
        finally:
            if dispatcher:
                dispatcher.cancel()
            link.shutdown()
            if self.links.get(session_id) is link:
                self.links.pop(session_id)
            st = link.stats
            slog.info("session end%s: rx=%d tx=%d id_gaps=%d echo_answered=%d echo_crc_errors=%d refused=%s",
                      f" ({link.closed_reason})" if link.closed_reason else "", st.rx_messages, st.tx_messages,
                      st.id_gaps, st.echo_answered, st.echo_crc_errors, st.refused)
            if link.authenticated:
                slog.info(link.clock_summary())

    async def _fail_handshake(self, link: Link, err: p.ProtocolError) -> bool:
        err.fatal = True
        await link.refuse(err, peer_fault=False)
        await link.close(p.CLOSE_AUTH_FAILED if err.code == "unauthorized" else p.CLOSE_PROTOCOL_ERROR, err.code)
        return False

    async def _peer_error_in_handshake(self, link: Link, r: Received) -> bool:
        """The body refused something (for example our proof): log it, never answer an error (section 9)."""
        pl = r.msg["payload"]
        link.log.warning("body refused the handshake: %s: %r", pl["code"], pl["message"])
        await link.close(p.CLOSE_AUTH_FAILED if pl["code"] == "unauthorized" else p.CLOSE_PROTOCOL_ERROR, pl["code"])
        return False

    async def _handshake(self, link: Link, session_id: str) -> bool:
        """hello / hello / auth / auth (section 4.2). Nothing else is processed until both sides have
        proved they hold the pairing token over the exact handshake transcript."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.hello_timeout_s

        async def next_message() -> Received:
            return await asyncio.wait_for(link.recv(), max(0.0, deadline - loop.time()))

        try:
            first = await next_message()
        except TimeoutError:
            return await self._fail_handshake(link, p.ProtocolError("bad_sequence", "no hello within timeout"))
        except p.ProtocolError as e:
            return await self._fail_handshake(link, e)
        if first.type == "error":
            return await self._peer_error_in_handshake(link, first)
        if first.type != "hello":
            return await self._fail_handshake(link, p.ProtocolError(
                "bad_sequence", f"first message was {first.type!r}, not hello", reply_to=first.msg["id"]))
        hp = first.msg["payload"]
        hid = first.msg["id"]
        if p.PROTOCOL_VERSION not in hp.get("versions", [p.PROTOCOL_VERSION]):
            return await self._fail_handshake(link, p.ProtocolError(
                "unsupported_version", f"body speaks {hp['versions']}; this brain speaks [0]", reply_to=hid))
        if "nonce" not in (hp.get("auth") or {}):
            return await self._fail_handshake(link, p.ProtocolError(
                "unauthorized", "not paired: hello carries no auth nonce", reply_to=hid))
        unknown = sorted(set(hp["capabilities"]) - set(p.CAPABILITIES))
        # Peer-supplied strings are logged with %r so they cannot forge log lines.
        link.log.info("hello from %s %r boot_id=%r capabilities=%r commands=%r%s", hp["role"], hp["software"],
                      hp.get("boot_id"), hp["capabilities"], hp.get("commands", []),
                      f" (unknown, ignored: {unknown!r})" if unknown else "")
        link.peer_hello = hp
        await link.send("hello", {
            "role": p.BRAIN, "software": f"embodied-agent-brain/{__version__}", "versions": [p.PROTOCOL_VERSION],
            "boot_id": self.boot_id, "session_id": session_id,
            "wall_time_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "capabilities": BRAIN_CAPABILITIES, "heartbeat_ms": round(self.heartbeat_s * 1000),
            "liveness_timeout_ms": round(self.liveness_s * 1000), "max_binary_bytes": p.MAX_BINARY_BYTES,
            "auth": {"nonce": auth.new_nonce()},
        }, reply_to=hid)
        transcript = [auth.digest(first.raw), auth.digest(link.last_sent_text)]
        try:
            second = await next_message()
        except TimeoutError:
            return await self._fail_handshake(link, p.ProtocolError("unauthorized", "no auth within timeout"))
        except p.ProtocolError as e:
            return await self._fail_handshake(link, e)
        if second.type == "error":
            return await self._peer_error_in_handshake(link, second)
        if second.type != "auth":
            return await self._fail_handshake(link, p.ProtocolError(
                "bad_sequence", f"expected auth, got {second.type!r}", reply_to=second.msg["id"]))
        if not auth.check(self._token, p.BODY, transcript, second.msg["payload"]["mac"]):
            return await self._fail_handshake(link, p.ProtocolError(
                "unauthorized", "authentication failed: the body does not hold this brain's token",
                reply_to=second.msg["id"]))
        transcript.append(auth.digest(second.raw))
        await link.send("auth", {"mac": auth.proof(self._token, p.BRAIN, transcript)}, reply_to=second.msg["id"])
        link.authenticated = True
        clock = self._clock_for(hp.get("boot_id"))
        if len(clock):
            link.log.info("same body run (boot_id %r): keeping its clock estimate (%d samples)",
                          hp.get("boot_id"), len(clock))
        link.clock = clock
        link.log.info("body authenticated; session %s", session_id)
        return True

    async def _dispatch_loop(self, link: Link, queue: asyncio.Queue[Received]) -> None:
        while True:
            r = await queue.get()
            try:
                await self._dispatch(link, r)
            except ConnectionClosed:
                return
            except Exception as e:  # a handler bug must not take the session down
                link.log.exception("handler failed on %s id=%d", r.type, r.msg["id"])
                try:
                    await link.refuse(p.ProtocolError("internal", f"brain failed handling {r.type}: "
                                                      f"{type(e).__name__}", reply_to=r.msg["id"]),
                                      peer_fault=False)
                except ConnectionClosed:
                    return

    async def _dispatch(self, link: Link, r: Received) -> None:
        if self.on_message is not None:
            await self.on_message(link, r)
            return
        msg = r.msg
        if r.type == "event":
            # Names and keys only at INFO, all with %r: payload contents may be personal data later.
            name, data = msg["payload"]["name"], msg["payload"]["data"]
            if name in DIAGNOSTIC_EVENTS:
                link.log.info("event %s %r", name, data)
            else:
                link.log.info("event %s (data keys %r)", name, sorted(data))
                link.log.debug("event %s data %r", name, data)
        elif r.type == "ack":
            link.log.info("ack for %s", msg["reply_to"])
        else:
            link.log.debug("%s id=%d (no handler)", r.type, msg["id"])
