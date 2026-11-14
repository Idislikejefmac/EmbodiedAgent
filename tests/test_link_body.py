"""PhoneBody / LinkBody over the real protocol server on loopback, with a scripted test body playing the
phone. The real phone is not needed (and not touched): `phone_link=False` skips its port forward.
Also the body factory and the ReplayBody stub."""

import asyncio

import pytest

from brain.body import BodyUnavailable, Capability, NavGoal, NotSupported, NotYetImplemented, create_body
from brain.body.link import LinkBody
from brain.body.phone import PhoneBody
from brain.body.replay import ReplayBody
from brain.body.sim import SimBody
from brain.body.types import AudioChunk
from brain.transport import protocol as p
from brain.transport.client import TestBodyClient
from brain.transport.clock import ClockOffsetEstimator

TOKEN = bytes(range(32))


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


class ScriptedBody(TestBodyClient):
    """A test body whose answer to each command is supplied by the test."""

    def __init__(self, url, answer, **kw):
        super().__init__(url, token=TOKEN, heartbeat_s=0.1, liveness_s=3.0, **kw)
        self.answer = answer
        self.commands_seen = []

    async def _command(self, r):
        self.commands_seen.append(r.msg["payload"]["name"])
        await self.answer(self, r)


def ack(result):
    async def answer(client, r):
        await client.link.send("ack", {"status": "ok", "result": result}, reply_to=r.msg["id"])
    return answer


def error(code):
    async def answer(client, r):
        await client.link.refuse(p.ProtocolError(code, "scripted", reply_to=r.msg["id"]), peer_fault=False)
    return answer


async def with_phone(fn, answer=None, *, connect=True, **client_kw):
    body = PhoneBody(token=TOKEN, port=0, phone_link=False, heartbeat_s=0.1, liveness_s=3.0, status_timeout_s=0.4)
    await body.start()
    try:
        if not connect:
            return await fn(body, None)
        async with ScriptedBody(f"ws://127.0.0.1:{body.port}/", answer or ack({}), **client_kw) as client:
            assert await body.wait_connected(5.0)
            return await fn(body, client)
    finally:
        await body.close()


# ---- status --------------------------------------------------------------------------------------------

def test_status_reads_what_the_body_reports_and_ignores_the_rest():
    async def go(body, client):
        s = await body.status()
        assert (s.connected, s.battery_pct, s.charging, s.temperature_c) == (True, 81.0, False, 36.5)
        assert s.capabilities == () and client.commands_seen == ["get_status"]
        assert not hasattr(s, "os_version")

    run(with_phone(go, ack({"battery_pct": 81, "charging": False, "temperature_c": 36.5, "os_version": "15"})))


def test_status_as_the_phone_sends_it_today():
    async def go(body, client):  # the app answers {battery_pct, charging} and nothing else
        s = await body.status()
        assert (s.battery_pct, s.charging, s.temperature_c) == (64.0, True, None)

    run(with_phone(go, ack({"battery_pct": 64, "charging": True})))


def test_status_treats_nonsense_from_the_body_as_missing():
    async def go(body, client):
        s = await body.status()
        assert s.connected and (s.battery_pct, s.charging, s.temperature_c) == (None, None, None)

    run(with_phone(go, ack({"battery_pct": "full", "charging": "yes", "temperature_c": 9999})))


def test_status_with_no_body_connected_says_so_without_raising():
    async def go(body, client):
        assert body.connected is False and await body.wait_connected(0.05) is False
        s = await body.status()
        assert s.connected is False and s.battery_pct is None
        with pytest.raises(BodyUnavailable):
            await body.stop()

    run(with_phone(go, connect=False))


def test_status_of_a_body_without_get_status_asks_nothing():
    async def go(body, client):
        s = await body.status()
        assert s.connected and s.battery_pct is None and client.commands_seen == []

    run(with_phone(go, commands=()))


def test_status_never_raises_a_silent_body_gives_a_stale_status():
    """The contract a watchdog can poll: connected but no answer is `stale`, not an exception."""
    async def silent(client, r):
        pass

    async def go(body, client):
        s = await body.status()
        assert (s.connected, s.stale) == (True, True)
        assert (s.battery_pct, s.charging, s.temperature_c) == (None, None, None)
        assert body.connected  # a slow answer is not a dropped link

    run(with_phone(go, silent))


@pytest.mark.parametrize("code", ["busy", "internal", "not_implemented"])
def test_status_never_raises_a_refusing_body_gives_a_stale_status(code):
    async def go(body, client):
        s = await body.status()
        assert (s.connected, s.stale, s.battery_pct) == (True, True, None)

    run(with_phone(go, error(code)))


def test_healthy_status_is_not_stale():
    async def go(body, client):
        assert (await body.status()).stale is False

    run(with_phone(go, ack({"battery_pct": 50})))


def test_a_body_cannot_announce_the_links_state():
    async def go(body, client):
        events = body.events()
        await client.link.send("event", {"name": "link_state", "data": {"connected": False}})
        await client.link.send("event", {"name": "button", "data": {}})
        event = await asyncio.wait_for(anext(events), 5)
        assert event.name == "button" and body.connected  # the forged link_state was dropped

    run(with_phone(go))


def test_status_when_the_link_drops_mid_request_reports_not_connected():
    async def hang_up(client, r):
        await client.link.close(p.CLOSE_NORMAL, "bye")

    async def go(body, client):
        assert (await body.status()).connected is False

    run(with_phone(go, hang_up))


# ---- events and the clock ------------------------------------------------------------------------------

def test_link_state_events_follow_the_connection():
    async def go():
        body = PhoneBody(token=TOKEN, port=0, phone_link=False, heartbeat_s=0.1, liveness_s=3.0)
        await body.start()
        events = body.events()
        try:
            async with ScriptedBody(f"ws://127.0.0.1:{body.port}/", ack({})) as client:
                up = await asyncio.wait_for(anext(events), 5)
                assert (up.name, up.data, up.source, up.t_body_us) == ("link_state", {"connected": True}, "adapter", None)
                assert body.connected
                await client.link.send("event", {"name": "button", "data": {"which": "poke"}})
                poke = await asyncio.wait_for(anext(events), 5)
                assert (poke.name, poke.source) == ("button", "body") and poke.t_body_us is not None
            down = await asyncio.wait_for(anext(events), 5)
            assert (down.name, down.data) == ("link_state", {"connected": False})
            assert not body.connected and (await body.status()).connected is False
        finally:
            await body.close()
        with pytest.raises(StopAsyncIteration):  # closing the body ends its streams
            await anext(events)

    run(go())


def test_a_replacing_body_takes_over_without_a_false_disconnect():
    async def go(body, first):
        events = body.events()
        async with ScriptedBody(f"ws://127.0.0.1:{body.port}/", ack({"battery_pct": 5})) as second:
            up = await asyncio.wait_for(anext(events), 5)
            assert up.data == {"connected": True}
            await asyncio.sleep(0.3)  # the first connection is closed with 4001 meanwhile
            assert body.connected and (await body.status()).battery_pct == 5.0
            assert second.commands_seen == ["get_status"] and first.commands_seen == []
            await second.link.send("event", {"name": "button", "data": {}})
            assert (await asyncio.wait_for(anext(events), 5)).name == "button"  # no link_state False came between

    run(with_phone(go, ack({"battery_pct": 99})))


def test_event_times_are_converted_with_the_transport_clock_estimate():
    async def go(body, client):
        events = body.events()
        offset = 76_222_231_000  # body clock ahead of the brain's, as on the real link
        clock = ClockOffsetEstimator()
        now = p.now_us()
        clock.add(now - 2000, now - 1000 + offset, now)  # one 2 ms round trip
        body._clock = clock  # stand in for the estimate the link would have for such a body
        assert body.clock_sync().offset_us == pytest.approx(offset, abs=1) and body.clock_sync().rtt_us == 2000
        await client.link.send("event", {"name": "button", "data": {}}, ts=p.now_us() + offset - 50_000)
        event = await asyncio.wait_for(anext(events), 5)
        age_us = p.now_us() - event.t_us
        assert 50_000 <= age_us < 2_000_000  # stamped 50 ms before it was sent, on the brain clock
        assert event.t_body_us > offset
        # A stamp from the future (clock error) is clamped to the arrival time.
        await client.link.send("event", {"name": "button", "data": {}}, ts=p.now_us() + offset + 60_000_000)
        late = await asyncio.wait_for(anext(events), 5)
        assert late.t_us <= p.now_us()

    run(with_phone(go))


def test_clock_sync_comes_from_the_links_own_heartbeats():
    async def go(body, client):
        assert body.clock_sync().synced is False or abs(body.clock_sync().offset_us) < 1_000_000
        for _ in range(100):
            if body.clock_sync().synced:
                break
            await asyncio.sleep(0.05)
        sync = body.clock_sync()
        assert sync.synced and abs(sync.offset_us) < 1_000_000  # the test body shares this process's clock
        assert sync.rtt_us is not None and (await body.status()).link_rtt_ms == pytest.approx(sync.rtt_us / 1000)

    run(with_phone(go))


# ---- what is not built ---------------------------------------------------------------------------------

def test_unbuilt_methods_say_which_card_builds_them():
    async def go(body, client):
        chunk = AudioChunk(0, 0, 24000, 1, bytes(480))
        calls = {
            Capability.POSE: (lambda: body.pose_stream(), "P4-01"),
            Capability.IMAGE: (lambda: body.capture_image(), "P1-01"),
            Capability.AUDIO_IN: (lambda: body.audio_in_stream(), "P2-02"),
            Capability.AUDIO_OUT: (lambda: body.play_audio(chunk, utterance_id="u-1"), "P2-01"),
            Capability.SPEAK_TEXT: (lambda: body.speak_text("hi", utterance_id="u-1"), "P0-07"),
        }
        for capability, (call, card) in calls.items():
            with pytest.raises(NotYetImplemented) as e:
                result = call()
                if asyncio.iscoroutine(result):
                    await result
            assert e.value.capability is capability and card in str(e.value)
        with pytest.raises(NotSupported) as e:  # a carried body will never move itself: not "not yet"
            await body.navigate(NavGoal("f", (0, 0, 0)))
        assert not isinstance(e.value, NotYetImplemented) and "carried" in str(e.value)
        assert client.commands_seen == []  # nothing was sent to the body

    run(with_phone(go))


def test_stop_with_a_connected_body_succeeds():
    async def go(body, client):
        await body.stop()
        assert client.commands_seen == []  # nothing is driven over the link yet, so nothing to send

    run(with_phone(go))


# ---- factory and replay stub ---------------------------------------------------------------------------

def test_create_body_picks_by_kind():
    assert isinstance(create_body("sim", {}), SimBody)
    phone = create_body("phone", {"network": {"host": "127.0.0.1", "port": 9}}, token=TOKEN, phone_link=False)
    assert isinstance(phone, PhoneBody) and isinstance(phone, LinkBody) and phone.name == "phone" and phone.port == 9
    with pytest.raises(ValueError):
        create_body("phone", {})  # no token
    with pytest.raises(ValueError):
        create_body("robot", {})


def test_create_sim_body_uses_configured_media_folders(tmp_path, monkeypatch):
    from brain.body.sim_assets import write_default_media
    from brain.config import parse_config

    write_default_media(tmp_path / "i", tmp_path / "a")
    # Since H-05 the folders resolve under the config's own folder (here tmp_path), not a patched REPO_ROOT.
    config = parse_config({"body": {"sim": {"image_dir": "i", "audio_dir": "a"}}}, base=tmp_path)

    async def go():
        async with create_body("sim", config) as sim:
            assert (await sim.capture_image()).width_px == 160

    run(go())


def test_replay_body_is_a_stub_that_is_never_connected():
    async def go():
        async with ReplayBody("s-20261113-201501-3f9a") as replay:
            assert replay.capabilities == frozenset() and not replay.connected
            assert (await replay.status()).connected is False and await replay.wait_connected(0.01) is False
            with pytest.raises(NotYetImplemented, match="P1-03"):
                await replay.capture_image()
            with pytest.raises(NotYetImplemented):
                replay.pose_stream()
            with pytest.raises(BodyUnavailable):
                await replay.stop()

    run(go())
    with pytest.raises(ValueError):
        ReplayBody("..\\recordings\\x")  # an identifier, never a path
