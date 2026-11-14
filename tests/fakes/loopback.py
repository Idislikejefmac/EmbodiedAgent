"""Loopback helpers for the transport tests (CONVENTIONS C6: doubles live in tests/fakes/).

Moved here unchanged from tests/test_transport_server.py (H-27): an in-process BrainServer on an ephemeral
port, a body client that holds the test token, and raw sessions that put exact frames on the wire.
"""

import asyncio
import json

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from brain.transport import auth
from brain.transport import protocol as p
from brain.transport.client import TestBodyClient
from brain.transport.server import BrainServer

TOKEN = bytes(range(32))
OTHER_TOKEN = bytes(range(1, 33))


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


async def with_server(fn, token=TOKEN, **kw):
    server = await BrainServer("127.0.0.1", 0, token=token, **kw).start()
    try:
        return await fn(server, f"ws://127.0.0.1:{server.port}/")
    finally:
        await server.stop()


class LoopbackBody(TestBodyClient):
    """The body side of a loopback link (doc 00 section 5): a TestBodyClient that holds the test token by default."""

    def __init__(self, url, **kw):
        kw.setdefault("token", TOKEN)
        super().__init__(url, **kw)


body = LoopbackBody  # the short name the transport tests use


async def wait_for(cond, timeout=3.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


def the_link(server):
    assert len(server.links) == 1
    return next(iter(server.links.values()))


# ---- raw sessions (bypass the client to put exact frames on the wire) ------------------------------

def raw(mtype, msg_id, payload, **kw):
    return json.dumps(p.make_message(mtype, msg_id, payload, sender=p.BODY, ts=kw.pop("ts", p.now_us()), **kw),
                      ensure_ascii=False)


async def raw_handshake(ws, token=TOKEN, *, hello_text=None, mac=None, hello_payload=None):
    """hello / hello / auth / auth by hand. Returns (brain hello, the brain's answer to auth, our mac, hello text)."""
    payload = hello_payload or {"role": "body", "software": "raw", "capabilities": [],
                                "auth": {"nonce": auth.new_nonce()}}
    hello_text = hello_text or raw("hello", 1, payload)
    await ws.send(hello_text)
    bh_text = await ws.recv()
    bh = json.loads(bh_text)
    if bh["type"] != "hello":
        return bh, None, None, hello_text
    mac = mac or auth.proof(token, p.BODY, [auth.digest(hello_text), auth.digest(bh_text)])
    await ws.send(raw("auth", 2, {"mac": mac}))
    return bh, json.loads(await ws.recv()), mac, hello_text


async def collect(ws, quiet=0.5):
    got = []
    try:
        while True:
            got.append(json.loads(await asyncio.wait_for(ws.recv(), quiet)))
    except TimeoutError:
        pass
    except ConnectionClosed:
        await asyncio.wait_for(ws.wait_closed(), 2)  # close_code is set once TCP is down too
    return got


async def raw_session(url, frames, quiet=0.5):
    """Send raw frames after a valid handshake; collect errors and replies until the server closes or goes quiet."""
    async with ws_connect(url) as ws:
        bh, ack, _, _ = await raw_handshake(ws)
        assert ack["type"] == "auth", ack
        for f in frames:
            try:
                await ws.send(f)
            except ConnectionClosed:
                break
        got = [m for m in await collect(ws, quiet) if m["type"] != "heartbeat"]
        return got, ws.close_code


def error_codes(got):
    return [m["payload"]["code"] for m in got if m["type"] == "error"]
