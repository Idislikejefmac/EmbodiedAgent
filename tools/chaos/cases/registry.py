"""P0-06 tool registry: the command boundary (plan "Command boundary"; B6, B7, B16).

Two experiments:
- P0-06.registry.call drives `ToolRegistry.call` with SimBody behind it, for every listed and reserved
  tool, from both channels, with valid, invalid, hostile and failing inputs.
- P0-06.registry.args_gate checks the argument gate `_plain_json_problem` against an independent
  reference model of its contract (differential oracle), so a removed depth guard is caught whether it
  shows up as a crash (RecursionError) or as deep input let through.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any

from brain.__main__ import BOUNDARY_REQUESTS
from brain.agent import tools as T
from brain.agent.body_tools import register_body_tools
from brain.agent.tools import APP, CONSOLE, Caller, Channel, ToolRegistry, ToolResult
from brain.body.sim import SimBody
from brain.body.types import ConfidenceBand

from .. import fuzz
from ..api import Case, CaseClass, Ctx, Gen, Violation, experiment, failure, hostile, invalid, not_applicable, valid
from ..thresholds import TIMER_TOLERANCE_MS

_CATALOGUE: T.Catalogue | None = None
ABANDONED_TASKS_PER_TOOL_MAX = 4  # Appendix A.2, Provisional (H-20): not enforced by the registry yet


def catalogue() -> T.Catalogue:
    global _CATALOGUE
    if _CATALOGUE is None:
        _CATALOGUE = T.load_catalogue()
    return _CATALOGUE


CALLERS: dict[str, Caller] = {
    "console": CONSOLE,
    "app": APP,
    "app_confirmed_voice_only": Caller(Channel.APP, ConfidenceBand.CONFIRMED, frozenset({"voice"})),
    "app_confirmed_no_evidence": Caller(Channel.APP, ConfidenceBand.CONFIRMED, frozenset()),
    "app_probable_both": Caller(Channel.APP, ConfidenceBand.PROBABLE, frozenset({"voice", "face"})),
    "app_confirmed_both": Caller(Channel.APP, ConfidenceBand.CONFIRMED, frozenset({"voice", "face"})),
}
OK_OR_NOT_BUILT = {("ok", "ok"), ("unavailable", "not_built")}
REFUSED_ARGS = {("refused", "invalid_arguments")}


def _call(tool: Any, args: Any, caller: str, expect: set[tuple[str, str]] | None = None, *, statuses: set[str] | None = None,
          body: str = "none", setup: str = "", **extra: Any) -> dict[str, Any]:
    return {"tool": tool, "args": args, "caller": caller, "expect": expect, "statuses": statuses, "body": body,
            "setup": setup, **extra}


# ---- generators: P0-06.registry.call ---------------------------------------------------------------------


def gen_valid(g: Gen) -> list[Case]:
    cat = catalogue()
    out = []
    for name, spec in sorted(cat.tools.items()):
        for caller in ("console", "app"):
            for i in range(g.n(1, 5)):
                args = fuzz.valid_from_schema(g.rng, spec.input_schema or {})
                out.append(valid(f"{name}.{caller}.{i}", _call(name, args, caller, OK_OR_NOT_BUILT, body="started")))
    for name, spec in sorted(cat.reserved.items()):
        out.append(valid(f"reserved.{name}.console", _call(name, {}, "console", {("unavailable", "not_built")})))
        app_expect = {("refused", "identity_not_confirmed")} if spec.sensitive else {("unavailable", "not_built")}
        out.append(valid(f"reserved.{name}.app", _call(name, {}, "app", app_expect)))
    out.append(valid("get_body_status.args_none", _call("get_body_status", None, "app", {("ok", "ok")}, body="started")))
    return out


def gen_invalid(g: Gen) -> list[Case]:
    cat = catalogue()
    out: list[Case] = []
    for name, spec in sorted(cat.tools.items()):
        props = (spec.input_schema or {}).get("properties", {})
        required = (spec.input_schema or {}).get("required", [])
        base = {r: fuzz.valid_from_schema(g.rng, props[r]) for r in required}
        for param, schema in sorted(props.items()):
            variants = fuzz.invalid_variants(schema)
            chosen = variants if g.mode == "full" else g.rng.sample(variants, min(len(variants), 4))
            for vname, value, tag in chosen:
                kind = schema.get("x-kind")
                expect = REFUSED_ARGS
                if tag == "N5" and kind in ("spoken_text", "reason_text") and not any(
                        0 <= ord(c) < 32 and c not in "\n\t" for c in str(value)):
                    expect = OK_OR_NOT_BUILT | REFUSED_ARGS  # free text may hold any printable character
                out.append(invalid(f"{name}.{param}.{vname}", _call(name, {**base, param: value}, "console", expect,
                                                                      body="started"), tag))
        if required:
            out.append(invalid(f"{name}.missing_required", _call(name, {}, "console", REFUSED_ARGS), "N3"))
        out.append(invalid(f"{name}.unexpected_key", _call(name, {**base, "zz_extra": 1}, "console", REFUSED_ARGS), "N4"))
    # Structure and size, on one tool (the checks run before anything tool-specific).
    deep = {"deep_dict_9": fuzz.nested(8, "dict"), "deep_dict_100": fuzz.nested(100, "dict"),
            "deep_dict_50000": fuzz.nested(50_000, "dict"), "deep_list_50000": fuzz.nested(50_000, "list")}
    for vname, value in deep.items():
        out.append(invalid(f"speak.{vname}", _call("speak", {"text": "hi", "x": value}, "console", REFUSED_ARGS), "N4"))
    out.append(invalid("speak.deep_whole_args", _call("speak", fuzz.nested(50_000, "dict"), "console", REFUSED_ARGS), "N4"))
    out.append(invalid("speak.many_keys_3000", _call("speak", {"text": "hi", **fuzz.many_keys(3000)}, "app", REFUSED_ARGS),
                       "N4", "N2"))
    out.append(invalid("speak.nodes_2001", _call("speak", {"text": "hi", "x": list(range(2001))}, "console", REFUSED_ARGS),
                       "N4"))
    out.append(invalid("speak.total_over_64k", _call("speak", {"text": "hi", "pad": ["x" * 1000] * 70}, "console",
                                                     REFUSED_ARGS), "N2"))
    out.append(invalid("speak.string_over_64k", _call("speak", {"text": "x" * (T.MAX_ARGS_BYTES + 1)}, "console",
                                                      REFUSED_ARGS), "N2"))
    out.append(invalid("speak.non_str_key", _call("speak", {"text": "hi", 1: "x"}, "console", REFUSED_ARGS), "N1"))
    out.append(invalid("speak.nan_value", _call("speak", {"text": "hi", "x": math.nan}, "console", REFUSED_ARGS), "N6"))
    for i, args in enumerate(("{\"text\": \"hi\"}", ["text", "hi"], 7, b"{}", ("text", "hi"), {"text": b"hi"})):
        out.append(invalid(f"speak.args_wrong_type_{i}", _call("speak", args, "console", REFUSED_ARGS), "N1"))
    return out


class _EvilDict(dict):  # type: ignore[type-arg]
    def __iter__(self) -> Any:
        raise RuntimeError("iterating me runs code")

    def keys(self) -> Any:
        raise RuntimeError("keys runs code")

    def __len__(self) -> int:
        raise RuntimeError("len runs code")


class _SneakyStr(str):
    pass


def gen_hostile(g: Gen) -> list[Case]:
    cat = catalogue()
    out: list[Case] = []
    names: list[Any] = [n for n, _ in BOUNDARY_REQUESTS] + [
        "read_file", "delete_files", "run_command", "GET_POSE", "get_pose\n", "get_pose​", "ｇｅｔ＿ｐｏｓｅ",
        "../stop", "stop\nINFO brain.agent.tools: tool stop ok (channel console)", "x" * 500, 7, None, b"stop",
        ["stop"], "__class__", "",
    ]
    for i, name in enumerate(names):
        for caller in ("app", "console"):
            out.append(hostile(f"unknown_tool_{i}.{caller}", _call(name, {}, caller, {("refused", "unknown_tool")}),
                               "N7" if isinstance(name, str) else "N16"))
    texts = fuzz.HOSTILE_TEXT if g.mode == "full" else g.rng.sample(fuzz.HOSTILE_TEXT, 8)
    for i, text in enumerate(texts):
        # Free text is data: spoken or refused, never acted on (the monitors watch for any side effect).
        out.append(hostile(f"speak.hostile_text_{i}", _call("speak", {"text": text}, "app", None,
                                                            statuses={"ok", "refused"}, body="started"), "N7"))
        out.append(hostile(f"navigate_to.hostile_name_{i}", _call("navigate_to", {"waypoint": text}, "app", None,
                                                                  statuses={"refused", "unavailable"}), "N7"))
    for name, spec in sorted(cat.reserved.items()):
        if not spec.sensitive:
            continue
        for caller in ("app_confirmed_voice_only", "app_confirmed_no_evidence", "app_probable_both"):
            out.append(hostile(f"{name}.{caller}", _call(name, {}, caller, {("refused", "identity_not_confirmed")}),
                               "N16"))
        out.append(hostile(f"{name}.app_confirmed_both", _call(name, {}, "app_confirmed_both",
                                                               {("unavailable", "not_built")}), "N16"))
    out.append(hostile("speak.evil_dict_args", _call("speak", "EVIL_DICT", "app", REFUSED_ARGS), "N7"))
    out.append(hostile("speak.sneaky_str_value", _call("speak", {"text": "SNEAKY_STR"}, "app", REFUSED_ARGS), "N7"))
    for i, ev in enumerate(("voiceface", ["retina"], b"voice")):
        out.append(hostile(f"caller.forged_evidence_{i}", {"forge_evidence": ev}, "N16"))
    return out


def gen_failure(g: Gen) -> list[Case]:
    return [
        failure("body_not_started.capture", _call("capture_image", {"reason": "look"}, "console", None,
                                                  statuses={"unavailable"}, body="created"), "N14"),
        failure("body_not_started.status", _call("get_body_status", {}, "console", {("ok", "ok")}, body="created"),
                "N14"),
        failure("link_down.speak", _call("speak", {"text": "hello"}, "console", None, statuses={"unavailable"},
                                         body="started", setup="link_down"), "N14"),
        failure("handler_raises", _call("who_is_present", {}, "console", {("error", "internal")}, setup="raises"),
                "N14", isolated_fault=True),
        failure("handler_bad_output", _call("who_is_present", {}, "console", {("error", "internal")},
                                            setup="bad_output"), "N14"),
        failure("handler_hangs", _call("who_is_present", {}, "console", {("unavailable", "timeout")}, setup="hangs",
                                       timeout_s=0.2), "N12"),
        failure("handler_ignores_cancel", _call("who_is_present", {}, "console", {("unavailable", "timeout")},
                                                setup="ignores_cancel", timeout_s=0.05, calls=ABANDONED_TASKS_PER_TOOL_MAX + 2),
                "N11", "N12", known_defect="H-20: ToolRegistry._abandoned has no cap; a handler that ignores "
                                           "cancellation stays in it (ABANDONED_TASKS_PER_TOOL_MAX not enforced)"),
        failure("concurrent_calls", _call("get_body_status", {}, "app", {("ok", "ok")}, body="started",
                                          setup="concurrent", calls=30), "N10"),
        failure("close_during_get_pose", _call("get_pose", {}, "console", None, statuses={"ok", "unavailable", "error"},
                                               body="started", setup="close_mid_call"), "N10"),
    ]


def _check_result(ctx: Ctx, r: object, d: dict[str, Any]) -> ToolResult:
    if not isinstance(r, ToolResult):
        raise Violation("no_crash", f"call returned {type(r).__name__}, not a ToolResult")
    ctx.expect(not (r.detail or "").startswith("arguments could not be checked"), "no_crash",
               f"the argument check itself raised: {r.detail}")
    ctx.expect(r.message in T.MESSAGES.values() or r.ok, "right_to_refuse", "a message the registry did not write")
    pair = (r.status, r.code)
    if d["expect"] is not None:
        ctx.expect(pair in d["expect"], "right_to_refuse" if r.ok else "validate_before_use",
                   f"got {pair}, expected one of {sorted(d['expect'])}")
    if d["statuses"] is not None:
        ctx.expect(r.status in d["statuses"], "validate_before_use", f"got status {r.status}/{r.code}, "
                                                                     f"expected one of {sorted(d['statuses'])}")
    if r.detail is not None:
        ctx.expect(len(r.detail) <= 400, "bounded_resources", f"detail of {len(r.detail)} characters goes to the model")
    json.dumps(r.to_json())  # the model's view must be plain JSON
    return r


@experiment(
    id="P0-06.registry.call", card="P0-06",
    entries=("tool.*", "reserved_tool.*", "tool_param.*", "gate.brain.agent.tools.ToolRegistry.call",
             "gate.brain.agent.tools.Caller", "cli_command.brain.body-check"),
    hypothesis="Every call returns a ToolResult and never raises; only listed tools with valid arguments run; "
               "everything else is refused with a fixed message, one bounded log line, and no side effect.",
    blast_radius="sim", valid=gen_valid, invalid=gen_invalid, hostile=gen_hostile, failure=gen_failure)
async def check_call(case: Case, ctx: Ctx) -> Any:
    d = case.data
    if "forge_evidence" in d:
        try:
            Caller(Channel.APP, ConfidenceBand.CONFIRMED, d["forge_evidence"])
        except ValueError:
            return "refused at the Caller gate"
        ctx.expect(False, "validate_before_use", "Caller accepted forged evidence")
    registry = ToolRegistry(catalogue(), timeout_s=d.get("timeout_s", T.DEFAULT_TIMEOUT_S))
    body = ctx.sandbox.sim_body()
    register_body_tools(registry, body)
    args = d["args"]
    if args == "EVIL_DICT":
        args = _EvilDict(text="hi")
    elif isinstance(args, dict) and args.get("text") == "SNEAKY_STR":
        args = {"text": _SneakyStr("hi")}
    setup = d["setup"]
    if setup == "raises":
        async def boom(a: dict[str, Any], c: Caller) -> dict[str, Any]:
            raise RuntimeError("handler exploded")
        registry.register(d["tool"], boom)
    elif setup == "bad_output":
        async def bad(a: dict[str, Any], c: Caller) -> dict[str, Any]:
            return {"not_in_the_output_schema": True}
        registry.register(d["tool"], bad)
    elif setup == "hangs":
        async def hang(a: dict[str, Any], c: Caller) -> dict[str, Any]:
            await asyncio.sleep(30)
            return {}
        registry.register(d["tool"], hang)
    elif setup == "ignores_cancel":
        async def stubborn(a: dict[str, Any], c: Caller) -> dict[str, Any]:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(0.3)  # ignores the cancellation for a while (H-20)
            return {}
        registry.register(d["tool"], stubborn)
    caller = CALLERS[d["caller"]]
    if d["body"] == "started":
        await body.start()
    try:
        if setup == "link_down":
            body.set_link(False)
        if setup in ("concurrent", "ignores_cancel"):
            results = await asyncio.gather(*(registry.call(d["tool"], args, caller) for _ in range(d["calls"])))
            for r in results:
                _check_result(ctx, r, d)
            if setup == "ignores_cancel":
                ctx.monitors.observe_queue("ToolRegistry._abandoned", len(registry._abandoned),
                                           ABANDONED_TASKS_PER_TOOL_MAX)
                await asyncio.sleep(0.4)  # let the stubborn handlers end, so nothing outlives the case
            return [r.to_json() for r in results[:2]]
        if setup == "close_mid_call":
            task = asyncio.ensure_future(registry.call(d["tool"], args, caller))
            await asyncio.sleep(0)
            await body.close()
            return _check_result(ctx, await task, d).to_json()
        t0 = time.monotonic()
        r = await registry.call(d["tool"], args, caller)
        elapsed = time.monotonic() - t0
        if setup == "hangs":
            limit = d["timeout_s"] + 2 * TIMER_TOLERANCE_MS / 1000
            ctx.note(f"timeout answered after {elapsed * 1000:.0f} ms")
            ctx.expect(elapsed <= limit, "bounded_resources", f"timeout answered after {elapsed:.3f} s (limit {limit:.3f})")
        return _check_result(ctx, r, d).to_json()
    finally:
        await body.close()


# ---- P0-06.registry.args_gate: differential check of the argument gate -------------------------------------


def reference_problem(args: object) -> bool:
    """The contract of `_plain_json_problem`, written independently: True when `args` must be refused."""
    if type(args) is not dict:
        return True
    nodes = 0
    stack: list[tuple[object, int]] = [(args, 1)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > T.MAX_ARGS_NODES:
            return True
        kind = type(value)
        if kind is dict or kind is list:
            if depth > T.MAX_ARGS_DEPTH:
                return True
            if kind is dict:
                if any(type(k) is not str for k in dict.keys(value)):  # type: ignore[arg-type]
                    return True
                stack.extend((v, depth + 1) for v in dict.values(value))  # type: ignore[arg-type]
            else:
                stack.extend((v, depth + 1) for v in list.__iter__(value))  # type: ignore[arg-type]
        elif kind is float:
            if not math.isfinite(value):  # type: ignore[arg-type]
                return True
        elif kind is str:
            if len(value) > T.MAX_ARGS_BYTES:  # type: ignore[arg-type]
                return True
        elif kind not in (int, bool, type(None)):
            return True
    return len(json.dumps(args, allow_nan=False)) > T.MAX_ARGS_BYTES


def gate_valid(g: Gen) -> list[Case]:
    out = [valid("empty", {}), valid("depth_at_limit", fuzz.nested(T.MAX_ARGS_DEPTH, "dict")),
           valid("nodes_at_limit", {"x": list(range(T.MAX_ARGS_NODES - 2))})]
    for i in range(g.n(30, 300)):
        out.append(valid(f"random_{i}", {"a": fuzz.json_value(g.rng, max_depth=6)}))
    return out


def gate_invalid(g: Gen) -> list[Case]:
    out = [invalid("depth_one_over", fuzz.nested(T.MAX_ARGS_DEPTH + 1, "dict"), "N4")]
    for depth in (10, 100, 900, 5000, 50_000):
        out.append(invalid(f"deep_dict_{depth}", {"a": fuzz.nested(depth, "dict")}, "N4"))
        out.append(invalid(f"deep_list_{depth}", {"a": fuzz.nested(depth, "list")}, "N4"))
    for i in range(g.n(10, 100)):
        out.append(invalid(f"random_deep_{i}", {"a": fuzz.nested(g.rng.randint(9, 3000), g.rng.choice(("dict", "list")),
                                                                 fuzz.json_value(g.rng, 2))}, "N4"))
    out += [invalid("nodes_one_over", {"x": list(range(T.MAX_ARGS_NODES))}, "N4"),
            invalid("string_one_over", {"x": "a" * (T.MAX_ARGS_BYTES + 1)}, "N2"),
            invalid("total_over", {"x": ["y" * 1000] * 70}, "N2"),
            invalid("nan", {"x": math.nan}, "N6"), invalid("inf", {"x": [math.inf]}, "N6"),
            invalid("int_key", {1: "x"}, "N1"), invalid("tuple", {"x": (1, 2)}, "N1"), invalid("bytes", {"x": b"x"}, "N1"),
            invalid("set", {"x": {1}}, "N1"), invalid("not_a_dict", [1], "N1"), invalid("none", None, "N1")]
    return out


def gate_hostile(g: Gen) -> list[Case]:
    return [hostile("evil_dict", "EVIL_DICT", "N7"), hostile("sneaky_str", "SNEAKY_STR", "N7"),
            hostile("object", {"x": "OBJECT"}, "N7")]


@experiment(
    id="P0-06.registry.args_gate", card="P0-06", entries=("gate.brain.agent.tools._plain_json_problem",),
    hypothesis="The argument gate never raises, and refuses exactly the arguments its contract refuses "
               "(not a dict, deeper than MAX_ARGS_DEPTH, more than MAX_ARGS_NODES values, non-finite, "
               "not plain JSON, larger than MAX_ARGS_BYTES).",
    blast_radius="pure", valid=gate_valid, invalid=gate_invalid, hostile=gate_hostile,
    failure=not_applicable("a pure function: there is no I/O, timing or dependency to disturb"))
def check_gate(case: Case, ctx: Ctx) -> Any:
    value = case.data
    if value == "EVIL_DICT":
        value = _EvilDict(a=1)
    elif value == "SNEAKY_STR":
        value = {"x": _SneakyStr("x")}
    elif isinstance(value, dict) and value.get("x") == "OBJECT":
        value = {"x": object()}
    problem = T._plain_json_problem(value)  # a crash here is the finding
    ctx.expect(problem is None or isinstance(problem, str), "no_crash", f"returned {type(problem).__name__}")
    want = True if case.cls is CaseClass.HOSTILE else reference_problem(value)
    ctx.expect((problem is not None) == want, "validate_before_use",
               f"gate said {'refuse' if problem else 'accept'}, contract says {'refuse' if want else 'accept'}")
    return problem
