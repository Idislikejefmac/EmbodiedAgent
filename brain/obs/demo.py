"""`python -m brain demo-trace`: a scripted conversation against SimBody, with no hardware (card X-02 step 6).

It shows the whole logic path as a live trace and ends with the health panel:

  part 1  every capability present: microphone audio flowing, a spoken question (status, a photo, an
          answer), a console turn (pose, stop), hostile requests refused at the boundary, and a fault
          injected into a supervised component (it crashes twice, is restarted, recovers);
  part 2  the body link drops, so camera, microphone and speaker become unavailable; the conversation
          goes on, every tool answers "unavailable", and the health panel says why.

With `unavailable=True` the body link, camera and microphone are unavailable from the start (the
acceptance scenario of card X-02). No turn ever contains the words spoken: only their sources and sizes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import HEALTH, LOG, emit
from .events import correlate
from .health import format_panel
from .runtime import ObsRuntime
from .supervisor import Policy, Supervisor
from .viewer import format_event

DEMO_POLICY = Policy(max_failures=5, window_s=60.0, backoff_initial_s=0.05, backoff_max_s=0.2)


@dataclass
class DemoResult:
    events: list[dict[str, Any]] = field(default_factory=list)
    health: dict[str, Any] = field(default_factory=dict)
    turns: int = 0


async def _turn(registry: Any, turn_id: str, sources: list[str], calls: list[tuple[str, Any]],
                result: DemoResult) -> None:
    from ..agent.tools import Caller

    t0 = time.perf_counter()
    # The script runs turns far faster than people speak; each starts with full per-type event budgets, as a
    # real turn (seconds after the last) would, so the demo trace is always complete.
    LOG.reset_limits()
    with correlate(turn=turn_id):
        emit("turn.start", f"turn {turn_id}", turn_id=turn_id, sources=sources)
        caller = Caller.for_turn(sources)
        outcomes = []
        for name, args in calls:
            r = await registry.call(name, args, caller)
            outcomes.append(r.code)
        emit("turn.end", f"turn {turn_id} done", turn_id=turn_id, tool_calls=len(calls), outcomes=outcomes,
             ms=round((time.perf_counter() - t0) * 1000, 1))
    result.turns += 1


async def run_demo(*, unavailable: bool = False, data_dir: Path | None = None,
                   out: Callable[[str], None] | None = print, detail: bool = False, pace: float = 1.0) -> DemoResult:
    """Run the scripted session. Events go to the process's event log (and, with `data_dir`, to its event
    files and health.json); `out` gets each event as a compact line; `pace` scales the pauses (the chaos
    harness runs it at 0.3). Never leaves a task running."""
    from ..agent.body_tools import register_body_tools
    from ..agent.tools import ToolRegistry
    from ..body.sim import SimBody
    from ..body.types import Capability

    result = DemoResult()
    LOG.reset_limits()

    def sink(e: Mapping[str, Any]) -> None:
        result.events.append(dict(e))
        if out is not None:
            out(format_event(e, detail=detail))

    LOG.add_sink(sink)
    session = f"demo-{datetime.now():%Y%m%d-%H%M%S}"
    rt = ObsRuntime(data_dir, log=LOG, board=HEALTH, session_id=session) if data_dir is not None else None
    sup = Supervisor(policy=DEMO_POLICY)
    caps = [c for c in Capability if c not in (Capability.IMAGE, Capability.AUDIO_IN)] if unavailable else None
    body = SimBody(capabilities=caps)
    mic = None
    def flush_health() -> None:
        """Health changes as events now (serve does this once a second in its health writer)."""
        if rt is not None:
            rt.write_health()
            return
        for name, state, reason in HEALTH.take_changes():
            emit("health.change", f"{name} is {state}", source=name, state=state, reason=reason,
                 level="INFO" if state in ("up", "unknown") else "WARNING")

    try:
        with correlate(session=session):
            if rt is not None:
                rt.start()
                rt.supervise(sup)
            emit("demo.step", "part 1: SimBody " + ("with no camera or microphone, link down" if unavailable
                                                     else "with every capability, link up"))
            await body.start()
            if unavailable:
                body.set_link(False)
            flush_health()
            registry = ToolRegistry()
            register_body_tools(registry, body)
            emit("clock.estimate", "sim body clock", offset_ms=round(body.clock_sync().offset_us or 0) / 1000,
                 best_rtt_ms=0.0, drift_ppm=0.0, samples=1)
            if body.supports(Capability.AUDIO_IN):
                mic = body.audio_in_stream()
                stream = mic

                async def listen() -> None:
                    async for _ in stream:
                        pass

                sup.start("microphone-listener", listen, restart_on_return=False)
            else:
                emit("demo.step", "no microphone: listening is skipped, the conversation goes on")
            await asyncio.sleep(0.2 * pace)
            flush_health()
            await _turn(registry, "t-1", ["body"], [
                ("get_body_status", {}), ("capture_image", {"reason": "to see who is asking"}),
                ("speak", {"text": "I am here and I can see the room."})], result)
            await _turn(registry, "t-2", ["local_console"], [("get_pose", {}), ("stop", {})], result)
            emit("demo.step", "hostile requests: each is refused at the boundary")
            await _turn(registry, "t-3", ["body", "tool_result"], [
                ("run_command", {"command": "whoami"}), ("delete_person_data", {"person_id": "p1"}),
                ("speak", {"text": "hello\x00\x1b[2J forged"}), ("capture_image", {"reason": "x" * 5000})], result)
            emit("demo.step", "fault injection: a component crashes twice, is restarted, then recovers")
            attempts = {"n": 0}

            async def flaky() -> None:
                attempts["n"] += 1
                if attempts["n"] <= 2:
                    raise RuntimeError("injected fault")
                await asyncio.Event().wait()

            sup.start("flaky-component", flaky)
            await asyncio.sleep(0.4 * pace)
            flush_health()
            if not unavailable:
                emit("demo.step", "part 2: the body link drops; camera, microphone and speaker are unavailable")
                body.set_link(False)
            await _turn(registry, "t-4", ["body"], [
                ("get_body_status", {}), ("capture_image", {"reason": "look again"}),
                ("speak", {"text": "Can you hear me?"})], result)
            await asyncio.sleep(0.1 * pace)
            flush_health()
            result.health = HEALTH.snapshot(data_dir=data_dir, obs_stats=LOG.stats())
            emit("demo.step", "demo finished; health panel follows")
    finally:
        await sup.stop()
        if mic is not None:
            await mic.aclose()
        await body.close()
        if rt is not None:
            rt.stop()
        LOG.remove_sink(sink)
    if out is not None:
        out("")
        out(format_panel(result.health))
    return result
