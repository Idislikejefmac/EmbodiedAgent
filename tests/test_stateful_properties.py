"""Stateful property tests (Hypothesis; derandomised in quick mode, see tests/conftest.py profiles).

(a) Body lifecycle on SimBody and LinkBody: random sequences of start, open a stream, close a stream from another
    task, link or capability changes, a body that connects and drops, stop, status, close and restart.
    Invariants: nothing unhandled escapes (a BodyError, ValueError or TypeError at a call is fine), status() never
    raises, every stream ends once the body is closed, no task outlives the body.
(b) The link and handshake state machine on the loopback server: random sequences of hello, auth, heartbeat, event,
    echo, binary, garbage, repeats and floods from an UNAUTHENTICATED peer, beside a body that is paired.
    Invariants: the server survives, answers only with allowed message types, hands nothing from the peer to the
    brain before it has authenticated, and never drops the paired body.
(c) The tool registry: random calls with random callers, channels and hostile arguments.
    Invariants: call() never raises, a refusal is a structured ToolResult with a fixed message, a sensitive tool
    never returns ok on the app channel without confirmed face and voice evidence, and nothing opens a file for
    writing or starts a process (an audit hook watches).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from brain.agent import tools as T
from brain.agent.body_tools import register_body_tools
from brain.agent.tools import Caller, Channel, ToolRegistry, ToolResult
from brain.body.base import BodyError
from brain.body.link import LinkBody
from brain.body.types import ConfidenceBand
from brain.transport import auth
from brain.transport import protocol as p
from brain.transport.client import TestBodyClient
from brain.transport.server import BrainServer

pytestmark = pytest.mark.chaos
FULL = os.environ.get("HYPOTHESIS_PROFILE") == "chaos-full"  # quick: derandomised, a few dozen examples; full: ten times more


def examples(quick: int) -> int:
    return quick * 10 if FULL else quick


FAST = settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture, HealthCheck.data_too_large],
                deadline=None)
ALLOWED = (BodyError, ValueError, TypeError)
TOKEN = b"\x22" * 32


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---- (a) body lifecycle -------------------------------------------------------------------------------------------

SIM_OPS = st.lists(st.sampled_from(["start", "close", "status", "pose", "events", "audio", "close_stream", "link_up", "link_down",
                                    "stop", "capture", "speak", "restart", "close_stream_other_task"]), min_size=1, max_size=25)


async def _drive_sim(body: Any, ops: list[str]) -> None:
    streams: list[Any] = []
    for op in ops:
        try:
            if op == "start":
                await body.start()
            elif op == "close":
                await body.close()
            elif op == "restart":
                await body.close()
                await body.start()
            elif op == "status":
                await body.status()  # must not raise, whatever happened before
            elif op in ("pose", "events", "audio"):
                if not (body.connected or op == "events" and getattr(body, "_running", False)):
                    continue  # a stream opened on a body that is not running is X01B-10 (below), not part of this property
                streams.append({"pose": lambda: body.pose_stream(rate_hz=30.0), "events": body.events,
                                "audio": body.audio_in_stream}[op]())
            elif op == "close_stream" and streams:
                await streams.pop().aclose()
            elif op == "close_stream_other_task" and streams:
                await asyncio.ensure_future(streams.pop().aclose())
            elif op in ("link_up", "link_down"):
                body.set_link(op == "link_up")
            elif op == "stop":
                await body.stop()
            elif op == "capture":
                await body.capture_image(max_edge_px=64)
            elif op == "speak":
                await body.speak_text("hi", utterance_id="u-1")
        except ALLOWED:
            if op == "status":
                raise
    await body.close()
    for s in streams:  # every stream ends once the body is closed
        with contextlib.suppress(StopAsyncIteration):
            await asyncio.wait_for(anext(s), 1.0)
        await s.aclose()
    await asyncio.sleep(0.05)
    leaked = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert not leaked, f"tasks outlived the closed body: {leaked[:3]}"


@settings(FAST, max_examples=examples(30))
@given(SIM_OPS)
def test_sim_body_lifecycle_sequences(chaos_sandbox, ops: list[str]) -> None:
    run(_drive_sim(chaos_sandbox.sim_body(), ops))


@pytest.mark.xfail(strict=True, reason="X01B-10: SimBody.close() on a body that was never started returns early, so an events() stream "
                   "opened before start() never ends (and start() replaces the stream hub, orphaning it for good)")
def test_a_stream_opened_before_start_ends_when_the_body_is_closed(chaos_sandbox) -> None:  # type: ignore[no-untyped-def]
    async def go() -> None:
        body = chaos_sandbox.sim_body()
        s = body.events()
        await body.close()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(s), 1.0)

    run(go())


LINK_OPS = st.lists(st.sampled_from(["start", "close", "status", "connect", "disconnect", "events", "close_stream", "stop",
                                     "restart", "wait", "gated"]), min_size=1, max_size=18)


async def _drive_link(ops: list[str]) -> None:
    body = LinkBody(token=TOKEN, port=0, status_timeout_s=2.0)
    clients: list[Any] = []
    streams: list[Any] = []
    stack = contextlib.AsyncExitStack()
    port = 0
    started = False
    for op in ops:
        try:
            if op == "start":
                await body.start()
                started, port = True, body.port
            elif op == "close":
                await body.close()
                started = False
            elif op == "restart":
                await body.close()
                await body.start()
                started, port = True, body.port
            elif op == "status":
                await asyncio.wait_for(body.status(), 8)
            elif op == "connect" and started:
                c = TestBodyClient(f"ws://127.0.0.1:{port}/", token=TOKEN)
                await asyncio.wait_for(stack.enter_async_context(c), 5)
                clients.append(c)
            elif op == "disconnect" and clients:
                c = clients.pop()
                with contextlib.suppress(Exception):  # broad-except: the client side of a drop may complain; the brain must not
                    await c.__aexit__(None, None, None)
            elif op == "events" and started:  # before start(): X01B-10
                streams.append(body.events())
            elif op == "close_stream" and streams:
                await streams.pop().aclose()
            elif op == "stop":
                await body.stop()
            elif op == "wait":
                await body.wait_connected(0.05)
            elif op == "gated":
                await body.capture_image()
        except (OSError, ConnectionClosed, TimeoutError, auth.AuthError, RuntimeError):
            continue  # a client the closed server cannot reach is the scenario, not a failure
        except ALLOWED:
            if op == "status":
                raise
    for c in clients:
        with contextlib.suppress(Exception):  # broad-except: tear-down of test clients
            await c.__aexit__(None, None, None)
    await body.close()
    for s in streams:
        with contextlib.suppress(StopAsyncIteration):
            await asyncio.wait_for(anext(s), 1.0)
        await s.aclose()
    await asyncio.sleep(0.1)
    leaked = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert not leaked, f"tasks outlived the closed LinkBody: {leaked[:3]}"


@settings(FAST, max_examples=examples(15))
@given(LINK_OPS)
def test_link_body_lifecycle_sequences(ops: list[str]) -> None:
    run(_drive_link(ops))


# ---- (b) the link and handshake state machine ---------------------------------------------------------------------


def _frame(kind: str, n: int) -> str | bytes:
    now = p.now_us()
    if kind == "hello":
        return json.dumps(p.make_message("hello", n, {"role": "body", "software": "peer/0", "capabilities": ["event"],
                                                      "auth": {"nonce": auth.new_nonce()}}, sender=p.BODY, ts=now))
    if kind == "hello_bad":
        return json.dumps(p.make_message("hello", n, {"role": "brain", "software": "x\x07", "capabilities": []}, sender=p.BODY, ts=now))
    if kind == "auth_wrong":
        return json.dumps(p.make_message("auth", n, {"mac": "0" * 64}, sender=p.BODY, ts=now))
    if kind == "heartbeat":
        return json.dumps(p.make_message("heartbeat", n, {}, sender=p.BODY, ts=now))
    if kind == "event":
        return json.dumps(p.make_message("event", n, {"name": "button", "data": {"id": "x"}}, sender=p.BODY, ts=now))
    if kind == "echo":
        return json.dumps(p.make_message("echo", n, {"seq": 0, "text": "t", "byte_length": 0, "crc32": 0}, sender=p.BODY, ts=now))
    if kind == "command":
        return json.dumps(p.make_message("command", n, {"name": "get_status", "args": {}}, sender=p.BODY, ts=now))
    if kind == "binary_garbage":
        return b"\x00\x01" * 20
    if kind == "binary_echo":
        return p.pack_binary("echo", n, now, b"abc")
    if kind == "text_garbage":
        return "{not json"
    if kind == "deep":
        return "[" * 3000
    return "x" * 20_000  # oversize before auth


FRAME_KINDS = ["hello", "hello_bad", "auth_wrong", "heartbeat", "event", "echo", "command", "binary_garbage", "binary_echo",
               "text_garbage", "deep", "oversize"]
PEER_OPS = st.lists(st.tuples(st.sampled_from(FRAME_KINDS), st.integers(1, 3)), min_size=1, max_size=7)
ALLOWED_PRE_AUTH_TYPES = {"hello", "error"}  # the brain says hello, or refuses


async def _attack(ops: list[tuple[str, int]]) -> None:
    received: list[Any] = []

    async def on_message(link: Any, r: Any) -> None:
        received.append(r)

    server = BrainServer("127.0.0.1", 0, token=TOKEN, on_message=on_message)
    await server.start()
    url = f"ws://127.0.0.1:{server.port}/"
    try:
        async with TestBodyClient(url, token=TOKEN) as good:
            await asyncio.sleep(0.05)
            good_link = good.link
            assert len(server.links) == 1
            try:
                async with ws_connect(url, max_size=None) as ws:
                    for i, (kind, repeat) in enumerate(ops):
                        for j in range(repeat):
                            try:
                                await ws.send(_frame(kind, 1 + i * 4 + j))
                            except ConnectionClosed:
                                break
                    got = []
                    with contextlib.suppress(TimeoutError, ConnectionClosed):
                        while True:
                            got.append(await asyncio.wait_for(ws.recv(), 0.15))
                    for frame in got:
                        if isinstance(frame, str):
                            m = json.loads(frame)
                            assert m["type"] in ALLOWED_PRE_AUTH_TYPES, f"an unauthenticated peer was sent a {m['type']!r}"
            except (OSError, ConnectionClosed):
                pass
            # the paired body was not evicted and nothing from the peer reached the brain
            assert len(server.links) == 1 and good_link is not None, "the paired body was dropped"
            assert not [r for r in received if r.type != "heartbeat"], f"unauthenticated frames reached the handler: {[r.type for r in received]}"
            await asyncio.wait_for(good.echo_test(2, 10, timeout_s=3), 8)
    finally:
        await server.stop()


@settings(FAST, max_examples=examples(8))
@given(PEER_OPS)
def test_unauthenticated_peer_sequences_never_break_the_server(ops: list[tuple[str, int]]) -> None:
    run(_attack(ops))


# ---- (c) the tool registry ----------------------------------------------------------------------------------------

_json = st.recursive(
    st.none() | st.booleans() | st.integers(-2**70, 2**70) | st.floats(allow_nan=True, allow_infinity=True) | st.text(max_size=50)
    | st.binary(max_size=8),
    lambda c: st.lists(c, max_size=4) | st.dictionaries(st.text(max_size=8) | st.integers(), c, max_size=4), max_leaves=12)
CATALOGUE = T.load_catalogue()
NAMES = sorted(CATALOGUE.tools) + sorted(CATALOGUE.reserved)
TOOL_NAME = st.sampled_from(NAMES) | st.text(max_size=30) | st.sampled_from(["delete_files", "run_command", "read_file", "stop\n", "GET_POSE"]) | st.none() | st.integers()
CALLER = st.one_of(
    st.just(T.CONSOLE), st.just(T.APP),
    st.builds(lambda band, ev: ("caller", band, ev), st.sampled_from(list(ConfidenceBand)),
              st.sets(st.sampled_from(["voice", "face", "retina", ""]), max_size=3)))
SIDE_EFFECTS: list[str] = []
WATCHING = [False]


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if WATCHING[0] and event in ("subprocess.Popen", "os.system", "os.exec", "os.spawn", "shutil.rmtree", "os.remove", "os.rmdir"):
        SIDE_EFFECTS.append(event)
    if WATCHING[0] and event == "open" and len(args) >= 2 and isinstance(args[1], str) and any(c in args[1] for c in "wax+"):
        SIDE_EFFECTS.append(f"open {args[0]!r} {args[1]}")


sys.addaudithook(_audit)


def _caller(spec: Any) -> Caller | None:
    if isinstance(spec, Caller):
        return spec
    _, band, evidence = spec
    try:
        return Caller(Channel.APP, band, frozenset(evidence))
    except (ValueError, TypeError):
        return None  # a malformed identity is refused at construction


async def _calls(sandbox: Any, calls: list[tuple[Any, Any, Any]]) -> None:
    registry = ToolRegistry(CATALOGUE, timeout_s=1.0)
    body = sandbox.sim_body()
    register_body_tools(registry, body)
    await body.start()
    try:
        for name, args, spec in calls:
            caller = _caller(spec)
            if caller is None:
                continue
            r = await registry.call(name, args, caller)
            assert isinstance(r, ToolResult), f"call returned {type(r).__name__}"
            json.dumps(r.to_json())
            assert r.ok or r.message in T.MESSAGES.values(), "a refusal that is not one of the fixed messages"
            reserved = CATALOGUE.reserved.get(name) if isinstance(name, str) else None
            if reserved is not None and reserved.sensitive and caller.channel is Channel.APP \
                    and not caller.confirmed_by_face_and_voice:
                assert not r.ok, f"{name} ran on the app channel without confirmed face and voice"
            if caller.channel is Channel.APP and isinstance(name, str) and name in CATALOGUE.tools \
                    and Channel.APP not in CATALOGUE.tools[name].channels:
                assert not r.ok, f"{name} ran on the app channel but is console-only"
    finally:
        await body.close()


@settings(FAST, max_examples=examples(40))
@given(st.lists(st.tuples(TOOL_NAME, _json, CALLER), min_size=1, max_size=6))
def test_registry_random_calls_never_raise_and_never_cross_the_boundary(chaos_sandbox, calls: list[tuple[Any, Any, Any]]) -> None:
    SIDE_EFFECTS.clear()
    WATCHING[0] = True
    try:
        run(_calls(chaos_sandbox, calls))
    finally:
        WATCHING[0] = False
    writes = [e for e in SIDE_EFFECTS if not (e.startswith("open") and str(chaos_sandbox.root) in e)]
    assert not writes, f"a tool call had a side effect: {writes[:3]}"
