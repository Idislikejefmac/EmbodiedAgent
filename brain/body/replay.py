"""ReplayBody: a recorded session played back as a body. A stub; card P1-03 builds it.

Recordings are stored as protocol v0 messages (protocol/v0.md), so the real ReplayBody will feed the
recorded envelopes through the same decoding as LinkBody, with the original timing. Until then it
behaves as a body that is never reachable: status says not connected, nothing is supported.

Note for P1-03: recorded times cannot be replayed as they are. `t_us` is the brain's monotonic clock
of the process that made the recording, and a new process's clock starts somewhere else. ReplayBody
must re-base every recorded `t_us` onto the replaying process's clock (new `t_us` = replay start +
(recorded `t_us` - recording start), keeping the original spacing and never below 0), and keep the
recorded stamps only in `t_body_us` or a field of their own.
"""

from __future__ import annotations

from ..transport.protocol import now_us
from .base import Body, BodyUnavailable
from .streams import BodyStream, Fanout
from .types import IDENTIFIER, AudioChunk, BodyEvent, BodyStatus, Capability, ClockSync, Image, NavGoal, Pose

NOT_BUILT = "replay is card P1-03"


class ReplayBody(Body):
    def __init__(self, session_id: str):
        if not IDENTIFIER.match(session_id):
            raise ValueError("session_id must be an identifier, not a path")
        self.session_id = session_id
        self._events: Fanout[BodyEvent] = Fanout()

    @property
    def name(self) -> str:
        return "replay"

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset()

    @property
    def connected(self) -> bool:
        return False

    async def start(self) -> None:
        self._events = Fanout()

    async def close(self) -> None:
        self._events.close()

    async def wait_connected(self, timeout_s: float) -> bool:
        return False

    def clock_sync(self) -> ClockSync:
        return ClockSync()

    async def status(self) -> BodyStatus:
        return BodyStatus(connected=False, t_us=now_us())

    def events(self) -> BodyStream[BodyEvent]:
        return self._events.subscribe()

    async def stop(self, *, motion: bool = True, audio: bool = True) -> None:
        raise BodyUnavailable(f"no recording is playing ({NOT_BUILT})")

    def pose_stream(self, *, rate_hz: float = 10.0) -> BodyStream[Pose]:
        self.require(Capability.POSE, not_built=NOT_BUILT)
        raise AssertionError("unreachable")

    async def capture_image(self, *, max_edge_px: int = 640) -> Image:
        self.require(Capability.IMAGE, not_built=NOT_BUILT)
        raise AssertionError("unreachable")

    def audio_in_stream(self) -> BodyStream[AudioChunk]:
        self.require(Capability.AUDIO_IN, not_built=NOT_BUILT)
        raise AssertionError("unreachable")

    async def play_audio(self, chunk: AudioChunk, *, utterance_id: str, final: bool = False) -> None:
        self.require(Capability.AUDIO_OUT, not_built=NOT_BUILT)
        raise AssertionError("unreachable")

    async def speak_text(self, text: str, *, utterance_id: str, interrupt: bool = False) -> None:
        self.require(Capability.SPEAK_TEXT, not_built=NOT_BUILT)
        raise AssertionError("unreachable")

    async def navigate(self, goal: NavGoal) -> str:
        self.require(Capability.NAVIGATE, not_built=NOT_BUILT)
        raise AssertionError("unreachable")
