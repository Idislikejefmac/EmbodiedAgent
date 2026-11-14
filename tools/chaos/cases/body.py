"""P0-06 Body interface: random call sequences against SimBody (plan card X-01 step 6, first slice).

The contract (brain/body/base.py): `status()` never raises; every other method either works or raises a
BodyError (NotSupported, BodyUnavailable, ...); a wrong argument raises ValueError or TypeError at the
call; streams stay bounded for a slow consumer; close() is safe twice. Anything else is a crash.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from brain.body.base import BodyError
from brain.body.sim import SimBody
from brain.body.streams import POSE_QUEUE
from brain.body.types import AudioChunk, NavGoal, TrackingState

from ..api import Case, Ctx, Gen, Violation, experiment, failure, hostile, invalid, valid

ALLOWED_ERRORS = (BodyError, ValueError, TypeError)
VALID_OPS = ("status", "capture", "speak", "stop", "pose_one", "events", "link_toggle", "tracking", "navigate",
             "play_audio", "reset_frame", "inject_event", "audio_one")


def _valid_op(g: Gen) -> tuple[str, Any]:
    op = g.rng.choice(VALID_OPS)
    arg: Any = None
    if op == "capture":
        arg = g.rng.choice([64, 640, 1568, 4096])
    elif op == "speak":
        arg = ("hello " * g.rng.randint(1, 20), f"u-{g.rng.randint(1, 999)}")
    elif op == "tracking":
        arg = g.rng.choice([t.value for t in TrackingState])
    elif op == "navigate":
        arg = (round(g.rng.uniform(-3, 3), 2), round(g.rng.uniform(-3, 3), 2))
    elif op == "inject_event":
        arg = g.rng.choice(["button", "poke", "battery_low"])
    return op, arg


def _sequence(g: Gen, n: int) -> list[tuple[str, Any]]:
    return [_valid_op(g) for _ in range(n)]


def gen_valid(g: Gen) -> list[Case]:
    return [valid(f"sequence_{i}", {"start": True, "ops": _sequence(g, g.n(8, 30))}) for i in range(g.n(6, 60))]


BAD_ARGS = [("capture", 0, "N6"), ("capture", 63, "N6"), ("capture", 4097, "N6"), ("capture", 10**9, "N6"),
            ("capture", -1, "N6"), ("capture", "640", "N1"), ("capture", 640.5, "N1"), ("capture", None, "N1"),
            ("capture", True, "N1"), ("speak", ("", "u-1"), "N3"), ("speak", ("x" * 5000, "u-1"), "N2"),
            ("speak", (None, "u-1"), "N1"), ("speak", ("hi", "../x"), "N5"), ("speak", ("hi", ""), "N3"),
            ("speak", ("hi", "a" * 100), "N2"), ("pose_rate", 0, "N6"), ("pose_rate", -1, "N6"), ("pose_rate", 31, "N6"),
            ("pose_rate", math.nan, "N6"), ("pose_rate", math.inf, "N6"), ("navigate_raw", (math.nan, 0.0), "N6"),
            ("navigate_raw", ("a", 0.0), "N1"), ("inject_event", "Bad Name", "N5"), ("inject_event", "", "N3"),
            ("play_audio_raw", -5, "N6")]


def gen_invalid(g: Gen) -> list[Case]:
    out = []
    for i, (op, arg, tag) in enumerate(BAD_ARGS):
        ops = _sequence(g, 2) + [(op, arg)] + _sequence(g, 2)
        out.append(invalid(f"bad_{op}_{i}", {"start": True, "ops": ops}, tag))
    return out


def gen_hostile(g: Gen) -> list[Case]:
    return [
        hostile("calls_before_start", {"start": False, "ops": [(op, None) for op in ("status", "stop", "capture",
                                                                                    "pose_one", "speak")]}, "N8"),
        hostile("calls_after_close", {"start": True, "ops": [("close", None)] + _sequence(g, 6)}, "N8"),
        hostile("start_twice_close_twice", {"start": True, "ops": [("start", None), ("close", None), ("close", None),
                                                                   ("status", None)]}, "N8"),
        hostile("body_fakes_link_state", {"start": True, "ops": [("fake_link_state", None)]}, "N16"),
    ]


def gen_failure(g: Gen) -> list[Case]:
    return [
        failure("concurrent_calls_then_close", {"start": True, "concurrent": True, "ops": _sequence(g, g.n(20, 100))
                                                + [("close", None)]}, "N10"),
        failure("link_drop_mid_sequence", {"start": True, "ops": _sequence(g, 3) + [("link_down", None)]
                                           + _sequence(g, 5) + [("link_up", None), ("status", None)]}, "N14"),
        failure("slow_pose_consumer", {"start": True, "ops": [("slow_consumer", 1.0)]}, "N11"),
    ]


async def _op(body: SimBody, ctx: Ctx, op: str, arg: Any) -> Any:
    if op == "status":
        return await body.status()
    if op == "capture":
        return await body.capture_image(max_edge_px=arg if arg is not None else 640)
    if op == "speak":
        text, uid = arg if arg else ("hi", "u-1")
        return await body.speak_text(text, utterance_id=uid)
    if op == "stop":
        return await body.stop()
    if op in ("pose_one", "pose_rate"):
        s = body.pose_stream(rate_hz=arg if op == "pose_rate" else 30.0)
        try:
            return await asyncio.wait_for(anext(s), 1.0)
        finally:
            await s.aclose()
    if op == "audio_one":
        a = body.audio_in_stream()
        try:
            return await asyncio.wait_for(anext(a), 1.0)
        finally:
            await a.aclose()
    if op == "events":
        e = body.events()
        await e.aclose()
        return None
    if op == "link_toggle":
        body.set_link(not body.connected)
        return None
    if op == "link_down":
        body.set_link(False)
        return None
    if op == "link_up":
        body.set_link(True)
        return None
    if op == "tracking":
        body.set_tracking(TrackingState(arg))
        return None
    if op == "navigate":
        return await body.navigate(NavGoal(frame_id=body.frame_id, position_m=(arg[0], 0.0, arg[1])))
    if op == "navigate_raw":
        return await body.navigate(NavGoal(frame_id=body.frame_id, position_m=(arg[0], 0.0, arg[1])))
    if op in ("play_audio", "play_audio_raw"):
        rate = 16000 if op == "play_audio" else arg
        chunk = AudioChunk(stream_id=0, seq=0, sample_rate_hz=rate, channels=1, data=b"\x00\x00" * 160, t_us=0)
        return await body.play_audio(chunk, utterance_id="u-play", final=True)
    if op == "reset_frame":
        return body.reset_frame()
    if op == "inject_event":
        return body.inject_event(arg, {"x": 1})
    if op == "fake_link_state":
        e = body.events()
        try:
            body.inject_event("link_state", {"connected": False})
            try:
                item = await asyncio.wait_for(anext(e), 0.1)
            except TimeoutError:
                return "dropped"
            if item.name == "link_state":
                raise Violation("right_to_refuse", "a body published the adapter's reserved link_state event")
            return "other event"
        finally:
            await e.aclose()
    if op == "start":
        return await body.start()
    if op == "close":
        return await body.close()
    if op == "slow_consumer":
        s = body.pose_stream(rate_hz=30.0)
        try:
            await asyncio.sleep(arg)  # read nothing: the queue must stay bounded
            ctx.monitors.observe_queue("pose stream", len(getattr(s, "_items", ())), POSE_QUEUE)
            return await asyncio.wait_for(anext(s), 1.0)
        finally:
            await s.aclose()
    raise ValueError(f"unknown op {op!r}")


async def _guarded(body: SimBody, ctx: Ctx, op: str, arg: Any) -> str:
    try:
        await asyncio.wait_for(_op(body, ctx, op, arg), 3.0)
        return "ok"
    except Violation:
        raise
    except ALLOWED_ERRORS as e:
        if op == "status":
            raise Violation("no_crash", f"status() raised {type(e).__name__}; it must never raise") from None
        return type(e).__name__
    except TimeoutError:
        raise Violation("bounded_resources", f"{op} did not return within 3 s") from None
    except StopAsyncIteration:
        return "stream ended"
    except Exception as e:  # broad-except: the crash detector for one body call
        raise Violation("no_crash", f"{op}({str(arg)[:40]}) raised {type(e).__name__}: {str(e)[:120]}") from None


@experiment(
    id="P0-06.body.sequences", card="P0-06",
    entries=("body_method.*", "body_impl.SimBody", "body_impl_method.SimBody.*", "capability.*", "body_kind.sim",
             "gate.brain.body.types.Pose", "gate.brain.body.types.NavGoal", "gate.brain.body.types.AudioChunk"),
    hypothesis="For any sequence of Body calls on SimBody, status() never raises, every other call returns or "
               "raises a BodyError (ValueError or TypeError for a wrong argument), streams stay bounded for a slow "
               "consumer, and close() is safe at any point.",
    blast_radius="sim", valid=gen_valid, invalid=gen_invalid, hostile=gen_hostile, failure=gen_failure)
async def check(case: Case, ctx: Ctx) -> Any:
    d = case.data
    body = ctx.sandbox.sim_body()
    if d["start"]:
        await body.start()
    outcomes: list[str] = []
    try:
        if d.get("concurrent"):
            res = await asyncio.gather(*(_guarded(body, ctx, op, arg) for op, arg in d["ops"]), return_exceptions=True)
            for r in res:
                if isinstance(r, BaseException):
                    raise r
                outcomes.append(r)
        else:
            for op, arg in d["ops"]:
                outcomes.append(f"{op}:{await _guarded(body, ctx, op, arg)}")
    finally:
        await body.close()
        await body.close()
    return outcomes
