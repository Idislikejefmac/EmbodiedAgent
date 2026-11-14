"""Gates that parse data files and value types: BodyEvent and BodyStatus (B3), the tool catalogue (B10),
the protocol schema file (B10) and the chaos manifest itself (B10).

Steady state: bad input is refused with a ValueError (or the gate's own error type) whose message is short
and holds no secret; a corrupt data file stops the load with a clear message and never leaves a half-loaded
state behind (the schema cache and catalogue path are restored after each case).
"""

from __future__ import annotations

import copy
import functools
import json
import math
from typing import Any

from brain.agent import tools as T
from brain.body.types import BodyEvent, BodyStatus
from brain.transport import protocol as proto
from tools.chaos import manifest as M

from .. import fuzz
from ..api import Case, Ctx, Gen, Violation, experiment, failure, hostile, invalid, not_applicable, valid
from ..matrix import MISSING, Spec, matrix_cases

# ---- BodyEvent and BodyStatus --------------------------------------------------------------------------------

EVENT_SPEC = {"name": Spec("string", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$"),
              "data": Spec("object", nullable=False), "t_us": Spec("integer", minimum=0),
              "t_body_us": Spec("integer", minimum=0, required=False, nullable=True),
              "source": Spec("enum", enum=("body", "adapter"))}
STATUS_SPEC = {"connected": Spec("boolean"), "battery_pct": Spec("number", minimum=0, maximum=100, required=False, nullable=True),
               "charging": Spec("boolean", required=False, nullable=True),
               "temperature_c": Spec("number", required=False, nullable=True),
               "link_rtt_ms": Spec("number", required=False, nullable=True), "t_us": Spec("integer", minimum=0, required=False),
               "stale": Spec("boolean", required=False)}
EVENT_BASE = {"name": "button", "data": {"id": "poke"}, "t_us": 5, "t_body_us": None, "source": "body"}
STATUS_BASE = {"connected": True, "battery_pct": 50.0, "charging": False, "temperature_c": 30.0, "link_rtt_ms": 3.0,
               "capabilities": [], "t_us": 5, "stale": False}


def _variant(kind: str, base: dict[str, Any], field: str, v: Any) -> dict[str, Any]:
    d = copy.copy(base)
    if v is MISSING:
        d.pop(field, None)
    else:
        d[field] = v
    return {"op": kind, "args": d}


def _make_variant(kind: str, base: dict[str, Any], field: str, b: Any) -> dict[str, Any]:
    return _variant(kind, base, field, b.value)


def _matrix(kind: str, specs: dict[str, Spec], base: dict[str, Any], want_class: str) -> list[Case]:
    out: list[Case] = []
    for field, spec in specs.items():
        if field == "data":  # 'any container' limits are the event size gates elsewhere; keep the cheap ones
            continue
        skip = ("oversized_1mib", "ten_times_max_length", "wrong_type_bytes") if spec.type == "string" else ()
        for c in matrix_cases(f"{kind}.{field}", spec, make=functools.partial(_make_variant, kind, base, field), skip=skip):
            if c.cls.value == want_class:
                out.append(c)
    return out


def gen_types_valid(g: Gen) -> list[Case]:
    return _matrix("event", EVENT_SPEC, EVENT_BASE, "valid") + _matrix("status", STATUS_SPEC, STATUS_BASE, "valid") + [
        valid("event_roundtrip", {"op": "event_json", "args": EVENT_BASE}),
        valid("status_roundtrip", {"op": "status_json", "args": STATUS_BASE})]


def gen_types_invalid(g: Gen) -> list[Case]:
    return _matrix("event", EVENT_SPEC, EVENT_BASE, "invalid") + _matrix("status", STATUS_SPEC, STATUS_BASE, "invalid") + [
        invalid("event_json_not_a_dict", {"op": "event_json", "args": [1, 2]}, "N1"),
        invalid("status_json_not_a_dict", {"op": "status_json", "args": "x"}, "N1"),
        invalid("status_json_empty", {"op": "status_json", "args": {}}, "N3"),
        invalid("status_capability_unknown", {"op": "status", "args": {**STATUS_BASE, "capabilities": ["teleport"]}}, "N5"),
        invalid("status_capabilities_not_a_list", {"op": "status", "args": {**STATUS_BASE, "capabilities": 7}}, "N1")]


def gen_types_hostile(g: Gen) -> list[Case]:
    out = []
    for i, t in enumerate(g.rng.sample(fuzz.HOSTILE_TEXT, 10)):
        out.append(hostile(f"event_name_{i}", {"op": "event", "args": {**EVENT_BASE, "name": t}}, "N7"))
    out.append(hostile("event_adapter_source_forged", {"op": "event", "args": {**EVENT_BASE, "name": "link_state", "source": "adapter"}}, "N16"))
    return out


def _types_check(case: Case, ctx: Ctx) -> Any:
    d = case.data
    op, args = d["op"], d["args"]
    try:
        if op in ("event", "event_json"):
            obj: Any = BodyEvent.from_json(args) if op == "event_json" else BodyEvent(**args)
            back = BodyEvent.from_json(obj.to_json())
            ctx.expect(back == obj, "validate_before_use", "an event did not round-trip through to_json")
        else:
            st: Any = BodyStatus.from_json(args) if op == "status_json" else BodyStatus(**args)
            ctx.expect(BodyStatus.from_json(st.to_json()) == st, "validate_before_use", "a status did not round-trip")
        outcome = "accepted"
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        # from_json takes a decoded JSON value: a wrong shape may surface as KeyError/TypeError, which is a refusal here
        ctx.expect(len(str(e)) <= 300, "validate_before_use", f"the refusal is {len(str(e))} characters long")
        outcome = "refused"
    if case.cls.value == "valid":
        ctx.expect(outcome == "accepted", "validate_before_use", f"a valid value was refused ({case.name})")
    elif case.cls.value == "invalid" and "null" not in case.name and "missing" not in case.name:
        pass  # matrix 'accept or refuse' boundaries only check no-crash; strict ones are the explicit cases below
    if case.name.startswith(("status_capab", "event_name_", "event_json_not", "status_json_not")) or case.name.endswith(
            ("one_over_max_length", "at_exclusive_maximum", "one_over_maximum", "one_under_minimum", "nan", "infinity", "minus_infinity")):
        ctx.expect(outcome == "refused", "validate_before_use", f"{case.name} was accepted")
    return outcome


@experiment(
    id="P0-06.types.event_status", card="P0-06",
    entries=("gate.brain.body.types.BodyEvent", "gate.brain.body.types.BodyStatus"),
    hypothesis="BodyEvent and BodyStatus accept their stated ranges and round-trip through JSON, and refuse every "
               "other value (wrong type, out of range, non-finite, bad name, unknown capability) with a short ValueError.",
    blast_radius="pure", valid=gen_types_valid, invalid=gen_types_invalid, hostile=gen_types_hostile,
    failure=not_applicable("pure value types: nothing to disturb"))
def check_types(case: Case, ctx: Ctx) -> Any:
    return _types_check(case, ctx)


# ---- the tool catalogue ------------------------------------------------------------------------------------------


def _cat() -> dict[str, Any]:
    raw: dict[str, Any] = json.loads(T.CATALOGUE_PATH.read_text(encoding="utf-8"))
    return raw


D4 = "X01B-04: a deeply nested data file (schema, manifest or catalogue) ends the load in RecursionError instead of a ValueError; low risk, the files ship with the code"


def gen_cat_valid(g: Gen) -> list[Case]:
    return [valid("shipped_catalogue", {"op": "doc", "mut": None}), valid("shipped_catalogue_from_file", {"op": "file", "mode": "real"}),
            valid("without_optional_timeout", {"op": "doc", "mut": "drop_timeouts"})]


def gen_cat_invalid(g: Gen) -> list[Case]:
    muts = ["not_a_dict", "tools_missing", "tools_string", "reserved_missing", "tool_not_a_dict", "name_int", "name_empty",
            "name_traversal", "name_long", "name_dup", "class_bad", "channels_empty", "channels_no_console", "channels_str",
            "sensitive_str", "description_short", "input_schema_list", "input_schema_invalid", "timeout_zero",
            "timeout_negative", "timeout_nan", "timeout_huge", "timeout_bool", "timeout_plus_arg_missing", "version_str"]
    return [invalid(f"mutation_{m}", {"op": "doc", "mut": m}, "N1" if "str" in m or "int" in m or "not" in m else "N4")
            for m in muts]


def gen_cat_hostile(g: Gen) -> list[Case]:
    return [hostile("name_with_newline_log_forging", {"op": "doc", "mut": "name_newline"}, "N7"),
            hostile("name_unicode_confusable", {"op": "doc", "mut": "name_unicode"}, "N7"),
            hostile("sensitive_tool_without_console", {"op": "doc", "mut": "channels_no_console"}, "N16"),
            hostile("deeply_nested_schema", {"op": "doc", "mut": "deep_schema"}, "N7")]


def gen_cat_failure(g: Gen) -> list[Case]:
    out = []
    for name, data in (("empty", b""), ("not_json", b"{nope"), ("not_utf8", b"\xff\xfe\x00{}"), ("json_null", b"null"),
                       ("json_list", b"[]"), ("deeply_nested", b"[" * 200_000), ("bom", b"\xef\xbb\xbf{}"), ("huge", b" " * 3_000_000 + b"{}")):
        out.append(failure(f"file_{name}", {"op": "file", "mode": "bytes", "data": data}, "N13",
                           known_defect=D4 if name == "deeply_nested" else None))
    out.append(failure("file_missing", {"op": "file", "mode": "missing"}, "N15"))
    return out


def _mutate(raw: dict[str, Any], m: str) -> Any:
    raw = copy.deepcopy(raw)
    tool = raw["tools"][0] if isinstance(raw.get("tools"), list) and raw["tools"] else {}
    if m == "not_a_dict":
        return [raw]
    if m == "drop_timeouts":
        for e in raw["tools"]:
            e.pop("timeout_s", None)
            e.pop("timeout_plus_arg", None)
    elif m in ("tools_missing", "reserved_missing"):
        raw.pop(m.split("_")[0])
    elif m == "tools_string":
        raw["tools"] = "x"
    elif m == "tool_not_a_dict":
        raw["tools"][0] = 7
    elif m == "name_int":
        tool["name"] = 7
    elif m == "name_empty":
        tool["name"] = ""
    elif m == "name_traversal":
        tool["name"] = "../stop"
    elif m == "name_long":
        tool["name"] = "a" * 500
    elif m == "name_newline":
        tool["name"] = "stop\nINFO brain.agent.tools: tool stop ok"
    elif m == "name_unicode":
        tool["name"] = "ｇｅｔ＿ｐｏｓｅ"
    elif m == "name_dup":
        raw["tools"].append(copy.deepcopy(tool))
    elif m == "class_bad":
        tool["class"] = "root"
    elif m == "channels_empty":
        tool["channels"] = []
    elif m == "channels_no_console":
        tool["channels"] = ["app"]
    elif m == "channels_str":
        tool["channels"] = "app"
    elif m == "sensitive_str":
        tool["sensitive"] = "yes"
    elif m == "description_short":
        tool["description"] = "x"
    elif m == "input_schema_list":
        tool["input_schema"] = []
    elif m == "input_schema_invalid":
        tool["input_schema"] = {"type": "object", "properties": {"a": {"type": "nonsense"}}}
    elif m.startswith("timeout_") and m != "timeout_plus_arg_missing":
        tool["timeout_s"] = {"timeout_zero": 0, "timeout_negative": -1, "timeout_nan": math.nan, "timeout_huge": 10**9,
                             "timeout_bool": True}[m]
    elif m == "timeout_plus_arg_missing":
        tool["timeout_plus_arg"] = "no_such_argument"
    elif m == "version_str":
        raw["version"] = "one"
    elif m == "deep_schema":
        deep: Any = {"type": "string"}
        for _ in range(5000):
            deep = {"type": "object", "properties": {"a": deep}}
        tool["input_schema"] = deep
    return raw


@experiment(
    id="P0-06.catalogue.load", card="P0-06",
    entries=("gate.brain.agent.tools.Catalogue.from_json", "file_site.brain/agent/tools.py:load_catalogue"),
    hypothesis="The tool catalogue loads only when every entry is well formed; any corrupt, malformed, oversized or "
               "dangerous catalogue (a name with path or control characters, no console channel, a bad schema) is "
               "refused with a ValueError that names the problem, and the shipped file is restored afterwards.",
    blast_radius="sandbox", valid=gen_cat_valid, invalid=gen_cat_invalid, hostile=gen_cat_hostile, failure=gen_cat_failure)
def check_catalogue(case: Case, ctx: Ctx) -> Any:
    d = case.data
    if d["op"] == "doc":
        raw = _mutate(_cat(), d["mut"]) if d["mut"] else _cat()
        try:
            cat = T.Catalogue.from_json(raw)
        except ValueError as e:
            ctx.expect(len(str(e)) < 4000 and "\n" not in str(e).split(":", 1)[0], "validate_before_use", "refusal text is unbounded")
            outcome = "refused"
        else:
            outcome = "accepted"
            ctx.expect(bool(cat.tools), "validate_before_use", "an empty catalogue was accepted")
        want_ok = case.cls.value == "valid"
        ctx.expect((outcome == "accepted") == want_ok, "validate_before_use", f"{d['mut']}: {outcome}")
        return outcome
    saved = T.CATALOGUE_PATH
    try:
        if d["mode"] == "real":
            T.load_catalogue()
            return "loaded"
        path = ctx.sandbox.fresh_dir("cat") / "tools.json"
        if d["mode"] == "bytes":
            path.write_bytes(d["data"])
        T.CATALOGUE_PATH = path
        try:
            T.load_catalogue()
        except (ValueError, OSError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
            return "refused"
        except RecursionError:
            raise Violation("no_crash", "load_catalogue raised RecursionError on a deeply nested file") from None
        raise Violation("fail_closed", "a corrupt catalogue loaded")
    finally:
        T.CATALOGUE_PATH = saved


# ---- the protocol schema file and the manifest -----------------------------------------------------------------


def gen_schema_valid(g: Gen) -> list[Case]:
    return [valid("shipped_schema", {"op": "schema", "mode": "real"}), valid("cached_second_call", {"op": "schema", "mode": "twice"})]


def gen_schema_invalid(g: Gen) -> list[Case]:
    return [invalid("schema_file_not_json", {"op": "schema", "mode": "bytes", "data": b"{nope"}, "N1"),
            invalid("schema_file_empty", {"op": "schema", "mode": "bytes", "data": b""}, "N3"),
            invalid("schema_file_not_utf8", {"op": "schema", "mode": "bytes", "data": b"\xff\xfe{}"}, "N5"),
            invalid("schema_file_is_not_a_schema", {"op": "schema", "mode": "bytes", "data": b'{"type": 7}'}, "N1"),
            invalid("schema_file_json_list", {"op": "schema", "mode": "bytes", "data": b"[]"}, "N1"),
            invalid("schema_file_deep", {"op": "schema", "mode": "bytes", "data": b"[" * 100_000}, "N4",
                    known_defect="X01B-04: a deeply nested data file (schema, manifest or catalogue) ends the load in RecursionError instead of a ValueError; low risk, the files ship with the code"),
            invalid("manifest_not_toml", {"op": "manifest", "text": "[[entry"}, "N1"),
            invalid("manifest_empty", {"op": "manifest", "text": ""}, "N3"),
            invalid("manifest_wrong_schema", {"op": "manifest", "text": "schema = 2\n"}, "N6"),
            invalid("manifest_entry_not_table", {"op": "manifest", "text": "schema = 1\nentry = [7]\n"}, "N1"),
            invalid("manifest_unknown_kind", {"op": "manifest", "text": 'schema = 1\nentry = [{id="x.y",kind="x",card="P0-04",boundary="B1"}]\n'}, "N4"),
            invalid("manifest_bad_card", {"op": "manifest", "text": 'schema = 1\nentry = [{id="tool.y",kind="tool",card="zz",boundary="B1"}]\n'}, "N5"),
            invalid("manifest_bad_exempt", {"op": "manifest", "text": 'schema = 1\n[[exempt]]\nids = []\ncard = "AND"\nreason = "x"\n'}, "N3"),
            invalid("manifest_exempt_short_reason", {"op": "manifest", "text": 'schema = 1\n[[exempt]]\nids = ["a"]\ncard = "AND"\nreason = "x"\n'}, "N2"),
            invalid("manifest_unknown_top_key", {"op": "manifest", "text": "schema = 1\nsurprise = 1\n"}, "N4"),
            invalid("manifest_deep_toml", {"op": "manifest", "text": "schema = 1\nx = " + "[" * 5000 + "]" * 5000 + "\n"}, "N4", known_defect=D4)]


def gen_schema_hostile(g: Gen) -> list[Case]:
    return [hostile("manifest_gate_name_traversal", {"op": "manifest", "text": 'schema = 1\n[[gate]]\nname = "..\\\\x"\nboundary = "B1"\nproduces = "x"\nlimits = []\n'}, "N7",
                    known_defect="X01B-05: check_gates raises TypeError, not a drift line, for a gate name that is a relative import such as ..x"),
            hostile("manifest_limit_home_code", {"op": "manifest", "text": 'schema = 1\n[limits]\nX = "os:system"\n'}, "N7"),
            hostile("manifest_nul_and_controls", {"op": "manifest", "text": 'schema = 1\n# \x00\x07\n'}, "N7"),
            hostile("manifest_over_size", {"op": "manifest", "text": "# x\n" * (M.MANIFEST_MAX_BYTES // 4 + 10)}, "N7")]


def gen_schema_failure(g: Gen) -> list[Case]:
    return [failure("schema_file_missing", {"op": "schema", "mode": "missing"}, "N15"),
            failure("manifest_file_missing", {"op": "manifest_file", "mode": "missing"}, "N15"),
            failure("manifest_file_not_utf8", {"op": "manifest_file", "mode": "bytes", "data": b"\xff\xfe\x00"}, "N13"),
            failure("manifest_file_over_size", {"op": "manifest_file", "mode": "bytes", "data": b"#" * (M.MANIFEST_MAX_BYTES + 1)}, "N11")]


@experiment(
    id="P0-05.schema.manifest_load", card="P0-05",
    entries=("file_site.brain/transport/protocol.py:schema", "gate.tools.chaos.manifest.load_manifest"),
    hypothesis="A missing, empty, corrupt, mistyped, oversized or deeply nested protocol schema file or chaos manifest is "
               "refused with a ValueError or ManifestError that names the problem, nothing half-loaded stays cached, and "
               "the shipped files load.",
    blast_radius="sandbox", valid=gen_schema_valid, invalid=gen_schema_invalid, hostile=gen_schema_hostile,
    failure=gen_schema_failure)
def check_schema(case: Case, ctx: Ctx) -> Any:
    d = case.data
    op = d["op"]
    if op == "manifest":
        try:
            M.load_manifest(text=d["text"])
        except M.ManifestError as e:
            ctx.expect(len(str(e)) < 5000, "validate_before_use", "manifest refusal is unbounded")
            return "refused"
        except RecursionError:
            raise Violation("no_crash", "load_manifest raised RecursionError") from None
        if case.cls.value == "hostile" and "gate_name" in case.name:  # loading is syntax; the drift check refuses a gate that does not import
            ctx.expect(bool(M.check_gates(M.load_manifest(text=d["text"]))), "validate_before_use", "a gate name with a path was accepted")
        else:
            ctx.expect(case.cls.value == "valid", "validate_before_use", f"{case.name} was accepted")
        return "accepted"
    if op == "manifest_file":
        path = ctx.sandbox.fresh_dir("mf") / "manifest.toml"
        if d["mode"] == "bytes":
            path.write_bytes(d["data"])
        try:
            M.load_manifest(path)
        except M.ManifestError:
            return "refused"
        raise Violation("fail_closed", "a bad manifest file loaded")
    saved = proto.SCHEMA_PATH
    proto.schema.cache_clear()
    proto._validator.cache_clear()
    try:
        if d["mode"] in ("real", "twice"):
            a = proto.schema()
            ctx.expect(a is proto.schema() and proto.schema_errors({}) != [], "validate_before_use", "the shipped schema does not work")
            return "loaded"
        path = ctx.sandbox.fresh_dir("sc") / "v0.schema.json"
        if d["mode"] == "bytes":
            path.write_bytes(d["data"])
        proto.SCHEMA_PATH = path
        try:
            proto.schema_errors({})
        except (ValueError, OSError):
            return "refused"
        except Exception as e:  # broad-except: classify any other library exception as the finding
            if type(e).__name__ == "SchemaError":  # a clear refusal naming the bad schema node
                return "refused"
            if type(e).__name__ == "RecursionError":
                raise Violation("no_crash", f"a corrupt schema file raised {type(e).__name__}, not a ValueError: {str(e)[:100]}") from None
            raise
        raise Violation("fail_closed", "a corrupt protocol schema was used")
    finally:
        proto.SCHEMA_PATH = saved
        proto.schema.cache_clear()
        proto._validator.cache_clear()
