"""Generate protocol/corpus/v0-hostile.json, the hostile-input corpus shared by Python and Kotlin.

Run from the repo root:  .\\.venv\\Scripts\\python tools\\gen_hostile_corpus.py   (add --check to verify only)

Every verdict is decided by RUNNING the Python validator (brain.transport.protocol.decode + validate, or
unpack_binary), never written by hand. Deterministic: one fixed-seed random.Random, sorted ids, no clock.
The Kotlin side (body-android HostileCorpusTest) replays the same file and must agree, apart from the
documented divergences listed in that test.
"""

from __future__ import annotations

import argparse
import copy
import json
import random
import re
import struct
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from brain.transport import auth as pauth  # noqa: E402
from brain.transport import protocol as p  # noqa: E402

OUT = REPO / "protocol" / "corpus" / "v0-hostile.json"
VERSION = 1
GENERATOR = "tools/gen_hostile_corpus.py"
SEED = 20261113

TOKEN = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"

_EXAMPLES: list[dict[str, Any]] | None = None


def examples() -> list[dict[str, Any]]:
    global _EXAMPLES
    if _EXAMPLES is None:
        doc = (REPO / "protocol" / "v0.md").read_text(encoding="utf-8")
        _EXAMPLES = [json.loads(b) for b in re.findall(r"```json\n(.*?)```", doc, re.S)]
    return _EXAMPLES


def sender_of(msg: dict[str, Any]) -> str:
    return p.BODY if msg["ts_brain"] is None else p.BRAIN


def other(s: str) -> str:
    return p.BRAIN if s == p.BODY else p.BODY


# ---- running the Python validator ----------------------------------------------------------------------

def verdict_text(text: str, sender: str | None) -> tuple[str, str | None]:
    try:
        p.validate(p.decode(text), sender=sender)
        return "accept", None
    except p.ProtocolError as e:
        return "reject", e.code


def verdict_binary(hexstr: str) -> tuple[str, str | None]:
    try:
        p.unpack_binary(bytes.fromhex(hexstr))
        return "accept", None
    except p.ProtocolError as e:
        return "reject", e.code


class Corpus:
    def __init__(self) -> None:
        self.cases: dict[str, dict[str, Any]] = {}

    def text(self, cid: str, text: str, sender: str | None) -> None:
        if cid in self.cases:
            raise RuntimeError(f"duplicate id {cid}")
        v, code = verdict_text(text, sender)
        self.cases[cid] = {"id": cid, "kind": "text", "input": text, "sender": sender, "verdict": v, "code": code}

    def msg(self, cid: str, obj: Any, sender: str | None, **kw: Any) -> None:
        self.text(cid, json.dumps(obj, ensure_ascii=kw.get("ascii", True)), sender)

    def binary(self, cid: str, data: bytes) -> None:
        if cid in self.cases:
            raise RuntimeError(f"duplicate id {cid}")
        h = data.hex()
        v, code = verdict_binary(h)
        self.cases[cid] = {"id": cid, "kind": "binary", "input": h, "sender": None, "verdict": v, "code": code}


def nested(depth: int, kind: str) -> str:
    if kind == "list":
        return "[" * depth + "]" * depth
    return '{"a":' * depth + "1" + "}" * depth


def build() -> dict[str, Any]:
    rng = random.Random(SEED)
    c = Corpus()
    exs = examples()

    # ---- valid examples, both directions ----
    for i, m in enumerate(exs):
        s = sender_of(m)
        c.msg(f"ex{i:02d}_{m['type']}_from_{s}", m, s)
        c.msg(f"ex{i:02d}_{m['type']}_from_{other(s)}", m, other(s))

    # ---- non-JSON and structural garbage ----
    raw = {
        "empty": "", "space": " ", "newline": "\n", "garbage": "hello there", "brace": "{", "close": "}",
        "trunc_obj": '{"v":0,"type":"heartbeat","id":1,"reply_to":null,"ts_body":null,"ts_brain":5,"payl',
        "trunc_mid_string": '{"v":0,"type":"hear', "trailing_comma": '{"v":0,}', "single_quotes": "{'v':0}",
        "json_null": "null", "json_true": "true", "json_number": "42", "json_string": '"hello"', "json_list": "[]",
        "json_list_obj": '[{"v":0}]', "empty_obj": "{}", "nan": '{"v":0,"type":"heartbeat","id":1,"x":NaN}',
        "infinity": '{"v":0,"type":"heartbeat","id":1,"x":Infinity}',
        "neg_infinity": '{"v":0,"type":"heartbeat","id":1,"x":-Infinity}', "bare_nan": "NaN",
        "bom_prefix": "\ufeff{}", "two_objects": "{}{}", "trailing_garbage": '{"v":0} x',
        "comment": '{"v":0}//x', "hex_number": '{"v":0x0}', "leading_zero": '{"v":00}', "plus_number": '{"v":+0}',
        "nul_only": "\u0000", "nul_in_obj": '{"v":0,"type":"heartbeat"}\u0000',
        "ctrl_in_string": '{"v":0,"type":"hea\u0001rtbeat"}', "raw_newline_in_string": '{"v":0,"type":"hea\nrtbeat"}',
        "tab_in_string": '{"v":0,"type":"hea\trtbeat"}', "bad_escape": '{"v":0,"type":"\\q"}',
        "short_u_escape": '{"v":0,"type":"\\u12"}', "unquoted_key": "{v:0}", "key_only": '{"v"}',
        "colon_only": '{:}', "double_comma": '{"v":0,,"type":"x"}', "huge_exponent": '{"v":1e400}',
        "neg_huge_exponent": '{"v":-1e400}', "tiny_exponent": '{"v":1e-400}', "big_int_literal": '{"v":' + "9" * 400 + "}",
        "id_1e400": '{"v":0,"type":"heartbeat","id":1e400,"reply_to":null,"ts_body":null,"ts_brain":5,"payload":{}}',
    }
    for k, t in sorted(raw.items()):
        c.text(f"raw_{k}", t, None)
        if k in ("empty_obj", "bom_prefix", "two_objects", "trailing_garbage", "json_list", "json_null", "id_1e400"):
            c.text(f"raw_{k}_brain", t, p.BRAIN)

    # duplicate keys
    hb = next(m for m in exs if m["type"] == "heartbeat" and sender_of(m) == p.BRAIN)
    hbt = json.dumps(hb)
    c.text("dup_key_v_bad_then_good", hbt.replace('{"v": 0,', '{"v": 1, "v": 0,', 1), p.BRAIN)
    c.text("dup_key_v_good_then_bad", hbt.replace('{"v": 0,', '{"v": 0, "v": 1,', 1), p.BRAIN)
    c.text("dup_key_type_hb_then_command", hbt.replace('"type": "heartbeat"', '"type": "command", "type": "heartbeat"', 1), p.BRAIN)
    c.text("dup_key_type_command_then_hb", hbt.replace('"type": "heartbeat"', '"type": "heartbeat", "type": "command"', 1), p.BRAIN)
    c.text("dup_key_id", hbt.replace('"id": 57', '"id": 0, "id": 57', 1), p.BRAIN)
    c.text("dup_key_payload", hbt.replace('"payload": {}', '"payload": 5, "payload": {}', 1), p.BRAIN)
    c.text("dup_key_ts_brain", hbt.replace('"ts_brain": 5', '"ts_brain": null, "ts_brain": 5', 1), p.BRAIN)

    # ---- depth ----
    for d in (5, 64, 200, 5000):
        c.text(f"deep_list_{d}", nested(d, "list"), None)
        c.text(f"deep_object_{d}", nested(d, "dict"), None)
        for where in (("payload", "extra_field", "id", "reply_to") if d < 5000 else ("payload", "extra_field")):
            m = copy.deepcopy(hb)
            val = json.loads(nested(min(d, 200), "list")) if d == 5000 else json.loads(nested(d, "list"))
            if where == "payload":
                m["payload"] = {"x": val}
            elif where == "extra_field":
                m["extra"] = val
            else:
                m[where] = val
            if d == 5000:
                text = json.dumps(m).replace('"x": ' + json.dumps(val), '"x": ' + nested(5000, "list"))
                if where == "extra_field":
                    text = json.dumps(m).replace('"extra": ' + json.dumps(val), '"extra": ' + nested(5000, "list"))
                if where in ("id", "reply_to"):
                    text = json.dumps(m).replace(f'"{where}": ' + json.dumps(val), f'"{where}": ' + nested(5000, "list"))
                c.text(f"deep_{where}_{d}", text, p.BRAIN)
            else:
                c.msg(f"deep_{where}_{d}", m, p.BRAIN)
    ack = next(m for m in exs if m["type"] == "ack")
    m = copy.deepcopy(ack)
    m["payload"]["result"] = json.loads(nested(60, "dict"))
    c.msg("ack_result_nested_60", m, p.BODY)
    m["payload"]["result"] = json.loads(nested(200, "dict"))
    c.msg("ack_result_nested_200", m, p.BODY)

    # ---- huge strings, at and over limits ----
    hello_body = next(m for m in exs if m["type"] == "hello" and sender_of(m) == p.BODY)
    hello_brain = next(m for m in exs if m["type"] == "hello" and sender_of(m) == p.BRAIN)
    for n in (127, 128, 129, 5000):
        m = copy.deepcopy(hello_body)
        m["payload"]["software"] = "x" * n
        c.msg(f"hello_body_software_{n}", m, p.BODY)
    for n in (63, 64, 65, 200):
        m = copy.deepcopy(hello_brain)
        m["payload"]["session_id"] = "s" * n
        c.msg(f"hello_brain_session_id_{n}", m, p.BRAIN)
    for n in (499, 500, 501):
        m = copy.deepcopy(next(x for x in exs if x["type"] == "error"))
        m["payload"]["message"] = "m" * n
        c.msg(f"error_message_{n}", m, p.BODY)
        c.msg(f"error_message_{n}_brain", m | {"ts_body": None, "ts_brain": 5}, p.BRAIN)
    speak = next(m for m in exs if m["type"] == "speak_local")
    for n in (1000, 1001, 4000):
        m = copy.deepcopy(speak)
        m["payload"]["text"] = "t" * n
        c.msg(f"speak_text_{n}", m, p.BRAIN)
    for n in (65536,):  # text frame size around the 64 KiB cap (below it, so decode+validate decide)
        m = copy.deepcopy(speak)
        m["payload"]["text"] = "t" * (n - 600)
        c.msg(f"speak_frame_about_{n}", m, p.BRAIN)

    # ---- unicode ----
    uni = {
        "lone_high": "\\ud800", "lone_low": "\\udc00", "lone_high_then_a": "\\ud800a", "reversed_pair": "\\udc00\\ud800",
        "valid_pair": "\\ud83d\\ude00", "nul": "\\u0000", "bell": "\\u0007", "esc": "\\u001b", "del": "\\u007f",
        "newline": "\\n", "cr": "\\r", "tab": "\\t", "c1": "\\u0085", "ls": "\\u2028", "ps": "\\u2029",
        "rlo": "\\u202e", "lri": "\\u2066", "zwsp": "\\u200b", "zwj": "\\u200d", "bom": "\\ufeff",
        "fullwidth": "\\uff21\\uff22\\uff23", "fullwidth_digits": "\\uff10", "combining": "e\\u0301",
        "nonchar": "\\uffff", "fffe": "\\ufffe", "astral": "\U0001f600", "turkish_i": "\\u0130", "nbsp": "\\u00a0",
        "cyrillic_a": "\\u0430", "long_mixed": "a\\u202eb\\u200bc",
    }
    for k in ("nonchar", "fffe", "cr", "tab", "ps", "lri", "zwj", "fullwidth_digits", "turkish_i", "cyrillic_a", "long_mixed"):
        uni.pop(k, None)
    for k, esc in sorted(uni.items()):
        t = json.dumps(hello_body).replace("embodied-body/0.1.0", f"b{esc}x")
        c.text(f"uni_software_{k}", t, p.BODY)
        t = json.dumps(speak).replace("Hello, I can hear you.", f"say{esc}")
        c.text(f"uni_speak_text_{k}", t, p.BRAIN)
        cmd = next(m for m in exs if m["type"] == "command")
        t = json.dumps(cmd).replace('"capture_image"', f'"get_status{esc}"')
        c.text(f"uni_command_name_{k}", t, p.BRAIN)
        t = json.dumps(hello_brain).replace("s-20261113-201501-3f9a", f"s-{esc}")
        c.text(f"uni_session_id_{k}", t, p.BRAIN)
        if k in ("lone_high", "nul", "fullwidth", "nbsp", "newline", "astral"):
            t = json.dumps(hello_brain).replace(hello_brain["payload"]["auth"]["nonce"], hello_brain["payload"]["auth"]["nonce"][:63] + esc)
            c.text(f"uni_nonce_{k}", t, p.BRAIN)
    for k, esc in (("lone_high", "\\ud800"), ("nul", "\\u0000"), ("rlo", "\\u202e")):
        t = json.dumps(hb).replace('"type": "heartbeat"', f'"type": "heart{esc}beat"')
        c.text(f"uni_type_{k}", t, p.BRAIN)
        t = json.dumps(hb).replace('"payload"', f'"pay{esc}load"')
        c.text(f"uni_envelope_key_{k}", t, p.BRAIN)
        t = json.dumps(hb).replace('"payload": {}', f'"payload": {{"{esc}": 1}}')
        c.text(f"uni_payload_key_{k}", t, p.BRAIN)
    c.text("uni_raw_bidi_type", '{"v":0,"type":"‮heartbeat","id":1}', p.BRAIN)
    c.text("uni_raw_fullwidth_v", '{"v":０,"type":"heartbeat"}', p.BRAIN)

    # ---- integer edge values on every numeric envelope/payload field ----
    edge = {
        "zero": 0, "one": 1, "two": 2, "neg1": -1, "u32max": 2**32 - 1, "u32over": 2**32, "i64over": 2**63, "p53m1": 2**53 - 1, "p53": 2**53, "float1": 1.0, "float_frac": 1.5, "float_neg0": -0.0, 
        "float_exp": 1e2, "true": True, "false": False, "null": None, "str1": "1", "list": [1],
    }
    for field, base in (("id", hb), ("reply_to", hb), ("ts_brain", hb), ("v", hb)):
        for k, val in sorted(edge.items()):
            m = copy.deepcopy(base)
            m[field] = val
            c.msg(f"int_hb_{field}_{k}", m, p.BRAIN)
            if k in ("float1", "float_exp", "float_neg0", "p53m1"):
              c.text(f"int_hb_{field}_{k}_exp", json.dumps(base).replace(f'"{field}": {json.dumps(base[field])}',
                                                                      f'"{field}": {json.dumps(val)}'), p.BRAIN)
    c.text("int_id_text_1.0", json.dumps(hb).replace('"id": 57', '"id": 1.0'), p.BRAIN)
    c.text("int_id_text_1e0", json.dumps(hb).replace('"id": 57', '"id": 1e0'), p.BRAIN)
    c.text("int_id_text_1E2", json.dumps(hb).replace('"id": 57', '"id": 1E2'), p.BRAIN)
    c.text("int_id_text_neg0", json.dumps(hb).replace('"id": 57', '"id": -0'), p.BRAIN)
    c.text("int_v_text_0.0", json.dumps(hb).replace('"v": 0', '"v": 0.0'), p.BRAIN)
    c.text("int_v_text_0e0", json.dumps(hb).replace('"v": 0', '"v": 0e0'), p.BRAIN)
    c.text("int_v_text_neg0", json.dumps(hb).replace('"v": 0', '"v": -0'), p.BRAIN)
    cmd_cap = next(m for m in exs if m["type"] == "command" and m["payload"]["name"] == "capture_image")
    for k, val in sorted(edge.items()):
        m = copy.deepcopy(cmd_cap)
        m["payload"]["args"]["max_edge"] = val
        c.msg(f"int_capture_max_edge_{k}", m, p.BRAIN)
    for val, k in ((63, "63"), (64, "64"), (4096, "4096"), (4097, "4097"), (640.0, "640f"), (640.5, "640_5")):
        m = copy.deepcopy(cmd_cap)
        m["payload"]["args"]["max_edge"] = val
        c.msg(f"capture_max_edge_{k}", m, p.BRAIN)
    pose_cmd = copy.deepcopy(cmd_cap)
    pose_cmd["payload"] = {"name": "set_pose_rate", "args": {"hz": 10}}
    for k, val in (("0", 0), ("0f", 0.0), ("30", 30), ("30_1", 30.1), ("neg", -1), ("nan_str", "NaN"), ("float", 10.5),
                   ("1e2", 100), ("true", True), ("null", None), ("big", 2**53)):
        m = copy.deepcopy(pose_cmd)
        m["payload"]["args"]["hz"] = val
        c.msg(f"set_pose_rate_hz_{k}", m, p.BRAIN)
    c.msg("set_pose_rate_ok", pose_cmd, p.BRAIN)
    for k, val in (("0", 0), ("big", 2**53), ("neg", -1), ("float", 1.0), ("true", True), ("str", "5"), ("u32", 2**32),
                   ("i32over", 2**31)):
        for field in ("seq", "crc32", "byte_length"):
            m = copy.deepcopy(next(x for x in exs if x["type"] == "echo" and sender_of(x) == p.BRAIN))
            m["payload"][field] = val
            c.msg(f"echo_{field}_{k}", m, p.BRAIN)
    for k, val in (("0", 0), ("8mib", 8388608), ("8mib1", 8388609), ("neg", -1), ("float", 1.0), ("null", None)):
        m = copy.deepcopy(next(x for x in exs if x["type"] == "audio_out"))
        m["payload"]["byte_length"] = val
        c.msg(f"audio_out_byte_length_{k}", m, p.BRAIN)

    # ---- wrong types for every envelope field and payload field of every example ----
    wrong_vals = {"null": None, "str": "x", "int": 7, "float": 1.5, "true": True, "list": [], "obj": {}}
    mutations: list[tuple[str, str, dict[str, Any]]] = []  # (brain-sent?, id, message) for sampling
    for i, m0 in enumerate(exs):
        s = sender_of(m0)
        for field in p.ENVELOPE_FIELDS:
            m = copy.deepcopy(m0)
            del m[field]
            mutations.append((s, f"mut{i:02d}_{m0['type']}_{s}_del_{field}", m))
            for vk, vv in sorted(wrong_vals.items()):
                m = copy.deepcopy(m0)
                m[field] = vv
                mutations.append((s, f"mut{i:02d}_{m0['type']}_{s}_{field}_{vk}", m))
        m = copy.deepcopy(m0)
        m["zz_unknown"] = 1
        mutations.append((s, f"mut{i:02d}_{m0['type']}_{s}_unknown_field", m))
        for key in sorted(m0["payload"]):
            m = copy.deepcopy(m0)
            del m["payload"][key]
            mutations.append((s, f"mut{i:02d}_{m0['type']}_{s}_payload_del_{key}", m))
            for vk, vv in sorted(wrong_vals.items()):
                m = copy.deepcopy(m0)
                m["payload"][key] = vv
                mutations.append((s, f"mut{i:02d}_{m0['type']}_{s}_payload_{key}_{vk}", m))
        m = copy.deepcopy(m0)
        m["payload"]["zz_unknown"] = 1
        mutations.append((s, f"mut{i:02d}_{m0['type']}_{s}_payload_unknown", m))
        m = copy.deepcopy(m0)
        m["ts_body"], m["ts_brain"] = m0["ts_brain"], m0["ts_body"]
        mutations.append((s, f"mut{i:02d}_{m0['type']}_{s}_ts_swapped", m))
        m = copy.deepcopy(m0)
        m["ts_body"] = m["ts_brain"] = 5
        mutations.append((s, f"mut{i:02d}_{m0['type']}_{s}_both_ts", m))
        m = copy.deepcopy(m0)
        m["ts_body"] = m["ts_brain"] = None
        mutations.append((s, f"mut{i:02d}_{m0['type']}_{s}_no_ts", m))
        for tname in p.MESSAGE_TYPES + ("Hello", "HELLO", "", "hello ", "hello\n", "__proto__"):
            m = copy.deepcopy(m0)
            m["type"] = tname
            mutations.append((s, f"mut{i:02d}_{m0['type']}_{s}_type_{tname.encode('unicode_escape').decode() or 'empty'}", m))
    # all brain-sent mutations that hit Kotlin directly are kept; body-sent ones are sampled (Kotlin never sees them)
    brain_m = [x for x in mutations if x[0] == p.BRAIN]
    body_m = [x for x in mutations if x[0] == p.BODY]
    rng.shuffle(brain_m)
    rng.shuffle(body_m)
    for s, cid, m in sorted(brain_m[:150] + body_m[:20], key=lambda x: x[1]):
        c.msg(cid, m, s)

    # commands: allowlist, args shapes
    for name in ("get_status", "capture_image", "start_audio_in", "stop_audio_in", "stop_audio_out", "set_pose_rate",
                 "delete_files", "run_command", "GET_STATUS", "get_status ", "get_status\n", " get_status", "",
                 "../get_status", "get_status\u0000", "__proto__", "x" * 5000):
        for args in ({}, {"max_edge": 640}, None, {"extra": 1}):
            m = copy.deepcopy(next(x for x in exs if x["type"] == "command"))
            m["payload"] = {"name": name, "args": args}
            key = name.encode("unicode_escape").decode()[:20] or "empty"
            a = "none" if args is None else json.dumps(args, sort_keys=True).replace(" ", "")
            c.msg(f"cmd_{key}_{len(name)}_args_{a}", m, p.BRAIN)
    m = copy.deepcopy(cmd_cap)
    del m["payload"]["args"]
    c.msg("cmd_capture_no_args", m, p.BRAIN)
    m["payload"] = {"args": {}}
    c.msg("cmd_no_name", m, p.BRAIN)
    m["payload"] = {"name": ["get_status"], "args": {}}
    c.msg("cmd_name_list", m, p.BRAIN)
    m["payload"] = {"name": {"x": 1}, "args": {}}
    c.msg("cmd_name_obj", m, p.BRAIN)
    m["payload"] = {"name": "get_status", "args": {}, "extra": 1}
    c.msg("cmd_extra_payload_field", m, p.BRAIN)
    m["payload"] = {"name": "get_status", "args": {}, "args2": {}}
    c.msg("cmd_extra_payload_field2", m, p.BRAIN)
    c.msg("cmd_from_body", next(x for x in exs if x["type"] == "command") , p.BODY)

    # hello specifics
    for k, versions in (("empty", []), ("only1", [1]), ("zero_and_one", [0, 1]), ("one_and_zero", [1, 0]),
                        ("str0", ["0"]), ("float0", [0.0]), ("true", [True]), ("null", None), ("str", "0"), ("nested", [[0]]),
                        ("dupes", [0, 0]), ("neg", [-1]), ("big", [2**53])):
        m = copy.deepcopy(hello_brain)
        m["payload"]["versions"] = versions
        c.msg(f"hello_brain_versions_{k}", m, p.BRAIN)
    for k, role in (("body", "body"), ("empty", ""), ("upper", "Brain"), ("none", None), ("list", ["brain"]), ("space", "brain ")):
        m = copy.deepcopy(hello_brain)
        m["payload"]["role"] = role
        c.msg(f"hello_brain_role_{k}", m, p.BRAIN)
        m = copy.deepcopy(hello_body)
        m["payload"]["role"] = role
        c.msg(f"hello_body_role_{k}", m, p.BODY)
    for k, auth in (("missing", "DEL"), ("null", None), ("str", "x"), ("list", []), ("empty", {}), ("extra", {"nonce": hello_brain["payload"]["auth"]["nonce"], "x": 1}),
                    ("nonce_int", {"nonce": 5}), ("nonce_none", {"nonce": None}), ("mac_not_nonce", {"mac": hello_brain["payload"]["auth"]["nonce"]})):
        m = copy.deepcopy(hello_brain)
        if auth == "DEL":
            del m["payload"]["auth"]
        else:
            m["payload"]["auth"] = auth
        c.msg(f"hello_brain_auth_{k}", m, p.BRAIN)
    nonce = hello_brain["payload"]["auth"]["nonce"]
    for k, nv in (("upper", nonce.upper()), ("short", nonce[:63]), ("long", nonce + "0"), ("nl", nonce + "\n"), ("nl_prefix", "\n" + nonce),
                  ("space", " " + nonce), ("nonhex", nonce[:63] + "g"), ("empty", ""), ("mixed_case", nonce[:32] + nonce[32:].upper()),
                  ("fullwidth", nonce[:63] + "０"), ("crlf", nonce + "\r\n"), ("tab", nonce + "\t")):
        m = copy.deepcopy(hello_brain)
        m["payload"]["auth"]["nonce"] = nv
        c.msg(f"hello_brain_nonce_{k}", m, p.BRAIN)
    auth_brain = next(m for m in exs if m["type"] == "auth" and sender_of(m) == p.BRAIN)
    mac = auth_brain["payload"]["mac"]
    for k, mv in (("upper", mac.upper()), ("short", mac[:63]), ("long", mac + "0"), ("nl", mac + "\n"), ("nonhex", mac[:63] + "z"),
                  ("empty", ""), ("int", 5), ("null", None), ("list", [mac]), ("space", mac + " "), ("fullwidth", mac[:63] + "０"),
                  ("nul", mac[:63] + "\u0000")):
        m = copy.deepcopy(auth_brain)
        m["payload"]["mac"] = mv
        c.msg(f"auth_brain_mac_{k}", m, p.BRAIN)
    for k, sid in (("slash", "a/b"), ("dotdot", ".."), ("dot_start", ".x"), ("dash_start", "-x"), ("backslash", "a\\b"),
                   ("space", "a b"), ("nl", "abc\n"), ("empty", ""), ("int", 5), ("null", None), ("colon", "a:b"),
                   ("long64", "a" * 64), ("long65", "a" * 65), ("unicode", "café"), ("dollar", "$x")):
        m = copy.deepcopy(hello_brain)
        m["payload"]["session_id"] = sid
        c.msg(f"hello_brain_session_id_{k}", m, p.BRAIN)
    # error / ack / heartbeat / echo specifics from the brain
    err = next(m for m in exs if m["type"] == "error")
    err_b = err | {"ts_body": None, "ts_brain": 5}
    for k, pl in (("unknown_code", {"code": "nope", "message": "x", "fatal": False}),
                  ("fatal_int", {"code": "busy", "message": "x", "fatal": 1}),
                  ("fatal_str", {"code": "busy", "message": "x", "fatal": "false"}),
                  ("no_fatal", {"code": "busy", "message": "x"}),
                  ("no_message", {"code": "busy", "fatal": False}),
                  ("no_code", {"message": "x", "fatal": False}),
                  ("code_int", {"code": 5, "message": "x", "fatal": False}),
                  ("message_nl", {"code": "busy", "message": "a\nb", "fatal": False}),
                  ("message_int", {"code": "busy", "message": 5, "fatal": False}),
                  ("ok", {"code": "busy", "message": "x", "fatal": False}),
                  ("extra", {"code": "busy", "message": "x", "fatal": False, "y": 1}),
                  ("payload_list", []), ("payload_null", None), ("payload_str", "x")):
        c.msg(f"error_brain_{k}", err_b | {"payload": pl}, p.BRAIN)
        c.msg(f"error_brain_{k}_no_reply", err_b | {"payload": pl, "reply_to": None}, p.BRAIN)
    for k, rt in (("none", None), ("zero", 0), ("u32", 2**32 - 1), ("over", 2**32), ("float", 1.0), ("str", "1"), ("neg", -1)):
        m = copy.deepcopy(ack)
        m["ts_body"], m["ts_brain"] = None, 5
        m["reply_to"] = rt
        c.msg(f"ack_brain_reply_to_{k}", m, p.BRAIN)
    for k, pl in (("empty", {}), ("status_bad", {"status": "maybe"}), ("status_err_no_error", {"status": "error"}),
                  ("status_ok_extra", {"status": "ok", "zz": 1}), ("status_none", {"status": None}), ("list", []), ("ok_result_list", {"status": "ok", "result": []})):
        m = copy.deepcopy(ack)
        m["ts_body"], m["ts_brain"] = None, 5
        m["payload"] = pl
        c.msg(f"ack_brain_payload_{k}", m, p.BRAIN)
    for k, pl in (("empty", {}), ("extra", {"x": 1}), ("list", []), ("null", None), ("str", "x")):
        c.msg(f"heartbeat_brain_payload_{k}", hb | {"payload": pl}, p.BRAIN)
        c.msg(f"heartbeat_body_payload_{k}", hb | {"payload": pl, "ts_body": 5, "ts_brain": None}, p.BODY)
    # types that only the body may send, delivered "from brain" (wrong direction), and vice versa
    for i, m0 in enumerate(exs):
        s = sender_of(m0)
        m = copy.deepcopy(m0)
        m["ts_body"], m["ts_brain"] = (None, 5) if s == p.BODY else (5, None)
        c.msg(f"swapclock{i:02d}_{m0['type']}_{s}_claims_{other(s)}", m, other(s))

    # ---- lenient-JSON probes: a valid brain ack with one non-JSON deviation (a strict parser must refuse them) ----
    ack_b = copy.deepcopy(ack)
    ack_b["ts_body"], ack_b["ts_brain"] = None, 5
    base = json.dumps(ack_b)
    lenient = {
        "trailing_comma": base[:-1] + ",}", "single_quotes": base.replace('"', "'"), "line_comment": base + "//x",
        "block_comment": "/*x*/" + base, "trailing_garbage": base + " x", "trailing_object": base + base,
        "unquoted_keys": base.replace('"v"', "v", 1), "semicolon_sep": base.replace('"v": 0, ', '"v": 0; ', 1),
        "equals_sep": base.replace('"v": 0', '"v"=0', 1), "arrow_sep": base.replace('"v": 0', '"v"=>0', 1),
        "nan_in_result": base.replace('"charging": false', '"charging": NaN'), "inf_in_result": base.replace('"charging": false', '"charging": Infinity'),
        "overflow_in_result": base.replace('"battery_pct": 81', '"battery_pct": 1e400'),
        "hex_in_result": base.replace('"battery_pct": 81', '"battery_pct": 0x51'),
        "leading_zero_in_result": base.replace('"battery_pct": 81', '"battery_pct": 081'),
        "plus_in_result": base.replace('"battery_pct": 81', '"battery_pct": +81'),
        "raw_newline_in_string": base.replace('"ok"', '"o' + chr(10) + 'k"', 1),
        "raw_tab_in_string": base.replace('"ok"', '"o' + chr(9) + 'k"', 1),
        "bom_prefix": chr(0xFEFF) + base, "leading_whitespace": " " + chr(10) + chr(9) + base + chr(10) + " ",
        "unterminated_string": base[:-3] + '"', "empty_key": base.replace('"payload"', '""', 1),
        "null_byte_suffix": base + chr(0), "single_quoted_value": base.replace('"ok"', "'ok'", 1),
    }
    for k, text in sorted(lenient.items()):
        c.text(f"lenient_{k}", text, p.BRAIN)

    # ---- binary frames ----
    H = p.BINARY_HEADER
    good = lambda v=0, t=4, f=0, i=7, ts=1234567890123, n=None, d=b"hello": H.pack(v, t, f, i, ts, len(d) if n is None else n) + d  # noqa: E731
    c.binary("bin_empty", b"")
    for n in (1, 5, 19):
        c.binary(f"bin_truncated_header_{n}", good()[:n])
    c.binary("bin_header_only_len0", good(d=b""))
    c.binary("bin_header_only_len1", good(d=b"", n=1))
    c.binary("bin_header_only_len_u32max", good(d=b"", n=2**32 - 1))
    c.binary("bin_header_only_len_8mib", good(d=b"", n=8 * 1024 * 1024))
    c.binary("bin_header_only_len_8mib1", good(d=b"", n=8 * 1024 * 1024 + 1))
    c.binary("bin_header_only_len_2p31", good(d=b"", n=2**31))
    c.binary("bin_header_only_len_2p31m1", good(d=b"", n=2**31 - 1))
    c.binary("bin_len_too_small", good(n=4))
    c.binary("bin_len_too_big", good(n=6))
    c.binary("bin_trailing_byte", good() + b"x")
    c.binary("bin_short_by_one", good()[:-1])
    for tname, code in sorted(p.BINARY_TYPE_CODES.items()):
        c.binary(f"bin_valid_{tname}", good(t=code))
        c.binary(f"bin_valid_{tname}_empty", good(t=code, d=b""))
        c.binary(f"bin_valid_{tname}_max_id_ts", good(t=code, i=2**32 - 1, ts=p.MAX_TS))
        c.binary(f"bin_valid_{tname}_1k", good(t=code, d=bytes(range(256)) * 4))
    for v in (1, 2, 127, 128, 255):
        c.binary(f"bin_version_{v}", good(v=v))
    for t in (0, 5, 6, 127, 128, 255):
        c.binary(f"bin_type_{t}", good(t=t))
    for f in (1, 2, 0x8000, 0xFF, 0xFFFF):
        c.binary(f"bin_flags_{f:#06x}", good(f=f))
    for i in (0, 1, 2**32 - 1):
        c.binary(f"bin_id_{i}", good(i=i))
    c.binary("bin_id_2p32_via_wrap", struct.pack(">BBHIqI", 0, 4, 0, 0, 5, 0) + b"")  # id wraps to 0 in uint32 (same as id_0)
    for ts in (0, 1, p.MAX_TS - 1, p.MAX_TS, p.MAX_TS + 1, 2**53 + 1, 2**62, 2**63 - 1, -1, -2**53, -2**63):
        tn = f"neg{abs(ts)}" if ts < 0 else str(ts)
        c.binary(f"bin_ts_{tn}", good(ts=ts))
    c.binary("bin_all_ff_header", b"\xff" * 20)
    c.binary("bin_all_00_header", b"\x00" * 20)
    c.binary("bin_all_ff_long", b"\xff" * 64)
    c.binary("bin_text_as_binary", b'{"v":0,"type":"heartbeat"}')
    c.binary("bin_big_valid_10k", good(d=bytes(10000)))
    c.binary("bin_version_and_type_bad", good(v=9, t=9))
    c.binary("bin_version_and_ts_bad", good(v=9, ts=-1))
    c.binary("bin_type_and_ts_bad", good(t=9, ts=-1))
    c.binary("bin_id_and_ts_bad", good(i=0, ts=-1))
    c.binary("bin_ts_and_len_bad", good(ts=2**60, n=9))
    c.binary("bin_id_and_len_bad", good(i=0, n=2**32 - 1))
    for _ in range(10):  # fixed-seed random headers
        hdr = bytes(rng.randrange(256) for _ in range(20))
        body = bytes(rng.randrange(256) for _ in range(rng.choice((0, 0, 3, 40))))
        c.binary(f"bin_random_{hdr.hex()[:16]}_{len(body)}", hdr + body)

    cases = [c.cases[k] for k in sorted(c.cases)]

    # ---- link-auth input-shape gates (LinkAuth.parseToken / isHex256 / isIdent / check) ----
    sch = p.schema()
    hex_pat = re.compile(sch["$defs"]["hex256"]["pattern"])
    ident_pat = re.compile(sch["$defs"]["ident"]["pattern"])
    auth_cases: list[dict[str, Any]] = []

    def add_auth(cid: str, kind: str, value: str, ok: bool) -> None:
        auth_cases.append({"id": cid, "kind": kind, "input": value, "python_ok": ok})

    def py_token(s: str) -> bool:
        try:
            pauth.parse_token(s)
            return True
        except ValueError:
            return False

    good_tok = TOKEN
    strings = {
        "good": good_tok, "upper": good_tok.upper(), "mixed": good_tok[:32] + good_tok[32:].upper(), "empty": "",
        "short63": good_tok[:63], "long65": good_tok + "0", "pad_space": " " + good_tok + " ", "pad_nl": good_tok + "\n",
        "pad_crlf": "\r\n" + good_tok + "\r\n", "pad_tab": "\t" + good_tok, "pad_nbsp": " " + good_tok,
        "pad_ideographic_space": "　" + good_tok, "pad_nul": good_tok + "\u0000", "pad_fs": "\x1c" + good_tok,
        "pad_zwsp": "​" + good_tok, "pad_nel": "\u0085" + good_tok, "pad_ls": good_tok + " ",
        "inner_space": good_tok[:32] + " " + good_tok[32:], "nonhex_g": good_tok[:63] + "g", "fullwidth_digit": good_tok[:63] + "０",
        "fullwidth_letter": good_tok[:63] + "ａ", "arabic_digit": good_tok[:63] + "٠", "turkish_I": "İ" * 64,
        "kelvin_k": "K" + good_tok[1:], "long_i_dot": "İ" + good_tok[1:], "lone_surrogate": good_tok[:63] + "\ud800",
        "astral": good_tok[:62] + "\U0001f600", "all_f": "f" * 64, "all_0": "0" * 64, "0x_prefix": "0x" + good_tok[:62],
        "dash": good_tok[:63] + "-", "huge": "a" * 20000, "emoji_pairs": "\U0001f600" * 32, "bidi": "‮" + good_tok,
        "combining": good_tok[:63] + "́", "sharp_s_upper": "ẞ" * 64, "dotless_i": "ı" * 64,
    }
    for k, s in sorted(strings.items()):
        add_auth(f"token_{k}", "token", s, py_token(s))
        add_auth(f"nonce_{k}", "hex256", s, bool(hex_pat.search(s)))
        add_auth(f"ident_{k}", "ident", s, bool(ident_pat.search(s)))
    for k, s in sorted({"a": "a", "dot": ".", "dotdot": "..", "dash": "-", "under": "_", "a_dot": "a.b", "dot_start": ".a",
                        "dash_start": "-a", "dash_start2": "--", "slash": "a/b", "len64": "a" * 64, "len65": "a" * 65,
                        "nl": "a\n", "empty": "", "sp": "a b", "unicode": "café", "fullwidth": "Ａ", "date": "s-20261113-201501-3f9a",
                        "nul": "a\u0000", "colon": "a:b", "bs": "a\\b", "tilde": "~", "star": "*"}.items()):
        add_auth(f"ident_x_{k}", "ident", s, bool(ident_pat.search(s)))
    # mac check: the real proof, then perturbations (compared with pauth.check)
    tok = bytes.fromhex(good_tok)
    transcript = [pauth.digest("hello-text-1"), pauth.digest("hello-text-2")]
    real = pauth.proof(tok, "brain", transcript)
    for k, s in sorted({"real": real, "upper": real.upper(), "short": real[:63], "long": real + "0", "empty": "", "nl": real + "\n",
                        "fullwidth": real[:63] + "０", "surrogate": real[:63] + "\ud800", "astral": real[:62] + "\U0001f600",
                        "nul": real[:63] + "\u0000", "other_role": pauth.proof(tok, "body", transcript), "zeros": "0" * 64,
                        "non_ascii_same_len": real[:63] + "é", "huge": "f" * 20000}.items()):
        add_auth(f"mac_{k}", "mac_check", s, pauth.check(tok, "brain", transcript, s))
    auth_cases.sort(key=lambda x: x["id"])

    return {
        "version": VERSION, "generator": GENERATOR,
        "description": "Hostile protocol v0 frames with the Python validator's verdict; Kotlin must agree (see body-android HostileCorpusTest).",
        "auth_context": {"token": good_tok, "role": "brain",
                         "transcript_texts": ["hello-text-1", "hello-text-2"]},
        "cases": cases, "auth_cases": auth_cases,
    }


def render() -> str:
    return json.dumps(build(), ensure_ascii=True, indent=0, sort_keys=False, separators=(",", ":")).replace('},{"id"', '},\n{"id"') + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if the committed corpus differs from a fresh generation")
    args = ap.parse_args()
    text = render()
    if args.check:
        have = OUT.read_bytes().decode("ascii") if OUT.exists() else None
        if have != text:
            print(f"{OUT} is out of date; rerun tools/gen_hostile_corpus.py", file=sys.stderr)
            return 1
        print(f"{OUT.name}: up to date")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="ascii", newline="\n") as f:
        f.write(text)
    d = json.loads(text)
    from collections import Counter
    print(f"wrote {OUT} ({len(text)} bytes): {len(d['cases'])} cases, {len(d['auth_cases'])} auth cases")
    print(Counter((x["kind"], x["verdict"], x["code"]) for x in d["cases"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
