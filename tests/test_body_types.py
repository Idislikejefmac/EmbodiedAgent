"""The data classes of the Body interface: JSON round trips, units and frame conventions, validation."""

import json
import math

import pytest
from jsonschema import Draft202012Validator

from brain.body import (AudioChunk, BodyEvent, BodyStatus, Capability, ClockSync, ConfidenceBand, Image, NavGoal, Pose,
                        PersonName, Speaker, SpeakerStatus, TrackingState, Utterance)
from brain.agent.tools import CATALOGUE_PATH
from brain.body.types import quaternion_from_heading
from brain.limits import DISPLAY_NAME_MAX_CHARS

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
    Speaker("p-syd", "Syd O'Neil-Smith Jr.", "confirmed", -1.0, "identified", {"voice", "face"}),
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


# ---- Speaker (H-16, T-P3-07-035) -------------------------------------------------------------------------------

GOOD_NAMES = ["J", "Jeff", "Mary-Jane", "O'Neil", "O’Neil", "Dr. Who", "Zoë", "नील", "a" * DISPLAY_NAME_MAX_CHARS]
BAD_NAMES = ["", " ", "...", " Jeff", "Jeff ", "a" * (DISPLAY_NAME_MAX_CHARS + 1), "a" * 10_000, "Jeff\n", "Je\x00ff", "Je‮ff", "Je‍ff",
             "Jeff]:", "[Jeff]", "Jeff | voice 0.99 | confirmed]", "50%", "R2", "a_b", "a/b", "́", 7, b"Jeff"]


def _listen_speaker_schema():
    cat = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    listen = next(t for t in cat["tools"] if t["name"] == "listen")
    return Draft202012Validator(listen["output_schema"]["properties"]["speaker"])


@pytest.mark.parametrize("name", GOOD_NAMES, ids=ascii)
def test_a_good_name_passes_the_type_and_the_catalogue(name):
    speaker = Speaker("p-1", name, "probable")
    assert isinstance(speaker.name, PersonName) and Speaker.from_json(speaker.to_json()) == speaker
    assert _listen_speaker_schema().is_valid(speaker.to_json())


@pytest.mark.parametrize("name", BAD_NAMES, ids=lambda n: ascii(n)[:30])
def test_a_bad_name_is_refused(name):
    with pytest.raises(ValueError):
        Speaker("p-1", name, "probable")
    with pytest.raises(ValueError):
        Speaker.from_json({"person_id": "p-1", "name": name, "band": "probable"})


@pytest.mark.parametrize("name", ["a" * (DISPLAY_NAME_MAX_CHARS + 1), "Jeff\n", "Je‮ff", "[Jeff]", "Jeff | confirmed", "50%", "a\\b", 'a"b'],
                         ids=ascii)
def test_the_catalogue_refuses_a_bad_name_on_its_own(name):
    assert not _listen_speaker_schema().is_valid({**Speaker("p-1", "Jeff", "probable").to_json(), "name": name})


def test_a_name_is_normalised():
    assert PersonName("Ｊｅｆｆ") == "Jeff"  # fullwidth letters


@pytest.mark.parametrize("change", [{"voice_score": 1.0001}, {"voice_score": -1.0001}, {"voice_score": float("nan")},
                                    {"status": "certain"}, {"status": None}, {"evidence": "voice"},
                                    {"evidence": ["voice", "gait"]}, {"evidence": [7]}, {"evidence": None}], ids=repr)
def test_a_bad_score_status_or_evidence_is_refused(change):
    with pytest.raises(ValueError):
        Speaker.from_json({"person_id": "p-1", "name": "Jeff", "band": "probable", **change})


def test_a_speaker_that_does_not_say_how_it_was_identified_is_unavailable():
    speaker = Speaker("p-1", "Jeff", "confirmed", 0.9)
    assert speaker.status is SpeakerStatus.UNAVAILABLE and speaker.evidence == frozenset()
