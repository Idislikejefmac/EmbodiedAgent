"""SimBody: a scripted body with no hardware, for development and tests with the real body switched off.

What it simulates:

- **Pose**: a walk round a square (side `side_m`, at `walk_speed_m_s`, carried at `height_m`), starting
  at the origin facing world -z and turning left at each corner. The pose is a pure function of time.
- **Images**: the PNG and JPEG files of `image_dir` in name order, round and round; with no folder,
  three built-in synthetic PNGs. The camera is the body's reference point, so `camera_pose` is the
  body pose.
- **Microphone**: the 16-bit WAV files of `audio_dir` in name order, round and round, in 80 ms
  chunks in real time; with no folder, two built-in tones.
- **Speaker, speech, navigation, stop**: recorded in plain attributes (`played`, `spoken`, `stops`)
  so tests can see what the brain asked for. `navigate` moves the simulated pose to the goal.
- **Its own clock**: body time runs `clock_offset_us` ahead of brain time, so code that confuses the
  two clocks fails in tests instead of on the real link.
- **A link that can drop**: `set_link(False)` makes the body unreachable while the adapter stays up
  (streams go quiet, capabilities vanish, a `link_state` event is raised), `set_link(True)` restores it.

`capabilities` can be narrowed to imitate a body that lacks something.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .. import obs
from ..transport.protocol import now_us
from . import sim_assets
from .base import (MAX_EDGE_PX_RANGE, MAX_POSE_RATE_HZ, Body, BodyError, BodyUnavailable, check_real,
                   check_whole)
from .streams import AUDIO_QUEUE, POSE_QUEUE, BodyStream, Fanout, Subscription, spawn_stream
from .types import (ADAPTER_EVENT_NAMES, IDENTIFIER, AudioChunk, BodyEvent, BodyStatus, Capability, ClockSync, Image, NavGoal, Pose,
                    TrackingState, quaternion_from_heading)

log = logging.getLogger("brain.body.sim")


def new_frame_id() -> str:
    """A coordinate frame no other run can share: `sim-` and a random UUID (DEC-081, H-30). 36 characters."""
    return f"sim-{uuid.uuid4().hex}"

CHUNK_S = 0.080  # microphone chunk length, as on the wire (80 ms)
AUDIO_EVENT_EVERY = 25  # a body.audio event for the first chunk and every 25th (2 s), not every chunk
ALL_CAPABILITIES = frozenset(Capability)

Place = tuple[tuple[float, float, float], float]  # (position in metres, heading in radians)


class SimBody(Body):
    def __init__(self, *, image_dir: Path | str | None = None, audio_dir: Path | str | None = None,
                 capabilities: Iterable[Capability] | None = None, side_m: float = 2.0,
                 walk_speed_m_s: float = 0.5, height_m: float = 1.4, nav_speed_m_s: float = 1.0,
                 clock_offset_us: int = 76_222_231_000, battery_pct: float = 87.0, temperature_c: float = 31.0,
                 pose_queue: int = POSE_QUEUE, audio_queue: int = AUDIO_QUEUE):
        self._image_dir = None if image_dir is None else Path(image_dir)
        self._audio_dir = None if audio_dir is None else Path(audio_dir)
        self._capabilities = ALL_CAPABILITIES if capabilities is None else frozenset(Capability(c) for c in capabilities)
        self.side_m = check_real("side_m", side_m)
        self.walk_speed_m_s = check_real("walk_speed_m_s", walk_speed_m_s)
        self.height_m = check_real("height_m", height_m)
        self.nav_speed_m_s = check_real("nav_speed_m_s", nav_speed_m_s)
        self.clock_offset_us = check_whole("clock_offset_us", clock_offset_us, low=-(2**63))
        self.battery_pct = check_real("battery_pct", battery_pct)
        self.temperature_c = check_real("temperature_c", temperature_c)
        self._pose_queue = check_whole("pose_queue", pose_queue)
        self._audio_queue = check_whole("audio_queue", audio_queue)
        self._running = False
        self._link_up = False
        self._events: Fanout[BodyEvent] = Fanout()
        self._producers: set[asyncio.Task[None]] = set()
        self._images: list[tuple[str, bytes]] = []
        self._audio: list[tuple[int, int, bytes]] = []
        self._image_count = 0
        self._stream_count = 0
        self._frame_id = new_frame_id()
        self._tracking = TrackingState.OK
        # Motion: "walk" (the square), "hold" (standing still) or "nav" (moving to a goal).
        self._mode = "walk"
        self._t0_us = 0
        self._hold: Place = ((0.0, height_m, 0.0), 0.0)
        self._nav: tuple[tuple[float, float, float], tuple[float, float, float], int, float | None] | None = None
        self._nav_task: asyncio.Task[None] | None = None
        self._goal_count = 0
        self._goal_id: str | None = None
        # What the brain asked the simulated actuators to do.
        self.played: dict[str, list[AudioChunk]] = {}
        self.finished_utterances: list[str] = []
        self.spoken: list[tuple[str, str, bool]] = []  # (utterance_id, text, interrupt)
        self.stops: list[tuple[bool, bool]] = []  # (motion, audio)

    # ---- identity and lifecycle ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "sim"

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self._capabilities if self.connected else frozenset()

    @property
    def connected(self) -> bool:
        return self._running and self._link_up

    @property
    def frame_id(self) -> str:
        return self._frame_id

    async def start(self) -> None:
        if self._running:
            return
        images = (sim_assets.default_images() if self._image_dir is None else
                  {p.name: p.read_bytes() for p in sorted(self._image_dir.iterdir())
                   if p.suffix.lower() in (".png", ".jpg", ".jpeg")})
        audio = (sim_assets.default_audio() if self._audio_dir is None else
                 {p.name: p.read_bytes() for p in sorted(self._audio_dir.iterdir()) if p.suffix.lower() == ".wav"})
        self._images = sorted(images.items())
        self._audio = [sim_assets.read_wav(data) for _, data in sorted(audio.items())]
        if not self._images or not self._audio:
            raise ValueError("SimBody needs at least one image and one WAV file")
        self._t0_us = now_us()
        self._frame_id = new_frame_id()  # tracking starts afresh: a frame no earlier run can share (DEC-081)
        self._events = Fanout()
        self._running = True
        obs.emit("body.start", "sim body started", body="sim", images=len(self._images), audio_clips=len(self._audio),
                 capabilities=sorted(c.value for c in self._capabilities))
        self.set_link(True)
        log.info("sim body started: %d images, %d audio clips, frame %s", len(self._images), len(self._audio),
                 self.frame_id)

    async def close(self) -> None:
        if not self._running:
            return
        self._cancel_nav("cancelled")
        self.set_link(False)
        self._running = False
        for task in list(self._producers):
            task.cancel()  # each producer ends its stream as it goes
        self._events.close()
        obs.emit("body.close", "sim body closed", body="sim")

    async def wait_connected(self, timeout_s: float) -> bool:
        return self.connected

    def clock_sync(self) -> ClockSync:
        return ClockSync(offset_us=float(self.clock_offset_us), rtt_us=0, drift_ppm=0.0)

    def _need_link(self) -> None:
        if not self.connected:
            raise BodyUnavailable("the sim body is not started" if not self._running else "the sim body's link is down")

    # ---- mandatory behaviour ---------------------------------------------------------------------

    async def status(self) -> BodyStatus:
        if not self.connected:
            obs.emit("body.status", "sim body not connected", body="sim", connected=False)
            return BodyStatus(connected=False, t_us=now_us())
        obs.emit("body.status", "sim body status", body="sim", connected=True, stale=False,
                 battery_pct=self.battery_pct, charging=False, temperature_c=self.temperature_c)
        return BodyStatus(connected=True, battery_pct=self.battery_pct, charging=False,
                          temperature_c=self.temperature_c, link_rtt_ms=0.0,
                          capabilities=tuple(c.value for c in self._capabilities), t_us=now_us())

    def events(self) -> BodyStream[BodyEvent]:
        return self._events.subscribe()

    async def stop(self, *, motion: bool = True, audio: bool = True) -> None:
        self._need_link()
        obs.emit("body.stop", "stop", body="sim", motion=motion, audio=audio)
        self.stops.append((motion, audio))
        if motion:
            if self._mode == "nav":
                self._cancel_nav("cancelled")
            elif self._mode == "walk":
                self._hold_here()

    # ---- pose ------------------------------------------------------------------------------------

    def _square(self, distance_m: float) -> Place:
        """Position and heading after walking `distance_m` round the square from the origin."""
        s = self.side_m
        leg, along = divmod(distance_m % (4 * s), s)
        x, z = ((0.0, -along), (-along, -s), (-s, -s + along), (-s + along, 0.0))[int(leg)]
        return (x, self.height_m, z), int(leg) * math.pi / 2

    def _where(self, t_us: int) -> Place:
        if self._mode == "walk":
            return self._square(self.walk_speed_m_s * (t_us - self._t0_us) / 1e6)
        if self._mode == "nav" and self._nav is not None:
            start, end, t_start, final_heading = self._nav
            length = math.dist(start, end)
            done = 1.0 if length == 0 else min(1.0, self.nav_speed_m_s * (t_us - t_start) / 1e6 / length)
            pos = (start[0] + (end[0] - start[0]) * done, start[1] + (end[1] - start[1]) * done,
                   start[2] + (end[2] - start[2]) * done)
            travel = math.atan2(-(end[0] - start[0]), -(end[2] - start[2])) if length else self._hold[1]
            return pos, final_heading if done >= 1.0 and final_heading is not None else travel
        return self._hold

    def _hold_here(self) -> None:
        self._hold = self._where(now_us())
        self._mode = "hold"

    def pose_now(self) -> Pose:
        t = now_us()
        pos, heading = self._where(t)
        return Pose(self.frame_id, pos, quaternion_from_heading(heading), self._tracking, t, t + self.clock_offset_us)

    def pose_stream(self, *, rate_hz: float = 10.0) -> BodyStream[Pose]:
        self.require(Capability.POSE)
        rate_hz = check_real("rate_hz", rate_hz, low=0, high=MAX_POSE_RATE_HZ, low_open=True)  # a bool is a TypeError

        async def produce(stream: Subscription[Pose]) -> None:
            loop = asyncio.get_running_loop()
            due = loop.time()
            while self._running:
                if self._link_up:  # a dropped link makes the stream go quiet, not end
                    stream.push(self.pose_now())
                due += 1.0 / rate_hz
                await asyncio.sleep(max(0.0, due - loop.time()))

        return spawn_stream(self._pose_queue, produce, self._producers, name="pose")

    # ---- test controls (what a real body would do on its own) --------------------------------------

    def set_link(self, up: bool) -> None:
        """Drop or restore the simulated link while the adapter stays started."""
        if not self._running or up == self._link_up:
            return
        self._link_up = up
        t = now_us()
        obs.emit("body.link", f"sim link {'up' if up else 'down'}", body="sim", connected=up)
        obs.emit("body.capabilities", "capabilities changed", body="sim", connected=up,
                 capabilities=sorted(c.value for c in self.capabilities))
        self._events.publish(BodyEvent("link_state", {"connected": up}, t, source="adapter"))

    def set_tracking(self, state: TrackingState, reason: str | None = None) -> None:
        self._tracking = TrackingState(state)
        self._emit("tracking_state", {"state": self._tracking.value, "reason": reason})

    def reset_frame(self) -> str:
        """Tracking restarted: later poses are in a new frame that cannot be related to the old one."""
        self._frame_id = new_frame_id()
        return self.frame_id

    def inject_event(self, name: str, data: dict[str, Any] | None = None) -> None:
        """Have the simulated body report an event. Names reserved for adapters are dropped, as every
        adapter must drop them when a body sends one."""
        if name in ADAPTER_EVENT_NAMES:
            log.warning("dropped a body event named %r: that name is reserved for the adapter", name)
            return
        self._emit(name, data or {})

    def _emit(self, name: str, data: dict[str, Any]) -> None:
        obs.emit("body.event", "body event", body="sim", name=name, data_keys=len(data))
        t = now_us()
        self._events.publish(BodyEvent(name, data, t, t + self.clock_offset_us))

    # ---- camera ----------------------------------------------------------------------------------

    async def capture_image(self, *, max_edge_px: int = 640) -> Image:
        self.require(Capability.IMAGE)
        low, high = MAX_EDGE_PX_RANGE
        if not isinstance(max_edge_px, int) or isinstance(max_edge_px, bool) or not low <= max_edge_px <= high:
            obs.emit("validation.failed", "max_edge_px refused", parameter="max_edge_px", problem="not an integer in range")
            raise ValueError(f"max_edge_px must be an integer from {low} to {high}")
        _, data = self._images[self._image_count % len(self._images)]
        self._image_count += 1
        encoding, width, height = sim_assets.image_size(data)
        if max(width, height) > max_edge_px:
            if encoding != "png":
                raise BodyError(f"the sim body cannot shrink a {encoding} image; use files no larger than "
                                f"max_edge_px or PNG files", code="unsupported")
            data, width, height = sim_assets.shrink_png(data, max_edge_px)
        pose = self.pose_now() if Capability.POSE in self._capabilities else None
        t = now_us()
        obs.emit("body.frame", "camera still", body="sim", image_id=f"sim-img-{self._image_count}", encoding=encoding,
                 width_px=width, height_px=height, size_bytes=len(data))
        return Image(f"sim-img-{self._image_count}", encoding, width, height, data, t, t + self.clock_offset_us, pose)

    # ---- microphone ------------------------------------------------------------------------------

    def audio_in_stream(self) -> BodyStream[AudioChunk]:
        self.require(Capability.AUDIO_IN)
        self._stream_count += 1
        stream_id = self._stream_count

        async def produce(stream: Subscription[AudioChunk]) -> None:
            loop = asyncio.get_running_loop()
            due = loop.time()
            t_first = now_us()  # brain time of the first sample of chunk 0
            seq = 0
            clip = 0
            while self._running:
                rate, channels, pcm = self._audio[clip % len(self._audio)]
                clip += 1
                step = int(rate * CHUNK_S) * 2 * channels
                for start in range(0, len(pcm), step):
                    due += CHUNK_S
                    await asyncio.sleep(max(0.0, due - loop.time()))  # a chunk exists once its last sample is in
                    if not self._running:
                        return
                    t = t_first + round(seq * CHUNK_S * 1e6)
                    if self._link_up:  # chunks lost while the link is down show as a gap in seq
                        stream.push(AudioChunk(stream_id, seq, rate, channels, pcm[start:start + step], t,
                                               t + self.clock_offset_us))
                        if seq % AUDIO_EVENT_EVERY == 0:
                            obs.emit("body.audio", "microphone audio", body="sim", stream_id=stream_id, seq=seq,
                                     sample_rate_hz=rate)
                    seq += 1

        return spawn_stream(self._audio_queue, produce, self._producers, name="microphone")

    # ---- speaker ---------------------------------------------------------------------------------

    @staticmethod
    def _check_utterance_id(utterance_id: str) -> None:
        if not isinstance(utterance_id, str) or not IDENTIFIER.match(utterance_id):
            obs.emit("validation.failed", "utterance_id refused", parameter="utterance_id", problem="not an identifier")
            raise ValueError("utterance_id must be an identifier")

    async def play_audio(self, chunk: AudioChunk, *, utterance_id: str, final: bool = False) -> None:
        self.require(Capability.AUDIO_OUT)
        self._check_utterance_id(utterance_id)
        self.played.setdefault(utterance_id, []).append(chunk)
        if chunk.seq == 0 or final:
            obs.emit("body.speak", "speaker audio", body="sim", utterance_id=utterance_id, seq=chunk.seq, final=final)
        if final:
            self.finished_utterances.append(utterance_id)

    async def speak_text(self, text: str, *, utterance_id: str, interrupt: bool = False) -> None:
        self.require(Capability.SPEAK_TEXT)
        self._check_utterance_id(utterance_id)
        if not isinstance(text, str) or not text:
            obs.emit("validation.failed", "speech refused", parameter="speech", problem="not a non-empty string")
            raise ValueError("text must be a non-empty string")
        obs.emit("body.speak", "speak text", body="sim", utterance_id=utterance_id, chars=len(text),
                 interrupt=interrupt)
        self.spoken.append((utterance_id, text, interrupt))

    # ---- navigation ------------------------------------------------------------------------------

    async def navigate(self, goal: NavGoal) -> str:
        self.require(Capability.NAVIGATE)
        if goal.frame_id != self.frame_id:
            raise BodyError(f"goal is in frame {goal.frame_id!r} but the body is in {self.frame_id!r}", code="bad_frame")
        self._cancel_nav("cancelled")
        t = now_us()
        start, heading = self._where(t)
        self._hold = (start, heading)
        end = (goal.position_m[0], self.height_m, goal.position_m[2])  # the sim stays at carrying height
        self._nav = (start, end, t, goal.heading_rad)
        self._mode = "nav"
        self._goal_count += 1
        self._goal_id = goal_id = f"nav-{self._goal_count}"
        obs.emit("body.navigate", "navigation started", body="sim", goal_id=goal_id, state="started")
        self._emit("navigation", {"goal_id": goal_id, "state": "started"})
        self._nav_task = asyncio.create_task(self._arrive(goal_id, math.dist(start, end) / self.nav_speed_m_s))
        return goal_id

    async def _arrive(self, goal_id: str, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if self._goal_id == goal_id and self._nav is not None:
            _, end, _, final_heading = self._nav
            self._hold = (end, self._where(now_us() + 10**9)[1] if final_heading is None else final_heading)
            self._mode, self._nav, self._goal_id, self._nav_task = "hold", None, None, None
            self._emit("navigation", {"goal_id": goal_id, "state": "arrived"})

    def _cancel_nav(self, state: str) -> None:
        if self._mode != "nav":
            return
        self._hold_here()
        if self._nav_task is not None:
            self._nav_task.cancel()
        goal_id, self._goal_id, self._nav, self._nav_task = self._goal_id, None, None, None
        self._emit("navigation", {"goal_id": goal_id, "state": "cancelled"})
