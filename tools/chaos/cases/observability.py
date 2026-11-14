"""X-02 live trace, health view and crash barrier (brain/obs; docs/observability.md).

Steady states:
- emit() never raises, never blocks, and every event it lets through is plain JSON of at most
  OBS_EVENT_MAX_BYTES with no control character and no secret, whatever the fields hold;
- a failing event file never affects the caller (swallowed, counted, reported once);
- data/state.json loads to a valid state or to defaults (a bad file is quarantined), never raising;
- the viewer and the health panel read only event files and health.json, skip what is not an event, and
  print nothing that could forge a terminal line;
- a supervised component that fails is contained: restarted with a bounded backoff, stopped after too many
  failures, never retried after a security refusal, and shutdown always works;
- a crash report holds no secret and writing one never raises;
- the scripted demo session completes with the body link, camera and microphone up or unavailable.

The process launcher (`serve --supervised`) starts processes, which the sandbox refuses: it is covered by
tests/test_obs_crash_barrier.py and has a manifest [[exempt]] record.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any

from brain.limits import OBS_EVENT_MAX_BYTES, OBS_VIEW_LINE_MAX_BYTES, STATE_FILE_MAX_BYTES
from brain.obs import LOG, emit
from brain.obs.crash import exception_details, write_report
from brain.obs.health import format_panel, read_snapshot
from brain.obs.pipeline import EventLog, event_line
from brain.obs.state import BrainState, StateStore
from brain.obs.supervisor import FatalComponentError, Policy, Supervisor, run_with_restarts
from brain.obs.viewer import Filter, watch

from .. import log_hygiene
from ..api import Case, Ctx, Gen, experiment, failure, hostile, invalid, not_applicable, valid

FAST = Policy(max_failures=3, window_s=60.0, backoff_initial_s=0.01, backoff_max_s=0.05)


def _unique(ctx: Ctx, name: str) -> Path:
    folder = Path(ctx.sandbox.root) / "x02" / f"{name}-{ctx.case_seed}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ---- emit ------------------------------------------------------------------------------------------------


class _BadRepr:
    def __repr__(self) -> str:
        raise RuntimeError("repr refused")

    __str__ = __repr__


class _BadDict(dict[str, Any]):
    def items(self) -> Any:
        raise RuntimeError("items refused")


def _deep(n: int) -> Any:
    v: Any = "leaf"
    for _ in range(n):
        v = {"k": [v]}
    return v


def emit_valid(g: Gen) -> list[Case]:
    return [valid("tool_result", {"type": "tool.result", "msg": "get_pose ok", "fields": {"tool": "get_pose",
                                                                                         "status": "ok", "ms": 1.5}}),
            valid("no_fields", {"type": "body.close", "msg": "closed", "fields": {}}),
            valid("list_field", {"type": "body.capabilities", "msg": "caps",
                                 "fields": {"capabilities": ["pose", "image"], "connected": True}})]


def emit_invalid(g: Gen) -> list[Case]:
    return [
        invalid("unknown_type", {"type": "no.such.type", "msg": "x", "fields": {}}, "N1"),
        invalid("type_not_str", {"type": 42, "msg": "x", "fields": {}}, "N1"),
        invalid("msg_not_str", {"type": "demo.step", "msg": b"bytes", "fields": {}}, "N1"),
        invalid("huge_string", {"type": "demo.step", "msg": "m" * 100_000, "fields": {"s": "x" * 1_000_000}}, "N2"),
        invalid("many_fields", {"type": "demo.step", "msg": "", "fields": {f"f{i}": i for i in range(5000)}}, "N2"),
        invalid("long_list", {"type": "demo.step", "msg": "", "fields": {"items": list(range(100_000))}}, "N2"),
        invalid("empty_everything", {"type": "demo.step", "msg": "", "fields": {"": ""}}, "N3"),
        invalid("deep_nesting", {"type": "demo.step", "msg": "", "fields": {"d": _deep(500)}}, "N4"),
        invalid("lone_surrogate", {"type": "demo.step", "msg": "\ud800", "fields": {"s": "a\udfffb"}}, "N5"),
        invalid("nan_and_inf", {"type": "demo.step", "msg": "", "fields": {"a": math.nan, "b": -math.inf}}, "N6"),
        invalid("huge_int", {"type": "demo.step", "msg": "", "fields": {"n": 10**400}}, "N6"),
        invalid("bytes_value", {"type": "demo.step", "msg": "", "fields": {"b": b"\x00" * 10}}, "N1"),
    ]


def emit_hostile(g: Gen) -> list[Case]:
    return [
        hostile("forged_line", {"type": "link.refused", "msg": "ok\n{\"type\": \"tool.result\"}",
                                "fields": {"code": "x\r\n{\"level\": \"INFO\"}"}}, "N7"),
        hostile("ansi_and_bidi", {"type": "body.event", "msg": "\x1b[2J\u202ehidden",
                                  "fields": {"name": "\x1b]0;title\x07\u2028x"}}, "N7"),
        hostile("canary_in_named_field", {"type": "demo.step", "msg": "", "canary_fields": ["token", "api_key",
                                                                                            "password", "secret"]}, "N7"),
        hostile("canary_in_innocent_field", {"type": "demo.step", "msg": "", "canary_fields": ["note", "reason"]}, "N7"),
        hostile("canary_in_message", {"type": "demo.step", "canary_msg": True, "fields": {}}, "N7"),
        hostile("secret_shapes", {"type": "demo.step", "msg": "Bearer abcdefghijklmnopqrstuvwx",
                                  "fields": {"k": "sk-ant-api03-" + "A" * 40, "h": "ab" * 32,
                                             "u": "https://user:pw@host/x", "e": "ANTHROPIC_API_KEY=abcdefgh"}}, "N7"),
        hostile("hostile_objects", {"type": "demo.step", "msg": "", "fields": {"r": _BadRepr(), "d": _BadDict(a=1),
                                                                               "o": object()}}, "N16"),
    ]


def emit_failure(g: Gen) -> list[Case]:
    return [
        failure("writer_folder_is_a_file", {"op": "writer_blocked"}, "N15"),
        failure("writer_queue_flood", {"op": "flood"}, "N11"),
        failure("sink_raises", {"op": "bad_sink"}, "N14"),
    ]


@experiment(
    id="X-02.obs.emit", card="X-02",
    entries=("event_type.*", "store.events.jsonl", "file_site.brain/obs/pipeline.py:*"),
    hypothesis="emit() never raises or blocks; every event it records is plain JSON of at most OBS_EVENT_MAX_BYTES "
               "with no control character and no secret; a failing event file or sink is swallowed and counted.",
    blast_radius="sandbox", valid=emit_valid, invalid=emit_invalid, hostile=emit_hostile, failure=emit_failure)
async def check_emit(case: Case, ctx: Ctx) -> Any:
    d = case.data
    canaries = dict(ctx.sandbox.canaries)
    if d.get("op") == "writer_blocked":
        blocker = _unique(ctx, "blocked") / "not-a-folder"
        blocker.write_text("x", encoding="utf-8")
        log = EventLog(burst=1000, rate_per_s=1000)
        w = log.start_files(blocker)
        for i in range(50):
            log.emit("demo.step", f"step {i}")
        for _ in range(100):  # the writer notices at its first batch (well under a second)
            if w.state == "down":
                break
            await asyncio.sleep(0.01)
        log.stop_files()
        ctx.expect(w.state in ("down", "stopped") and w.failures >= 1, "fail_closed", f"writer state {w.state}")
        failed = [e for e in log.ring if e["type"] == "obs.sink_failed"]
        ctx.expect(len(failed) == 1, "bounded_resources", f"the failure was reported {len(failed)} times, not once")
        return {"failures": w.failures, "dropped": w.dropped}
    if d.get("op") == "flood":
        log = EventLog(burst=10**6, rate_per_s=10**6)
        w = log.start_files(_unique(ctx, "flood"))
        w.queue.maxsize = 100  # a tiny queue: most of the flood must be dropped and counted, never block
        for i in range(5000):
            log.emit("demo.step", "flood", i=i)
        log.stop_files()
        ctx.expect(w.dropped + w.written >= 5000, "bounded_resources", "events were neither written nor counted")
        return {"written": w.written, "dropped": w.dropped}
    if d.get("op") == "bad_sink":
        log = EventLog()

        def sink(e: Any) -> None:
            raise RuntimeError("sink broke")

        log.add_sink(sink)
        for _ in range(5):
            log.emit("demo.step", "x")
        ctx.expect(sink not in log.sinks and log.counts["sink_errors"] == 3, "bounded_resources",
                   "a failing sink was not counted and removed")
        return dict(log.counts)
    fields = dict(d.get("fields", {}))
    for name in d.get("canary_fields", []):
        fields[name] = " ".join(canaries.values())
    msg = " ".join(canaries.values()) if d.get("canary_msg") else d.get("msg", "")
    event = emit(d["type"], msg, **fields)  # through the process log: the monitors check it too
    if event is None:
        return None
    line = event_line(event)
    ctx.expect(len(line.encode("utf-8")) <= OBS_EVENT_MAX_BYTES, "bounded_resources", f"event of {len(line)} chars")
    findings = log_hygiene.check_event(event, "event", canaries)
    ctx.expect(not findings, "no_secret_in_output" if any("secret" in f.kind for f in findings)
               else "validate_before_use", f"{findings[:2]}")
    ctx.expect(json.loads(line)["type"] == event["type"], "validate_before_use", "the line is not the event")
    return {"type": event["type"], "fields": len(event["f"])}


# ---- the state file -------------------------------------------------------------------------------------


def state_valid(g: Gen) -> list[Case]:
    good = BrainState(session_id="s-1", starts=3, requests_this_hour=7, spend_usd_total=1.25,
                      pending_request_ids=("r-1", "r-2")).to_json()
    return [valid("missing", {"raw": None}), valid("good", {"raw": json.dumps(good).encode(), "starts": 3}),
            valid("defaults", {"raw": json.dumps(BrainState().to_json()).encode(), "starts": 0})]


def _state_doc(**over: Any) -> bytes:
    doc = BrainState().to_json()
    doc.update(over)
    return json.dumps(doc).encode()


def state_invalid(g: Gen) -> list[Case]:
    good = json.dumps(BrainState(session_id="s-1").to_json()).encode()
    return [
        invalid("cut_mid_write", {"raw": good[: len(good) // 2]}, "N4"),
        invalid("empty_file", {"raw": b""}, "N3"),
        invalid("garbage", {"raw": bytes(range(256))}, "N5"),
        invalid("utf16", {"raw": good.decode().encode("utf-16")}, "N5"),
        invalid("oversize", {"raw": b" " * (STATE_FILE_MAX_BYTES + 1) + b"{}"}, "N2"),
        invalid("not_object", {"raw": b"[1, 2, 3]"}, "N4"),
        invalid("wrong_schema", {"raw": _state_doc(schema=99)}, "N4"),
        invalid("starts_text", {"raw": _state_doc(starts="7")}, "N1"),
        invalid("starts_bool", {"raw": _state_doc(starts=True)}, "N1"),
        invalid("negative_spend", {"raw": _state_doc(spend_usd_total=-1.0)}, "N6"),
        invalid("nan_spend", {"raw": b'{"schema": 1, "spend_usd_total": NaN}'}, "N6"),
        invalid("huge_count", {"raw": _state_doc(requests_this_hour=10**12)}, "N6"),
        invalid("unknown_field", {"raw": _state_doc(api_key="x")}, "N4"),
        invalid("deep", {"raw": b"[" * 100_000}, "N4"),
    ]


def state_hostile(g: Gen) -> list[Case]:
    return [
        hostile("pathlike_session", {"raw": _state_doc(session_id="..\\..\\Windows")}, "N7"),
        hostile("control_in_id", {"raw": _state_doc(session_id="a\nb")}, "N7"),
        hostile("too_many_pending", {"raw": _state_doc(pending_request_ids=[f"r{i}" for i in range(1000)])}, "N7"),
    ]


def state_failure(g: Gen) -> list[Case]:
    return [failure("state_path_is_a_folder", {"folder": True}, "N15"),
            failure("stray_temporary_file", {"raw": None, "tmp": True}, "N13")]


@experiment(
    id="X-02.obs.state", card="X-02",
    entries=("store.state.json", "file_site.brain/obs/state.py:*", "file_site.brain/obs/files.py:*",
             "event_type.state.*"),
    hypothesis="data/state.json loads to a valid state or, when missing, damaged, oversize or wrong, to the defaults "
               "with the bad file quarantined and reported; loading never raises; saving is atomic.",
    blast_radius="sandbox", valid=state_valid, invalid=state_invalid, hostile=state_hostile, failure=state_failure)
def check_state(case: Case, ctx: Ctx) -> Any:
    d = case.data
    folder = _unique(ctx, "state")
    events: list[tuple[str, dict[str, Any]]] = []
    store = StateStore(folder, lambda t, m, **f: events.append((t, f)))
    if d.get("folder"):
        store.path.mkdir(exist_ok=True)
    elif d.get("raw") is not None:
        store.path.write_bytes(d["raw"])
    if d.get("tmp"):
        (folder / ".state.json.999.tmp").write_text('{"half', encoding="utf-8")
    state = store.load()
    ctx.expect(isinstance(state, BrainState), "fail_closed", "load did not return a state")
    if case.cls.value == "valid":
        ctx.expect(state.starts == d.get("starts", 0), "validate_before_use", "a valid file was not read")
    else:
        ctx.expect(state == BrainState(), "validate_before_use", "a bad state file was accepted")
        if not d.get("tmp"):
            ctx.expect(any(t == "state.quarantined" for t, _ in events), "right_to_refuse", "not quarantined")
    saved = store.save() if not d.get("folder") else False
    return {"starts": state.starts, "events": [t for t, _ in events], "saved": saved}


# ---- the viewer and the health panel ---------------------------------------------------------------------


def _event(**over: Any) -> dict[str, Any]:
    e: dict[str, Any] = {"v": 1, "seq": 1, "ts": "2026-11-13T10:00:00.000000Z", "mono": 1.0, "level": "INFO",
                         "comp": "agent", "type": "tool.result", "session": "s-1", "turn": "t-1", "msg": "ok",
                         "f": {"tool": "get_pose"}}
    e.update(over)
    return e


def view_valid(g: Gen) -> list[Case]:
    lines = [json.dumps(_event(seq=i)) for i in range(5)]
    return [valid("events", {"lines": lines, "shown": 5}),
            valid("filtered", {"lines": lines + [json.dumps(_event(comp="transport", type="link.up"))],
                               "filter": {"components": ["transport"]}, "shown": 1}),
            valid("health", {"health": {"written_at": 0, "sources": {"camera": {"state": "down", "reason": "no link"}}}})]


def view_invalid(g: Gen) -> list[Case]:
    return [
        invalid("garbage_lines", {"lines": ["not json", "{", "[]", "42", '{"no_type": 1}'], "shown": 0}, "N4"),
        invalid("oversize_line", {"lines": [json.dumps(_event(msg="x" * (OBS_VIEW_LINE_MAX_BYTES * 2)))],
                                  "shown": 0}, "N2"),
        invalid("empty_file", {"lines": [], "shown": 0}, "N3"),
        invalid("bad_utf8", {"raw": b"\xff\xfe\x00garbage\n", "shown": 0}, "N5"),
        invalid("fields_not_object", {"lines": [json.dumps(_event(f=[1, 2]))], "shown": 0}, "N1"),
        invalid("health_garbage", {"health_raw": b"\x00\x01not json"}, "N5"),
        invalid("health_wrong_shape", {"health_raw": b'{"sources": []}'}, "N4"),
    ]


def view_hostile(g: Gen) -> list[Case]:
    return [
        hostile("terminal_escapes", {"lines": [json.dumps(_event(msg="\x1b[2J\x1b]0;pwned\x07", comp="\x1b[31m",
                                                                 f={"k\n": "v\r\nFORGED"}))], "shown": 1}, "N7"),
        hostile("not_an_event_file", {"wrong_name": True}, "N7"),
        hostile("health_escapes", {"health": {"written_at": "x", "session": "\x1b[2J",
                                              "sources": {"cam\nera": {"state": "\x07up", "reason": "\u202eevil"}}}},
                "N7"),
    ]


def view_failure(g: Gen) -> list[Case]:
    return [failure("health_missing", {"health_missing": True}, "N15"),
            failure("folder_without_events", {"empty_folder": True}, "N15")]


@experiment(
    id="X-02.obs.viewer", card="X-02",
    entries=("cli_command.brain.watch", "cli_arg.brain.watch.*", "cli_command.brain.health", "store.health.json",
             "file_site.brain/obs/viewer.py:*", "event_type.health.change"),
    hypothesis="watch and health read only event files and health.json, skip and count what is not an event, never "
               "raise on a damaged file, and print no control character (no forged or hijacked terminal line).",
    blast_radius="sandbox", valid=view_valid, invalid=view_invalid, hostile=view_hostile, failure=view_failure)
def check_viewer(case: Case, ctx: Ctx) -> Any:
    d = case.data
    folder = _unique(ctx, "view")
    out: list[str] = []
    if "health" in d or "health_raw" in d or d.get("health_missing"):
        path = folder / "health.json"
        if "health" in d:
            path.write_text(json.dumps(d["health"]), encoding="utf-8")
        elif "health_raw" in d:
            path.write_bytes(d["health_raw"])
        snap, problem = read_snapshot(path)
        text = format_panel(snap) if snap is not None else problem
        ctx.expect(not log_hygiene.CONTROL.search(text.replace("\n", "")), "validate_before_use",
                   f"control character in the panel: {text[:120]!r}")
        ctx.expect((snap is not None) == (case.cls.value != "invalid" and not d.get("health_missing")),
                   "validate_before_use", f"snapshot accepted: {snap is not None} ({problem})")
        return {"ok": snap is not None, "problem": problem}
    if d.get("wrong_name"):
        other = folder / "secrets.txt"
        other.write_text("not an event file", encoding="utf-8")
        try:
            watch(other, Filter(), out=out.append)
        except ValueError:
            return {"refused": True}
        ctx.expect(False, "right_to_refuse", "watch read a file that is not an event file")
    path = folder / "events-case-0001.jsonl"
    if not d.get("empty_folder"):
        if "raw" in d:
            path.write_bytes(d["raw"])
        else:
            path.write_text("".join(line + "\n" for line in d["lines"]), encoding="utf-8")
    flt = Filter(components=frozenset(d.get("filter", {}).get("components", ())))
    counts = watch(folder, flt, out=out.append)
    for line in out:
        ctx.expect(not log_hygiene.CONTROL.search(line.replace("\n", "")), "validate_before_use",
                   f"control character in viewer output: {line[:120]!r}")
    if "shown" in d:
        ctx.expect(counts.shown == d["shown"], "validate_before_use", f"shown {counts.shown}, expected {d['shown']}")
    return {"shown": counts.shown, "skipped": counts.skipped_bad}


# ---- the supervisor -------------------------------------------------------------------------------------


def sup_valid(g: Gen) -> list[Case]:
    return [valid("runs_until_stopped", {"op": "forever"}), valid("finishes", {"op": "finish"})]


def sup_failure(g: Gen) -> list[Case]:
    return [
        failure("crashes_then_recovers", {"op": "flaky"}, "N14", isolated_fault=True),
        failure("always_crashes", {"op": "always"}, "N14", isolated_fault=True),
        failure("security_refusal", {"op": "fatal"}, "N14", isolated_fault=True),
        failure("ignores_cancellation", {"op": "stubborn"}, "N12"),
        failure("others_keep_running", {"op": "neighbour"}, "N10", isolated_fault=True),
    ]


@experiment(
    id="X-02.obs.supervisor", card="X-02",
    entries=("event_type.supervisor.*", "event_type.process.crash", "store.crash_report",
             "file_site.brain/obs/crash.py:write_report"),
    hypothesis="A failing supervised component is contained: restarted with a bounded backoff, stopped and reported "
               "after too many failures, never retried after a security refusal; the others keep running; stop() "
               "always returns within its timeout; a crash report holds no secret and writing it never raises.",
    blast_radius="sandbox", valid=sup_valid,
    invalid=not_applicable("a supervisor takes our own coroutines, not outside input; the failure class covers it"),
    hostile=not_applicable("no outside party can reach the supervisor; injection is covered by the emit experiment"),
    failure=sup_failure)
async def check_supervisor(case: Case, ctx: Ctx) -> Any:
    op = case.data["op"]
    sup = Supervisor(policy=FAST)
    counter = {"n": 0, "ticks": 0}

    async def forever() -> None:
        while True:
            counter["ticks"] += 1
            await asyncio.sleep(0.01)

    async def finish() -> None:
        return

    async def flaky() -> None:
        counter["n"] += 1
        if counter["n"] <= 2:
            raise RuntimeError("injected")
        await forever()

    async def always() -> None:
        raise ValueError("injected")

    async def fatal() -> None:
        raise FatalComponentError("the frame limit cannot be enforced")

    async def stubborn() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:  # misbehaves on purpose: ignores the first cancel, outlasting stop()'s wait
            await asyncio.sleep(0.2)

    factories = {"forever": forever, "finish": finish, "flaky": flaky, "always": always, "fatal": fatal,
                 "stubborn": stubborn, "neighbour": always}
    sup.start(op, factories[op], critical=op == "fatal", restart_on_return=op != "finish")
    if op == "neighbour":
        sup.start("healthy", forever)
    await asyncio.sleep(0.1)
    states = sup.states()
    stuck = await sup.stop(timeout=0.1)
    if op == "flaky":
        ctx.expect(counter["n"] == 3 and counter["ticks"] > 0, "no_crash", f"did not recover: {counter}")
    if op in ("always", "neighbour"):
        ctx.expect(sup.results.get(op) == "failed", "bounded_resources", f"not stopped after failures: {sup.results}")
    if op == "neighbour":
        ctx.expect(counter["ticks"] > 2, "no_crash", "a failing component stopped its neighbour")
    if op == "fatal":
        ctx.expect(sup.results.get(op) == "fatal" and sup.exit_code == 2, "fail_closed", f"{sup.results}")
    if op == "stubborn":
        ctx.expect(stuck == [f"supervised:{op}"], "bounded_resources", f"stop() reported {stuck}")
    report = write_report(_unique(ctx, "crash"), "crash",
                          exception_details(RuntimeError("boom " + " ".join(ctx.sandbox.canaries.values())), "serve"),
                          LOG.recent(5))
    if report is not None:
        text = report.read_text(encoding="utf-8")
        ctx.expect(not any(s and s in text for s in ctx.sandbox.canaries.values()), "no_secret_in_output",
                   "a canary secret reached the crash report")
    return {"states": states, "results": dict(sup.results), "stuck": stuck}


# ---- the scripted demo ------------------------------------------------------------------------------------


def demo_valid(g: Gen) -> list[Case]:
    cases = [valid("link_camera_microphone_unavailable", {"unavailable": True}, isolated_fault=True,
                   max_log_lines=1000)]
    if g.mode == "full":  # quick mode keeps the acceptance scenario only (each run takes about half a second)
        cases.append(valid("everything_up", {"unavailable": False}, isolated_fault=True, max_log_lines=1000))
    return cases


def demo_failure(g: Gen) -> list[Case]:
    return [failure("data_folder_is_a_file", {"unavailable": True, "blocked": True}, "N15", isolated_fault=True,
                    max_log_lines=1000)]


@experiment(
    id="X-02.obs.demo", card="X-02",
    entries=("cli_command.brain.demo-trace", "cli_arg.brain.demo-trace.*", "event_type.turn.*",
             "event_type.demo.step"),
    hypothesis="The scripted conversation against SimBody completes and traces every turn and tool call, with the "
               "body link, camera and microphone up or unavailable, and even when the data folder cannot be written.",
    blast_radius="sim", valid=demo_valid,
    invalid=not_applicable("demo-trace takes only two on/off flags; argparse refuses anything else (P0-04 CLI cases)"),
    hostile=not_applicable("the script is fixed; its hostile turn is checked inside every run (refusals asserted)"),
    failure=demo_failure)
async def check_demo(case: Case, ctx: Ctx) -> Any:
    from brain.obs.demo import run_demo

    data = _unique(ctx, "demo")
    if case.data.get("blocked"):
        data = data / "data-is-a-file"
        data.write_text("x", encoding="utf-8")
    result = await run_demo(unavailable=case.data["unavailable"], data_dir=data, out=None, pace=0.3)
    types = [e["type"] for e in result.events]
    ctx.expect(types.count("turn.start") == 4 and types.count("turn.end") == 4, "no_crash", f"turns: {types[:20]}")
    calls = types.count("tool.call")
    ends = types.count("tool.result") + types.count("tool.refused")
    ctx.expect(calls == ends == 12, "validate_before_use", f"{calls} calls, {ends} results")
    ctx.expect(types.count("tool.refused") >= 4, "right_to_refuse", "the hostile turn was not refused")
    states = {k: v["state"] for k, v in result.health.get("sources", {}).items()}
    if case.data["unavailable"]:
        ctx.expect(states.get("body_link") == "down" and states.get("camera") == "down"
                   and states.get("microphone") == "down", "fail_closed", f"health: {states}")
    return {"events": len(types), "health": states}
