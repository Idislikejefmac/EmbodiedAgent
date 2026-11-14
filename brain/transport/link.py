"""One protocol v0 connection, either side: framing, ids, binary pairing, heartbeats, liveness, requests, echo.

Shared by the brain server and the test client so both ends follow the same rules.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from . import protocol as p
from .clock import ClockOffsetEstimator

RECENT_IDS_KEPT = 1024  # peer ids remembered to refuse a repeat (H-25); a replay older than this is not detected
CLOCK_INFO_EVERY = 30  # one INFO line with the clock estimate per 30 samples (1 minute); the rest at DEBUG


@dataclass
class Received:
    msg: dict[str, Any]
    data: bytes | None
    rx_us: int
    raw: str = ""  # the exact text frame (the handshake transcript hashes it)

    @property
    def type(self) -> str:
        return str(self.msg["type"])


@dataclass
class LinkStats:
    rx_messages: int = 0
    tx_messages: int = 0
    id_gaps: int = 0
    duplicate_ids: int = 0
    echo_answered: int = 0
    echo_crc_errors: int = 0
    errors_received: int = 0
    errors_sent: int = 0
    refused: dict[str, int] = field(default_factory=dict)


class RequestError(Exception):
    """The peer answered a request with `error`."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class LinkClosed(Exception):
    """The connection closed while a request was in flight (section 7: in-flight requests fail)."""


class Link:
    """Wraps a websockets connection. `role` is this side: protocol.BODY or protocol.BRAIN."""

    def __init__(self, ws: Any, role: str, *, log: logging.Logger | None = None,
                 heartbeat_s: float = p.HEARTBEAT_MS / 1000, liveness_s: float = p.LIVENESS_TIMEOUT_MS / 1000,
                 binary_timeout_s: float = 10.0, send_timeout_s: float = 30.0,
                 clock: ClockOffsetEstimator | None = None, max_peer_errors_per_min: int = 20):
        heartbeat_s, liveness_s = p.check_link_timing(heartbeat_s, liveness_s)
        binary_timeout_s = p.check_timeout("binary_timeout_s", binary_timeout_s)
        send_timeout_s = p.check_timeout("send_timeout_s", send_timeout_s)
        self.ws = ws
        self.role = role
        self.peer = p.other_role(role)
        self.log = log or logging.getLogger(f"brain.transport.{role}")
        self.heartbeat_s = heartbeat_s
        self.liveness_s = liveness_s
        self.binary_timeout_s = binary_timeout_s
        self.send_timeout_s = send_timeout_s
        self.clock = ClockOffsetEstimator() if clock is None else clock
        self.session_start_us = p.now_us()
        self._clock_samples = 0
        self.last_sent_text = ""
        self.stats = LinkStats()
        self._next_id = 1
        self._peer_last_id = 0
        self._recent_ids: deque[int] = deque(maxlen=RECENT_IDS_KEPT)  # ids of messages already accepted
        self._recent_set: set[int] = set()
        self._send_lock = asyncio.Lock()
        self._pending_heartbeats: dict[int, int] = {}  # heartbeat id -> our own send stamp (t0)
        self._requests: dict[int, tuple[asyncio.Future[Received], frozenset[str]]] = {}
        self._peer_errors: deque[float] = deque()
        self.max_peer_errors_per_min = max_peer_errors_per_min
        self._tasks: list[asyncio.Task[None]] = []
        self.peer_hello: dict[str, Any] | None = None
        self.authenticated = False
        self.last_rx = time.monotonic()
        self.closed_reason: str | None = None

    # ---- sending -------------------------------------------------------------------------------

    async def send(self, mtype: str, payload: dict[str, Any], *, reply_to: int | None = None,
                   data: bytes | None = None, other_ts: int | None = None, ts: int | None = None,
                   check: bool = True,
                   _register: tuple[asyncio.Future[Received], frozenset[str], list[int]] | None = None) -> dict[str, Any]:
        """Send one message (and its binary frame, if any) atomically with the next id.

        The message is validated against v0 first (check=False only for tests that put bad messages on
        the wire). A send that cannot finish within send_timeout_s aborts the connection, because a
        half-written pair would corrupt the stream.
        """
        if not isinstance(payload, dict):  # before payload.get below
            raise p.FrameTypeError(f"payload must be a dict, not {type(payload).__name__}")
        if data is not None:
            p.check_data(data)
        if ts is not None:
            p.check_timestamp(ts)
        if mtype in p.BINARY_TYPE_CODES:
            n = len(data) if data else 0
            if payload.get("byte_length") != n:
                raise ValueError(f"payload byte_length {payload.get('byte_length')} != {n} data bytes")
        elif data:
            raise ValueError(f"{mtype!r} carries no binary frame")
        async with self._send_lock:
            msg_id = self._next_id
            if msg_id > p.MAX_ID:
                raise RuntimeError("id space exhausted; reconnect")
            stamp = p.now_us() if ts is None else ts
            msg = p.make_message(mtype, msg_id, payload, sender=self.role, ts=stamp,
                                 reply_to=reply_to, other_ts=other_ts)
            text = p.encode(msg)
            if check:
                try:
                    p.validate(msg, sender=self.role)
                except p.ProtocolError as e:
                    raise ValueError(f"refusing to send an invalid {mtype}: {e}") from None
                if len(text.encode("utf-8")) > p.MAX_TEXT_BYTES:
                    raise ValueError(f"{mtype} is over {p.MAX_TEXT_BYTES} bytes")
            self._next_id += 1
            if _register is not None:  # register before the bytes leave, so a fast answer cannot slip past
                self._requests[msg_id] = (_register[0], _register[1])
                _register[2].append(msg_id)
            self.last_sent_text = text
            try:
                await asyncio.wait_for(self.ws.send(text), self.send_timeout_s)
                if data:
                    await asyncio.wait_for(self.ws.send(p.pack_binary(mtype, msg_id, stamp, data)),
                                           self.send_timeout_s)
            except TimeoutError:
                self.abort(f"send of {mtype} blocked for {self.send_timeout_s:.0f} s")
                raise ConnectionClosedError(None, None) from None
            self.stats.tx_messages += 1
        return msg

    async def refuse(self, err: p.ProtocolError, *, peer_fault: bool = True) -> bool:
        """Report a refused message to the peer. Returns True if the connection must now close.

        Errors about a received `error` are only logged (section 9). More than max_peer_errors_per_min
        protocol errors from the peer in one minute make the error fatal.
        """
        self.stats.refused[err.code] = self.stats.refused.get(err.code, 0) + 1
        fatal = err.fatal
        if peer_fault:
            now = time.monotonic()
            self._peer_errors.append(now)
            while self._peer_errors and now - self._peer_errors[0] > 60:
                self._peer_errors.popleft()
            if len(self._peer_errors) > self.max_peer_errors_per_min and not fatal:
                fatal = True
                err = p.ProtocolError(err.code, f"too many protocol errors ({len(self._peer_errors)} in 1 min); "
                                      f"last: {err.message}", fatal=True, reply_to=err.reply_to)
        self.log.warning("refused: %s (reply_to=%s, fatal=%s%s): %r", err.code, err.reply_to, fatal,
                         ", not answered" if err.silent else "", err.message)
        if not err.silent:
            self.stats.errors_sent += 1
            try:
                await self.send("error", {"code": err.code, "message": err.message, "fatal": fatal},
                                reply_to=err.reply_to)
            except ConnectionClosed:  # swallowed: the peer is gone; the refusal is already counted and logged
                pass
        return fatal

    # ---- receiving -----------------------------------------------------------------------------

    async def recv(self) -> Received:
        """Next message with its binary data. Raises ProtocolError or ConnectionClosed.

        last_rx (liveness) moves only for frames that pass validation, so garbage does not keep a
        dead or hostile peer alive.
        """
        raw = await self.ws.recv()
        rx = p.now_us()
        if isinstance(raw, bytes):
            raise p.ProtocolError("bad_sequence", f"binary frame of {len(raw)} bytes was not due; dropped")
        if len(raw) > p.MAX_TEXT_BYTES or len(raw.encode("utf-8")) > p.MAX_TEXT_BYTES:
            raise p.ProtocolError("too_large", f"text frame over {p.MAX_TEXT_BYTES} bytes", fatal=True)
        decoded = p.decode(raw)
        if isinstance(decoded, dict) and p.valid_id(decoded.get("id")) in self._recent_set:
            # A repeated id is a replay or a bug; never dispatch it twice (10-F18, T-P0-05-045). The id check
            # runs before validation, so a repeat is refused without being looked at again.
            self.stats.duplicate_ids += 1
            raise p.ProtocolError("bad_sequence", f"id {decoded['id']} was already received; dropped",
                                  reply_to=decoded["id"], silent=decoded.get("type") == "error")
        if isinstance(decoded, dict) and p.valid_id(decoded.get("id")) is not None:
            if decoded["id"] != self._peer_last_id + 1:
                self.stats.id_gaps += 1
                self.log.warning("id gap: expected %d, got %d", self._peer_last_id + 1, decoded["id"])
            self._peer_last_id = decoded["id"]
        msg = p.validate(decoded, sender=self.peer)
        if len(self._recent_ids) == self._recent_ids.maxlen:
            self._recent_set.discard(self._recent_ids[0])
        self._recent_ids.append(msg["id"])
        self._recent_set.add(msg["id"])
        self.stats.rx_messages += 1
        data = None
        if p.has_binary(msg):
            try:
                frame_raw = await asyncio.wait_for(self.ws.recv(), self.binary_timeout_s)
            except TimeoutError:
                raise p.ProtocolError("bad_binary", "binary frame did not arrive", fatal=True,
                                      reply_to=msg["id"]) from None
            if not isinstance(frame_raw, bytes):
                raise p.ProtocolError("bad_binary", "text frame where a binary frame was due", fatal=True,
                                      reply_to=msg["id"])
            frame = p.unpack_binary(frame_raw)
            p.check_pair(msg, frame, sender=self.peer)
            data = frame.data
        self.last_rx = time.monotonic()
        return Received(msg, data, rx, raw)

    # ---- behaviour common to both sides --------------------------------------------------------

    async def handle_common(self, r: Received) -> bool:
        """Resolve requests, answer heartbeats and echo requests, log errors. True if consumed."""
        msg = r.msg
        mtype = msg["type"]
        reply_to = msg["reply_to"]
        if mtype == "error":
            self.stats.errors_received += 1
            pl = msg["payload"]
            self.log.warning("peer error %s (reply_to=%s, fatal=%s): %r", pl["code"], reply_to,
                             pl["fatal"], pl["message"])
        pending = self._requests.get(reply_to) if reply_to is not None else None
        if pending is not None and (mtype == "error" or mtype in pending[1]):
            fut = self._requests.pop(reply_to)[0]
            if not fut.done():
                if mtype == "error":
                    fut.set_exception(RequestError(msg["payload"]["code"], msg["payload"]["message"]))
                else:
                    fut.set_result(r)
            return True
        if mtype == "heartbeat":
            if reply_to is None:
                await self.send("heartbeat", {}, reply_to=msg["id"], other_ts=msg[p.ts_field(self.peer)])
            elif reply_to in self._pending_heartbeats:
                t0 = self._pending_heartbeats.pop(reply_to)  # our own stamp, not the echoed copy
                resets = self.clock.resets
                s = self.clock.add(t0, msg[p.ts_field(self.peer)], r.rx_us)
                if s is not None:
                    if self.clock.resets != resets:
                        self.log.warning("clock: new sample (%.3f ms +/- %.1f) is inconsistent with the estimate; "
                                         "dropped the older samples", s.offset_us / 1000, s.rtt_us / 2000)
                    self._clock_samples += 1
                    level = logging.INFO if self._clock_samples % CLOCK_INFO_EVERY == 1 else logging.DEBUG
                    drift = self.clock.drift_fit_ppm
                    estimate, best = self.clock.estimate_us, self.clock.best
                    if estimate is not None and best is not None:
                        self.log.log(level, "clock offset_ms=%.3f best_rtt_ms=%.2f rtt_ms=%.2f sample_ms=%.3f "
                                     "drift_ppm=%s (%s minus %s)", estimate / 1000, best.rtt_us / 1000,
                                     s.rtt_us / 1000, s.offset_us / 1000,
                                     "none" if drift is None else f"{drift:.1f}", self.peer, self.role)
            return True
        if mtype == "echo" and reply_to is None:
            payload = msg["payload"]
            data = r.data or b""
            if p.crc32(data) != payload["crc32"]:
                self.stats.echo_crc_errors += 1
                await self.refuse(p.ProtocolError("bad_binary", "echo crc32 mismatch", reply_to=msg["id"]))
                return True
            await self.send("echo", dict(payload), reply_to=msg["id"], data=r.data)
            self.stats.echo_answered += 1
            return True
        return bool(mtype == "error")

    async def request(self, mtype: str, payload: dict[str, Any], *, expect: tuple[str, ...] | str, timeout: float = 10.0,
                      data: bytes | None = None) -> Received:
        """Send a message and wait for the peer's answer: a message of a type in `expect` (for example
        "ack", or "image" for capture_image) whose reply_to is its id, or an `error`.

        Raises RequestError for an `error` answer, TimeoutError, or LinkClosed if the connection drops.
        The answer is consumed here and does not reach the normal message handlers; a reply of any other
        type is not taken as the answer.
        """
        expect_set = frozenset((expect,) if isinstance(expect, str) else expect)
        fut = asyncio.get_running_loop().create_future()
        holder: list[int] = []
        try:
            await self.send(mtype, payload, data=data, _register=(fut, expect_set, holder))
            return await asyncio.wait_for(fut, timeout)
        finally:
            if holder:
                self._requests.pop(holder[0], None)

    # ---- background tasks and shutdown ---------------------------------------------------------

    def start_background(self) -> None:
        """Start the heartbeat sender and the liveness watchdog (separate, so a blocked send cannot
        stop the watchdog from noticing a silent peer)."""
        self._tasks += [asyncio.create_task(self._heartbeat_loop()), asyncio.create_task(self._watchdog())]

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat_s)
                msg = await self.send("heartbeat", {
                    "offset_us": None if self.clock.estimate_us is None else round(self.clock.estimate_us),
                    "rtt_us": None if self.clock.best is None else self.clock.best.rtt_us,
                })
                self._pending_heartbeats[msg["id"]] = msg[p.ts_field(self.role)]
                while len(self._pending_heartbeats) > 16:
                    self._pending_heartbeats.pop(next(iter(self._pending_heartbeats)))
        except (ConnectionClosed, asyncio.CancelledError):
            return

    async def _watchdog(self) -> None:
        tick = max(0.02, min(self.heartbeat_s, self.liveness_s) / 4)
        while True:
            await asyncio.sleep(tick)
            silent = time.monotonic() - self.last_rx
            if silent > self.liveness_s:
                self.abort(f"no valid frame for {silent:.1f} s (liveness)")
                return

    def abort(self, reason: str) -> None:
        """Drop the TCP connection at once, without a closing handshake."""
        if self.closed_reason is None:
            self.closed_reason = reason
            self.log.warning("aborting connection: %s", reason)
        transport = getattr(self.ws, "transport", None)
        if transport is not None:
            transport.abort()

    async def close(self, code: int, reason: str, timeout: float = 2.0) -> None:
        if self.closed_reason is None:
            self.closed_reason = f"closed by us: {code} {reason}"
        try:
            await asyncio.wait_for(self.ws.close(code, reason[:120]), timeout)
        except (TimeoutError, ConnectionClosed, OSError):
            self.abort("close handshake timed out")

    def shutdown(self) -> None:
        """Stop background tasks and fail every in-flight request."""
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        for fut, _ in self._requests.values():
            if not fut.done():
                fut.set_exception(LinkClosed(self.closed_reason or "connection closed"))
        self._requests.clear()

    def clock_summary(self) -> str:
        """Section 6 stability, both after a 60 s warm-up: the largest change of the estimate within any 60 s
        window, and the largest deviation of the estimate from a straight line fitted to it over the session
        (both held to 20 ms). The whole-session spread includes real clock drift and is for information."""
        c = self.clock
        estimate = c.estimate_us
        if self._clock_samples == 0 or c.best is None or estimate is None:
            return "clock summary: no samples this session"
        change = c.max_change_us(since_us=self.session_start_us)
        dev = c.max_dev_from_fit_us(since_us=self.session_start_us)
        spread = c.spread_us(since_us=self.session_start_us)
        drift = c.drift_fit_ppm
        return (f"clock summary: samples={self._clock_samples} max_change_60s_ms="
                f"{'n/a (under 2 min of data)' if change is None else f'{change / 1000:.3f}'} "
                f"max_dev_from_fit_ms={'n/a (under 2 min of data)' if dev is None else f'{dev / 1000:.3f}'} "
                f"(limits 20, after 60 s warm-up) drift_ppm={'none' if drift is None else f'{drift:.1f}'} "
                f"estimate_ms={estimate / 1000:.3f} best_rtt_ms={c.best.rtt_us / 1000:.2f} "
                f"session_spread_ms={'n/a' if spread is None else f'{spread / 1000:.3f}'} resets={c.resets}")
