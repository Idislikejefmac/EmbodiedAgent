"""Brain WebSocket server against the Python test client, in-process on an ephemeral port."""

import asyncio
import json
import logging

import pytest
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from brain.__main__ import is_loopback
from brain.transport import auth
from brain.transport import protocol as p
from brain.transport.client import TestBodyClient
from brain.transport.clock import ClockOffsetEstimator
from brain.transport.link import LinkClosed, RequestError
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


def body(url, **kw):
    kw.setdefault("token", TOKEN)
    return TestBodyClient(url, **kw)


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
        got =[m for m in await collect(ws, quiet) if m["type"] != "heartbeat"]
        return got, ws.close_code


def error_codes(got):
    return [m["payload"]["code"] for m in got if m["type"] == "error"]


# ---- handshake and authentication ------------------------------------------------------------------

def test_client_connects_authenticates_and_gets_hello_reply():
    async def go(server, url):
        async with body(url) as c:
            h = c.brain_hello
            assert p.schema_errors(h) == []
            assert h["type"] == "hello" and h["reply_to"] == 1 and h["id"] == 1
            pl = h["payload"]
            assert pl["role"] == "brain" and pl["session_id"] and pl["versions"] == [0]
            assert pl["boot_id"] == server.boot_id
            assert h["ts_brain"] is not None and h["ts_body"] is None
            assert the_link(server).authenticated
    run(with_server(go))


def test_wrong_token_is_refused():
    async def go(server, url):
        with pytest.raises(auth.AuthError, match="unauthorized"):
            async with body(url, token=OTHER_TOKEN):
                pass
        assert server.links == {}
    run(with_server(go))


def test_body_without_a_token_is_refused():
    async def go(server, url):
        with pytest.raises(auth.AuthError, match="not paired"):
            async with body(url, token=None):
                pass
        # A hello without an auth nonce gets unauthorized, fatal, close 1008.
        async with ws_connect(url) as ws:
            bh, _, _, _ = await raw_handshake(ws, hello_payload={"role": "body", "software": "raw",
                                                                 "capabilities": []})
            assert bh["type"] == "error" and bh["payload"]["code"] == "unauthorized" and bh["payload"]["fatal"]
            await collect(ws)
            assert ws.close_code == p.CLOSE_AUTH_FAILED
        assert server.links == {}
    run(with_server(go))


def test_replayed_auth_is_refused():
    async def go(server, url):
        async with ws_connect(url) as ws:
            _, answer, mac, hello_text = await raw_handshake(ws)
            assert answer["type"] == "auth"
        # The same hello text and the recorded proof on a new connection: the brain's hello (nonce, session)
        # is fresh, so the transcript differs and the proof fails.
        async with ws_connect(url) as ws:
            _, answer, _, _ = await raw_handshake(ws, hello_text=hello_text, mac=mac)
            assert answer["type"] == "error" and answer["payload"]["code"] == "unauthorized"
            await collect(ws)
            assert ws.close_code == p.CLOSE_AUTH_FAILED
    run(with_server(go))


def test_proof_covers_the_hello_fields():
    """A proof made over one body hello does not validate a different hello (for example, other commands)."""
    async def go(server, url):
        async with ws_connect(url) as ws:
            payload = {"role": "body", "software": "raw", "capabilities": [], "commands": ["get_status"],
                       "auth": {"nonce": auth.new_nonce()}}
            sent = raw("hello", 1, payload)
            tampered = raw("hello", 1, {**payload, "commands": []})
            await ws.send(sent)
            bh_text = await ws.recv()
            mac = auth.proof(TOKEN, p.BODY, [auth.digest(tampered), auth.digest(bh_text)])
            await ws.send(raw("auth", 2, {"mac": mac}))
            answer = json.loads(await ws.recv())
            assert answer["payload"]["code"] == "unauthorized"
    run(with_server(go))


class _AcceptAnyBody:
    """Stand-in for the auth module in a rogue brain that accepts any body proof."""
    def __getattr__(self, name):
        return getattr(auth, name)

    @staticmethod
    def check(*a):
        return True


def test_rogue_brain_is_detected_by_the_body(caplog, monkeypatch):
    """A brain without the token: either it refuses our proof (unauthorized), or, if it pretends to accept,
    its own proof fails our check. Either way no session starts, and nobody answers an error with an error."""
    from brain.transport import server as server_mod

    async def go(server, url):
        with pytest.raises(auth.AuthError, match="unauthorized"):
            async with body(url, token=TOKEN):
                pass
        monkeypatch.setattr(server_mod, "auth", _AcceptAnyBody())
        with pytest.raises(auth.AuthError, match="brain failed authentication"):
            async with body(url, token=TOKEN):
                pass
        await asyncio.sleep(0.2)
    with caplog.at_level(logging.INFO):
        run(with_server(go, token=OTHER_TOKEN))
    assert any("peer error unauthorized" in r.getMessage() and r.name.startswith("brain.transport.s-")
               for r in caplog.records)  # logged, not answered
    brain_refusals = [r.getMessage() for r in caplog.records
                      if r.name.startswith("brain.transport.s-") and r.getMessage().startswith("refused:")]
    assert all("authentication failed" in m for m in brain_refusals)  # only the first case; nothing else


def test_hmac_known_answer_vectors_match_the_kotlin_body():
    """Same vectors as LinkLogicTest.hmacProofMatchesThePythonBrain."""
    t = bytes(range(32))
    assert auth.proof(t, "body", ["aa" * 32, "bb" * 32]) == \
        "361f76624d4ad34c13145c8beb4d0af5485f0bfa6c9ec01147b51af80f0d1715"
    assert auth.proof(t, "brain", ["aa" * 32, "bb" * 32, "cc" * 32]) == \
        "00e75be5aa0f7100fedccde85f835bb4cd24f399bc4fae1d260c1bff6ab1819e"
    assert auth.digest("hello") == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    # UTF-8 framing of non-ASCII text (also checked with .NET).
    assert auth.digest("héllo ✓ 你好") == \
        "f3db413984a06d69e3e0f5fed463b2a20cd9b960f4fe1e0effcb44f5301f06cd"
    # One full transcript from literal frames, as in LinkLogicTest.fullTranscriptFromLiteralFrames.
    bh = ('{"v":0,"type":"hello","id":1,"reply_to":null,"ts_body":1,"ts_brain":null,"payload":{"role":"body",'
          '"software":"t","capabilities":[],"auth":{"nonce":"' + "aa" * 32 + '"}}}')
    rh = ('{"v":0,"type":"hello","id":1,"reply_to":1,"ts_body":null,"ts_brain":2,"payload":{"role":"brain",'
          '"software":"t","capabilities":[],"session_id":"s-1","auth":{"nonce":"' + "bb" * 32 + '"}}}')
    body_mac = auth.proof(t, "body", [auth.digest(bh), auth.digest(rh)])
    assert body_mac == "d639f06b20ca6144b318be527312c1c29b4b2f2746b6cdedf9bda454fd53c847"
    ba = '{"v":0,"type":"auth","id":2,"reply_to":null,"ts_body":3,"ts_brain":null,"payload":{"mac":"' + body_mac + '"}}'
    assert auth.proof(t, "brain", [auth.digest(bh), auth.digest(rh), auth.digest(ba)]) == \
        "cc7eb754d8b2c94088ffc88312d29d7d4693b3948dd5636a0d3079976f8d6fb1"


def test_proof_is_role_and_transcript_bound():
    tr = [auth.digest("a"), auth.digest("b")]
    brain_mac = auth.proof(TOKEN, p.BRAIN, tr)
    assert auth.check(TOKEN, p.BRAIN, tr, brain_mac)
    assert not auth.check(TOKEN, p.BODY, tr, brain_mac)  # no reflection
    assert not auth.check(TOKEN, p.BRAIN, [auth.digest("a"), auth.digest("c")], brain_mac)
    assert not auth.check(OTHER_TOKEN, p.BRAIN, tr, brain_mac)
    assert not auth.check(TOKEN, p.BRAIN, tr, None)


def test_nothing_is_processed_between_hello_and_auth():
    """Before the body's auth, any other frame ends the handshake; none is acted on."""
    seen = []

    async def handler(link, r):
        seen.append(r.type)

    async def go(server, url):
        for frame in [raw("echo", 2, {"seq": 0, "text": "x", "byte_length": 0, "crc32": 0}),
                      raw("heartbeat", 2, {}),
                      raw("event", 2, {"name": "x", "data": {}}),
                      p.pack_binary("echo", 2, 5, b"abc")]:
            async with ws_connect(url) as ws:
                await ws.send(raw("hello", 1, {"role": "body", "software": "raw", "capabilities": [],
                                               "auth": {"nonce": auth.new_nonce()}}))
                assert json.loads(await ws.recv())["type"] == "hello"
                await ws.send(frame)
                got = await collect(ws)
                assert [m["type"] for m in got] == ["error"], got  # no echo reply, no heartbeat, nothing else
                assert got[0]["payload"]["fatal"] and ws.close_code in (1002, 1008)
        assert seen == [] and server.links == {}
    run(with_server(go, on_message=handler))


def test_an_unauthenticated_client_cannot_evict_the_body():
    async def go(server, url):
        async with body(url) as victim:
            async with ws_connect(url) as attacker:
                _, answer, _, _ = await raw_handshake(attacker, token=OTHER_TOKEN)
                assert answer["payload"]["code"] == "unauthorized"
            async with ws_connect(url) as idle:  # a hello and then nothing
                await idle.send(raw("hello", 1, {"role": "body", "software": "raw", "capabilities": [],
                                                 "auth": {"nonce": auth.new_nonce()}}))
                await idle.recv()
                assert the_link(server).peer_hello["boot_id"] == victim.boot_id
                assert (await victim.echo_test(count=3, size=1000)).ok
            assert victim.link.ws.close_code is None
    run(with_server(go))


def test_idle_handshakes_cannot_lock_a_body_out():
    """4 idle unauthenticated sockets fill the slots; a real body still gets in (the oldest is evicted)."""
    async def go(server, url):
        idle = [await ws_connect(url) for _ in range(4)]
        try:
            await wait_for(lambda: server.pending_handshakes == 4)
            async with body(url) as c:
                assert (await c.echo_test(count=3, size=100_000)).ok
                assert server.pending_handshakes <= 4
            # At least one idle socket was evicted to make room.
            closed = 0
            for ws in idle:
                try:
                    await asyncio.wait_for(ws.recv(), 0.5)
                except (ConnectionClosed, TimeoutError):
                    closed += ws.close_code is not None or ws.state.name in ("CLOSING", "CLOSED")
            assert closed >= 1
        finally:
            for ws in idle:
                await ws.close()
    run(with_server(go))


def test_a_socket_that_sends_nothing_is_dropped_after_two_seconds():
    async def go(server, url):
        async with ws_connect(url) as ws:
            t = asyncio.get_running_loop().time()
            got = await collect(ws, quiet=4)
            assert got and got[0]["payload"]["code"] == "bad_sequence"
            assert ws.close_code == 1002 and asyncio.get_running_loop().time() - t < 3.5
    run(with_server(go))


def test_large_frames_are_refused_before_authentication():
    async def go(server, url):
        async with ws_connect(url, max_size=None) as ws:
            await ws.send("x" * 100_000)
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 3)
            assert ws.close_code == 1009
        async with body(url) as c:  # after auth, 100 kB frames are fine
            assert (await c.echo_test(count=2, size=100_000)).ok
    run(with_server(go))


def test_server_refuses_to_start_without_the_pre_auth_limit(monkeypatch):
    import websockets.server

    class NoLimit:
        def __init__(self, *a, **kw):
            pass
    monkeypatch.setattr(websockets.server, "ServerProtocol", NoLimit)
    with pytest.raises(RuntimeError, match="max_message_size"):
        BrainServer.check_library()


def test_peer_strings_cannot_forge_log_lines(caplog):
    async def go(server, url):
        async with ws_connect(url) as ws:
            forged = "x\nINFO brain.transport.server: body authenticated; session FAKE"
            await ws.send(raw("hello", 1, {"role": "body", "software": forged, "capabilities": [],
                                           "auth": {"nonce": auth.new_nonce()}}))
            answer = json.loads(await ws.recv())
            assert answer["payload"]["code"] == "bad_payload"  # control characters are not allowed
        async with ws_connect(url) as ws:
            await raw_handshake(ws, hello_payload={"role": "body", "software": "x y\"z", "capabilities": [],
                                                   "auth": {"nonce": auth.new_nonce()}})
    with caplog.at_level(logging.INFO):
        run(with_server(go))
    assert not any("\n" in r.getMessage() for r in caplog.records)


def test_numbers_must_be_written_as_integers():
    async def go(server, url):
        frames = ['{"v":0,"type":"echo","id":3.0,"reply_to":null,"ts_body":5,"ts_brain":null,'
                  '"payload":{"seq":0,"text":"","byte_length":0,"crc32":0}}',
                  '{"v":0.0,"type":"echo","id":4,"reply_to":null,"ts_body":5,"ts_brain":null,'
                  '"payload":{"seq":0,"text":"","byte_length":0,"crc32":0}}']
        got, _ = await raw_session(url, frames[:1])
        assert error_codes(got) == ["bad_envelope"] and got[-1]["reply_to"] is None  # never reply_to: 3.0
    run(with_server(go))
    with pytest.raises(p.ProtocolError) as e:
        p.validate({"v": 0.0, "type": "heartbeat", "id": 1, "reply_to": None, "ts_body": 1, "ts_brain": None,
                    "payload": {}})
    assert e.value.code == "unsupported_version"


def test_trailing_newlines_do_not_pass_patterns():
    base = {"v": 0, "type": "hello", "id": 1, "reply_to": None, "ts_body": 1, "ts_brain": None}
    for payload in ({"role": "body", "software": "x", "capabilities": [], "boot_id": "b-1\n"},
                    {"role": "body", "software": "x\n", "capabilities": []},
                    {"role": "body", "software": "x", "capabilities": ["echo\n"]},
                    {"role": "body", "software": "x", "capabilities": [], "auth": {"nonce": "aa" * 32 + "\n"}}):
        assert p.schema_errors({**base, "payload": payload}), payload
    assert p.schema_errors({**base, "type": "auth", "payload": {"mac": "aa" * 32 + "\n"}})


def test_token_file_is_created_private_once_and_rotates(tmp_path, caplog):
    store = tmp_path / "secrets"
    with caplog.at_level(logging.WARNING):
        t1 = auth.load_or_create_token(store)
        caplog.clear()
        t2 = auth.load_or_create_token(store)
    assert t1 == t2 and len(t1) == 32
    assert not any("pair the body again" in r.getMessage() for r in caplog.records)  # unchanged token: silent
    assert auth.acl_problems(auth.token_path(store)) == []
    assert auth.acl_problems(store) == []
    t3 = auth.load_or_create_token(store, rotate=True)
    assert t3 != t1 and auth.load_or_create_token(store) == t3
    with pytest.raises(ValueError):
        auth.parse_token("abc")


def test_legacy_token_files_are_deleted_without_changing_the_current_token(tmp_path, caplog):
    store = tmp_path / "secrets"
    t1 = auth.load_or_create_token(store)
    old = tmp_path / "link_token"
    old.write_text("11" * 32)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert auth.load_or_create_token(store, legacy=(old,)) == t1
    assert not old.exists()
    msgs = [r.getMessage() for r in caplog.records]
    assert any("deleted the old token file" in m for m in msgs)
    assert not any("pair the body again" in m for m in msgs)


def test_a_token_file_others_can_open_or_a_corrupt_one_is_refused(tmp_path):
    import os
    import subprocess
    store = auth.ensure_store(tmp_path / "secrets")
    planted = auth.token_path(store)
    planted.write_text("00" * 32)
    if os.name == "nt":  # give another principal access, as a planted or exposed file would have
        assert subprocess.run([auth._icacls(), str(planted), "/grant", "*S-1-5-32-545:R"],
                              capture_output=True).returncode == 0  # BUILTIN\Users
    else:
        os.chmod(planted, 0o644)
    fd = os.open(planted, os.O_RDONLY)
    try:
        assert auth.fd_problems(fd)  # checked on the open handle, not by path
    finally:
        os.close(fd)
    with pytest.raises(auth.TokenStoreError, match="has access|lets others in"):
        auth.load_or_create_token(store)
    planted.unlink()
    planted.write_text("not a token")
    with pytest.raises(auth.TokenStoreError, match="valid token"):
        auth.load_or_create_token(store)


def test_cli_reports_token_store_problems_without_a_traceback(tmp_path, capsys):
    from brain import __main__ as cli
    bad = tmp_path / "secrets"
    auth.ensure_store(bad)
    auth.token_path(bad).write_text("junk")
    token, problem = cli.load_token({"paths": {"secrets_dir": str(bad), "data_dir": str(tmp_path / "data")}})
    assert token is None and "valid token" in problem


def test_hello_role_must_match_the_sender():
    async def go(server, url):
        async with ws_connect(url) as ws:
            bh, _, _, _ = await raw_handshake(ws, hello_payload={"role": "brain", "software": "x",
                                                                 "capabilities": [], "session_id": "s-1",
                                                                 "auth": {"nonce": auth.new_nonce()}})
            assert bh["type"] == "error" and bh["payload"]["code"] == "bad_payload" and bh["payload"]["fatal"]
    run(with_server(go))


def test_hello_with_unknown_capabilities_and_commands_is_accepted():
    async def go(server, url):
        async with body(url, capabilities=("event", "echo", "teleport"), commands=("get_status", "fly")) as c:
            assert (await c.echo_test(count=1, size=10)).ok
    run(with_server(go))


def test_hello_without_version_0_is_refused():
    async def go(server, url):
        async with ws_connect(url) as ws:
            bh, _, _, _ = await raw_handshake(ws, hello_payload={"role": "body", "software": "x", "capabilities": [],
                                                              "versions": [1],
                                                              "auth": {"nonce": auth.new_nonce()}})
            assert bh["payload"]["code"] == "unsupported_version"
    run(with_server(go))


def test_first_message_must_be_hello():
    async def go(server, url):
        async with ws_connect(url) as ws:
            await ws.send(raw("heartbeat", 1, {}))
            err = json.loads(await ws.recv())
            assert err["payload"]["code"] == "bad_sequence" and err["payload"]["fatal"]
            with pytest.raises(ConnectionClosed):
                await ws.recv()
            assert ws.close_code == 1002
    run(with_server(go))


def test_origin_header_is_refused():
    async def go(server, url):
        with pytest.raises(InvalidStatus) as e:
            async with ws_connect(url, origin="http://evil.example"):
                pass
        assert e.value.response.status_code == 403
    run(with_server(go))


def test_non_loopback_detection():
    assert is_loopback("127.0.0.1") and is_loopback("localhost") and is_loopback("::1")
    assert not is_loopback("0.0.0.0") and not is_loopback("192.168.1.5") and not is_loopback("example.com")


def test_newest_body_replaces_the_older_one():
    async def go(server, url):
        async with body(url) as c1:
            async with body(url) as c2:
                await asyncio.wait_for(c1.link.ws.wait_closed(), 3)
                assert c1.link.ws.close_code == p.CLOSE_REPLACED
                assert the_link(server).peer_hello["boot_id"] == c2.boot_id
                assert (await c2.echo_test(count=2, size=10)).ok
    run(with_server(go))


# ---- heartbeats, clocks, echo -----------------------------------------------------------------------

def test_heartbeats_flow_both_ways_and_give_a_clock_offset():
    async def go(server, url):
        async with body(url, heartbeat_s=0.1) as c:
            await asyncio.sleep(0.8)
            brain_link = the_link(server)
            # Same machine, same clock: the offset is about 0 and within half the RTT.
            for link in (c.link, brain_link):
                assert len(link.clock.history) >= 3
                assert abs(link.clock.estimate_us) <= link.clock.best.rtt_us / 2 + 1000
            assert brain_link.stats.id_gaps == 0 and c.link.stats.id_gaps == 0
    run(with_server(go, heartbeat_s=0.1))


def test_clock_estimate_is_kept_across_reconnects_of_the_same_body_run():
    async def go(server, url):
        async with body(url, heartbeat_s=0.1, boot_id="b-same") as c:
            await wait_for(lambda: len(the_link(server).clock.history) >= 3)
            est = the_link(server).clock
        async with body(url, heartbeat_s=0.1, boot_id="b-same"):
            assert the_link(server).clock is est
        async with body(url, heartbeat_s=0.1, boot_id="b-other"):
            assert the_link(server).clock is not est
    run(with_server(go, heartbeat_s=0.1))


def test_echo_json_and_100kb_binary_round_trip_intact():
    async def go(server, url):
        async with body(url) as c:
            res = await c.echo_test(count=50, size=100 * 1024)
            assert res.ok, res
            assert res.received == 50
    run(with_server(go))


def test_server_restart_accepts_a_new_session():
    async def go():
        server = await BrainServer("127.0.0.1", 0, token=TOKEN).start()
        port = server.port
        url = f"ws://127.0.0.1:{port}/"
        async with body(url) as c:
            first = c.brain_hello["payload"]["session_id"]
        await server.stop()
        server = await BrainServer("127.0.0.1", port, token=TOKEN).start()
        try:
            async with body(url) as c:
                assert c.brain_hello["payload"]["session_id"] != first
                assert c.brain_hello["id"] == 1  # ids restart per connection
        finally:
            await server.stop()
    run(go())


def test_server_stop_closes_with_1001_and_logs_the_clock_summary(caplog):
    async def go():
        server = await BrainServer("127.0.0.1", 0, token=TOKEN, heartbeat_s=0.1).start()
        async with body(f"ws://127.0.0.1:{server.port}/", heartbeat_s=0.1) as c:
            await wait_for(lambda: len(the_link(server).clock.history) >= 5)
            await server.stop()
            await asyncio.wait_for(c.link.ws.wait_closed(), 3)
            assert c.link.ws.close_code == p.CLOSE_GOING_AWAY
    with caplog.at_level(logging.INFO):
        run(go())
    assert any("clock summary: samples=" in r.getMessage() for r in caplog.records)
    assert any("server stopped" in r.getMessage() for r in caplog.records)


# ---- liveness ---------------------------------------------------------------------------------------

def test_liveness_timeout_closes_a_silent_peer():
    async def go(server, url):
        async with ws_connect(url) as ws:
            await raw_handshake(ws)
            with pytest.raises(ConnectionClosed):
                while True:
                    await asyncio.wait_for(ws.recv(), 3)  # read heartbeats, never answer
    run(with_server(go, heartbeat_s=0.1, liveness_s=0.3))


def test_watchdog_fires_even_when_the_peer_stops_reading():
    """The brain's sends block once the peer stops reading; the separate watchdog must still close it."""
    async def go(server, url):
        async with ws_connect(url, max_queue=1, max_size=None) as ws:
            await raw_handshake(ws)
            brain_link = the_link(server)
            blob = bytes(1024 * 1024)

            async def flood():
                seq = 0
                while True:
                    await brain_link.send("echo", {"seq": seq, "text": "", "byte_length": len(blob),
                                                   "crc32": p.crc32(blob)}, data=blob)
                    seq += 1

            sender = asyncio.create_task(flood())
            await asyncio.wait_for(brain_link.ws.wait_closed(), 5)
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(sender, 3)
            assert "liveness" in brain_link.closed_reason
    run(with_server(go, heartbeat_s=0.1, liveness_s=0.5))


# ---- refusals ---------------------------------------------------------------------------------------

def test_body_may_not_send_commands_to_the_brain():
    async def go(server, url):
        async with body(url) as c:
            with pytest.raises(ValueError):  # our own side refuses to send it
                await c.link.send("command", {"name": "get_status", "args": {}})
            msg = await c.link.send("command", {"name": "get_status", "args": {}}, check=False)
            r = await c.next_message()
            assert r.type == "error" and r.msg["reply_to"] == msg["id"]
            assert r.msg["payload"]["code"] == "wrong_direction" and not r.msg["payload"]["fatal"]
            assert (await c.echo_test(count=1, size=10)).ok  # still usable
    run(with_server(go))


def test_body_side_rejects_unknown_and_smuggled_commands():
    async def go(server, url):
        async with body(url) as c:
            brain_link = the_link(server)
            for i, (payload, code) in enumerate([
                ({"name": "delete_files", "args": {"path": "C:\\"}}, "unknown_command"),
                ({"name": "get_status", "args": {"cmd": "dir"}}, "bad_args"),
                ({"name": "capture_image", "args": {"max_edge": 640}}, "not_implemented"),
            ]):
                await brain_link.send("command", payload, check=False)
                assert await wait_for(lambda: brain_link.stats.errors_received > i)
                assert brain_link.stats.errors_received == i + 1
            ack = await brain_link.request("command", {"name": "get_status", "args": {}}, expect="ack")
            assert ack.type == "ack" and ack.msg["payload"]["result"]["battery_pct"] == 100
    run(with_server(go))


def test_outbound_messages_are_validated():
    async def go(server, url):
        async with body(url) as c:
            before = c.link.stats.tx_messages
            for mtype, payload in [("event", {"name": "Bad Name", "data": {}}),
                                   ("pose", {"frame_id": "../etc", "position_m": [0, 0, 0],
                                             "orientation_xyzw": [0, 0, 0, 1], "tracking": "ok"}),
                                   ("event", {"name": "x", "data": {"v": float("nan")}})]:
                with pytest.raises(ValueError):
                    await c.link.send(mtype, payload)
            assert c.link.stats.tx_messages == before
            brain_link = the_link(server)
            with pytest.raises(ValueError):
                await brain_link.send("error", {"code": "internal", "message": "x", "fatal": False},
                                      reply_to=0)
    run(with_server(go))


def test_bad_frames_get_errors_and_binary_mismatch_is_fatal():
    async def go(server, url):
        echo = p.make_message("echo", 6, {"seq": 0, "text": "", "byte_length": 3, "crc32": p.crc32(b"abc")},
                              sender=p.BODY, ts=5)
        frames = [
            "not json",
            raw("shell", 4, {}),
            b"\x00\x04stray binary",
            json.dumps(echo),
            p.pack_binary("echo", 99, 5, b"abc"),  # wrong id: fatal
        ]
        got, close_code = await raw_session(url, frames)
        assert error_codes(got) == ["bad_json", "unknown_type", "bad_sequence", "bad_binary"]
        assert got[-1]["payload"]["fatal"] is True
        assert close_code == 1002
    run(with_server(go))


def test_nan_and_infinity_are_refused():
    async def go(server, url):
        frames = ['{"v":0,"type":"event","id":3,"reply_to":null,"ts_body":5,"ts_brain":null,'
                  '"payload":{"name":"x","data":{"a":NaN}}}',
                  '{"v":0,"type":"event","id":4,"reply_to":null,"ts_body":5,"ts_brain":null,'
                  '"payload":{"name":"x","data":{"a":-Infinity}}}']
        got, close_code = await raw_session(url, frames)
        assert error_codes(got) == ["bad_json", "bad_json"] and close_code is None
    run(with_server(go))


def test_text_cap_counts_utf8_bytes_not_characters():
    async def go(server, url):
        big = raw("event", 3, {"name": "x", "data": {"s": "\u00e9" * 40_000}})  # 40k chars, 80k bytes
        assert len(big) < p.MAX_TEXT_BYTES < len(big.encode("utf-8"))
        got, close_code = await raw_session(url, [big])
        assert error_codes(got) == ["too_large"] and got[-1]["payload"]["fatal"] and close_code == 1002
    run(with_server(go))


def test_deep_nesting_is_bad_json_and_not_fatal():
    async def go(server, url):
        got, close_code = await raw_session(url, ["[" * 5000 + "]" * 5000, raw("echo", 4, {
            "seq": 0, "text": "after", "byte_length": 0, "crc32": 0})])
        assert error_codes(got) == ["bad_json"] and close_code is None
        assert any(m["type"] == "echo" and m["payload"]["text"] == "after" for m in got)
    run(with_server(go))


def test_sender_must_stamp_its_own_timestamp():
    with pytest.raises(p.ProtocolError) as e:
        p.validate(p.make_message("pose", 3, {"frame_id": "w-1", "position_m": [0, 0, 0],
                                              "orientation_xyzw": [0, 0, 0, 1], "tracking": "ok"},
                                  sender=p.BRAIN, ts=1), sender=p.BODY)
    assert e.value.code in ("wrong_direction", "bad_envelope")

    async def go(server, url):
        msg = p.make_message("echo", 3, {"seq": 0, "text": "", "byte_length": 3, "crc32": p.crc32(b"abc")},
                             sender=p.BODY, ts=5)
        msg["ts_body"] = None
        got, close_code = await raw_session(url, [json.dumps(msg), raw("echo", 4, {
            "seq": 1, "text": "ok", "byte_length": 0, "crc32": 0})])
        assert error_codes(got)[0] == "bad_envelope"
        assert any(m["type"] == "echo" and m["payload"]["text"] == "ok" for m in got)
    run(with_server(go))


def test_an_invalid_error_is_logged_but_never_answered():
    async def go(server, url):
        got, close_code = await raw_session(url, [
            raw("error", 3, {"code": "no_such_code", "message": "x", "fatal": False}),
            raw("echo", 4, {"seq": 0, "text": "alive", "byte_length": 0, "crc32": 0})])
        assert error_codes(got) == [] and close_code is None
        assert any(m["type"] == "echo" for m in got)
    run(with_server(go))


def test_too_many_protocol_errors_close_the_link():
    async def go(server, url):
        got, close_code = await raw_session(url, ["garbage"] * 21)  # limit is 20 per minute
        assert close_code == 1002
        assert got[-1]["payload"]["fatal"] is True and len(error_codes(got)) == 21
    run(with_server(go))


# ---- handlers and requests --------------------------------------------------------------------------

def test_handler_exception_answers_internal_and_the_link_survives():
    seen = []

    async def handler(link, r):
        if r.msg["payload"]["name"] == "boom":
            raise RuntimeError("handler bug")
        seen.append(r.msg["payload"]["name"])

    async def go(server, url):
        async with body(url) as c:
            m = await c.link.send("event", {"name": "boom", "data": {}})
            r = await c.next_message()
            assert r.type == "error" and r.msg["payload"]["code"] == "internal" and r.msg["reply_to"] == m["id"]
            await c.link.send("event", {"name": "fine", "data": {}})
            assert await wait_for(lambda: seen == ["fine"])
            assert (await c.echo_test(count=1, size=10)).ok
    run(with_server(go, on_message=handler))


def test_a_handler_may_await_a_request_without_deadlock():
    results = []

    async def handler(link, r):
        ack = await link.request("command", {"name": "get_status", "args": {}}, expect="ack", timeout=3)
        results.append(ack.msg["payload"]["result"]["charging"])

    async def go(server, url):
        async with body(url) as c:
            await c.link.send("event", {"name": "ask", "data": {}})
            assert await wait_for(lambda: results == [True])
    run(with_server(go, on_message=handler))


def test_request_helper_resolves_errors_timeouts_and_disconnects():
    async def go(server, url):
        async with body(url) as c:
            link = the_link(server)
            with pytest.raises(RequestError) as e:
                await link.request("command", {"name": "capture_image", "args": {"max_edge": 640}}, expect="image")
            assert e.value.code == "not_implemented"
            speak = {"utterance_id": "u-1", "text": "hi", "interrupt": False}
            with pytest.raises(TimeoutError):
                await link.request("speak_local", speak, expect="ack", timeout=0.2)  # the test body never answers it
            assert link._requests == {}
            pending = asyncio.create_task(link.request("speak_local", speak, expect="ack", timeout=10))
            await asyncio.sleep(0.1)
        with pytest.raises(LinkClosed):
            await asyncio.wait_for(pending, 3)
    run(with_server(go))


# ---- clock estimator --------------------------------------------------------------------------------

def test_clock_estimator_picks_min_rtt_sample():
    est = ClockOffsetEstimator()
    true_offset = 1_000_000
    # Symmetric 2 ms path, plus one sample delayed 50 ms on the way back (asymmetric: biased).
    est.add(0, true_offset + 1000, 2000)
    est.add(10_000, true_offset + 11_000, 62_000)
    assert est.estimate_us == pytest.approx(true_offset)
    assert est.best.rtt_us == 2000
    assert est.spread_us() == 0


def test_clock_estimate_holds_through_a_long_burst_of_delayed_samples():
    """The P0-05 failure: 2 minutes of 0.3 to 1.35 s RTTs under load, all delayed on one leg."""
    est = ClockOffsetEstimator()
    off = 5_000_000
    t = 0
    for _ in range(30):  # idle: 20 ms symmetric
        est.add(t, off + t + 10_000, t + 20_000)
        t += 2_000_000
    idle = est.estimate_us
    for i in range(60):  # load: return leg delayed up to 1.33 s
        rtt = 300_000 + i * 17_500
        est.add(t, off + t + 10_000, t + rtt)
        t += 2_000_000
    assert abs(est.estimate_us - idle) < 20_000
    assert est.estimate_us == pytest.approx(off)


def test_clock_estimate_follows_drift_with_fresh_samples():
    est = ClockOffsetEstimator(aging_ppm=50)
    est.add(0, 10_000, 20_000)  # rtt 20 ms, offset 0
    # 20 minutes later the offset has drifted 30 ms; fresh 24 ms samples now beat the aged 20 ms one.
    t = 1_200_000_000
    est.add(t, t + 30_000 + 12_000, t + 24_000)
    assert est.estimate_us == pytest.approx(30_000)


def _drifting_link(est, *, ppm, minutes, rtt_us=20_000, start_us=0, offset_us=5_000_000, seed=1):
    """Heartbeats every 2 s over a symmetric path (plus random queueing on either leg) between clocks
    whose offset drifts at `ppm`. Returns the true offset function."""
    import random
    rng = random.Random(seed)

    def true_offset(t):
        return offset_us + ppm * 1e-6 * t

    t = start_us
    for _ in range(int(minutes * 30)):
        up, down = rtt_us / 2 + rng.expovariate(1 / 15_000), rtt_us / 2 + rng.expovariate(1 / 15_000)
        t1 = t + up + true_offset(t + up)
        est.add(t, round(t1), round(t + up + down))
        t += 2_000_000
    return true_offset, t


def test_clock_drift_is_fitted_and_followed():
    """S25 Edge vs this laptop drift about -28.6 ppm (13 Nov 2026)."""
    est = ClockOffsetEstimator()
    true_offset, t_end = _drifting_link(est, ppm=-28.6, minutes=20)
    assert est.drift_fit_ppm == pytest.approx(-28.6, abs=3)
    assert abs(est.estimate_us - true_offset(est.now_us)) < 5_000
    # Max-minus-min over the whole run includes the real drift (about 34 ms in 20 min) ...
    assert est.spread_us() > 20_000
    # ... but the section 6 stability metric (largest change within any 60 s, after 60 s) is small.
    assert est.max_change_us() < 20_000


def test_clock_consistency_check_drops_a_different_peer():
    est = ClockOffsetEstimator()
    _drifting_link(est, ppm=0, minutes=5)
    t = est.now_us + 2_000_000
    est.add(t, t + 10_000 + 250_000_000, t + 20_000)  # another body reusing the boot_id: 250 s off
    assert est.resets == 1
    assert est.estimate_us == pytest.approx(250_000_000 + 0, abs=20_000)


def test_one_bogus_low_rtt_sample_is_undone_by_the_next_good_one():
    est = ClockOffsetEstimator()
    true_offset, _ = _drifting_link(est, ppm=0, minutes=5)
    t = est.now_us + 2_000_000
    est.add(t, round(t + 1_000 + true_offset(t) + 80_000), t + 2_000)  # 80 ms off, claims 2 ms RTT
    t += 2_000_000
    est.add(t, round(t + 10_000 + true_offset(t)), t + 20_000)  # good again
    assert est.resets == 2
    assert abs(est.estimate_us - true_offset(t)) < 10_000


def test_clock_history_is_bounded():
    est = ClockOffsetEstimator(history_cap=100)
    _drifting_link(est, ppm=0, minutes=10)
    assert len(est.history) == 100 and est.count == 300


def shared_clock_samples():
    """600 heartbeats 2 s apart, -28.6 ppm drift, integer queueing pattern, one minute of load.
    Identical to LinkLogicTest.sharedClockSamples in the Kotlin suite."""
    for i in range(600):
        t = i * 2_000_000
        up = 10_000 + (i * 7919 % 37) * 1000
        down = 10_000 + (i * 104729 % 41) * 1000
        if 200 <= i < 230:
            down += 400_000
        x = t + up
        yield t, x + 5_000_000 - (x * 286) // 10_000_000, t + up + down


def test_shared_clock_vector_matches_the_kotlin_body():
    est = ClockOffsetEstimator()
    for s in shared_clock_samples():
        est.add(*s)
    assert est.estimate_us == pytest.approx(4964788.187875573, abs=0.01)
    assert est.drift_fit_ppm == pytest.approx(-28.017090186765994, abs=1e-6)
    assert est.resets == 0 and est.best.rtt_us == 22000
    assert est.max_change_us() == pytest.approx(7314.878631321713, abs=0.01)
    assert est.max_dev_from_fit_us() == pytest.approx(3797.1683047330007, abs=0.01)


def test_max_deviation_from_the_fitted_line_ignores_steady_drift():
    est = ClockOffsetEstimator()
    _drifting_link(est, ppm=-28.6, minutes=20)
    assert est.spread_us() > 20_000  # the literal max-minus-min fails because of real drift
    assert est.max_dev_from_fit_us() < 10_000
    assert ClockOffsetEstimator().max_dev_from_fit_us() is None


def test_an_empty_estimator_is_still_reused():
    from brain.transport.link import Link
    est = ClockOffsetEstimator()
    assert len(est) == 0
    assert Link(object(), p.BRAIN, clock=est).clock is est
