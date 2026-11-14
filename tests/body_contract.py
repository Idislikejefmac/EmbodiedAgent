"""The Body contract: checks that every body must pass, whichever body it is (card P0-06 step 6).

Each `check_*` coroutine takes a Harness holding a started, connected body. A check exercises a
capability if the body has it and, if not, checks that the method refuses in the agreed way, so a body
that implements only part of the interface passes "the implemented subset" and cannot mis-declare.

To put a new body under contract, add a harness for it in tests/test_body_contract.py: a way to build
it connected, to make it emit an event, and to drop and restore its link while the adapter stays up.
Nothing in this file may name a particular body.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest

from brain.body import (AudioChunk, Body, BodyError, BodyEvent, BodyStatus, BodyUnavailable, Capability, ClockSync,
                        Image, NavGoal, NotSupported, Pose, TrackingState)
from brain.body.sim_assets import image_size
from brain.body.types import IDENTIFIER
from brain.transport.protocol import now_us

STEP_S = 5.0  # no single step of a check may take longer


@dataclass
class Harness:
    body: Body
    emit_event: Callable[[str, dict], Awaitable[None]]  # make the body itself report an event
    drop_link: Callable[[], Awaitable[None]]  # make the body unreachable; the adapter stays started
    restore_link: Callable[[], Awaitable[None]]  # make it reachable again
    started_us: int


async def step(awaitable):
    return await asyncio.wait_for(awaitable, STEP_S)


def on_brain_clock(h: Harness, t_us: int) -> bool:
    """A timestamp from the body must be brain time: between the start of the test and now."""
    return h.started_us - 1_000_000 <= t_us <= now_us() + 1000


async def refuses(body: Body, capability: Capability, call) -> None:
    """A method whose capability is absent raises NotSupported at the call and changes nothing."""
    before = body.capabilities
    with pytest.raises(NotSupported) as e:
        result = call()
        if asyncio.iscoroutine(result):
            await step(result)
    assert e.value.capability is capability
    assert e.value.code in ("not_supported", "not_implemented")
    assert str(e.value)
    assert body.capabilities == before


async def next_event(stream, name: str) -> BodyEvent:
    while True:
        event = await step(anext(stream))
        if event.name == name:
            return event


def open_streams(body: Body) -> dict[str, object]:
    """One of every stream this body supports."""
    streams = {"events": body.events()}
    if body.supports(Capability.POSE):
        streams["pose"] = body.pose_stream(rate_hz=30)
    if body.supports(Capability.AUDIO_IN):
        streams["audio"] = body.audio_in_stream()
    return streams


# ---- mandatory core ----------------------------------------------------------------------------------


async def check_identity_and_clock(h: Harness) -> None:
    body = h.body
    assert isinstance(body.name, str) and body.name
    assert body.connected is True
    assert await step(body.wait_connected(0.1)) is True
    caps = body.capabilities
    assert isinstance(caps, frozenset) and all(isinstance(c, Capability) for c in caps)
    assert all(body.supports(c) for c in caps)
    sync = body.clock_sync()
    assert isinstance(sync, ClockSync)
    if sync.synced:
        assert abs(sync.to_brain_us(round(now_us() + sync.offset_us)) - now_us()) < 1_000_000
    else:
        assert sync.to_brain_us(123) is None


async def check_status(h: Harness) -> None:
    status = await step(h.body.status())
    assert isinstance(status, BodyStatus)
    assert status.connected is True and status.stale is False
    assert set(status.capabilities) == {c.value for c in h.body.capabilities}
    assert status.battery_pct is None or 0 <= status.battery_pct <= 100
    assert status.charging is None or isinstance(status.charging, bool)
    assert on_brain_clock(h, status.t_us)
    assert BodyStatus.from_json(status.to_json()) == status


async def check_events(h: Harness) -> None:
    first, second = h.body.events(), h.body.events()
    try:
        await h.emit_event("button", {"which": "poke"})
        for stream in (first, second):  # every subscriber gets every event
            event = await next_event(stream, "button")
            assert isinstance(event, BodyEvent)
            assert event.data == {"which": "poke"} and event.source == "body"
            assert on_brain_clock(h, event.t_us)
            assert BodyEvent.from_json(event.to_json()) == event
        await first.aclose()
        with pytest.raises(StopAsyncIteration):  # a closed stream ends
            await step(anext(first))
        await h.emit_event("button", {"which": "again"})  # the other subscriber is unaffected
        assert (await step(anext(second))).data == {"which": "again"}
    finally:
        await first.aclose()
        await second.aclose()


async def check_a_body_cannot_fake_the_adapters_events(h: Harness) -> None:
    """`link_state` is the adapter's word about its link. Whatever a body sends under that name never
    reaches a consumer as the adapter's: it is dropped, or at least marked as coming from the body."""
    events = h.body.events()
    try:
        await h.emit_event("link_state", {"connected": False})
        await h.emit_event("button", {"which": "after"})
        while True:
            event = await step(anext(events))
            assert not (event.name == "link_state" and event.source == "adapter")
            if event.name == "button":
                break
        assert h.body.connected is True
    finally:
        await events.aclose()


async def check_stop(h: Harness) -> None:
    assert await step(h.body.stop()) is None
    assert await step(h.body.stop(motion=False)) is None
    assert await step(h.body.stop(audio=False)) is None
    assert h.body.connected is True


# ---- streams -----------------------------------------------------------------------------------------


async def check_streams_can_be_closed_from_another_task(h: Harness) -> None:
    """Closing a stream while a consumer waits on it (for example "switch the microphone off") ends that
    consumer's `async for` cleanly, for every kind of stream the body has."""
    for label, stream in open_streams(h.body).items():
        async def consume(stream=stream) -> int:
            count = 0
            async for _ in stream:
                count += 1
            return count

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0.2)
        assert not consumer.done(), label
        await step(stream.aclose())  # from this task, not the consumer's
        assert await step(consumer) >= 0, label
        with pytest.raises(StopAsyncIteration):
            await step(anext(stream))
        await step(stream.aclose())  # closing twice is fine


async def check_a_slow_consumer_loses_the_oldest_events(h: Harness) -> None:
    """Drop policy: a consumer that does not keep up loses its oldest items and can see how many."""
    stream = h.body.events()
    total = 400
    try:
        for n in range(total):
            await h.emit_event("button", {"n": n})
            if n % 20 == 19:
                await asyncio.sleep(0.01)  # let the adapter take them in; the consumer still reads nothing
        for _ in range(250):
            if stream.dropped:
                break
            await asyncio.sleep(0.02)
        assert stream.dropped > 0
        received = []
        while not received or received[-1] != total - 1:
            event = await step(anext(stream))
            if event.name == "button":
                received.append(event.data["n"])
        assert received == list(range(received[0], total))  # the newest survive, in order
        assert received[0] > 0 and len(received) < total  # the oldest were dropped
    finally:
        await stream.aclose()


async def check_a_stalled_pose_consumer_gets_fresh_poses(h: Harness) -> None:
    if not h.body.supports(Capability.POSE):
        return
    stream = h.body.pose_stream(rate_hz=30)
    try:
        await step(anext(stream))
        await asyncio.sleep(1.5)  # stall: about 45 poses arrive meanwhile
        pose = await step(anext(stream))
        assert (now_us() - pose.t_us) / 1e6 < 1.0, "a stale backlog was delivered"
        assert stream.dropped > 0
    finally:
        await stream.aclose()


# ---- capability-gated --------------------------------------------------------------------------------


async def check_pose_stream(h: Harness) -> None:
    body = h.body
    if not body.supports(Capability.POSE):
        return await refuses(body, Capability.POSE, lambda: body.pose_stream())
    for not_a_number in (True, False, "10", None, [10]):  # a bool is not a number here (X01-F04)
        with pytest.raises(TypeError):
            body.pose_stream(rate_hz=not_a_number)
    for not_finite in (float("nan"), float("inf"), -1.0):
        with pytest.raises(ValueError):
            body.pose_stream(rate_hz=not_finite)
    with pytest.raises(ValueError):
        body.pose_stream(rate_hz=0)
    with pytest.raises(ValueError):
        body.pose_stream(rate_hz=1000)
    stream = body.pose_stream(rate_hz=20)
    try:
        poses = [await step(anext(stream)) for _ in range(3)]
    finally:
        await stream.aclose()
    for pose in poses:
        assert isinstance(pose, Pose)
        assert IDENTIFIER.match(pose.frame_id)
        assert isinstance(pose.tracking, TrackingState)
        assert len(pose.position_m) == 3 and len(pose.orientation_xyzw) == 4
        assert abs(sum(c * c for c in pose.orientation_xyzw) - 1) < 1e-3
        assert on_brain_clock(h, pose.t_us)
        assert Pose.from_json(pose.to_json()) == pose
    assert poses[0].t_us <= poses[1].t_us <= poses[2].t_us
    assert poses[2].t_us > poses[0].t_us


async def check_capture_image(h: Harness) -> None:
    body = h.body
    if not body.supports(Capability.IMAGE):
        return await refuses(body, Capability.IMAGE, lambda: body.capture_image())
    for bad in (0, 63, 5000, 640.0, "640"):
        with pytest.raises(ValueError):
            await step(body.capture_image(max_edge_px=bad))
    small = await step(body.capture_image(max_edge_px=64))
    default = await step(body.capture_image())
    for image, edge in ((small, 64), (default, 640)):
        assert isinstance(image, Image)
        assert IDENTIFIER.match(image.image_id)
        assert max(image.width_px, image.height_px) <= edge
        assert image.data and image_size(image.data) == (image.encoding, image.width_px, image.height_px)
        assert image.media_type.startswith("image/")
        assert on_brain_clock(h, image.t_us)
        assert image.camera_pose is None or isinstance(image.camera_pose, Pose)
        assert Image.from_json(image.to_json()) == image
    assert small.image_id != default.image_id


async def check_audio_in(h: Harness) -> None:
    body = h.body
    if not body.supports(Capability.AUDIO_IN):
        return await refuses(body, Capability.AUDIO_IN, lambda: body.audio_in_stream())
    stream = body.audio_in_stream()
    try:
        chunks = [await step(anext(stream)) for _ in range(3)]
    finally:
        await stream.aclose()
    for i, chunk in enumerate(chunks):
        assert isinstance(chunk, AudioChunk)
        assert chunk.seq == i and chunk.stream_id == chunks[0].stream_id
        assert chunk.encoding == "pcm_s16le" and chunk.frames > 0 and 0 < chunk.duration_s <= 1.0
        assert on_brain_clock(h, chunk.t_us)
        assert AudioChunk.from_json(chunk.to_json()) == chunk
    assert chunks[0].t_us < chunks[1].t_us < chunks[2].t_us
    again = body.audio_in_stream()  # a new stream is told apart from the old one and counts from 0 again
    try:
        first = await step(anext(again))
    finally:
        await again.aclose()
    assert first.stream_id != chunks[0].stream_id and first.seq == 0


async def check_play_audio(h: Harness) -> None:
    body = h.body
    silence = AudioChunk(0, 0, 24000, 1, bytes(4800))
    if not body.supports(Capability.AUDIO_OUT):
        return await refuses(body, Capability.AUDIO_OUT, lambda: body.play_audio(silence, utterance_id="u-1"))
    assert await step(body.play_audio(silence, utterance_id="u-contract-1")) is None
    assert await step(body.play_audio(AudioChunk(0, 1, 24000, 1, b""), utterance_id="u-contract-1", final=True)) is None
    with pytest.raises(ValueError):
        await step(body.play_audio(silence, utterance_id="../not an id"))
    await step(body.stop(motion=False))


async def check_speak_text(h: Harness) -> None:
    body = h.body
    if not body.supports(Capability.SPEAK_TEXT):
        return await refuses(body, Capability.SPEAK_TEXT, lambda: body.speak_text("hello", utterance_id="u-1"))
    assert await step(body.speak_text("Hello.", utterance_id="u-contract-2")) is None
    assert await step(body.speak_text("Stop that.", utterance_id="u-contract-3", interrupt=True)) is None
    with pytest.raises(ValueError):
        await step(body.speak_text("", utterance_id="u-contract-4"))
    with pytest.raises(ValueError):
        await step(body.speak_text("hello", utterance_id="a/b"))


async def check_navigate(h: Harness) -> None:
    body = h.body
    if not body.supports(Capability.NAVIGATE):
        return await refuses(body, Capability.NAVIGATE, lambda: body.navigate(NavGoal("frame-1", (1.0, 0.0, 1.0))))
    assert body.supports(Capability.POSE), "a body that moves itself must report where it is"
    poses = body.pose_stream()
    try:
        here = await step(anext(poses))
    finally:
        await poses.aclose()
    events = body.events()
    try:
        with pytest.raises(BodyError):  # a goal in a frame the body does not know
            await step(body.navigate(NavGoal("no-such-frame", (0.0, 0.0, 0.0))))
        x, y, z = here.position_m
        goal_id = await step(body.navigate(NavGoal(here.frame_id, (x + 50.0, y, z + 50.0))))
        assert isinstance(goal_id, str) and IDENTIFIER.match(goal_id)

        async def next_navigation() -> BodyEvent:
            while True:
                event = await step(anext(events))
                if event.name == "navigation" and event.data.get("goal_id") == goal_id:
                    return event

        assert (await next_navigation()).data["state"] == "started"
        await step(body.stop())  # stop cancels the goal
        assert (await next_navigation()).data["state"] == "cancelled"
    finally:
        await events.aclose()


# ---- the link drops, and comes back ------------------------------------------------------------------


async def check_link_drop_and_restore(h: Harness) -> None:
    """A started body whose link drops: it says so, refuses work, keeps its streams open but quiet, and
    its capabilities change with the link. When the link returns everything resumes on the same streams."""
    body = h.body
    before = body.capabilities
    streams = open_streams(body)
    events = streams["events"]
    try:
        for name, stream in streams.items():
            if name != "events":
                await step(anext(stream))  # each sensor stream is flowing
        await h.drop_link()
        down = await next_event(events, "link_state")
        assert down.data == {"connected": False} and down.source == "adapter" and on_brain_clock(h, down.t_us)
        assert body.connected is False and await body.wait_connected(0.05) is False
        assert body.capabilities == frozenset()  # capabilities are dynamic
        status = await step(body.status())  # status never raises
        assert status.connected is False and status.battery_pct is None and status.capabilities == ()
        with pytest.raises(BodyUnavailable):
            await step(body.stop())
        with pytest.raises((NotSupported, BodyUnavailable)):
            await step(body.capture_image())
        with pytest.raises((NotSupported, BodyUnavailable)):
            body.pose_stream()
        for name, stream in streams.items():  # quiet, not ended
            if name == "events":
                continue
            with pytest.raises(TimeoutError):
                while True:
                    await asyncio.wait_for(anext(stream), 0.4)
        await h.restore_link()
        up = await next_event(events, "link_state")
        assert up.data == {"connected": True} and up.source == "adapter"
        assert await step(body.wait_connected(STEP_S)) is True
        assert body.capabilities == before
        assert (await step(body.status())).connected is True
        for name, stream in streams.items():  # the same streams carry on
            if name != "events":
                item = await step(anext(stream))
                assert on_brain_clock(h, item.t_us)
        await step(body.stop())
    finally:
        for stream in streams.values():
            await stream.aclose()


async def check_after_close(h: Harness) -> None:
    """Closing ends every stream; afterwards the body says it is not connected and refuses work."""
    body = h.body
    streams = open_streams(body)
    await step(body.close())
    await step(body.close())  # closing twice is fine
    assert body.connected is False
    for stream in streams.values():
        async def drain(stream=stream) -> None:
            async for _ in stream:  # delivers what was queued, then ends
                pass
        await step(drain())
    status = await step(body.status())
    assert status.connected is False and status.battery_pct is None and status.capabilities == ()
    assert body.capabilities == frozenset()
    with pytest.raises(BodyUnavailable):
        await step(body.stop())
    with pytest.raises((NotSupported, BodyUnavailable)):
        await step(body.capture_image())
    with pytest.raises((NotSupported, BodyUnavailable)):
        body.pose_stream()


CHECKS = [check_identity_and_clock, check_status, check_events, check_a_body_cannot_fake_the_adapters_events,
          check_stop, check_streams_can_be_closed_from_another_task, check_a_slow_consumer_loses_the_oldest_events,
          check_a_stalled_pose_consumer_gets_fresh_poses, check_pose_stream, check_capture_image, check_audio_in,
          check_play_audio, check_speak_text, check_navigate, check_link_drop_and_restore, check_after_close]
