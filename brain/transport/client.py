"""A protocol v0 test client that plays the body's side. Used by tests and `python -m brain probe`.

It is a generic body simulator, not any particular body: it authenticates, answers heartbeats, rejects
unknown commands, implements only `get_status`, and can run the echo test.
"""

from __future__ import annotations

import asyncio
import logging
import random
import secrets
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from ..limits import HANDSHAKE_TIMEOUT_S, MAX_FRAME_BYTES, WS_RECV_QUEUE_FRAMES
from . import auth
from . import protocol as p
from .link import Link, Received

log = logging.getLogger("brain.transport.client")


@dataclass
class EchoResult:
    sent: int = 0
    received: int = 0
    lost: int = 0
    corrupt: int = 0
    bytes_each: int = 0
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.sent > 0 and self.lost == 0 and self.corrupt == 0 and self.received == self.sent


class TestBodyClient:
    __test__ = False  # not a pytest class

    def __init__(self, url: str, *, token: bytes | None, heartbeat_s: float = p.HEARTBEAT_MS / 1000,
                 liveness_s: float = p.LIVENESS_TIMEOUT_MS / 1000, commands: tuple[str, ...] = ("get_status",),
                 capabilities: tuple[str, ...] = ("event", "echo"), boot_id: str | None = None):
        self.url = url
        self.token = token
        self.heartbeat_s = heartbeat_s
        self.liveness_s = liveness_s
        self.commands = commands
        self.capabilities = capabilities
        self.boot_id = boot_id or f"b-{secrets.token_hex(4)}"
        self.link: Link | None = None
        self.brain_hello: dict[str, Any] | None = None
        self.inbox: asyncio.Queue[Received] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []

    async def __aenter__(self) -> TestBodyClient:
        if self.token is None:
            raise auth.AuthError("not paired: no link token")
        ws = await ws_connect(self.url, max_size=MAX_FRAME_BYTES, max_queue=WS_RECV_QUEUE_FRAMES,
                              ping_interval=None, compression=None)
        link = self.link = Link(ws, p.BODY, log=log, heartbeat_s=self.heartbeat_s, liveness_s=self.liveness_s)
        try:
            await self._handshake()
        except BaseException:  # broad-except: close the socket on any failed handshake (even cancellation), then re-raise
            await link.close(p.CLOSE_NORMAL, "handshake failed")
            raise
        self._tasks = [asyncio.create_task(self._recv_loop())]
        link.start_background()
        return self

    @property
    def _live(self) -> Link:
        """The link, once the client has been entered."""
        if self.link is None:
            raise RuntimeError("the test body is not connected: use it inside `async with`")
        return self.link

    async def _handshake(self) -> None:
        link = self._live
        token = self.token
        if token is None:
            raise auth.AuthError("not paired: no link token")
        body_nonce = auth.new_nonce()
        hello = await link.send("hello", {
            "role": p.BODY, "software": "test-body/0", "versions": [p.PROTOCOL_VERSION], "boot_id": self.boot_id,
            "capabilities": list(self.capabilities), "commands": list(self.commands),
            "auth": {"nonce": body_nonce}})
        hello_text = link.last_sent_text
        r = await asyncio.wait_for(link.recv(), HANDSHAKE_TIMEOUT_S)
        if r.type == "error":
            raise auth.AuthError(f"brain refused hello: {r.msg['payload']['code']}: {r.msg['payload']['message']}")
        if r.type != "hello" or r.msg["reply_to"] != hello["id"]:
            raise RuntimeError(f"expected hello reply, got {r.type}")
        transcript = [auth.digest(hello_text), auth.digest(r.raw)]
        am = await link.send("auth", {"mac": auth.proof(token, p.BODY, transcript)})
        transcript.append(auth.digest(link.last_sent_text))
        r2 = await asyncio.wait_for(link.recv(), HANDSHAKE_TIMEOUT_S)
        if r2.type == "error":
            raise auth.AuthError(f"brain refused auth: {r2.msg['payload']['code']}: {r2.msg['payload']['message']}")
        if r2.type != "auth" or r2.msg["reply_to"] != am["id"]:
            raise RuntimeError(f"expected the brain's auth, got {r2.type}")
        if not auth.check(token, p.BRAIN, transcript, r2.msg["payload"]["mac"]):
            await link.refuse(p.ProtocolError("unauthorized", "the brain failed authentication",
                                              reply_to=r2.msg["id"], fatal=True), peer_fault=False)
            await link.close(p.CLOSE_AUTH_FAILED, "unauthorized")
            raise auth.AuthError("the brain failed authentication (wrong token or not our brain)")
        link.authenticated = True
        self.brain_hello = r.msg

    async def __aexit__(self, *exc: object) -> None:
        for t in self._tasks:
            t.cancel()
        if self.link:
            self.link.shutdown()
            await self.link.close(p.CLOSE_NORMAL, "done")

    async def _recv_loop(self) -> None:
        link = self._live
        try:
            while True:
                try:
                    r = await link.recv()
                except p.ProtocolError as e:
                    if await link.refuse(e):
                        await link.close(p.CLOSE_PROTOCOL_ERROR, e.code)
                        return
                    continue
                if r.type == "error":
                    await link.handle_common(r)
                    await self.inbox.put(r)
                    continue
                if await link.handle_common(r):
                    continue
                if r.type == "command":
                    await self._command(r)
                    continue
                await self.inbox.put(r)
        except ConnectionClosed:
            return
        finally:
            link.shutdown()

    async def _command(self, r: Received) -> None:
        """The body side of the command boundary. validate() already refused unknown names and bad args."""
        name = r.msg["payload"]["name"]
        if name == "get_status" and name in self.commands:
            await self._live.send("ack", {"status": "ok", "result": {"battery_pct": 100, "charging": True}},
                                 reply_to=r.msg["id"])
        else:
            await self._live.refuse(p.ProtocolError("not_implemented", f"{name} not implemented by this body",
                                                   reply_to=r.msg["id"]), peer_fault=False)

    async def next_message(self, timeout: float = 5.0) -> Received:
        return await asyncio.wait_for(self.inbox.get(), timeout)

    async def echo_test(self, count: int, size: int, *, window: int = 4, timeout_s: float = 15.0,
                        seed: int = 1) -> EchoResult:
        """Send `count` echo requests of `size` random bytes, at most `window` in flight; check each reply."""
        loop = asyncio.get_running_loop()
        res = EchoResult(bytes_each=size)
        rng = random.Random(seed)
        pending: dict[int, tuple[int, str, bytes]] = {}
        start = loop.time()
        seq = 0
        while res.received + res.corrupt + res.lost < count:
            while seq < count and len(pending) < window:
                data = rng.randbytes(size)
                text = f"echo {seq} {secrets.token_hex(8)}"
                msg = await self._live.send("echo", {"seq": seq, "text": text, "byte_length": size,
                                                    "crc32": p.crc32(data)}, data=data)
                pending[msg["id"]] = (seq, text, data)
                res.sent += 1
                seq += 1
            try:
                r = await self.next_message(timeout_s)
            except TimeoutError:
                res.lost += len(pending)
                pending.clear()
                break
            if r.type not in ("echo", "error") or r.msg["reply_to"] not in pending:
                continue
            want_seq, want_text, want_data = pending.pop(r.msg["reply_to"])
            pl = r.msg["payload"]
            if (r.type == "echo" and pl["seq"] == want_seq and pl["text"] == want_text
                    and (r.data or b"") == want_data and pl["crc32"] == p.crc32(want_data)):
                res.received += 1
            else:
                res.corrupt += 1
        res.lost += count - res.sent
        res.seconds = loop.time() - start
        return res
