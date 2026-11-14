"""Loopback proofs for the transport (hardening H-25 and H-26): zero loss over 1,000 echoes, and no message
dispatched twice when a peer repeats or reorders ids."""

import asyncio
import json
import time

import pytest
from websockets.asyncio.client import connect as ws_connect

from brain.transport import auth
from brain.transport import protocol as p
from brain.transport.server import BrainServer
from fakes import TOKEN, body, collect, raw, raw_handshake, run, with_server

ECHO_ACCEPT_BUDGET_S = 60  # Provisional (doc 10, T-P0-05-006): coarse, so a busy laptop does not fail it


@pytest.mark.trace("T-P0-05-006")
def test_1000_echoes_of_100_kib_arrive_with_zero_loss_and_intact_bytes():
    """T-P0-05-006: the plan's acceptance, automated. Every echo is checked by crc32 and length."""
    async def go(server, url):
        async with body(url) as c:
            t0 = time.monotonic()
            res = await c.echo_test(count=1000, size=100 * 1024)
            elapsed = time.monotonic() - t0
            assert (res.sent, res.received, res.lost, res.corrupt) == (1000, 1000, 0, 0), res
            assert res.ok and res.bytes_each == 100 * 1024
            assert elapsed < ECHO_ACCEPT_BUDGET_S, elapsed
            assert c.link.stats.id_gaps == 0 and next(iter(server.links.values())).stats.id_gaps == 0
            assert next(iter(server.links.values())).stats.echo_crc_errors == 0
    run(with_server(go))


async def _session(url, frames_after_auth, *, on_message):
    """A raw body: handshake, then the given (type, id, payload) frames exactly as written; returns what the
    brain sent back (errors) once it goes quiet."""
    async with ws_connect(url) as ws:
        _, ack, _, _ = await raw_handshake(ws)
        assert ack["type"] == "auth"
        for mtype, msg_id, payload in frames_after_auth:
            await ws.send(raw(mtype, msg_id, payload))
        return [m for m in await collect(ws, 0.6) if m["type"] != "heartbeat"]


def event(name):
    return {"name": name, "data": {}}


@pytest.mark.trace("T-P0-05-045")
def test_a_repeated_or_reordered_id_never_dispatches_the_same_message_twice():
    """T-P0-05-045 (H-25, 10-F18): ids 3, 3, 5, 4, 4, 3 arrive; each id is dispatched at most once, the repeats are
    refused (bad_sequence, not fatal), and the out-of-order 4 after 5 is accepted once and counted as a gap."""
    dispatched: list[tuple[int, str]] = []

    async def on_message(link, r):
        dispatched.append((r.msg["id"], r.msg["payload"]["name"]))

    async def go():
        server = await BrainServer("127.0.0.1", 0, token=TOKEN, on_message=on_message).start()
        try:
            got = await _session(f"ws://127.0.0.1:{server.port}/", [
                ("event", 3, event("first")), ("event", 3, event("replay_of_3")), ("event", 5, event("five")),
                ("event", 4, event("late_four")), ("event", 4, event("replay_of_4")), ("event", 3, event("again_3"))],
                on_message=on_message)
            return got
        finally:
            await server.stop()
    got = asyncio.run(asyncio.wait_for(go(), 30))
    assert dispatched == [(3, "first"), (5, "five"), (4, "late_four")]
    errors = [m["payload"] for m in got if m["type"] == "error"]
    assert [e["code"] for e in errors] == ["bad_sequence"] * 3 and not any(e["fatal"] for e in errors)


def test_a_repeated_answer_resolves_a_request_once():
    """A command-bearing exchange: the brain sends one request; the body answers with the same ack twice. The
    request completes once, and the repeat does not reach the message handler."""
    seen_acks: list[int] = []

    async def on_message(link, r):
        if r.type == "ack":
            seen_acks.append(r.msg["id"])

    async def go():
        server = await BrainServer("127.0.0.1", 0, token=TOKEN, on_message=on_message).start()
        try:
            async with ws_connect(f"ws://127.0.0.1:{server.port}/") as ws:
                _, ack, _, _ = await raw_handshake(ws)
                assert ack["type"] == "auth"
                for _ in range(100):
                    if server.links:
                        break
                    await asyncio.sleep(0.02)
                link = next(iter(server.links.values()))
                task = asyncio.create_task(link.request("command", {"name": "get_status", "args": {}}, expect="ack"))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
                    if msg["type"] == "command":
                        break
                answer = raw("ack", 3, {"status": "ok", "result": {}}, reply_to=msg["id"])
                await ws.send(answer)
                await ws.send(answer)  # the same frame again: same id
                result = await asyncio.wait_for(task, 5)
                await collect(ws, 0.4)
                return result, link.stats
        finally:
            await server.stop()
    result, stats = asyncio.run(asyncio.wait_for(go(), 30))
    assert result.msg["id"] == 3 and seen_acks == []  # consumed by the request, once; the repeat refused
    assert stats.refused.get("bad_sequence") == 1
