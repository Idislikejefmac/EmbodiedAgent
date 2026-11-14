"""The Body interface, version 0 (card P0-06).

A body is a thin shell of sensors and actuators. Everything in the brain above this module talks to
a `Body` and must be unable to tell which one it has: a phone carried by a person, a scripted
simulator, a replay of a recorded session, or a wheeled robot. docs/adding_a_body.md is the one-page
guide; tests/body_contract.py is the test suite every body must pass.

Shape of the interface:

- **Async.** The transport is asyncio and every call can wait on a remote device, so all I/O is
  `async def`. Cheap facts already held by the adapter are plain properties.
- **Request/response** for things asked for once: `status()`, `capture_image()`, `navigate()`,
  `stop()`, `speak_text()`, `play_audio()` (one call per chunk, so awaiting it is the backpressure).
- **Streams** for things that keep coming: `events()`, `pose_stream()`, `audio_in_stream()`. Each call
  returns an independent `BodyStream` that the caller must close, from any task. A stream does not
  end when the body drops off the link; it goes quiet and a `link_state` event says why. It ends when
  the body is closed. A slow consumer loses its oldest items (brain/body/streams.py).
- **Mandatory core**, on every body: `start`, `close`, `connected`, `wait_connected`, `clock_sync`,
  `status`, `events`, `stop`.
- **Capability-gated** methods: each is named by one `Capability`. If the capability is in
  `body.capabilities` the method works; if not, the method raises `NotSupported` *when called* (for
  streams: at the call, not at the first item) and does nothing. `NotYetImplemented` is the subclass
  an adapter raises for something its body could do but the adapter has not built yet. Callers check
  `body.supports(...)` or catch `NotSupported`; they never ask what kind of body it is.
  The gated methods are **not abstract**: the default here refuses, so a body overrides only what it
  has, and a capability added to the interface later (a lidar scan, odometry) needs no change to the
  bodies that lack it.
- `capabilities` can change while running (a body connects, announces more, or drops off), so check
  it at the time of use, not once at start.

v0: breaking changes are allowed until gate G2, then the interface freezes and is versioned (R-07).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .streams import BodyStream
from .types import (AudioChunk, BodyEvent, BodyStatus, Capability, ClockSync, Image, NavGoal, Pose)

INTERFACE_VERSION = 0

MAX_EDGE_PX_RANGE = (64, 4096)  # capture_image bounds, as in protocol/v0.md section 8
MAX_POSE_RATE_HZ = 30.0


class BodyError(Exception):
    """A body could not do what was asked. `code` is a short machine-readable word."""

    code = "body_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class NotSupported(BodyError):
    """This body does not have the capability the method needs (it is absent from `capabilities`)."""

    code = "not_supported"

    def __init__(self, capability: Capability, message: str | None = None):
        super().__init__(message or f"this body has no '{capability.value}' capability")
        self.capability = capability


class NotYetImplemented(NotSupported):
    """The body could do this, but its adapter has not been built that far yet."""

    code = "not_implemented"


class BodyUnavailable(BodyError):
    """The adapter cannot reach its body right now (not connected, or the link dropped mid-request)."""

    code = "unavailable"


class BodyBusy(BodyError):
    """The body is reachable but cannot serve this request now (for example the camera is in use)."""

    code = "busy"


class BodyTimeout(BodyError):
    """The body did not answer in time."""

    code = "timeout"


class Body(ABC):
    """See the module docstring. Units and time conventions are in brain/body/types.py."""

    # ---- identity and lifecycle (mandatory) ----------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """A short label for logs and operators only. Never shown to the agent and never branched on."""

    @property
    @abstractmethod
    def capabilities(self) -> frozenset[Capability]:
        """The gated methods that work right now. May change while running."""

    @property
    @abstractmethod
    def connected(self) -> bool:
        """True while the adapter can reach its body."""

    @abstractmethod
    async def start(self) -> None:
        """Bring the adapter up (open ports, load files). The body itself may connect later."""

    @abstractmethod
    async def close(self) -> None:
        """Release everything and end every stream. Safe to call twice."""

    @abstractmethod
    async def wait_connected(self, timeout_s: float) -> bool:
        """Wait up to `timeout_s` seconds for the body to be reachable. True if it is."""

    @abstractmethod
    def clock_sync(self) -> ClockSync:
        """The current body-clock offset estimate. Data objects already carry brain time in `t_us`;
        this is for diagnostics, recorders and latency measurement."""

    # ---- mandatory behaviour ---------------------------------------------------------------------

    @abstractmethod
    async def status(self) -> BodyStatus:
        """Battery, temperature, link quality, capabilities. Read-only. **Never raises** for anything the
        body does or fails to do: an unreachable body gives `BodyStatus(connected=False)`, and a
        connected body that does not answer (timeout, busy, refusal) gives `connected=True, stale=True`
        with the readings None. A watchdog can therefore poll it without handling exceptions."""

    @abstractmethod
    def events(self) -> BodyStream[BodyEvent]:
        """Events from now on: those the body sends and those the adapter raises about its link
        (`link_state`). Earlier events are not replayed."""

    @abstractmethod
    async def stop(self, *, motion: bool = True, audio: bool = True) -> None:
        """Stop what the body is doing: `motion` halts any movement and cancels navigation; `audio`
        stops speaker output at once. Always allowed and never capability-gated; with nothing to stop
        it simply returns. Raises BodyUnavailable if the body cannot be reached, which is why a body
        that moves MUST also stop by itself when it loses the link."""

    # ---- capability-gated (the defaults refuse; a body overrides what it has) -----------------------

    def _refuse(self, capability: Capability) -> NotSupported:
        """The error for a gated method this body does not implement. If the body nevertheless lists
        the capability, that is an adapter bug and the message says so."""
        if capability in self.capabilities:
            return NotSupported(capability, f"this body lists '{capability.value}' but does not implement it")
        return NotSupported(capability)

    def pose_stream(self, *, rate_hz: float = 10.0) -> BodyStream[Pose]:
        """[Capability.POSE] Poses at about `rate_hz` (0 < rate_hz <= 30). The rate is a request: use
        each pose's `t_us`, never a count of poses, to measure time. Poses keep coming while tracking
        is limited or lost, marked as such."""
        raise self._refuse(Capability.POSE)

    async def capture_image(self, *, max_edge_px: int = 640) -> Image:
        """[Capability.IMAGE] One still whose longer edge is at most `max_edge_px` pixels (64 to 4096).
        The body never enlarges an image to reach it. `Image.camera_pose` is the pose of the camera
        that took it."""
        raise self._refuse(Capability.IMAGE)

    def audio_in_stream(self) -> BodyStream[AudioChunk]:
        """[Capability.AUDIO_IN] Microphone audio in chunks (nominally 80 ms of 16 kHz mono). The
        microphone runs only while at least one such stream is open."""
        raise self._refuse(Capability.AUDIO_IN)

    async def play_audio(self, chunk: AudioChunk, *, utterance_id: str, final: bool = False) -> None:
        """[Capability.AUDIO_OUT] Queue one chunk of an utterance on the speaker. Chunks of one
        `utterance_id` are sent in `seq` order; `final=True` marks the last (it may be empty)."""
        raise self._refuse(Capability.AUDIO_OUT)

    async def speak_text(self, text: str, *, utterance_id: str, interrupt: bool = False) -> None:
        """[Capability.SPEAK_TEXT] Speak `text` with the body's own text-to-speech. The text is spoken,
        never interpreted. `interrupt=True` cuts off whatever is being said. Returns once the body has
        the request, not when speech ends. (The G1 skeleton's voice; streamed TTS uses play_audio.)"""
        raise self._refuse(Capability.SPEAK_TEXT)

    async def navigate(self, goal: NavGoal) -> str:
        """[Capability.NAVIGATE] Start moving to `goal` and return a goal id at once. Progress arrives
        as `navigation` events {goal_id, state: started | arrived | blocked | cancelled} and on the pose
        stream. A new goal replaces the current one. A body that is carried by a person does not have
        this capability; the brain's guidance loop speaks directions instead (card P4-06)."""
        raise self._refuse(Capability.NAVIGATE)

    # ---- helpers for callers and adapters --------------------------------------------------------

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def require(self, capability: Capability, *, not_built: str | None = None) -> None:
        """Raise NotSupported unless the capability is present. Adapters call this first in every gated
        method. `not_built` (for example "card P1-01") turns the error into NotYetImplemented. The
        text of these errors is for logs and developers; it is never shown to the agent."""
        if capability in self.capabilities:
            return
        if not_built:
            raise NotYetImplemented(capability, f"'{capability.value}' is not built for this body yet ({not_built})")
        raise NotSupported(capability)

    async def __aenter__(self) -> Body:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
