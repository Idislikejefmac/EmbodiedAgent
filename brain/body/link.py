"""LinkBody: a Body reached over the body-brain protocol v0 (protocol/v0.md), any make of body.

It owns the brain's WebSocket server: `start()` listens, a paired body dials in, and the Body methods
turn into protocol requests. Nothing here knows what kind of device is on the other end; a specific
body subclasses it only to add its own link setup (see the phone body).

Built so far (card P0-06): status (the `get_status` command), events (the `event` message, plus
`link_state` raised here when a body connects or drops), stop, and the clock offset. Every other method
raises NotYetImplemented, naming the card that builds it; the capability set is therefore empty.
"""

from __future__ import annotations

import asyncio
import logging

from websockets.exceptions import ConnectionClosed

from ..logsafe import short_items, short_repr
from ..transport.clock import ClockOffsetEstimator
from ..transport.link import Link, LinkClosed, Received, RequestError
from ..transport.protocol import now_us
from ..transport.server import DIAGNOSTIC_EVENTS, BrainServer
from .base import Body, BodyUnavailable
from .streams import BodyStream, Fanout
from .types import (ADAPTER_EVENT_NAMES, AudioChunk, BodyEvent, BodyStatus, Capability, ClockSync, Image, NavGoal,
                    Pose)

log = logging.getLogger("brain.body.link")


def _number(value: object, low: float, high: float) -> float | None:
    """A body-reported number if it is one and in range, else None (what a body says is untrusted data)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        return None
    return float(value)


class LinkBody(Body):
    def __init__(self, *, token: bytes, host: str = "127.0.0.1", port: int = 8765, name: str = "link",
                 status_timeout_s: float = 5.0, **server_options: float):
        self._name = name
        self._server = BrainServer(host, port, token=token, on_message=self._on_message,
                                   on_connect=self._on_connect, on_disconnect=self._on_disconnect,
                                   **server_options)
        self.status_timeout_s = status_timeout_s
        self._started = False
        self._link: Link | None = None
        self._clock: ClockOffsetEstimator | None = None
        self._connected = asyncio.Event()
        self._events: Fanout[BodyEvent] = Fanout()

    # ---- identity and lifecycle ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def port(self) -> int:
        """The port the server listens on (useful when it was started on port 0)."""
        return self._server.port

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset()  # none of the gated methods is built over the link yet

    @property
    def connected(self) -> bool:
        return self._link is not None

    async def start(self) -> None:
        if not self._started:
            self._events = Fanout()
            await self._server.start()
            self._started = True

    async def close(self) -> None:
        if self._started:
            self._started = False
            await self._server.stop()
            self._events.close()

    async def wait_connected(self, timeout_s: float) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout_s)
        except TimeoutError:  # swallowed: the caller asked how long to wait; the answer is `connected` below
            pass
        return self.connected

    def clock_sync(self) -> ClockSync:
        c = self._clock
        if c is None or c.best is None:
            return ClockSync()
        return ClockSync(offset_us=c.estimate_us, rtt_us=c.best.rtt_us, drift_ppm=c.drift_fit_ppm)

    def _brain_time(self, t_body_us: int, rx_us: int) -> int:
        """A body stamp on the brain clock. Never later than the arrival time; before the first clock
        sample the arrival time is the best there is."""
        t = self.clock_sync().to_brain_us(t_body_us)
        return rx_us if t is None else max(0, min(t, rx_us))

    # ---- link callbacks (from the server) ----------------------------------------------------------

    def _on_connect(self, link: Link) -> None:
        self._link = link
        self._clock = link.clock
        self._connected.set()
        hello = link.peer_hello or {}
        caps, cmds = hello.get("capabilities") or [], hello.get("commands") or []
        log.info("body connected: capabilities=%s commands=%s", short_items(caps, len(caps)),
                 short_items(cmds, len(cmds)))
        self._events.publish(BodyEvent("link_state", {"connected": True}, now_us(), source="adapter"))

    def _on_disconnect(self, link: Link) -> None:
        if self._link is not link:
            return  # an older connection that was already replaced
        self._link = None
        self._connected.clear()
        log.info("body disconnected")
        self._events.publish(BodyEvent("link_state", {"connected": False}, now_us(), source="adapter"))

    async def _on_message(self, link: Link, r: Received) -> None:
        msg = r.msg
        if r.type == "event":
            name, data = msg["payload"]["name"], msg["payload"]["data"]
            if name in ADAPTER_EVENT_NAMES:  # only this adapter may say what the link's state is
                link.log.warning("dropped a body event named %r: that name is reserved for the adapter", name)
                return
            # Names and keys only at INFO, with %r: event data may be personal later.
            if name in DIAGNOSTIC_EVENTS:
                link.log.info("event %s %s", name, short_repr(data, 500))
            else:
                link.log.info("event %s (data keys %s)", name, short_items(data, len(data)))
            self._events.publish(BodyEvent(name, data, self._brain_time(msg["ts_body"], r.rx_us), msg["ts_body"]))
        else:
            link.log.debug("%s id=%d: nothing in the brain consumes this yet", r.type, msg["id"])

    def _peer_commands(self, link: Link) -> list[str]:
        commands = (link.peer_hello or {}).get("commands", [])
        return list(commands) if isinstance(commands, list) else []

    # ---- mandatory behaviour ---------------------------------------------------------------------

    async def status(self) -> BodyStatus:
        link = self._link
        if link is None:
            return BodyStatus(connected=False, t_us=now_us())
        rtt_ms = None if link.clock.best is None else link.clock.best.rtt_us / 1000
        if "get_status" not in self._peer_commands(link):
            return BodyStatus(connected=True, link_rtt_ms=rtt_ms, t_us=now_us())
        try:
            r = await link.request("command", {"name": "get_status", "args": {}}, expect="ack",
                                   timeout=self.status_timeout_s)
        except (LinkClosed, ConnectionClosed):
            return BodyStatus(connected=False, t_us=now_us())
        except (TimeoutError, RequestError) as e:
            # Connected but no reading: status() never raises, it says the answer is stale.
            why = f"refused ({e.code})" if isinstance(e, RequestError) else f"no answer in {self.status_timeout_s:g} s"
            link.log.warning("get_status: %s; reporting a stale status", why)
            return BodyStatus(connected=True, link_rtt_ms=rtt_ms, capabilities=tuple(c.value for c in self.capabilities),
                              t_us=now_us(), stale=True)
        result = r.msg["payload"].get("result", {})
        charging = result.get("charging")
        return BodyStatus(connected=True, battery_pct=_number(result.get("battery_pct"), 0, 100),
                          charging=charging if isinstance(charging, bool) else None,
                          temperature_c=_number(result.get("temperature_c"), -60, 200), link_rtt_ms=rtt_ms,
                          capabilities=tuple(c.value for c in self.capabilities),
                          t_us=self._brain_time(r.msg["ts_body"], r.rx_us))

    def events(self) -> BodyStream[BodyEvent]:
        return self._events.subscribe()

    async def stop(self, *, motion: bool = True, audio: bool = True) -> None:
        if self._link is None:
            raise BodyUnavailable("no body is connected")
        # Nothing is driven over the link yet, so there is nothing to stop. When speaker output is
        # built (card P2-01) this sends the stop_audio_out command for `audio`.

    # ---- capability-gated: not built over the link yet --------------------------------------------

    def pose_stream(self, *, rate_hz: float = 10.0) -> BodyStream[Pose]:
        self.require(Capability.POSE, not_built="pose over the link is card P4-01")
        raise AssertionError("unreachable")

    async def capture_image(self, *, max_edge_px: int = 640) -> Image:
        self.require(Capability.IMAGE, not_built="stills over the link are cards P1-01 and P1-02")
        raise AssertionError("unreachable")

    def audio_in_stream(self) -> BodyStream[AudioChunk]:
        self.require(Capability.AUDIO_IN, not_built="microphone audio over the link is card P2-02")
        raise AssertionError("unreachable")

    async def play_audio(self, chunk: AudioChunk, *, utterance_id: str, final: bool = False) -> None:
        self.require(Capability.AUDIO_OUT, not_built="speaker audio over the link is card P2-01")
        raise AssertionError("unreachable")  # so it cannot silently succeed once the capability is listed

    async def speak_text(self, text: str, *, utterance_id: str, interrupt: bool = False) -> None:
        self.require(Capability.SPEAK_TEXT, not_built="speak_local over the link is card P0-07")
        raise AssertionError("unreachable")

    async def navigate(self, goal: NavGoal) -> str:
        self.require(Capability.NAVIGATE, not_built="no body that moves itself speaks the protocol yet")
        raise AssertionError("unreachable")
