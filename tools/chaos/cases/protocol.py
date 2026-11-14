"""P0-05 protocol gate: `protocol.decode` then `protocol.validate` on one text frame (B1).

Valid frames are the examples in protocol/v0.md (so they stay in step with the spec), re-stamped with
random ids and timestamps. Negative frames are mutations of them.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from brain.config import REPO_ROOT
from brain.transport import protocol as p

from .. import fuzz
from ..api import Case, Ctx, Gen, experiment, hostile, invalid, not_applicable, valid

_EXAMPLES: list[dict[str, Any]] | None = None
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def examples() -> list[dict[str, Any]]:
    global _EXAMPLES
    if _EXAMPLES is None:
        doc = (REPO_ROOT / "protocol" / "v0.md").read_text(encoding="utf-8")
        _EXAMPLES = [json.loads(b) for b in re.findall(r"```json\n(.*?)```", doc, re.S)]
    return _EXAMPLES


def sender_of(msg: dict[str, Any]) -> str | None:
    return p.BODY if msg["ts_brain"] is None else p.BRAIN if msg["ts_body"] is None else None


def frame(msg: Any, sender: str | None, expect: str | set[str], **extra: Any) -> dict[str, Any]:
    text = msg if isinstance(msg, str) else json.dumps(msg, ensure_ascii=False)
    return {"text": text, "sender": sender, "expect": {expect} if isinstance(expect, str) else expect, **extra}


def _pick(g: Gen, mtype: str | None = None, sender: str | None = None) -> dict[str, Any]:
    pool = [m for m in examples() if (mtype is None or m["type"] == mtype) and (sender is None or sender_of(m) == sender)]
    return copy.deepcopy(g.rng.choice(pool))


def gen_valid(g: Gen) -> list[Case]:
    out = [valid(f"doc_example_{i}_{m['type']}", frame(m, sender_of(m), "ok")) for i, m in enumerate(examples())]
    for i in range(g.n(10, 100)):
        m = _pick(g)
        m["id"] = g.rng.randint(1, p.MAX_ID)
        stamp = "ts_body" if m["ts_body"] is not None else "ts_brain"
        m[stamp] = g.rng.choice([0, p.MAX_TS, g.rng.randint(0, p.MAX_TS)])
        out.append(valid(f"restamped_{i}_{m['type']}", frame(m, sender_of(m), "ok")))
    return out


ANY_ERROR = set(p.ERROR_CODES)


def gen_invalid(g: Gen) -> list[Case]:
    out: list[Case] = []
    hello = _pick(g, "hello", p.BODY)
    for name, value in (("id_string", "1"), ("id_float", 1.0), ("id_bool", True), ("id_zero", 0),
                        ("id_too_big", p.MAX_ID + 1), ("id_none", None)):
        m = copy.deepcopy(hello)
        m["id"] = value
        out.append(invalid(name, frame(m, p.BODY, {"bad_envelope"}), "N1" if "too_big" not in name else "N6"))
    for name, value, code in (("v_string", "0", "unsupported_version"), ("v_one", 1, "unsupported_version"),
                              ("v_bool", False, "unsupported_version"), ("payload_list", [], "bad_payload")):
        m = copy.deepcopy(hello)
        m["v" if name.startswith("v") else "payload"] = value
        out.append(invalid(name, frame(m, p.BODY, code), "N1"))
    for field in p.ENVELOPE_FIELDS:
        m = copy.deepcopy(hello)
        del m[field]
        out.append(invalid(f"missing_{field}", frame(m, p.BODY, ANY_ERROR), "N3"))
    out += [invalid("empty_text", frame("", None, "bad_json"), "N3"),
            invalid("empty_object", frame("{}", None, ANY_ERROR), "N3"),
            invalid("not_json", frame("hello there", None, "bad_json"), "N1")]
    m = copy.deepcopy(hello)
    m["payload"]["software"] = "x" * 129
    out.append(invalid("software_one_over", frame(m, p.BODY, "bad_payload"), "N2"))
    m = copy.deepcopy(hello)
    m["unknown_field"] = 1
    out.append(invalid("unknown_envelope_field", frame(m, p.BODY, "bad_envelope"), "N4"))
    out.append(invalid("deep_list_5000", frame(fuzz.nested_json_text(5000), None, ANY_ERROR), "N4"))
    out.append(invalid("deep_list_100000", frame(fuzz.nested_json_text(100_000), None, "bad_json"), "N4"))
    out.append(invalid("deep_object_100000", frame(fuzz.nested_json_text(100_000, "dict"), None, "bad_json"), "N4"))
    for i, ch in enumerate(("\u0007", "\n", "\u001b", "\u007f")):
        m = copy.deepcopy(hello)
        m["payload"]["software"] = f"body{ch}x"
        out.append(invalid(f"software_control_{i}", frame(m, p.BODY, "bad_payload"), "N5"))
    for name, text, code in (("nan", '{"v":0,"type":"heartbeat","id":1,"x":NaN}', "bad_json"),
                             ("infinity", '{"v":0,"type":"heartbeat","id":1,"x":-Infinity}', "bad_json")):
        out.append(invalid(name, frame(text, None, code), "N6"))
    pose = _pick(g, "pose", p.BODY)
    pose["payload"]["position_m"][0] = 12345.678
    text = json.dumps(pose).replace("12345.678", "1e400")
    out.append(invalid("overflow_to_infinity", frame(text, p.BODY, ANY_ERROR), "N6",
                       known_defect="X01-F01: protocol.decode turns 1e400 into inf and validate accepts it; "
                                    "B1 says numbers must be finite (NaN is refused, overflow is not)"))
    return out


def gen_hostile(g: Gen) -> list[Case]:
    out: list[Case] = []
    hello = _pick(g, "hello", p.BODY)
    for i, text in enumerate(fuzz.LOG_FORGING):
        m = copy.deepcopy(hello)
        m["payload"]["software"] = text[:120]
        expect = "bad_payload" if _CONTROL.search(text[:120]) else "ok"
        out.append(hostile(f"forged_log_software_{i}", frame(m, p.BODY, expect), "N7"))
    m = copy.deepcopy(hello)
    m["payload"]["role"] = "brain"
    out.append(hostile("hello_role_spoof", frame(m, p.BODY, "bad_payload"), "N16"))
    cmd = _pick(g, "command")
    out.append(hostile("command_from_body", frame(cmd, p.BODY, "wrong_direction"), "N16", "N8"))
    for i, name in enumerate(("delete_files", "run_command", "read_file", "GET_STATUS", "get_status\n")):
        c = copy.deepcopy(cmd)
        c["payload"]["name"] = name
        out.append(hostile(f"smuggled_command_{i}", frame(c, p.BRAIN, "unknown_command"), "N16"))
    speak = [m for m in examples() if m["type"] == "speak_local"]
    if speak:
        out.append(hostile("speak_from_body", frame(copy.deepcopy(speak[0]), p.BODY, "wrong_direction"), "N8"))
    stamped = copy.deepcopy(hello)
    stamped["ts_body"], stamped["ts_brain"] = None, 5
    out.append(hostile("other_sides_clock", frame(stamped, p.BODY, "bad_envelope"), "N9"))
    return out


@experiment(
    id="P0-05.protocol.parse", card="P0-05",
    entries=("message.*", "field.*", "command.*", "command_arg.*", "error_code.*",
             "gate.brain.transport.protocol.decode", "gate.brain.transport.protocol.validate"),
    hypothesis="Every text frame is either accepted as a valid v0 message or refused with a ProtocolError whose "
               "code is a v0 error code and whose message is at most 500 characters with no control characters; "
               "nothing else is ever raised.",
    blast_radius="pure", valid=gen_valid, invalid=gen_invalid, hostile=gen_hostile,
    failure=not_applicable("a pure parser: link failures are in P0-05.server.session"))
def check(case: Case, ctx: Ctx) -> Any:
    d = case.data
    try:
        msg = p.decode(d["text"])
        p.validate(msg, sender=d["sender"])
        outcome = "ok"
    except p.ProtocolError as e:
        ctx.expect(e.code in p.ERROR_CODES, "validate_before_use", f"unknown error code {e.code!r}")
        ctx.expect(len(e.message) <= 500 and not _CONTROL.search(e.message), "validate_before_use",
                   f"error message of {len(e.message)} characters, or with control characters")
        outcome = e.code
    ctx.expect(outcome in d["expect"], "validate_before_use",
               f"outcome {outcome!r}, expected {sorted(d['expect'])[:4]}{'...' if len(d['expect']) > 4 else ''}")
    return outcome
