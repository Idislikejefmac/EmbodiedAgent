"""The data classes of the Body interface: JSON round trips, units and frame conventions, validation."""

import json
import math

import pytest

from brain.body import (AudioChunk, BodyEvent, BodyStatus, Capability, ClockSync, ConfidenceBand, Image, NavGoal, Pose,
                        Speaker, TrackingState, Utterance)
from brain.body.types import quaternion_from_heading

POSE = Pose("world-1", (0.12, 1.41, -2.03), (0.0, 0.3826834, 0.0, 0.9238795), TrackingState.OK, 5_000_000, 81_320_100_000)

EXAMPLES = [
    POSE,
    Pose("f", (0, 0, 0), (0, 0, 0, 1), "lost", 1),
    NavGoal("world-1", (1.0, 0.0, -2.0), math.pi / 2),
    NavGoal("world-1", (1.0, 0.0, -2.0)),
    Image("img-1", "jpeg", 640, 480, b"\xff\xd8\x00\x01binary", 7_000_000, 9_000_000, POSE),
    Image("img-2", "png", 2, 1, b"", 1),
    AudioChunk(3, 17, 16000, 1, bytes(range(256)) * 10, 81_000, 99_000),
    Speaker("p-jeff", "Jeff", ConfidenceBand.CONFIRMED, 0.93),
    Speaker(None, None, "unknown"),
    Utterance("u-12", "where are we?", 1_000_000, 2_500_000, Speaker("p-jeff", "Jeff", "probable", 0.8), "aud-12"),
    Utterance("u-13", "", 5, 5),
    BodyEvent("button", {"which": "poke", "n": 2}, 123, 456),
    BodyEvent("link_state", {"connected": False}, 123, source="adapter"),
    BodyStatus(True, 81.0, False, 33.5, 4.1, ("pose", "image"), 99),
    BodyStatus(False),
    BodyStatus(True, link_rtt_ms=3.0, stale=True),
    ClockSync(76_222_231_000.0, 4100, -28.6),
    ClockSync(),
]


@pytest.mark.parametrize("obj", EXAMPLES, ids=lambda o: type(o).__name__)
def test_round_trips_through_json_text(obj):
    text = json.dumps(obj.to_json(), allow_nan=False)  # only JSON types, no NaN
    assert type(obj).from_json(json.loads(text)) == obj


def test_unknown_keys_are_ignored_on_reading():
    assert Pose.from_json({**POSE.to_json(), "added_in_v1": 5}) == POSE


def test_pose_reads_a_protocol_pose_payload_as_is():
    payload = {"frame_id": "world-1", "position_m": [0.12, 1.41, -2.03],
               "orientation_xyzw": [0.0, 0.3826834, 0.0, 0.9238795], "tracking": "ok"}  # protocol/v0.md 4.4
    pose = Pose.from_json({**payload, "t_us": 5})
    assert pose.heading_deg == pytest.approx(45.0, abs=1e-3)  # the spec's example: a 45 degree turn about +y


@pytest.mark.parametrize("deg", [0, 45, 90, 179, -90, -135])
def test_heading_follows_the_protocol_convention(deg):
    pose = Pose("f", (0, 0, 0), quaternion_from_heading(math.radians(deg)), "ok", 1)
    assert pose.heading_deg == pytest.approx(deg, abs=1e-6)
    assert pose.heading_rad == pytest.approx(math.radians(deg), abs=1e-9)


def test_heading_is_undefined_looking_straight_up():
    s = math.sqrt(0.5)
    assert Pose("f", (0, 0, 0), (s, 0, 0, s), "ok", 1).heading_rad is None  # pitched up 90 degrees


def test_image_metadata_json_leaves_the_bytes_out():
    image = EXAMPLES[4]
    meta = image.to_json(include_data=False)
    assert "data_b64" not in meta and meta["byte_length"] == len(image.data)
    assert Image.from_json(meta).data == b""
    assert image.media_type == "image/jpeg"
    assert b"binary" not in repr(image).encode()  # bytes stay out of logs


def test_image_pose_is_the_cameras_pose_under_its_own_key():
    image = EXAMPLES[4]
    assert image.camera_pose == POSE and image.to_json()["camera_pose"] == POSE.to_json()
    assert "pose" not in image.to_json()


def test_audio_chunk_units():
    chunk = AudioChunk(1, 0, 16000, 1, bytes(2560))  # 80 ms of 16 kHz mono
    assert chunk.frames == 1280 and chunk.duration_s == pytest.approx(0.080)


def test_clock_sync_converts_body_time_to_brain_time():
    sync = ClockSync(offset_us=76_222_231_000.0, rtt_us=4100)
    assert sync.synced and sync.to_brain_us(81_316_233_000) == 5_094_002_000  # protocol/v0.md section 6 numbers
    assert not ClockSync().synced and ClockSync().to_brain_us(5) is None


def test_status_capabilities_are_sorted_capability_names():
    assert BodyStatus(True, capabilities=(Capability.POSE, "image")).capabilities == ("image", "pose")
    with pytest.raises(ValueError):
        BodyStatus(True, capabilities=("teleport",))


@pytest.mark.parametrize("make", [
    lambda: Pose("../etc", (0, 0, 0), (0, 0, 0, 1), "ok", 1),  # a frame id is an identifier, not a path
    lambda: Pose("f", (0, 0), (0, 0, 0, 1), "ok", 1),
    lambda: Pose("f", (0, 0, float("nan")), (0, 0, 0, 1), "ok", 1),
    lambda: Pose("f", (0, 0, 0), (0, 0, 0, 2), "ok", 1),  # not a unit quaternion
    lambda: Pose("f", (0, 0, 0), (0, 0, 0, 1), "great", 1),
    lambda: Pose("f", (0, 0, 0), (0, 0, 0, 1), "ok", 1.5),
    lambda: Image("a/b", "jpeg", 1, 1, b"x"),
    lambda: Image("a", "bmp", 1, 1, b"x"),
    lambda: Image("a", "jpeg", 0, 1, b"x"),
    lambda: AudioChunk(1, 0, 16000, 1, b"\x00"),  # half a sample
    lambda: AudioChunk(1, 0, 16000, 1, b"\x00\x00", encoding="mp3"),
    lambda: Utterance("u 1", "hi", 1, 2),
    lambda: Utterance("u-1", "hi", 5, 4),  # ends before it starts
    lambda: Utterance("u-1", "hi", 1, 2, audio_ref="C:\\audio.wav"),
    lambda: Speaker("p-1", "x", "certain"),
    lambda: BodyEvent("Run This", {}, 1),
    lambda: BodyEvent("button", [], 1),
    lambda: BodyEvent("button", {}, 1, source="agent"),
    lambda: BodyStatus(True, battery_pct=140),
    lambda: BodyStatus("yes"),
    lambda: BodyStatus(True, stale="no"),
    lambda: NavGoal("f", (0, 0, 0), float("inf")),
])
def test_bad_values_are_refused(make):
    with pytest.raises(ValueError):
        make()
