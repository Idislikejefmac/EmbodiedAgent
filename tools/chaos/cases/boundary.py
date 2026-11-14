"""The boundary-value matrix (tools/chaos/matrix.py) run over every input that exists today.

One experiment per surface: protocol fields (P0-05), the token parser (P0-05), tool parameters, Body
method arguments and egress entries (P0-06), command-line flags, config keys and the migration tool
(P0-04). For each input: minimum, maximum, one past each, zero, empty, null, missing, wrong type,
wrong encoding and oversized values, generated from the declared limits. The steady state everywhere:
accepted or refused with a structured error; never a traceback, a hang or a forbidden side effect.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import functools
import io
import json
import math
import os
from typing import Any

from brain import __main__ as cli
from brain.agent import tools as T
from brain.agent.body_tools import register_body_tools
from brain.agent.tools import CONSOLE, ToolRegistry
from brain.body import create_body
from brain.body.base import BodyError
from brain.body.types import AudioChunk, Image
from brain.config import ConfigError
from brain.egress import EgressPolicy
from brain.limits import (AUDIO_CHANNELS_MAX, AUDIO_SAMPLE_RATE_HZ_MAX, AUDIO_SAMPLE_RATE_HZ_MIN,
                          CAPTURE_MAX_EDGE_PX_MAX, CAPTURE_MAX_EDGE_PX_MIN, CONFIG_PATH_MAX_CHARS, CONFIG_TEXT_MAX_CHARS,
                          ECHO_TEST_MAX_BYTES, ECHO_TEST_MAX_COUNT, EGRESS_HOSTS_MAX, LLM_REQUESTS_PER_HOUR_MAX,
                          LOG_CAPTURE_DAYS_MAX, LOG_CAPTURE_MB_MAX, MODEL_NAME_MAX_CHARS, OUTPUT_TOKENS_SANITY_MAX,
                          PORT_MAX, PORT_MIN, POSE_RATE_MAX_HZ, SCRYPT_LOG_N, SPEND_CAP_SANITY_MAX,
                          WIRE_IMAGE_EDGE_MAX_PX)
from brain.transport import auth
from brain.transport import protocol as p

from .. import fuzz
from ..api import Case, CaseClass, Ctx, Gen, Violation, experiment, invalid, not_applicable, valid
from ..manifest import capture_parser
from ..matrix import MISSING, Boundary, Spec, matrix_cases, spec_from_schema
from .protocol import examples, sender_of
from .registry import catalogue

NOT_JSON = ("wrong_type_bytes",)  # a JSON frame cannot carry Python bytes
# Megabyte-sized and deep values cost tens of milliseconds each through the schema; quick mode keeps
# them for a few representative fields and full mode runs them for every field.
QUICK_HEAVY = ("oversized_1mib", "oversized", "many_keys", "deep", "any_deep", "ten_times_max_length")
QUICK_HEAVY_KEEP = {("hello", ("software",)), ("event", ("data",)), ("speak_local", ("text",)),
                    ("hello", ("capabilities",)), ("echo", ("text",))}


# ---- P0-05: every payload field of every message type -------------------------------------------------------


def _set(msg: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    m = copy.deepcopy(msg)
    target = m["payload"]
    for key in path[:-1]:
        target = target.setdefault(key, {})
    if value is MISSING:
        target.pop(path[-1], None)
    else:
        target[path[-1]] = value
    return m


def _shallow_set(payload: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    """Copy only the dicts on the path (a deep value elsewhere is never walked)."""
    out = dict(payload)
    out[path[0]] = value if len(path) == 1 else _shallow_set(dict(payload.get(path[0]) or {}), path[1:], value)
    return out


def _get(msg: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = msg["payload"]
    for key in path:
        value = value.get(key, MISSING) if isinstance(value, dict) else MISSING
    return value


def _field_case(b: Boundary, path: tuple[str, ...], msg: dict[str, Any]) -> dict[str, Any]:
    return {"msg": _set(msg, path, b.value), "path": path, "sender": sender_of(msg), "accept": b.accept}


def gen_protocol(g: Gen) -> list[Case]:
    s = p.schema()
    out: list[Case] = []
    seen_types: set[str] = set()
    for msg in examples():
        t = msg["type"]
        if t in seen_types or sender_of(msg) is None:
            continue
        seen_types.add(t)
        payload_def = s["$defs"].get(t, {})
        required = set(payload_def.get("required", []))
        for field, sub in payload_def.get("properties", {}).items():
            paths: list[tuple[tuple[str, ...], Spec]] = [((field,), spec_from_schema(sub, s, required=field in required))]
            resolved = sub if "$ref" not in sub else s["$defs"][sub["$ref"].split("/")[-1]]
            for inner, inner_schema in (resolved.get("properties") or {}).items():
                paths.append(((field, inner), spec_from_schema(inner_schema, s,
                                                               required=inner in resolved.get("required", []))))
            for path, spec in paths:
                original = _get(msg, path)

                def context(b: Boundary, original: Any = original) -> bool | None:
                    # Another enum value can break a rule beyond the field (role must equal the sender;
                    # a command's args depend on its name), so only the example's own value must pass.
                    return None if b.name.startswith("enum_") and b.value != original else b.accept

                heavy = () if g.mode == "full" or (t, path) in QUICK_HEAVY_KEEP else QUICK_HEAVY
                out += matrix_cases(f"{t}.{'.'.join(path)}", spec, skip=NOT_JSON + heavy, context=context,
                                    make=functools.partial(_field_case, path=path, msg=msg))
    return out


def _only(cls: CaseClass, gen: Any) -> Any:
    def pick(g: Gen) -> list[Case]:
        return [c for c in gen(g) if c.cls is cls]
    return pick


@experiment(
    id="P0-05.matrix.protocol_fields", card="P0-05", entries=("field.*", "message.*"),
    hypothesis="For every payload field of every message type, each boundary value is accepted when the schema "
               "allows it and otherwise refused with a ProtocolError; nothing else is ever raised.",
    blast_radius="pure", valid=_only(CaseClass.VALID, gen_protocol), invalid=_only(CaseClass.INVALID, gen_protocol),
    hostile=not_applicable("hostile frames are in P0-05.protocol.parse and P0-05.server.session"),
    failure=not_applicable("a pure parser: link failures are in P0-05.server.session"))
def check_protocol(case: Case, ctx: Ctx) -> Any:
    d = case.data
    try:
        text = json.dumps(d["msg"], ensure_ascii=False)
    except RecursionError:  # the harness's own encoder: put the deep value on the wire as text instead
        holder = dict(d["msg"], payload=_shallow_set(d["msg"]["payload"], d["path"], "__DEEP__"))
        text = json.dumps(holder, ensure_ascii=False).replace('"__DEEP__"', fuzz.nested_json_text(100_000, "dict"))
    try:
        p.validate(p.decode(text), sender=d["sender"])
        outcome = "ok"
    except p.ProtocolError as e:
        outcome = e.code
    if d["accept"] is True:
        ctx.expect(outcome == "ok", "validate_before_use", f"a value the schema allows was refused: {outcome}")
    elif d["accept"] is False:
        ctx.expect(outcome != "ok", "validate_before_use", "a value outside the declared limits was accepted")
    return outcome


# ---- P0-05: the token parser --------------------------------------------------------------------------------

TOKEN_VALUES = [("valid", "ab" * 32, True, ""), ("uppercase", "AB" * 32, True, ""), ("surrounding_space", " " + "ab" * 32 + "\n", True, ""),
                ("empty", "", False, "N3"), ("one_short", "a" * 63, False, "N3"), ("one_long", "a" * 65, False, "N2"),
                ("oversized", "a" * 1_000_000, False, "N2"), ("not_hex", "g" * 64, False, "N5"),
                ("arabic_digits", "١" * 64, False, "N5"), ("fullwidth", "ａ" * 64, False, "N5"),
                ("nul", "a" * 63 + "\x00", False, "N5"), ("inner_space", "a" * 31 + " " + "a" * 32, False, "N5")]


def gen_token(g: Gen) -> list[Case]:
    return [valid(n, {"text": t, "accept": a}) if a else invalid(n, {"text": t, "accept": a}, tag)
            for n, t, a, tag in TOKEN_VALUES]


@experiment(
    id="P0-05.matrix.token", card="P0-05", entries=("gate.brain.transport.auth.parse_token", "store.link_token"),
    hypothesis="The token parser accepts exactly 64 hex characters (case and surrounding space ignored) and "
               "refuses everything else with ValueError.",
    blast_radius="pure", valid=_only(CaseClass.VALID, gen_token), invalid=_only(CaseClass.INVALID, gen_token),
    hostile=not_applicable("the token comes only from the private token file; P0-04.config.fail_closed corrupts it"),
    failure=not_applicable("a pure function"))
def check_token(case: Case, ctx: Ctx) -> Any:
    try:
        token = auth.parse_token(case.data["text"])
    except ValueError:
        ctx.expect(not case.data["accept"], "validate_before_use", "a valid token was refused")
        return "refused"
    ctx.expect(case.data["accept"] and len(token) == auth.TOKEN_BYTES, "validate_before_use", "a bad token was accepted")
    return "accepted"


# ---- P0-06: every tool parameter, through the registry ---------------------------------------------------------


def _tool_case(b: Boundary, name: str, param: str, base: dict[str, Any]) -> dict[str, Any]:
    return {"tool": name, "args": _with(base, param, b.value), "accept": b.accept}


def gen_tools(g: Gen) -> list[Case]:
    out: list[Case] = []
    for name, spec in sorted(catalogue().tools.items()):
        schema = spec.input_schema or {}
        props, required = schema.get("properties", {}), set(schema.get("required", []))
        base = {r: fuzz.valid_from_schema(g.rng, props[r]) for r in required}
        for param, sub in sorted(props.items()):
            pspec = spec_from_schema(sub, required=param in required)
            out += matrix_cases(f"{name}.{param}", pspec,
                                make=functools.partial(_tool_case, name=name, param=param, base=base))
    return out


def _with(base: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    args = dict(base)
    if value is MISSING:
        args.pop(key, None)
    else:
        args[key] = value
    return args


@experiment(
    id="P0-06.matrix.tool_params", card="P0-06", entries=("tool_param.*",),
    hypothesis="For every tool parameter, each boundary value is accepted when the catalogue allows it and "
               "otherwise refused as invalid_arguments; the registry never answers error or raises.",
    blast_radius="sim", valid=_only(CaseClass.VALID, gen_tools), invalid=_only(CaseClass.INVALID, gen_tools),
    hostile=not_applicable("hostile arguments are in P0-06.registry.call"),
    failure=not_applicable("failure injection is in P0-06.registry.call"))
async def check_tools(case: Case, ctx: Ctx) -> Any:
    d = case.data
    registry = ToolRegistry(catalogue())
    body = ctx.sandbox.sim_body()
    register_body_tools(registry, body)
    await body.start()
    try:
        r = await registry.call(d["tool"], d["args"], CONSOLE)
    finally:
        await body.close()
    ctx.expect(r.status != "error", "no_crash", f"the registry answered {r.status}/{r.code}")
    if d["accept"] is True:
        ctx.expect(r.status in ("ok", "unavailable"), "validate_before_use", f"an allowed value was {r.status}/{r.code}")
    elif d["accept"] is False:
        ctx.expect((r.status, r.code) == ("refused", "invalid_arguments"), "validate_before_use",
                   f"a value outside the limits got {r.status}/{r.code}")
    return r.to_json()


# ---- P0-06: Body method arguments (no schema: limits from brain/body/base.py and the wire) -----------------------

BODY_INPUTS: list[tuple[str, Spec, dict[str, str]]] = [
    ("capture_image.max_edge_px", Spec("integer", minimum=CAPTURE_MAX_EDGE_PX_MIN, maximum=CAPTURE_MAX_EDGE_PX_MAX,
                                       required=False), {}),
    ("pose_stream.rate_hz", Spec("number", exclusive_minimum=0, maximum=POSE_RATE_MAX_HZ, required=False), {}),
    ("speak_text.text", Spec("string", min_length=1, strict_text=False), {}),
    ("speak_text.utterance_id", Spec("string", min_length=1, max_length=64, pattern=T.ID_PATTERN), {}),
    # The wire's bounds (schema `pcm`, `image`), enforced by the data classes since H-34 (Appendix A.10 item 2).
    ("audio_chunk.sample_rate_hz", Spec("integer", minimum=AUDIO_SAMPLE_RATE_HZ_MIN, maximum=AUDIO_SAMPLE_RATE_HZ_MAX),
     {}),
    ("audio_chunk.channels", Spec("integer", minimum=1, maximum=AUDIO_CHANNELS_MAX), {}),
    ("image.width_px", Spec("integer", minimum=1, maximum=WIRE_IMAGE_EDGE_MAX_PX), {}),
]


def _body_input_case(b: Boundary, name: str) -> dict[str, Any]:
    return {"input": name, "value": b.value, "accept": b.accept}


def gen_body(g: Gen) -> list[Case]:
    out: list[Case] = []
    for name, spec, known in BODY_INPUTS:
        out += matrix_cases(name, spec, known=known, make=functools.partial(_body_input_case, name=name))
    return out


async def _body_call(body: Any, name: str, value: Any) -> Any:
    kw: dict[str, Any] | None = {} if value is MISSING else None
    if name == "capture_image.max_edge_px":
        return await body.capture_image(**(kw if kw is not None else {"max_edge_px": value}))
    if name == "pose_stream.rate_hz":
        s = body.pose_stream(**(kw if kw is not None else {"rate_hz": value}))
        try:
            return await anext(s)
        finally:
            await s.aclose()
    if name == "speak_text.text":
        return await body.speak_text(**({} if value is MISSING else {"text": value}), utterance_id="u-1")
    if name == "speak_text.utterance_id":
        return await body.speak_text("hello", **({} if value is MISSING else {"utterance_id": value}))
    if name in ("audio_chunk.sample_rate_hz", "audio_chunk.channels"):
        args: dict[str, Any] = {"stream_id": 0, "seq": 0, "sample_rate_hz": 16_000, "channels": 1,
                                "data": b"\x00\x00" * 4}
        if value is MISSING:
            args.pop(name.split(".")[1])
        else:
            args[name.split(".")[1]] = value
        return AudioChunk(**args)
    if name == "image.width_px":
        image: dict[str, Any] = {"image_id": "i-1", "encoding": "jpeg", "height_px": 1, "data": b""}
        if value is not MISSING:
            image["width_px"] = value
        return Image(**image)
    raise ValueError(name)


@experiment(
    id="P0-06.matrix.body_args", card="P0-06",
    entries=("body_method.capture_image", "body_method.pose_stream", "body_method.speak_text",
             "gate.brain.body.types.AudioChunk", "gate.brain.body.types.Image"),
    hypothesis="For every Body argument, each boundary value is accepted inside the interface's stated range and "
               "otherwise refused with ValueError, TypeError or a BodyError at the call, never anything else.",
    blast_radius="sim", valid=_only(CaseClass.VALID, gen_body), invalid=_only(CaseClass.INVALID, gen_body),
    hostile=not_applicable("call sequences and order are in P0-06.body.sequences"),
    failure=not_applicable("link loss and concurrency are in P0-06.body.sequences"))
async def check_body(case: Case, ctx: Ctx) -> Any:
    d = case.data
    body = ctx.sandbox.sim_body()
    await body.start()
    try:
        try:
            await asyncio.wait_for(_body_call(body, d["input"], d["value"]), 3)
            outcome = "accepted"
        except (ValueError, TypeError, BodyError) as e:
            outcome = f"refused ({type(e).__name__})"
    finally:
        await body.close()
    if d["accept"] is True:
        ctx.expect(outcome == "accepted", "validate_before_use", f"an allowed value was {outcome}")
    elif d["accept"] is False:
        ctx.expect(outcome != "accepted", "validate_before_use", "a value outside the stated range was accepted")
    return outcome


# ---- P0-06: egress allowlist entries ---------------------------------------------------------------------------

LABEL63 = "a" * 63
HOST253 = ".".join([LABEL63] * 3 + ["a" * 61])  # 253 characters
EGRESS_ENTRIES = [("plain", "api.example.org", True, ""), ("uppercase", "API.Example.ORG", True, ""),
                  ("port_min", "h.example:1", True, ""), ("port_max", "h.example:65535", True, ""),
                  ("label_63", f"{LABEL63}.example", True, ""), ("host_253", HOST253, True, ""),
                  ("ipv4", "10.1.2.3:8443", True, ""), ("ipv6", "[2001:db8::1]:443", True, ""),
                  ("port_zero", "h.example:0", False, "N6"), ("port_over", "h.example:65536", False, "N6"),
                  ("port_negative", "h.example:-1", False, "N6"), ("label_64", f"{'a' * 64}.example", False, "N2"),
                  ("host_254", HOST253 + "a", False, "N2"), ("empty", "", False, "N3"), ("null", None, False, "N1"),
                  ("int", 443, False, "N1"), ("list", ["h.example"], False, "N1"),
                  ("oversized", "a" * 1_000_000, False, "N2"), ("idn_unicode", "bücher.example", False, "N5"),
                  ("control", "h.exa\nmple", False, "N5"), ("loopback", "127.0.0.1:3128", False, "N5"),
                  ("ipv6_loopback", "[::1]:3128", False, "N5"), ("wildcard", "*.example.org", False, "N5"),
                  ("scheme", "https://h.example", False, "N5"), ("userinfo", "user@h.example", False, "N5")]


def gen_egress(g: Gen) -> list[Case]:
    return [valid(n, {"entry": e, "accept": a}) if a else invalid(n, {"entry": e, "accept": a}, tag)
            for n, e, a, tag in EGRESS_ENTRIES]


@experiment(
    id="P0-06.matrix.egress_entries", card="P0-06", entries=("config_key.egress.allowed_hosts", "egress_host.*"),
    hypothesis="An [egress] entry is accepted only as an exact host name or address with an optional port in "
               "1 to 65535, and everything else is refused with ValueError.",
    blast_radius="pure", valid=_only(CaseClass.VALID, gen_egress), invalid=_only(CaseClass.INVALID, gen_egress),
    hostile=not_applicable("bypass attempts are in P0-06.egress.guard"), failure=not_applicable("a pure function"))
def check_egress(case: Case, ctx: Ctx) -> Any:
    try:
        policy = EgressPolicy.from_hosts([case.data["entry"]])
    except ValueError:
        ctx.expect(not case.data["accept"], "validate_before_use", "a plain entry was refused")
        return "refused"
    ctx.expect(case.data["accept"], "validate_before_use", f"accepted {sorted(policy.allowed)}")
    return sorted(policy.allowed)


# ---- P0-04: numeric command-line flags ----------------------------------------------------------------------------

# Stated ranges (Appendix A; H-18 is that the brain does not enforce them yet).
CLI_RANGES: dict[tuple[str, str, str], tuple[Spec, str | None]] = {  # (spec, recorded defect or None)
    ("brain", "serve", "--port"): (Spec("integer", minimum=PORT_MIN, maximum=PORT_MAX), None),
    ("brain", "probe", "--port"): (Spec("integer", minimum=PORT_MIN, maximum=PORT_MAX), None),
    ("brain", "probe", "--count"): (Spec("integer", minimum=1, maximum=ECHO_TEST_MAX_COUNT), None),
    ("brain", "probe", "--bytes"): (Spec("integer", minimum=0, maximum=ECHO_TEST_MAX_BYTES), None),
    ("brain", "body-check", "--wait"): (Spec("number", minimum=0, maximum=3600), None),  # X01-F02, H-18: fixed
}
_PARSERS: dict[str, argparse.ArgumentParser] = {}


def _parser(program: str) -> argparse.ArgumentParser:
    if program not in _PARSERS:
        from ..manifest import CLI_MAINS

        _PARSERS[program] = capture_parser(CLI_MAINS[program]())
    return _PARSERS[program]


def _argv_value(v: Any) -> str | None:
    if v is MISSING:
        return None
    if v is None:
        return ""
    if isinstance(v, float) and math.isnan(v):
        return "nan"
    return json.dumps(v) if isinstance(v, (list, dict)) else str(v)


def _parses(text: str | None, kind: str) -> bool:
    if text is None or (text.startswith("-") and not text[1:2].isdigit()):
        return False  # argparse reads "-inf" as an unknown option, so it refuses it already
    try:
        (int if kind == "integer" else float)(text)  # what argparse's type= does
        return True
    except (TypeError, ValueError):
        return False


def _cli_case(b: Boundary, program: str, cmd: str, flag: str) -> dict[str, Any]:
    return {"program": program, "argv": [cmd] + ([] if b.value is MISSING else [flag, _argv_value(b.value)]),
            "accept": b.accept}


def gen_cli(g: Gen) -> list[Case]:
    from ..matrix import boundary_values

    out: list[Case] = []
    for (program, cmd, flag), (spec, defect) in CLI_RANGES.items():
        spec = Spec(spec.type, spec.minimum, spec.maximum, required=False)
        # The recorded defect is "a value argparse can convert is not range-checked"; values it cannot
        # convert are refused already and must stay refused.
        known = {b.name: defect for b in boundary_values(spec)
                 if defect and b.accept is False and b.value is not MISSING and _parses(_argv_value(b.value), spec.type)}
        # skip numeric_string: every argv value is a string, so "1" is an ordinary valid value here
        out += matrix_cases(f"{program}.{cmd}.{flag}", spec, known=known,
                            skip=NOT_JSON + ("wrong_type_list", "numeric_string"),
                            make=functools.partial(_cli_case, program=program, cmd=cmd, flag=flag))
    return out


@experiment(
    id="P0-04.matrix.cli_args", card="P0-04", entries=("cli_arg.brain.*", "cli_command.brain.*"),
    hypothesis="Every numeric command-line flag accepts its stated range and refuses anything else at parse "
               "time with a usage message (exit 2), never a traceback.",
    blast_radius="pure", valid=_only(CaseClass.VALID, gen_cli), invalid=_only(CaseClass.INVALID, gen_cli),
    hostile=not_applicable("string flags and every command end to end, in a child process, are tests/test_cli_end_to_end.py (a harness case may not start processes); the chaos flags are X-01.cli_env.flags_and_environment"),
    failure=not_applicable("a broken environment (USERNAME, SystemRoot, LOCALAPPDATA) is X-01.cli_env.flags_and_environment; the commands in a child process are tests/test_cli_end_to_end.py"))
def check_cli(case: Case, ctx: Ctx) -> Any:
    d = case.data
    parser = _parser(d["program"])
    sink = io.StringIO()
    try:
        with contextlib.redirect_stderr(sink):
            ns = parser.parse_args(d["argv"])
        outcome = "accepted"
    except SystemExit as e:
        ctx.expect(e.code == 2 and "usage" in sink.getvalue(), "fail_closed", f"refused without a usage message ({e.code})")
        outcome = "refused"
    if d["accept"] is True:
        ctx.expect(outcome == "accepted", "validate_before_use", "a value in the stated range was refused")
    elif d["accept"] is False:
        ctx.expect(outcome == "refused", "validate_before_use", f"accepted {d['argv'][1:]} ({vars(ns) if outcome == 'accepted' else ''})")
    return outcome


# ---- P0-04: config keys, through everything that reads them at start ------------------------------------------------

# Every key the config gate knows (brain/config.py SCHEMA), with its stated range from brain/limits.py. Every
# key is optional (a missing one takes its safe default); an unknown key stops the brain (DEC-106).
_MONEY = Spec("number", minimum=0, maximum=SPEND_CAP_SANITY_MAX, required=False)
_MODEL = Spec("string", min_length=1, max_length=MODEL_NAME_MAX_CHARS, required=False)
_PATH = Spec("string", min_length=1, max_length=CONFIG_PATH_MAX_CHARS, required=False)
CONFIG_KEYS: list[tuple[tuple[str, ...], Spec]] = [
    (("network", "port"), Spec("integer", minimum=PORT_MIN, maximum=PORT_MAX, required=False)),
    (("network", "host"), Spec("string", min_length=1, max_length=CONFIG_TEXT_MAX_CHARS, required=False)),
    (("body", "kind"), Spec("enum", enum=("sim", "phone"), required=False)),
    (("egress", "allowed_hosts"), Spec("array", item=Spec("string"), max_items=EGRESS_HOSTS_MAX, required=False)),
    (("paths", "data_dir"), _PATH),
    (("paths", "secrets_dir"), _PATH),
    (("caps", "session_usd"), _MONEY),
    (("caps", "daily_usd"), _MONEY),
    (("caps", "hard_stop_usd"), _MONEY),
    (("caps", "requests_per_hour"), Spec("integer", minimum=1, maximum=LLM_REQUESTS_PER_HOUR_MAX, required=False)),
    (("caps", "max_tokens"), Spec("integer", minimum=1, maximum=OUTPUT_TOKENS_SANITY_MAX, required=False)),
    (("models", "live_conversation"), _MODEL),
    (("models", "careful_looking"), _MODEL),
    (("models", "offline"), _MODEL),
    (("logging", "full_prompts"), Spec("boolean", required=False)),
    (("logging", "capture_max_mb"), Spec("integer", minimum=1, maximum=LOG_CAPTURE_MB_MAX, required=False)),
    (("logging", "capture_max_days"), Spec("integer", minimum=1, maximum=LOG_CAPTURE_DAYS_MAX, required=False)),
]


def _config_case(b: Boundary, path: tuple[str, ...]) -> dict[str, Any]:
    return {"path": path, "value": b.value, "accept": b.accept}


def gen_config(g: Gen) -> list[Case]:
    out: list[Case] = []
    for path, spec in CONFIG_KEYS:
        heavy = () if g.mode == "full" or path == ("network", "host") else QUICK_HEAVY
        out += matrix_cases(".".join(path), spec, skip=NOT_JSON + heavy, make=functools.partial(_config_case, path=path))
    return out


def _start_brain_with(ctx: Ctx, cfg: dict[str, Any]) -> str:
    """What `python -m brain` does with its config before any command runs, without running one: the config
    goes through the gate inside main(); a refusal must end main() with exit code 2 and one clear message."""
    saved = getattr(cli, "load_config")
    setattr(cli, "load_config", lambda *a, **k: ctx.sandbox.typed(cfg))  # main() reads its config through this
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            try:
                capture_parser(cli.main, ["info"])
            except RuntimeError:  # main() returned before parse_args: it refused the config
                text = err.getvalue()
                ctx.expect(text.startswith("config error:") and "Traceback" not in text, "fail_closed",
                           f"main() stopped without one clear message: {text[:160]!r}")
                return "refused"
    finally:
        setattr(cli, "load_config", saved)
    config = ctx.sandbox.typed(cfg)
    EgressPolicy.from_config(config)
    cli.data_dir(config)
    cli.secrets_dir(config)
    if config.body.kind == "sim":
        create_body("sim", config)
    return "started"


@experiment(
    id="P0-04.matrix.config_keys", card="P0-04", entries=("config_key.*",),
    hypothesis="Each config key accepts its stated range; anything else stops the brain at start with a clear "
               "refusal (exit code 2 and one 'config error:' message naming the key), never a traceback and never "
               "a silent acceptance.",
    blast_radius="sandbox", valid=_only(CaseClass.VALID, gen_config), invalid=_only(CaseClass.INVALID, gen_config),
    hostile=not_applicable("path confinement is in P0-04.config.fail_closed"),
    failure=not_applicable("missing and corrupt files are in P0-04.config.fail_closed"))
def check_config(case: Case, ctx: Ctx) -> Any:
    d = case.data
    cfg = copy.deepcopy(ctx.sandbox.raw_config)
    section, key = d["path"]
    if d["value"] is MISSING:
        cfg.get(section, {}).pop(key, None)
    else:
        cfg.setdefault(section, {})[key] = d["value"]
    try:
        outcome = _start_brain_with(ctx, cfg)
    except Violation:
        raise
    except Exception as e:  # broad-except: any escape here is a traceback at start
        raise Violation("no_crash", f"start-up with {'.'.join(d['path'])}={str(d['value'])[:40]!r} ended in a "
                                    f"traceback: {type(e).__name__}: {str(e)[:100]}") from None
    shown = f"{'.'.join(d['path'])}={str(d['value'])[:40]!r}"
    if d["accept"] is True:
        ctx.expect(outcome == "started", "validate_before_use", f"{shown} is in its stated range and was refused")
    elif d["accept"] is False:
        ctx.expect(outcome == "refused", "fail_closed", f"{shown} is outside its stated range and was accepted")
        if outcome == "refused":
            try:
                ctx.sandbox.typed(cfg)
            except ConfigError as e:
                ctx.expect(key in str(e), "fail_closed", f"the refusal does not name the key: {str(e)[:120]}")
    return outcome


# ---- P0-04: the migration archive ----------------------------------------------------------------------------------


def gen_migrate(g: Gen) -> list[Case]:
    return [
        valid("round_trip", {"op": "round_trip"}),
        invalid("empty_file", {"op": "blob", "blob": b""}, "N3"),
        invalid("magic_only", {"op": "blob", "blob": b"EAMIG2"}, "N3"),
        invalid("wrong_magic", {"op": "blob", "blob": b"ZIPZIP" + bytes(40)}, "N1"),
        invalid("header_truncated", {"op": "blob", "blob": b"EAMIG2" + bytes([17]) + bytes(10)}, "N3"),
        invalid("log_n_13", {"op": "blob", "blob": b"EAMIG2" + bytes([13]) + bytes(28) + bytes(32)}, "N6"),
        invalid("log_n_255", {"op": "blob", "blob": b"EAMIG2" + bytes([255]) + bytes(28) + bytes(32)}, "N6"),
        invalid("garbage_ciphertext", {"op": "blob", "blob": b"EAMIG2" + bytes([SCRYPT_LOG_N]) + bytes(28)
                                       + g.rng.randbytes(64)}, "N5"),
        invalid("log_n_22_reaches_scrypt", {"op": "log_n", "log_n": 22}, "N6"),  # H-01: fixed
        invalid("log_n_one_under", {"op": "log_n", "log_n": SCRYPT_LOG_N - 1}, "N6"),
        invalid("log_n_one_over", {"op": "log_n", "log_n": SCRYPT_LOG_N + 1}, "N6"),
        invalid("empty_passphrase_export", {"op": "export", "passphrase": ""}, "N3"),
        invalid("passphrase_one_short", {"op": "export", "passphrase": "x" * 11}, "N3"),
        invalid("authentic_but_not_a_tar", {"op": "not_tar"}, "N4"),
        invalid("restore_over_non_empty_dest", {"op": "non_empty_dest"}, "N4"),  # H-33: refused without --force
    ]


@experiment(
    id="P0-04.matrix.migrate", card="P0-04",
    entries=("cli_command.migrate.*", "cli_arg.migrate.*", "store.migration_archive", "gate.tools.migrate.restore_data",
             "gate.tools.migrate.verify_archive", "file_site.tools/migrate.py:*"),
    hypothesis="restore_data refuses any archive it did not write with ValueError (what migrate.main reports as "
               "'Error: ...') before spending memory on it (no header key cost but SCRYPT_LOG_N reaches scrypt), "
               "never overwrites a non-empty destination without --force; export refuses a short passphrase.",
    blast_radius="sandbox", valid=lambda g: [c for c in gen_migrate(g) if c.cls is CaseClass.VALID],
    invalid=lambda g: [c for c in gen_migrate(g) if c.cls is CaseClass.INVALID],
    hostile=not_applicable("stage 1b: path traversal and link members inside an authenticated archive"),
    failure=not_applicable("stage 1b: disk full and a locked destination while restoring"))
def check_migrate(case: Case, ctx: Ctx) -> Any:
    from tools import migrate

    d = case.data
    work = ctx.sandbox.fresh_dir("mig")
    archive, dest = work / "a.eamig", work / "dest"
    if d["op"] == "round_trip":
        src = work / "src"
        (src / "data").mkdir(parents=True)
        (src / "data" / "note.txt").write_text("hello", encoding="utf-8")
        migrate.export_data(src, archive, "correct horse battery")
        entries = migrate.verify_archive(archive, "correct horse battery")
        ctx.expect(("data/note.txt", 5, False) in entries and not dest.exists(), "validate_before_use",
                   f"verify listed {entries[:4]} or extracted something")
        names = migrate.restore_data(archive, dest, "correct horse battery")
        ctx.expect(names == ["data"], "validate_before_use", f"round trip restored {names}")
        return names
    if d["op"] == "export":
        try:
            migrate.export_data(work, archive, d["passphrase"])
        except ValueError:
            return "refused"
        ctx.expect(False, "validate_before_use", "a short passphrase was accepted")
    if d["op"] == "blob":
        archive.write_bytes(d["blob"])
    if d["op"] == "log_n":
        archive.write_bytes(b"EAMIG2" + bytes([d["log_n"]]) + bytes(28) + bytes(32))
        real_key = migrate._key

        def watched(passphrase: str, salt: bytes, log_n: int) -> bytes:
            if log_n != SCRYPT_LOG_N:
                raise Violation("bounded_resources", f"header log_n {log_n} reached scrypt before the archive was "
                                                     "authenticated")
            return real_key(passphrase, salt, log_n)

        migrate._key = watched
        try:
            migrate.restore_data(archive, dest, "whatever passphrase")
        except ValueError:
            return "refused"
        finally:
            migrate._key = real_key
    if d["op"] == "not_tar":
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        salt, nonce, log_n = os.urandom(16), os.urandom(12), SCRYPT_LOG_N
        header = migrate.MAGIC + bytes([log_n]) + salt + nonce
        key = migrate._key("correct horse battery", salt, log_n)
        archive.write_bytes(header + AESGCM(key).encrypt(nonce, b"this is not a gzip tar", header))
        try:
            migrate.restore_data(archive, dest, "correct horse battery")
        except ValueError:
            return "refused"
        except Exception as e:  # broad-except: anything but ValueError is a traceback from migrate.main
            raise Violation("no_crash", f"restore raised {type(e).__name__}, which migrate.main does not catch") from None
    if d["op"] == "non_empty_dest":
        src = work / "src"
        (src / "data").mkdir(parents=True)
        (src / "data" / "note.txt").write_text("old", encoding="utf-8")
        (dest / "data").mkdir(parents=True)
        (dest / "data" / "note.txt").write_text("newer", encoding="utf-8")
        migrate.export_data(src, archive, "correct horse battery")
        try:
            migrate.restore_data(archive, dest, "correct horse battery")
        except ValueError:
            ctx.expect((dest / "data" / "note.txt").read_text(encoding="utf-8") == "newer", "fail_closed",
                       "the refused restore still changed the destination")
            return "refused"
        ctx.expect(False, "fail_closed", "restore overwrote a non-empty destination without --force")
    try:
        migrate.restore_data(archive, dest, "whatever passphrase")
    except ValueError:
        return "refused"
    except Exception as e:  # broad-except: anything but ValueError is a traceback from migrate.main
        raise Violation("no_crash", f"restore raised {type(e).__name__}: {str(e)[:100]}") from None
    ctx.expect(False, "fail_closed", "a corrupt archive was restored")
