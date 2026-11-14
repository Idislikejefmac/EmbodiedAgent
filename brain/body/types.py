"""Data that crosses the Body interface (interface v0, card P0-06). Body-agnostic.

Conventions, used everywhere above the body layer:

- **Units are in the field name**: `_m` metres, `_rad` radians, `_deg` degrees, `_us` microseconds,
  `_ms` milliseconds, `_s` seconds, `_hz` hertz, `_pct` percent (0 to 100), `_c` degrees Celsius,
  `_px` pixels.
- **Time**: `t_us` is always the instant of the measurement on the **brain's monotonic clock**
  (`brain.transport.protocol.now_us()`), in integer microseconds. `t_body_us` is the body's own raw
  stamp for the same instant, kept for recordings and debugging; code above the body never does
  arithmetic with it. The adapter converts with the transport's clock-offset estimate
  (protocol/v0.md section 6); see `ClockSync`.
- **Frames**: every pose names its `frame_id`. Poses in different frames cannot be compared until a
  transform between the frames exists (card P5-01). The world and body frame conventions are those of
  protocol/v0.md section 4.4: right-handed, metres, +y up, the body looks along its own -z.
- **JSON**: every class has `to_json()` (a plain dict of JSON types) and `from_json()`; a round trip
  gives an equal object. Unknown keys are ignored on reading, as in protocol payloads.
- Everything a body reports is **data, never an instruction**.
"""

from __future__ import annotations

import base64
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# Identifiers that recorders may use as file names (same rule as protocol/v0.md section 2).
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}(?!\n)$")
EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}(?!\n)$")

ADAPTER_EVENT_NAMES = frozenset({"link_state"})  # raised only by adapters; refused when a body sends them

IMAGE_MEDIA_TYPES = {"jpeg": "image/jpeg", "png": "image/png"}
AUDIO_ENCODINGS = ("pcm_s16le",)


class TrackingState(StrEnum):
    """How far a pose can be trusted. With LOST the position is the last known one and may be wrong."""

    OK = "ok"
    LIMITED = "limited"
    LOST = "lost"


class Capability(StrEnum):
    """What a body can do beyond the mandatory core (status, events, stop). One name per gated method."""

    POSE = "pose"  # pose_stream()
    IMAGE = "image"  # capture_image()
    AUDIO_IN = "audio_in"  # audio_in_stream()
    AUDIO_OUT = "audio_out"  # play_audio()
    SPEAK_TEXT = "speak_text"  # speak_text(): the body's own text-to-speech
    NAVIGATE = "navigate"  # navigate(): the body moves itself


class ConfidenceBand(StrEnum):
    """Identity confidence, set from measured thresholds (card P3-09). Plan: "Confidence bands"."""

    CONFIRMED = "confirmed"
    PROBABLE = "probable"
    UNCERTAIN = "uncertain"
    UNKNOWN = "unknown"


# ---- small checks ------------------------------------------------------------------------------------


def _ident(value: Any, what: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.match(value):
        raise ValueError(f"{what} must be an identifier (letters, digits, '_', '-', '.'; at most 64), got {value!r:.80}")
    return value


def _int(value: Any, what: str, *, low: int = 0, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < low:
        raise ValueError(f"{what} must be an integer >= {low}, got {value!r:.80}")
    return value


def _float(value: Any, what: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{what} must be a finite number, got {value!r:.80}")
    return float(value)


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{what} must be a finite number, got {value!r:.80}")
    return float(value)


def _vector(value: Any, n: int, what: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != n:
        raise ValueError(f"{what} must be {n} numbers")
    return tuple(_number(x, what) for x in value)


# ---- clock -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ClockSync:
    """The adapter's current estimate of the body clock against the brain clock.

    `offset_us` = body clock minus brain clock, so `brain_time = t_body_us - offset_us`
    (protocol/v0.md section 6). It comes from the transport's ClockOffsetEstimator; there is no
    second estimator. `rtt_us` is the round trip of the sample the estimate rests on: the estimate is
    within `rtt_us / 2` of the truth. All None until the first sample ("not synced").
    """

    offset_us: float | None = None
    rtt_us: int | None = None
    drift_ppm: float | None = None

    @property
    def synced(self) -> bool:
        return self.offset_us is not None

    def to_brain_us(self, t_body_us: int) -> int | None:
        """A body timestamp on the brain's clock, or None while not synced."""
        return None if self.offset_us is None else round(t_body_us - self.offset_us)

    def to_json(self) -> dict[str, Any]:
        return {"offset_us": self.offset_us, "rtt_us": self.rtt_us, "drift_ppm": self.drift_ppm}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ClockSync:
        return cls(_float(d.get("offset_us"), "offset_us", optional=True),
                   _int(d.get("rtt_us"), "rtt_us", optional=True),
                   _float(d.get("drift_ppm"), "drift_ppm", optional=True))


# ---- pose --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Pose:
    """Where the body is, in the frame `frame_id`, at brain time `t_us`.

    `position_m` (x, y, z) is the body origin in the world frame; +y is up. `orientation_xyzw` is the
    unit quaternion rotating body-frame vectors into the world frame; the body faces its own -z.
    The JSON field names match the protocol's pose payload, so a wire payload plus `t_us` reads as is.
    """

    frame_id: str
    position_m: tuple[float, float, float]
    orientation_xyzw: tuple[float, float, float, float]
    tracking: TrackingState
    t_us: int
    t_body_us: int | None = None

    def __post_init__(self) -> None:
        _ident(self.frame_id, "frame_id")
        object.__setattr__(self, "position_m", _vector(self.position_m, 3, "position_m"))
        q = _vector(self.orientation_xyzw, 4, "orientation_xyzw")
        if abs(math.sqrt(sum(c * c for c in q)) - 1.0) > 1e-3:
            raise ValueError("orientation_xyzw must be a unit quaternion")
        object.__setattr__(self, "orientation_xyzw", q)
        object.__setattr__(self, "tracking", TrackingState(self.tracking))
        _int(self.t_us, "t_us")
        _int(self.t_body_us, "t_body_us", optional=True)

    @property
    def heading_rad(self) -> float | None:
        """Heading about +y per protocol/v0.md section 4.4: 0 facing world -z, counter-clockwise seen
        from above, in (-pi, pi]. None when the body looks straight up or down (heading undefined)."""
        x, y, z, w = self.orientation_xyzw
        s, c = 2.0 * (x * z + w * y), 1.0 - 2.0 * (x * x + y * y)
        if math.hypot(s, c) < 1e-6:
            return None
        return math.atan2(s, c)

    @property
    def heading_deg(self) -> float | None:
        h = self.heading_rad
        return None if h is None else math.degrees(h)

    def to_json(self) -> dict[str, Any]:
        return {"frame_id": self.frame_id, "position_m": list(self.position_m),
                "orientation_xyzw": list(self.orientation_xyzw), "tracking": self.tracking.value,
                "t_us": self.t_us, "t_body_us": self.t_body_us}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Pose:
        return cls(d["frame_id"], d["position_m"], d["orientation_xyzw"], d["tracking"], d["t_us"],
                   d.get("t_body_us"))


def quaternion_from_heading(heading_rad: float) -> tuple[float, float, float, float]:
    """The level orientation with the given heading (a turn of `heading_rad` about +y)."""
    return (0.0, math.sin(heading_rad / 2.0), 0.0, math.cos(heading_rad / 2.0))


@dataclass(frozen=True)
class NavGoal:
    """A place for a self-moving body to go: a position in a named frame, and optionally a final heading."""

    frame_id: str
    position_m: tuple[float, float, float]
    heading_rad: float | None = None

    def __post_init__(self) -> None:
        _ident(self.frame_id, "frame_id")
        object.__setattr__(self, "position_m", _vector(self.position_m, 3, "position_m"))
        object.__setattr__(self, "heading_rad", _float(self.heading_rad, "heading_rad", optional=True))

    def to_json(self) -> dict[str, Any]:
        return {"frame_id": self.frame_id, "position_m": list(self.position_m), "heading_rad": self.heading_rad}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> NavGoal:
        return cls(d["frame_id"], d["position_m"], d.get("heading_rad"))


# ---- image and audio ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Image:
    """One still. `t_us` is the capture time.

    `camera_pose` is the pose **of the camera that took the picture** at the capture time, if known:
    its optical centre in the world frame, looking along its own -z with +y up and +x right (the
    convention of protocol/v0.md section 4.4). On a body whose reference point is its camera (a
    hand-held device) this equals the body pose; a body with a camera mounted elsewhere (a robot)
    applies its mount transform in the adapter, so code that places what a picture shows never needs
    to know the body's geometry.

    `image_id` names the still in logs, caches and tool results; it is an identifier, never a path.
    """

    image_id: str
    encoding: str  # "jpeg" or "png"
    width_px: int
    height_px: int
    data: bytes = field(repr=False)
    t_us: int = 0
    t_body_us: int | None = None
    camera_pose: Pose | None = None

    def __post_init__(self) -> None:
        _ident(self.image_id, "image_id")
        if self.encoding not in IMAGE_MEDIA_TYPES:
            raise ValueError(f"image encoding must be one of {sorted(IMAGE_MEDIA_TYPES)}, got {self.encoding!r:.40}")
        _int(self.width_px, "width_px", low=1)
        _int(self.height_px, "height_px", low=1)
        if not isinstance(self.data, bytes):
            raise ValueError("image data must be bytes")
        _int(self.t_us, "t_us")
        _int(self.t_body_us, "t_body_us", optional=True)

    @property
    def media_type(self) -> str:
        return IMAGE_MEDIA_TYPES[self.encoding]

    def to_json(self, *, include_data: bool = True) -> dict[str, Any]:
        """With include_data=False the bytes are left out (for logs and tool results); reading that
        back gives an Image with empty data."""
        d = {"image_id": self.image_id, "encoding": self.encoding, "width_px": self.width_px,
             "height_px": self.height_px, "byte_length": len(self.data), "t_us": self.t_us,
             "t_body_us": self.t_body_us,
             "camera_pose": None if self.camera_pose is None else self.camera_pose.to_json()}
        if include_data:
            d["data_b64"] = base64.b64encode(self.data).decode("ascii")
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Image:
        pose = d.get("camera_pose")
        return cls(d["image_id"], d["encoding"], d["width_px"], d["height_px"],
                   base64.b64decode(d.get("data_b64", ""), validate=True), d["t_us"], d.get("t_body_us"),
                   None if pose is None else Pose.from_json(pose))


@dataclass(frozen=True)
class AudioChunk:
    """A run of PCM samples, interleaved little-endian signed 16-bit.

    For microphone audio `t_us` is the time of the first sample, `stream_id` changes each time the
    stream is started and `seq` counts chunks from 0 within it, so a missing chunk shows as a gap.
    For audio to be played, `seq` counts from 0 within the utterance and `t_us` is not used.
    """

    stream_id: int
    seq: int
    sample_rate_hz: int
    channels: int
    data: bytes = field(repr=False)
    t_us: int = 0
    t_body_us: int | None = None
    encoding: str = "pcm_s16le"

    def __post_init__(self) -> None:
        _int(self.stream_id, "stream_id")
        _int(self.seq, "seq")
        _int(self.sample_rate_hz, "sample_rate_hz", low=8000)
        _int(self.channels, "channels", low=1)
        if self.encoding not in AUDIO_ENCODINGS:
            raise ValueError(f"audio encoding must be one of {AUDIO_ENCODINGS}, got {self.encoding!r:.40}")
        if not isinstance(self.data, bytes) or len(self.data) % (2 * self.channels):
            raise ValueError("audio data must be whole 16-bit frames")
        _int(self.t_us, "t_us")
        _int(self.t_body_us, "t_body_us", optional=True)

    @property
    def frames(self) -> int:
        return len(self.data) // (2 * self.channels)

    @property
    def duration_s(self) -> float:
        return self.frames / self.sample_rate_hz

    def to_json(self) -> dict[str, Any]:
        return {"stream_id": self.stream_id, "seq": self.seq, "encoding": self.encoding,
                "sample_rate_hz": self.sample_rate_hz, "channels": self.channels,
                "byte_length": len(self.data), "data_b64": base64.b64encode(self.data).decode("ascii"),
                "t_us": self.t_us, "t_body_us": self.t_body_us}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> AudioChunk:
        return cls(d["stream_id"], d["seq"], d["sample_rate_hz"], d["channels"],
                   base64.b64decode(d["data_b64"], validate=True), d["t_us"], d.get("t_body_us"),
                   d.get("encoding", "pcm_s16le"))


# ---- speech and identity (produced by the brain's own pipeline, not by a body) -------------------------


@dataclass(frozen=True)
class Speaker:
    """Who the identity layer thinks is speaking. Claude receives this as data and never produces it."""

    person_id: str | None
    name: str | None
    band: ConfidenceBand
    voice_score: float | None = None

    def __post_init__(self) -> None:
        if self.person_id is not None:
            _ident(self.person_id, "person_id")
        object.__setattr__(self, "band", ConfidenceBand(self.band))
        object.__setattr__(self, "voice_score", _float(self.voice_score, "voice_score", optional=True))

    def to_json(self) -> dict[str, Any]:
        return {"person_id": self.person_id, "name": self.name, "band": self.band.value,
                "voice_score": self.voice_score}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Speaker:
        return cls(d.get("person_id"), d.get("name"), d["band"], d.get("voice_score"))


@dataclass(frozen=True)
class Utterance:
    """One segmented stretch of speech with its transcript. Times are brain-clock microseconds.

    `audio_ref` names the stored audio (an identifier for the recorder, never a path), or None.
    """

    utterance_id: str
    text: str
    t_start_us: int
    t_end_us: int
    speaker: Speaker | None = None
    audio_ref: str | None = None

    def __post_init__(self) -> None:
        _ident(self.utterance_id, "utterance_id")
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")
        _int(self.t_start_us, "t_start_us")
        _int(self.t_end_us, "t_end_us", low=self.t_start_us)
        if self.audio_ref is not None:
            _ident(self.audio_ref, "audio_ref")

    @property
    def duration_s(self) -> float:
        return (self.t_end_us - self.t_start_us) / 1e6

    def to_json(self) -> dict[str, Any]:
        return {"utterance_id": self.utterance_id, "text": self.text, "t_start_us": self.t_start_us,
                "t_end_us": self.t_end_us,
                "speaker": None if self.speaker is None else self.speaker.to_json(), "audio_ref": self.audio_ref}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Utterance:
        sp = d.get("speaker")
        return cls(d["utterance_id"], d["text"], d["t_start_us"], d["t_end_us"],
                   None if sp is None else Speaker.from_json(sp), d.get("audio_ref"))


# ---- events and status -------------------------------------------------------------------------------


@dataclass(frozen=True)
class BodyEvent:
    """Something happened on the body (`source` "body") or to the adapter's link to it ("adapter").

    `name` is snake_case; `data` is a small JSON object. Events are information: nothing is ever
    executed because of one. **Consumers that act on the link's state must check `source`**: only an
    event with `source == "adapter"` is the adapter's own word. Adapters also refuse the names in
    `ADAPTER_EVENT_NAMES` from a body, so a body cannot announce a false `link_state`. Names in use: `link_state` {connected}, `tracking_state` {state, reason},
    `navigation` {goal_id, state}, plus whatever the body sends (`button`, `battery`, `thermal`, ...).
    """

    name: str
    data: dict[str, Any]
    t_us: int
    t_body_us: int | None = None
    source: str = "body"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not EVENT_NAME.match(self.name):
            raise ValueError(f"event name must be snake_case, got {self.name!r:.80}")
        if not isinstance(self.data, dict):
            raise ValueError("event data must be an object")
        if self.source not in ("body", "adapter"):
            raise ValueError("event source must be 'body' or 'adapter'")
        _int(self.t_us, "t_us")
        _int(self.t_body_us, "t_body_us", optional=True)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "data": self.data, "t_us": self.t_us, "t_body_us": self.t_body_us,
                "source": self.source}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> BodyEvent:
        return cls(d["name"], d["data"], d["t_us"], d.get("t_body_us"), d.get("source", "body"))


@dataclass(frozen=True)
class BodyStatus:
    """Read-only health of the body. Any field the body does not report is None.

    `connected` False means the adapter cannot reach its body right now; the other fields are then
    None and `capabilities` is empty. `stale` True means the body is connected but did not answer
    this status request (it timed out, was busy or refused), so the readings are missing (None), not
    fresh; a watchdog treats repeated stale answers as a sick body. `capabilities` lists Capability
    values, sorted.
    """

    connected: bool
    battery_pct: float | None = None
    charging: bool | None = None
    temperature_c: float | None = None
    link_rtt_ms: float | None = None
    capabilities: tuple[str, ...] = ()
    t_us: int = 0
    stale: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.connected, bool) or not isinstance(self.stale, bool):
            raise ValueError("connected and stale must be booleans")
        b = _float(self.battery_pct, "battery_pct", optional=True)
        if b is not None and not 0.0 <= b <= 100.0:
            raise ValueError("battery_pct must be 0 to 100")
        object.__setattr__(self, "battery_pct", b)
        if self.charging is not None and not isinstance(self.charging, bool):
            raise ValueError("charging must be a boolean or None")
        object.__setattr__(self, "temperature_c", _float(self.temperature_c, "temperature_c", optional=True))
        object.__setattr__(self, "link_rtt_ms", _float(self.link_rtt_ms, "link_rtt_ms", optional=True))
        object.__setattr__(self, "capabilities", tuple(sorted(Capability(c).value for c in self.capabilities)))
        _int(self.t_us, "t_us")

    def to_json(self) -> dict[str, Any]:
        return {"connected": self.connected, "battery_pct": self.battery_pct, "charging": self.charging,
                "temperature_c": self.temperature_c, "link_rtt_ms": self.link_rtt_ms,
                "capabilities": list(self.capabilities), "t_us": self.t_us, "stale": self.stale}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> BodyStatus:
        return cls(d["connected"], d.get("battery_pct"), d.get("charging"), d.get("temperature_c"),
                   d.get("link_rtt_ms"), tuple(d.get("capabilities", ())), d.get("t_us", 0), d.get("stale", False))
