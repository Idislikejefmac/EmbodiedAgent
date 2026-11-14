"""SimBody beyond the shared contract: the square walk, its media folders, its clock, navigation, and
the Fanout it and the link body use for streams."""

import asyncio
import math
import wave

import pytest

from brain.body import BodyUnavailable, Capability, NavGoal, NotSupported, TrackingState
from brain.body import sim_assets
from brain.body.sim import SimBody
from brain.body.streams import Fanout
from brain.transport.protocol import now_us


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


# ---- generated media -----------------------------------------------------------------------------------

def test_generated_png_decodes_and_shrinks():
    png = sim_assets.make_png(40, 20, lambda x, y: (x * 6, y * 12, 7))
    assert sim_assets.image_size(png) == ("png", 40, 20)
    width, height, rgb = sim_assets.decode_png(png)
    assert (width, height) == (40, 20) and rgb[:3] == bytes((0, 0, 7)) and rgb[-3:] == bytes((234, 228, 7))
    small, w, h = sim_assets.shrink_png(png, 10)
    assert (w, h) == (10, 5) and sim_assets.image_size(small) == ("png", 10, 5)
    assert sim_assets.shrink_png(png, 64)[0] is png  # never enlarged
    with pytest.raises(ValueError):
        sim_assets.image_size(b"GIF89a....")


def test_jpeg_size_is_read_from_the_header():
    jpeg = (b"\xff\xd8" + b"\xff\xe0\x00\x04\x00\x00" + b"\xff\xc0\x00\x11\x08" + (480).to_bytes(2, "big")
            + (640).to_bytes(2, "big") + bytes(12))
    assert sim_assets.image_size(jpeg) == ("jpeg", 640, 480)


def test_default_media_are_valid_files(tmp_path):
    sim_assets.write_default_media(tmp_path / "img", tmp_path / "aud")
    assert len(list((tmp_path / "img").glob("*.png"))) == 3
    for path in (tmp_path / "aud").glob("*.wav"):
        with wave.open(str(path)) as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2) and w.getnframes() == 12800


# ---- pose ----------------------------------------------------------------------------------------------

def test_square_walk_corners_and_headings():
    sim = SimBody(side_m=2.0, height_m=1.4)
    expect = {0.0: ((0, 1.4, 0), 0), 1.0: ((0, 1.4, -1), 0), 2.0: ((0, 1.4, -2), 90), 3.0: ((-1, 1.4, -2), 90),
              4.0: ((-2, 1.4, -2), 180), 5.0: ((-2, 1.4, -1), 180), 6.0: ((-2, 1.4, 0), 270), 7.0: ((-1, 1.4, 0), 270),
              8.0: ((0, 1.4, 0), 0), 9.0: ((0, 1.4, -1), 0)}
    for distance, (position, heading_deg) in expect.items():
        got_position, got_heading = sim._square(distance)
        assert got_position == pytest.approx(position, abs=1e-9), distance
        assert math.degrees(got_heading) == pytest.approx(heading_deg), distance


def test_poses_walk_forward_in_the_direction_faced():
    async def go():
        async with SimBody(walk_speed_m_s=0.5) as sim:
            first = sim.pose_now()
            await asyncio.sleep(0.2)
            second = sim.pose_now()
            assert first.heading_deg == pytest.approx(0) and first.frame_id == "sim-1"
            assert second.position_m[2] < first.position_m[2]  # heading 0 faces world -z
            moved = first.position_m[2] - second.position_m[2]
            assert moved == pytest.approx(0.5 * (second.t_us - first.t_us) / 1e6, abs=1e-6)

    run(go())


def test_body_clock_is_offset_and_data_is_stamped_in_brain_time():
    async def go():
        async with SimBody(clock_offset_us=9_000_000_000) as sim:
            pose = sim.pose_now()
            assert pose.t_body_us - pose.t_us == 9_000_000_000
            assert abs(pose.t_us - now_us()) < 1_000_000
            assert sim.clock_sync().to_brain_us(pose.t_body_us) == pose.t_us

    run(go())


def test_tracking_loss_and_frame_reset_show_on_the_pose_stream():
    async def go():
        async with SimBody() as sim:
            events = sim.events()
            sim.set_tracking(TrackingState.LOST, "insufficient_light")
            event = await anext(events)
            assert (event.name, event.data) == ("tracking_state", {"state": "lost", "reason": "insufficient_light"})
            assert sim.pose_now().tracking is TrackingState.LOST
            assert sim.reset_frame() == "sim-2" and sim.pose_now().frame_id == "sim-2"

    run(go())


# ---- camera and microphone -----------------------------------------------------------------------------

def test_images_come_from_the_folder_in_order_and_are_shrunk_on_request(tmp_path):
    (tmp_path / "b.png").write_bytes(sim_assets.make_png(300, 100, lambda x, y: (1, 2, 3)))
    (tmp_path / "a.png").write_bytes(sim_assets.make_png(8, 8, lambda x, y: (9, 9, 9)))
    (tmp_path / "notes.txt").write_text("not an image")
    aud = tmp_path / "aud"
    aud.mkdir()
    (aud / "x.wav").write_bytes(sim_assets.wav_bytes(sim_assets.tone(440, 0.2)))

    async def go():
        async with SimBody(image_dir=tmp_path, audio_dir=aud) as sim:
            one, two, three = [await sim.capture_image(max_edge_px=150) for _ in range(3)]
            assert (one.width_px, one.height_px) == (8, 8)
            assert (two.width_px, two.height_px) == (150, 50)  # 300 x 100 shrunk to fit 150
            assert three.data == one.data and three.image_id != one.image_id  # round again
            assert two.camera_pose is not None and two.camera_pose.frame_id == "sim-1"
            assert two.camera_pose.position_m == pytest.approx(sim.pose_now().position_m, abs=0.05)  # camera = body

    run(go())


def test_microphone_plays_the_wav_folder_in_80_ms_chunks(tmp_path):
    img = tmp_path / "img"
    sim_assets.write_default_media(img, tmp_path / "unused")
    pcm = sim_assets.tone(500, 0.2)
    (tmp_path / "clip.wav").write_bytes(sim_assets.wav_bytes(pcm))

    async def go():
        async with SimBody(image_dir=img, audio_dir=tmp_path) as sim:
            stream = sim.audio_in_stream()
            chunks = [await anext(stream) for _ in range(4)]
            await stream.aclose()
            assert b"".join(c.data for c in chunks[:3]) == pcm  # 80 + 80 + 40 ms
            assert [c.frames for c in chunks] == [1280, 1280, 640, 1280]  # then the clip starts again
            assert chunks[1].t_us - chunks[0].t_us == 80_000

    run(go())


def test_a_stalled_microphone_consumer_sees_a_gap_not_a_stale_backlog():
    """Audio drop policy: bounded, oldest dropped, and the loss shows as a gap in seq and in `dropped`."""
    async def go():
        async with SimBody(audio_queue=3) as sim:
            stream = sim.audio_in_stream()
            first = await anext(stream)
            await asyncio.sleep(0.8)  # about ten chunks arrive; only the newest three are kept
            burst = [await anext(stream) for _ in range(3)]
            await stream.aclose()
            assert first.seq == 0 and burst[0].seq > 1  # a visible gap
            assert [c.seq for c in burst] == list(range(burst[0].seq, burst[0].seq + 3))
            assert stream.dropped == burst[0].seq - 1
            assert (now_us() - burst[-1].t_us) / 1e6 < 0.5  # fresh, not a backlog

    run(go())


def test_closing_the_body_ends_sensor_streams_and_their_producers():
    async def go():
        sim = SimBody()
        await sim.start()
        poses, audio = sim.pose_stream(), sim.audio_in_stream()
        await anext(poses)
        assert len(sim._producers) == 2
        await sim.close()
        async for _ in poses:
            pass
        async for _ in audio:
            pass
        await asyncio.sleep(0)
        assert not sim._producers  # nothing left running

    run(go())


def test_a_dropped_link_is_reported_and_reserved_event_names_are_refused():
    async def go():
        async with SimBody() as sim:
            events = sim.events()
            sim.inject_event("link_state", {"connected": False})  # a body may not say this
            sim.set_link(False)
            event = await anext(events)
            assert (event.name, event.source, event.data) == ("link_state", "adapter", {"connected": False})
            assert not sim.connected and sim.capabilities == frozenset()
            sim.set_link(True)
            assert (await anext(events)).data == {"connected": True} and sim.connected

    run(go())


def test_empty_media_folder_is_an_error(tmp_path):
    with pytest.raises(ValueError):
        run(SimBody(image_dir=tmp_path, audio_dir=tmp_path).start())


# ---- actuators -----------------------------------------------------------------------------------------

def test_speech_audio_and_stops_are_recorded():
    async def go():
        async with SimBody() as sim:
            await sim.speak_text("Hello.", utterance_id="u-1", interrupt=True)
            stream = sim.audio_in_stream()
            chunk = await anext(stream)
            await stream.aclose()
            await sim.play_audio(chunk, utterance_id="u-2")
            await sim.play_audio(chunk, utterance_id="u-2", final=True)
            await sim.stop(motion=False)
            assert sim.spoken == [("u-1", "Hello.", True)]
            assert len(sim.played["u-2"]) == 2 and sim.finished_utterances == ["u-2"]
            assert sim.stops == [(False, True)]

    run(go())


def test_navigate_moves_to_the_goal_and_reports_arrival():
    async def go():
        async with SimBody(nav_speed_m_s=20.0) as sim:
            events = sim.events()
            goal_id = await sim.navigate(NavGoal("sim-1", (3.0, 0.0, 4.0), heading_rad=1.0))
            states = []
            while len(states) < 2:
                event = await anext(events)
                if event.name == "navigation":
                    assert event.data["goal_id"] == goal_id
                    states.append(event.data["state"])
            assert states == ["started", "arrived"]
            pose = sim.pose_now()
            assert pose.position_m == pytest.approx((3.0, 1.4, 4.0)) and pose.heading_rad == pytest.approx(1.0)
            await asyncio.sleep(0.05)
            assert sim.pose_now().position_m == pose.position_m  # it stays there

    run(go())


def test_stop_halts_the_walk():
    async def go():
        async with SimBody() as sim:
            await asyncio.sleep(0.05)
            await sim.stop()
            here = sim.pose_now().position_m
            await asyncio.sleep(0.1)
            assert sim.pose_now().position_m == here

    run(go())


def test_a_narrowed_sim_refuses_what_it_lacks_and_does_nothing():
    async def go():
        async with SimBody(capabilities={Capability.POSE}) as sim:
            with pytest.raises(NotSupported):
                await sim.speak_text("hello", utterance_id="u-1")
            assert sim.spoken == []
            assert (await sim.status()).capabilities == ("pose",)

    run(go())


def test_not_started_sim_is_unavailable():
    async def go():
        sim = SimBody()
        assert not sim.connected and (await sim.status()).connected is False
        with pytest.raises(BodyUnavailable):
            await sim.stop()

    run(go())


# ---- streams -------------------------------------------------------------------------------------------

def test_fanout_drops_the_oldest_for_a_slow_consumer_and_counts_it():
    async def go():
        fan: Fanout[int] = Fanout()
        slow, fast = fan.subscribe(maxsize=3), fan.subscribe(maxsize=100)
        for i in range(10):
            fan.publish(i)
        assert [await anext(slow) for _ in range(3)] == [7, 8, 9] and slow.dropped == 7
        assert [await anext(fast) for _ in range(10)] == list(range(10)) and fast.dropped == 0
        fan.close()
        with pytest.raises(StopAsyncIteration):
            await anext(slow)
        with pytest.raises(StopAsyncIteration):
            await anext(fan.subscribe())  # a closed fanout gives streams that end at once
        assert len(fan) == 0

    run(go())
