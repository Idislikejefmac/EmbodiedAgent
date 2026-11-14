"""P0-06 LinkBody, PhoneBody and ReplayBody (B3), and the phone's adb helpers (B13).

LinkBody owns the WebSocket server; a paired body connects to it. Steady state: construction refuses bad
arguments with ValueError or TypeError; status() never raises; every gated method raises a BodyError at the
call; a body that drops, floods or lies (a reserved link_state event) changes nothing it must not; close() is
safe at any point. PhoneBody runs here with phone_link=False (no adb); its adb helpers run against a fake
subprocess that returns hostile output (the real adb is never started, and the token never appears in a message).
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

from brain.body import phone
from brain.body.base import BodyError
from brain.body.link import LinkBody
from brain.body.phone import PhoneBody
from brain.body.replay import ReplayBody
from brain.body.types import AudioChunk, NavGoal
from brain.transport.client import TestBodyClient

from .. import fuzz
from ..api import Case, Ctx, Gen, Violation, experiment, failure, hostile, invalid, valid

TOKEN_PLACEHOLDER = b"\x11" * 32
GATED = ("pose", "capture", "audio_in", "play", "speak", "navigate")


def gen_valid(g: Gen) -> list[Case]:
    return [valid("link_start_status_close", {"op": "life", "kind": "link"}),
            valid("phone_start_status_close", {"op": "life", "kind": "phone"}),
            valid("replay_start_status_close", {"op": "life", "kind": "replay"}),
            valid("link_with_paired_body", {"op": "connected", "kind": "link", "status": True}),
            valid("phone_with_paired_body", {"op": "connected", "kind": "phone", "status": True}),
            valid("body_without_get_status_command", {"op": "connected", "kind": "link", "status": False}),
            valid("close_twice", {"op": "life", "kind": "link", "close_twice": True}),
            valid("create_body_phone", {"op": "create_phone"}),
            valid("port_zero_is_assigned", {"op": "port"}),
            valid("adb_pair_success_text", {"op": "adb", "out": ('data="paired"', "", 0)}),
            valid("adb_reverse_ok", {"op": "adb_reverse", "out": ("", "", 0)})]


def gen_invalid(g: Gen) -> list[Case]:
    out: list[Case] = []
    for name, tok in (("none", None), ("str", "a" * 64), ("empty", b""), ("short_31", b"\x01" * 31), ("int", 7), ("list", [1] * 32)):
        out.append(invalid(f"token_{name}", {"op": "ctor", "kwargs": {"token": tok}}, "N1" if name in ("none", "str", "int", "list") else "N2"))
    for name, port in (("negative", -1), ("one_over_max", 65536), ("str", "8765"), ("none", None), ("float", 80.5), ("bool", True)):
        out.append(invalid(f"port_{name}", {"op": "ctor", "kwargs": {"token": TOKEN_PLACEHOLDER, "port": port}}, "N6"))
    for name, v in (("zero", 0), ("negative", -1), ("nan", float("nan")), ("inf", float("inf")), ("str", "5"), ("none", None)):
        out.append(invalid(f"status_timeout_{name}", {"op": "ctor", "kwargs": {"token": TOKEN_PLACEHOLDER, "status_timeout_s": v}}, "N6",
                           known_defect="X01B-06: LinkBody(status_timeout_s=) is not validated (0, negative, NaN, infinity, a string or None are accepted; "
                                        "status() would then time out at once or never, or raise TypeError)"))
    for name, v in (("int", 7), ("none", None), ("empty", ""), ("slash", "../x"), ("long", "a" * 65), ("newline", "a\nb")):
        out.append(invalid(f"replay_session_{name}", {"op": "replay_ctor", "id": v}, "N5"))
    out.append(invalid("phone_unknown_option", {"op": "ctor", "kind": "phone", "kwargs": {"token": TOKEN_PLACEHOLDER, "shell": "x"}}, "N4"))
    out.append(invalid("gated_methods_with_bad_args", {"op": "gated", "kind": "link", "bad": True}, "N1"))
    out.append(invalid("stop_with_wrong_types", {"op": "stop_types"}, "N1"))
    return out


def gen_hostile(g: Gen) -> list[Case]:
    out = [hostile("body_event_named_link_state", {"op": "connected", "kind": "link", "event": ("link_state", {"connected": False})}, "N16"),
           hostile("body_event_every_hostile_name", {"op": "connected", "kind": "link", "events": True}, "N7"),
           hostile("body_event_huge_data", {"op": "connected", "kind": "link", "event": ("sensor", fuzz.many_keys(2000))}, "N7"),
           hostile("gated_methods_not_built", {"op": "gated", "kind": "link"}, "N16"),
           hostile("gated_methods_on_replay", {"op": "gated", "kind": "replay"}, "N16"),
           hostile("navigate_on_phone", {"op": "gated", "kind": "phone"}, "N16")]
    for i, text in enumerate(g.rng.sample(fuzz.HOSTILE_TEXT, 6)):
        out.append(hostile(f"adb_output_hostile_{i}", {"op": "adb", "out": (text, text, 1)}, "N7"))
    out.append(hostile("adb_output_echoes_token", {"op": "adb", "out": ("token " + TOKEN_PLACEHOLDER.hex(), "", 1)}, "N7"))
    return out


def gen_failure(g: Gen) -> list[Case]:
    return [failure("body_disconnects_then_status", {"op": "connected", "kind": "link", "drop": True}, "N14"),
            failure("body_replaced_by_newer", {"op": "connected", "kind": "link", "replace": True}, "N10"),
            failure("close_while_connected", {"op": "connected", "kind": "link", "close_first": True}, "N10"),
            failure("two_bodies_one_port", {"op": "port_clash"}, "N11"),
            failure("status_when_close_races", {"op": "connected", "kind": "link", "race_close": True}, "N10"),
            failure("adb_not_found", {"op": "adb_missing"}, "N15"),
            failure("adb_times_out", {"op": "adb_raises", "exc": "timeout"}, "N12"),
            failure("adb_oserror", {"op": "adb_raises", "exc": "oserror"}, "N14"),
            failure("adb_reverse_nonzero", {"op": "adb_reverse", "out": ("", "error: no devices/emulators found", 1)}, "N14"),
            failure("env_android_home_hostile", {"op": "find_adb"}, "N15")]


def _make(kind: str, **kw: Any) -> Any:
    if kind == "replay":
        return ReplayBody("session-1")
    if kind == "phone":
        return PhoneBody(token=TOKEN_PLACEHOLDER, port=0, phone_link=False, **kw)
    return LinkBody(token=TOKEN_PLACEHOLDER, port=0, **kw)


async def _gated(ctx: Ctx, body: Any, bad: bool) -> None:
    pose = NavGoal(frame_id="f", position_m=(0.0, 0.0, 0.0))
    chunk = AudioChunk(stream_id=0, seq=0, sample_rate_hz=16000, channels=1, data=b"\x00\x00" * 8, t_us=0)
    calls: list[Any] = [lambda: body.pose_stream(rate_hz=10.0), lambda: body.capture_image(max_edge_px=640),
                        lambda: body.audio_in_stream(), lambda: body.play_audio(chunk, utterance_id="u-1"),
                        lambda: body.speak_text("hi", utterance_id="u-1"), lambda: body.navigate(pose)]
    if bad:
        calls = [lambda: body.pose_stream(rate_hz="x"), lambda: body.capture_image(max_edge_px=None),
                 lambda: body.play_audio("not a chunk", utterance_id=7), lambda: body.speak_text(None, utterance_id=None),
                 lambda: body.navigate("nowhere")]
    for call in calls:
        try:
            r = call()
            if asyncio.iscoroutine(r):
                await r
        except (BodyError, ValueError, TypeError):
            continue
        raise Violation("right_to_refuse", "a method that is not built over the link returned without raising")


class _FakeRun:
    def __init__(self, ctx: Ctx, out: tuple[str, str, int] | None = None, exc: str | None = None):
        self.out, self.exc, self.ctx = out, exc, ctx

    def __call__(self, *a: Any, **k: Any) -> Any:
        if self.exc == "timeout":
            raise subprocess.TimeoutExpired("adb", 1)
        if self.exc == "oserror":
            raise OSError("adb cannot be started")
        out = self.out or ("", "", 0)
        return subprocess.CompletedProcess(a, out[2], stdout=out[0], stderr=out[1])


def _adb_call(ctx: Ctx, fn: Any, run: Any, *args: Any) -> tuple[bool, str]:
    saved_run, saved_find = subprocess.run, phone.find_adb
    subprocess.run, phone.find_adb = run, lambda: "adb"
    try:
        ok, message = fn(*args)
    finally:
        subprocess.run, phone.find_adb = saved_run, saved_find
    ctx.expect(isinstance(ok, bool) and isinstance(message, str) and len(message) < 600, "bounded_resources", "unbounded adb message")
    ctx.expect(TOKEN_PLACEHOLDER.hex() not in message, "no_secret_in_output", "the pairing token is in an adb refusal message")
    return ok, message


@experiment(
    id="P0-06.link_body.lifecycle", card="P0-06",
    entries=("body_impl.LinkBody", "body_impl.PhoneBody", "body_impl.ReplayBody", "body_impl_method.LinkBody.port",
             "body_kind.phone", "net_module.brain/body/link.py:websockets", "net_module.brain/body/phone.py:subprocess"),
    hypothesis="LinkBody, PhoneBody (without adb) and ReplayBody refuse bad construction arguments, never raise from "
               "status(), refuse every method that is not built over the link with a BodyError, survive a body that "
               "drops, is replaced, floods or sends a reserved link_state event, close safely at any point, and the adb "
               "helpers return (False, message) for every failure without leaking the token.",
    blast_radius="loopback", valid=gen_valid, invalid=gen_invalid, hostile=gen_hostile, failure=gen_failure,
    abort_after_failures=8)
async def check(case: Case, ctx: Ctx) -> Any:
    d = case.data
    op = d["op"]
    if op == "ctor":
        kind = d.get("kind", "link")
        try:
            body = (PhoneBody if kind == "phone" else LinkBody)(**d["kwargs"])
        except (ValueError, TypeError) as e:
            ctx.expect(len(str(e)) < 300 and "x" * 8 not in str(e), "validate_before_use", "refusal text is long or echoes the input")
            return "refused"
        await body.close()
        raise Violation("validate_before_use", f"{case.name}: the constructor accepted {d['kwargs']}")
    if op == "replay_ctor":
        try:
            ReplayBody(d["id"])
        except (ValueError, TypeError):
            return "refused"
        raise Violation("validate_before_use", f"ReplayBody accepted session id {d['id']!r}")
    if op == "stop_types":
        body = _make("link")
        await body.start()
        try:
            for kw in ({"motion": "yes"}, {"audio": None}, {"motion": 1}):
                try:
                    await body.stop(**kw)
                except (BodyError, TypeError, ValueError):
                    pass
        finally:
            await body.close()
        return "ok"
    if op == "create_phone":
        from brain.body import create_body

        made: Any = create_body("phone", ctx.sandbox.config, token=TOKEN_PLACEHOLDER, port=0, phone_link=False)
        ctx.expect(made.name == "phone", "validate_before_use", "create_body('phone') made another body")
        try:
            create_body("phone", ctx.sandbox.config)
        except ValueError:
            return "refused without a token"
        raise Violation("fail_closed", "create_body('phone') worked without a token")
    if op == "port":
        body = _make("link")
        await body.start()
        try:
            ctx.expect(0 < body.port < 65536, "validate_before_use", f"port {body.port}")
        finally:
            await body.close()
        return body.port
    if op == "port_clash":
        a = _make("link")
        await a.start()
        try:
            b = LinkBody(token=TOKEN_PLACEHOLDER, port=a.port)
            try:
                await b.start()
            except OSError:
                return "refused"
            finally:
                await b.close()
            raise Violation("fail_closed", "two bodies listened on one port")
        finally:
            await a.close()
    if op == "adb":
        ok, _ = _adb_call(ctx, phone.pair, _FakeRun(ctx, d["out"]), TOKEN_PLACEHOLDER)
        ctx.expect(ok == (case.cls.value == "valid"), "validate_before_use", f"pair said {ok}")
        return ok
    if op == "adb_reverse":
        ok, _ = _adb_call(ctx, phone.ensure_link, _FakeRun(ctx, d["out"]), 8765)
        ctx.expect(ok == (d["out"][2] == 0), "validate_before_use", "ensure_link verdict does not follow the exit code")
        return ok
    if op == "adb_raises":
        ok, _ = _adb_call(ctx, phone.pair, _FakeRun(ctx, None, d["exc"]), TOKEN_PLACEHOLDER)
        ctx.expect(not ok, "fail_closed", "pairing 'succeeded' after adb failed")
        return "refused"
    if op == "adb_missing":
        saved = phone.find_adb
        phone.find_adb = lambda: None
        try:
            ok, msg = phone.pair(TOKEN_PLACEHOLDER)
            ok2, _ = phone.ensure_link(8765)
        finally:
            phone.find_adb = saved
        ctx.expect(not ok and not ok2 and "adb" in msg, "fail_closed", "a missing adb was not reported")
        return "refused"
    if op == "find_adb":
        import os

        saved_env = {k: os.environ.get(k) for k in ("ANDROID_HOME", "ANDROID_SDK_ROOT", "LOCALAPPDATA")}
        try:
            for value in ("", "C:\\no\\such\\dir", "x" * 5000, "..\\..\\.."):
                for k in saved_env:
                    os.environ[k] = value
                found = phone.find_adb()
                ctx.expect(found is None or isinstance(found, str), "validate_before_use", "find_adb returned a non-string")
        finally:
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return "ok"
    body = _make(d["kind"])
    if op == "gated":
        await body.start()
        try:
            await _gated(ctx, body, d.get("bad", False))
            if d["kind"] == "phone":
                try:
                    await body.navigate(NavGoal(frame_id="f", position_m=(0.0, 0.0, 0.0)))
                except BodyError as e:
                    ctx.expect("carried" in str(e), "right_to_refuse", "navigate on the phone gave no reason")
        finally:
            await body.close()
        return "refused"
    if op == "life":
        await body.start()
        await body.start()
        st = await body.status()
        ctx.expect(st.connected is False, "validate_before_use", "a body with no peer says it is connected")
        ctx.expect(await body.wait_connected(0.01) is False, "validate_before_use", "wait_connected true with no peer")
        await body.close()
        await body.close()
        if d.get("close_twice"):
            await body.start()  # restart after close
            await body.close()
        return "ok"
    return await _connected(case, ctx, body, d)


async def _connected(case: Case, ctx: Ctx, body: Any, d: dict[str, Any]) -> Any:
    await body.start()
    url = f"ws://127.0.0.1:{body.port}/"
    commands = ("get_status",) if d.get("status", True) else ()
    stream = body.events()
    try:
        async with TestBodyClient(url, token=TOKEN_PLACEHOLDER, commands=commands) as c:
            ctx.expect(await body.wait_connected(2.0), "validate_before_use", "a paired body did not connect")
            if d.get("close_first"):
                await body.close()
                st = await asyncio.wait_for(body.status(), 3)
                ctx.expect(not st.connected, "validate_before_use", "status says connected after close")
                return "closed"
            if d.get("race_close"):
                t = asyncio.ensure_future(body.status())
                await body.close()
                st = await asyncio.wait_for(t, 6)
                return st.connected
            if d.get("replace"):
                async with TestBodyClient(url, token=TOKEN_PLACEHOLDER) as c2:
                    await asyncio.sleep(0.1)
                    ctx.expect(c2.link is not None, "validate_before_use", "the newer body did not connect")
                    await asyncio.wait_for(body.status(), 6)
                return "replaced"
            for name, data in ([d["event"]] if d.get("event") else
                               [(n[:60].lower().replace(" ", "_") or "x", {"t": n}) for n in fuzz.HOSTILE_TEXT[:12]] if d.get("events") else []):
                try:
                    await c._live.send("event", {"name": name, "data": data})
                except Exception:  # broad-except: the body-side helper itself refuses a bad name; that is the test's own client
                    pass
            await asyncio.sleep(0.1)
            if d.get("event"):
                for item in _drain(stream):
                    ctx.expect(not (item.name == "link_state" and item.source == "body"), "right_to_refuse",
                               "a body's own link_state event reached consumers")
            if d.get("drop"):
                await c._live.close(1000, "bye")
                await asyncio.sleep(0.2)
            st = await asyncio.wait_for(body.status(), 8)
            ctx.expect(st.connected == (not d.get("drop")), "validate_before_use", f"status.connected={st.connected}")
            if d.get("status", True) and not d.get("drop"):
                ctx.expect(st.battery_pct == 100.0, "validate_before_use", "the body's battery reading was lost")
    finally:
        await stream.aclose()
        await body.close()
    return "ok"


def _drain(stream: Any) -> list[Any]:
    items = []
    q = getattr(stream, "_items", None)
    if q is not None:
        items = list(q)
    return items
