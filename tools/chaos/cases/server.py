"""P0-05 link: the real BrainServer on a loopback port, driven by raw frames and the test body client (B1, B2).

Steady state after every disturbance: a paired body can still connect and authenticate within
STEADY_TIMEOUT_S, and the server never crashes. The token is the sandbox's canary token, so the secret
scan proves it never reaches a log line.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from brain.limits import PEER_LOG_LINES_PER_MIN_MAX
from brain.transport import auth
from brain.transport import protocol as p
from brain.transport import server as server_mod
from brain.transport.client import TestBodyClient
from brain.transport.server import BrainServer

from .. import fuzz
from ..api import Case, Ctx, Gen, Violation, experiment, failure, hostile, invalid, valid
from ..thresholds import TIMER_TOLERANCE_MS

STEADY_TIMEOUT_S = 3.0
FLOOD_CASE_OTHER_LINES = 25  # log lines in the flood case that are not the flooding connection's (see gen_failure)


def raw(mtype: str, msg_id: int, payload: dict[str, Any], **kw: Any) -> str:
    msg = p.make_message(mtype, msg_id, payload, sender=p.BODY, ts=kw.pop("ts", p.now_us()), **kw)
    return json.dumps(msg, ensure_ascii=False)


def body_hello(software: str = "chaos-body/0", nonce: str | None = None) -> dict[str, Any]:
    """A body hello. Generators pass a fixed nonce so the same seed gives byte-identical cases."""
    return {"role": "body", "software": software, "capabilities": ["event"],
            "auth": {"nonce": nonce or auth.new_nonce()}}


FIXED_NONCE = "0" * 64
FIXED_TS = 1_000_000


async def collect(ws: Any, quiet: float = 0.5) -> tuple[list[dict[str, Any]], int | None]:
    got: list[dict[str, Any]] = []
    try:
        while True:
            got.append(json.loads(await asyncio.wait_for(ws.recv(), quiet)))
    except TimeoutError:
        pass
    except ConnectionClosed:
        try:
            await asyncio.wait_for(ws.wait_closed(), 2)
        except TimeoutError:
            pass
    return got, ws.close_code


async def handshake(ws: Any, token: bytes, hello_payload: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    """hello / hello / auth / auth by hand. Returns (the brain's answer to auth, our auth text)."""
    hello_text = raw("hello", 1, hello_payload or body_hello())
    await ws.send(hello_text)
    bh_text = await asyncio.wait_for(ws.recv(), 3)
    bh = json.loads(bh_text)
    if bh["type"] != "hello":
        return bh, ""
    mac = auth.proof(token, p.BODY, [auth.digest(hello_text), auth.digest(bh_text)])
    auth_text = raw("auth", 2, {"mac": mac})
    await ws.send(auth_text)
    return json.loads(await asyncio.wait_for(ws.recv(), 3)), auth_text


async def steady(ctx: Ctx, url: str, token: bytes) -> None:
    """The steady state: a paired body authenticates within STEADY_TIMEOUT_S."""
    try:
        async def go() -> None:
            async with TestBodyClient(url, token=token) as c:
                if c.brain_hello is None:
                    raise RuntimeError("no hello from the brain")
        await asyncio.wait_for(go(), STEADY_TIMEOUT_S)
    except (TimeoutError, OSError, ConnectionClosed, auth.AuthError, RuntimeError) as e:
        raise Violation("bounded_resources", f"steady state lost: a paired body could not authenticate "
                                             f"({type(e).__name__}: {str(e)[:120]})") from None


def errors(got: list[dict[str, Any]]) -> list[str]:
    return [m["payload"]["code"] for m in got if m.get("type") == "error"]


# ---- scenarios ---------------------------------------------------------------------------------------------


async def sc_handshake(ctx: Ctx, server: BrainServer, url: str, d: dict[str, Any]) -> Any:
    async with TestBodyClient(url, token=ctx.sandbox.token) as c:
        ctx.expect(len(server.links) == 1, "right_to_refuse", "no authenticated link after a valid handshake")
        if d.get("echo"):
            res = await c.echo_test(d["echo"], d.get("bytes", 1000), timeout_s=5)
            ctx.expect(res.ok, "validate_before_use", f"echo test failed: {res}")
            return {"echo": res.received}
    return "authenticated"


async def sc_first_frame(ctx: Ctx, server: BrainServer, url: str, d: dict[str, Any]) -> Any:
    async with ws_connect(url) as ws:
        await ws.send(d["frame"])
        got, code = await collect(ws)
    errs = errors(got)
    ctx.expect(errs and errs[0] in d["expect"], "validate_before_use", f"first frame answered with {errs or got[:1]}")
    ctx.expect(code is not None, "validate_before_use", "the server kept a connection whose first frame was bad")
    ctx.expect(not server.links, "right_to_refuse", "a bad first frame produced an authenticated link")
    return errs


async def sc_oversize(ctx: Ctx, server: BrainServer, url: str, d: dict[str, Any]) -> Any:
    async with ws_connect(url, max_size=None) as ws:
        try:
            await ws.send("x" * d["size"])
        except ConnectionClosed:
            pass
        got, code = await collect(ws)
    ctx.expect(code == 1009, "bounded_resources", f"an oversize frame before auth gave close code {code}, not 1009")
    return code


async def sc_wrong_mac(ctx: Ctx, server: BrainServer, url: str, d: dict[str, Any]) -> Any:
    async with ws_connect(url) as ws:
        hello_text = raw("hello", 1, body_hello())
        await ws.send(hello_text)
        json.loads(await asyncio.wait_for(ws.recv(), 3))
        await ws.send(raw("auth", 2, {"mac": d["mac"]}))
        got, code = await collect(ws)
    want_code, want_close = d.get("expect", ("unauthorized", p.CLOSE_AUTH_FAILED))
    ctx.expect(want_code in errors(got) and code == want_close, "right_to_refuse",
               f"a wrong proof gave {errors(got)} and close {code}, expected {want_code} and {want_close}")
    ctx.expect(not server.links, "right_to_refuse", "a wrong proof produced an authenticated link")
    return code


async def sc_replayed_auth(ctx: Ctx, server: BrainServer, url: str, d: dict[str, Any]) -> Any:
    async with ws_connect(url) as ws:
        ack, old_auth = await handshake(ws, ctx.sandbox.token)
        ctx.expect(ack["type"] == "auth", "no_crash", f"the first handshake failed: {ack.get('type')}")
    async with ws_connect(url) as ws:
        await ws.send(raw("hello", 1, body_hello()))
        await asyncio.wait_for(ws.recv(), 3)
        await ws.send(old_auth)  # a proof over the previous session's transcript
        got, code = await collect(ws)
    ctx.expect("unauthorized" in errors(got), "right_to_refuse", f"a replayed proof gave {errors(got)}")
    return code


async def sc_after_auth(ctx: Ctx, server: BrainServer, url: str, d: dict[str, Any]) -> Any:
    async with ws_connect(url) as ws:
        ack, _ = await handshake(ws, ctx.sandbox.token)
        ctx.expect(ack["type"] == "auth", "no_crash", f"handshake failed: {ack.get('type')}")
        for i, (mtype, payload) in enumerate(d["frames"]):
            await ws.send(raw(mtype, 3 + i, payload))
        got, code = await collect(ws, d.get("quiet", 0.5))
    errs = errors(got)
    if d.get("expect"):
        ctx.expect(any(e in d["expect"] for e in errs), "validate_before_use", f"answered with {errs}")
    return {"errors": errs[:5], "close": code}


async def sc_hello_text(ctx: Ctx, server: BrainServer, url: str, d: dict[str, Any]) -> Any:
    async with ws_connect(url) as ws:
        await ws.send(raw("hello", 1, body_hello(d["software"])))
        got, code = await collect(ws)
    first = got[0] if got else {}
    ctx.expect(first.get("type") == "error" and first["payload"]["code"] == "bad_payload", "validate_before_use",
               f"a hello whose software holds a control character was answered with {first.get('type')!r}, "
               "not refused at the gate")
    return errors(got)


async def sc_idle_lockout(ctx: Ctx, server: BrainServer, url: str, d: dict[str, Any]) -> Any:
    idle = [await ws_connect(url) for _ in range(d["idle"])]
    try:
        await asyncio.sleep(0.1)
        await steady(ctx, url, ctx.sandbox.token)
    finally:
        for ws in idle:
            await ws.close()
    return f"{d['idle']} idle sockets did not lock the body out"


async def sc_disconnect(ctx: Ctx, server: BrainServer, url: str, d: dict[str, Any]) -> Any:
    ws = await ws_connect(url)
    await ws.send(raw("hello", 1, body_hello()))
    await ws.close()
    await asyncio.sleep(0.05)
    return "closed mid-handshake"


async def sc_slow_auth(ctx: Ctx, server: BrainServer, url: str, d: dict[str, Any]) -> Any:
    async with ws_connect(url) as ws:
        t0 = time.monotonic()
        await ws.send(raw("hello", 1, body_hello()))
        got, code = await collect(ws, quiet=d["hello_timeout_s"] * 4)
        elapsed = time.monotonic() - t0
    limit = d["hello_timeout_s"] + 0.5 + 2 * TIMER_TOLERANCE_MS / 1000
    ctx.note(f"handshake timeout closed the socket after {elapsed * 1000:.0f} ms")
    ctx.expect(code is not None and elapsed <= limit, "bounded_resources",
               f"a stalled handshake was closed after {elapsed:.2f} s (limit {limit:.2f}), close {code}")
    return code


SCENARIOS = {"handshake": sc_handshake, "first_frame": sc_first_frame, "oversize": sc_oversize,
             "wrong_mac": sc_wrong_mac, "replayed_auth": sc_replayed_auth, "after_auth": sc_after_auth,
             "hello_text": sc_hello_text, "idle_lockout": sc_idle_lockout, "disconnect": sc_disconnect,
             "slow_auth": sc_slow_auth}


# ---- generators ----------------------------------------------------------------------------------------------


def gen_valid(g: Gen) -> list[Case]:
    return [valid("handshake", {"scenario": "handshake"}),
            valid("echo_small", {"scenario": "handshake", "echo": g.n(3, 50), "bytes": 2000}),
            valid("event_and_heartbeat", {"scenario": "after_auth", "frames": [
                ("event", {"name": "button", "data": {"id": "poke"}}), ("heartbeat", {})]})]


def gen_invalid(g: Gen) -> list[Case]:
    hello_no_nonce = raw("hello", 1, {"role": "body", "software": "x", "capabilities": []}, ts=FIXED_TS)
    return [
        invalid("first_frame_garbage", {"scenario": "first_frame", "frame": "not json", "expect": {"bad_json"}}, "N1"),
        invalid("first_frame_empty", {"scenario": "first_frame", "frame": "", "expect": {"bad_json"}}, "N3"),
        invalid("first_frame_deep", {"scenario": "first_frame", "frame": fuzz.nested_json_text(5000),
                                     "expect": {"bad_json", "bad_envelope", "bad_payload"}}, "N4"),
        invalid("first_frame_nan", {"scenario": "first_frame", "frame": '{"v":0,"type":"hello","id":1,"x":NaN}',
                                    "expect": {"bad_json"}}, "N6"),
        invalid("first_frame_not_hello", {"scenario": "first_frame", "frame": raw("heartbeat", 1, {}, ts=FIXED_TS),
                                          "expect": {"bad_sequence"}}, "N3"),
        invalid("hello_without_nonce", {"scenario": "first_frame", "frame": hello_no_nonce,
                                        "expect": {"unauthorized"}}, "N3"),
        invalid("oversize_before_auth", {"scenario": "oversize", "size": server_mod.PRE_AUTH_MAX_BYTES + 1}, "N2"),
        invalid("software_bell", {"scenario": "hello_text", "software": "body\u0007"}, "N5"),
    ]


def gen_hostile(g: Gen) -> list[Case]:
    return [
        hostile("forged_log_hello", {"scenario": "hello_text",
                                     "software": "body\nINFO brain.transport.server: body authenticated; session s-x"},
                "N7"),
        hostile("wrong_mac", {"scenario": "wrong_mac", "mac": "0" * 64}, "N9"),
        hostile("mac_not_hex", {"scenario": "wrong_mac", "mac": "G" * 64,  # refused by the schema first
                                "expect": ("bad_payload", p.CLOSE_PROTOCOL_ERROR)}, "N9"),
        hostile("replayed_auth", {"scenario": "replayed_auth"}, "N9"),
        hostile("command_from_body", {"scenario": "after_auth", "expect": {"wrong_direction"}, "frames": [
            ("command", {"name": "get_status", "args": {}})]}, "N16", "N8"),
        hostile("hello_after_auth", {"scenario": "after_auth", "expect": {"bad_sequence"}, "frames": [
            ("hello", body_hello(nonce=FIXED_NONCE))]}, "N8"),
        hostile("unknown_type_after_auth", {"scenario": "after_auth", "expect": {"unknown_type"}, "frames": [
            ("shell", {"cmd": "whoami"})]}, "N16"),
    ]


def gen_failure(g: Gen) -> list[Case]:
    out = [
        failure("idle_handshake_lockout", {"scenario": "idle_lockout", "idle": server_mod.MAX_PENDING_HANDSHAKES + 2},
                "N11"),
        failure("disconnect_mid_handshake", {"scenario": "disconnect"}, "N12"),
        failure("slow_auth", {"scenario": "slow_auth", "hello_timeout_s": 0.5}, "N12"),
        failure("event_with_3000_keys", {"scenario": "after_auth", "frames": [
            ("event", {"name": "sensor", "data": fuzz.many_keys(3000)})]}, "N11"),
        # H-09 / T-P0-05-047, fixed: events over the per-type rate are dropped and counted, and the flooding
        # connection's log lines are capped at PEER_LOG_LINES_PER_MIN_MAX (one of them the "suppressed" line,
        # plus its two end-of-session summaries). The limit is per connection; this case also holds the
        # steady-state connection and the server's own start and stop lines (about 15), hence the allowance.
        # Before the fix the case logged over 150 lines.
        failure("heartbeat_flood", {"scenario": "after_auth", "expect": {"busy"}, "frames": [
            ("heartbeat", {}) for _ in range(40)]}, "N11"),  # H-09: over the heartbeat rate is answered `busy`
        failure("event_flood", {"scenario": "after_auth", "quiet": 1.0, "frames": [
            ("event", {"name": "tick", "data": {"n": i}}) for i in range(150)]}, "N11",
            max_log_lines=PEER_LOG_LINES_PER_MIN_MAX + FLOOD_CASE_OTHER_LINES),
    ]
    return out


@experiment(
    id="P0-05.server.session", card="P0-05",
    entries=("ws_option.*", "close_code.*", "gate.brain.transport.link.Link.recv",
             "gate.brain.transport.flood.FloodGuard", "cli_command.brain.serve",
             "cli_command.brain.probe", "net_module.brain/transport/*"),
    hypothesis="After any frame or connection pattern, the server keeps running, refuses what is invalid with "
               "the right error and close code, logs bounded lines, and a paired body can still authenticate "
               f"within {STEADY_TIMEOUT_S:g} s.",
    blast_radius="loopback", valid=gen_valid, invalid=gen_invalid, hostile=gen_hostile, failure=gen_failure)
async def check(case: Case, ctx: Ctx) -> Any:
    d = case.data
    server = BrainServer("127.0.0.1", 0, token=ctx.sandbox.token, hello_timeout_s=d.get("hello_timeout_s", 3.0))
    await server.start()
    url = f"ws://127.0.0.1:{server.port}/"
    try:
        out = await SCENARIOS[d["scenario"]](ctx, server, url, d)
        if d["scenario"] != "idle_lockout":
            await steady(ctx, url, ctx.sandbox.token)
        return out
    finally:
        await server.stop()
