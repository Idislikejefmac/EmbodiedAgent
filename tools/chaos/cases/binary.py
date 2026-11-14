"""P0-05 binary frames: `unpack_binary` and `check_pair` on hostile bytes (B2).

Steady state: every byte string is either a BinaryFrame or a ProtocolError with a v0 error code and a
bounded message; `check_pair` accepts exactly the frame that matches its message. The 20-byte header
is big-endian: v uint8, type uint8, flags uint16, id uint32, ts int64, length uint32.
"""

from __future__ import annotations

import copy
import re
import struct
from typing import Any

from brain.limits import MAX_BINARY_BYTES, MAX_ID, MAX_TS
from brain.transport import protocol as p

from .. import fuzz
from ..api import Case, Ctx, Gen, Violation, experiment, failure, hostile, invalid, not_applicable, valid
from .protocol import examples, sender_of

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
HDR = p.BINARY_HEADER


def header(v: int = 0, code: int = 1, flags: int = 0, msg_id: int = 1, ts: int = 5, length: int = 0) -> bytes:
    return HDR.pack(v, code, flags, msg_id, ts, length)


def frame(data: bytes, **kw: Any) -> bytes:
    kw.setdefault("length", len(data))
    return header(**kw) + data


def gen_valid(g: Gen) -> list[Case]:
    out = []
    for name, code in p.BINARY_TYPE_CODES.items():
        for n, size in (("empty", 0), ("one_byte", 1), ("small", 100)):
            out.append(valid(f"{name}_{n}", {"op": "unpack", "bytes": frame(b"x" * size, code=code), "expect": "ok"}))
    out += [valid("id_min", {"op": "unpack", "bytes": frame(b"", msg_id=1), "expect": "ok"}),
            valid("id_max", {"op": "unpack", "bytes": frame(b"", msg_id=MAX_ID), "expect": "ok"}),
            valid("ts_zero", {"op": "unpack", "bytes": frame(b"", ts=0), "expect": "ok"}),
            valid("ts_max", {"op": "unpack", "bytes": frame(b"", ts=MAX_TS), "expect": "ok"}),
            valid("flags_set_is_ignored_today", {"op": "unpack", "bytes": frame(b"ab", flags=0xFFFF), "expect": "ok"}),
            valid("data_at_max", {"op": "unpack", "bytes": frame(b"\x00" * MAX_BINARY_BYTES), "expect": "ok"})]
    for mtype in ("image", "audio_in", "audio_out", "echo"):
        msg = next((m for m in examples() if m["type"] == mtype), None)
        if msg is not None:
            out.append(valid(f"pair_{mtype}", {"op": "pair", "msg": msg, "mutate": None, "expect": "ok"}))
    return out


def gen_invalid(g: Gen) -> list[Case]:
    ok = frame(b"abcd", length=4)
    out = [
        invalid("empty", {"op": "unpack", "bytes": b"", "expect": "bad_binary"}, "N3"),
        invalid("one_byte", {"op": "unpack", "bytes": b"\x00", "expect": "bad_binary"}, "N3"),
        invalid("header_minus_one", {"op": "unpack", "bytes": ok[:19], "expect": "bad_binary"}, "N2"),
        invalid("version_1", {"op": "unpack", "bytes": frame(b"", v=1), "expect": "unsupported_version"}, "N6"),
        invalid("version_255", {"op": "unpack", "bytes": frame(b"", v=255), "expect": "unsupported_version"}, "N6"),
        invalid("type_zero", {"op": "unpack", "bytes": frame(b"", code=0), "expect": "bad_binary"}, "N6"),
        invalid("type_five", {"op": "unpack", "bytes": frame(b"", code=5), "expect": "bad_binary"}, "N6"),
        invalid("type_255", {"op": "unpack", "bytes": frame(b"", code=255), "expect": "bad_binary"}, "N6"),
        invalid("id_zero", {"op": "unpack", "bytes": frame(b"", msg_id=0), "expect": "bad_binary"}, "N6"),
        invalid("ts_negative", {"op": "unpack", "bytes": frame(b"", ts=-1), "expect": "bad_binary"}, "N6"),
        invalid("ts_one_over_max", {"op": "unpack", "bytes": frame(b"", ts=MAX_TS + 1), "expect": "bad_binary"}, "N6"),
        invalid("ts_int64_max", {"op": "unpack", "bytes": frame(b"", ts=2**63 - 1), "expect": "bad_binary"}, "N6"),
        invalid("length_over_data", {"op": "unpack", "bytes": header(length=5) + b"abc", "expect": "bad_binary"}, "N2"),
        invalid("length_under_data", {"op": "unpack", "bytes": header(length=1) + b"abc", "expect": "bad_binary"}, "N2"),
        invalid("length_one_over_max", {"op": "unpack", "bytes": header(length=MAX_BINARY_BYTES + 1),
                                        "expect": "too_large"}, "N2"),
        invalid("length_uint32_max", {"op": "unpack", "bytes": header(length=2**32 - 1) + b"x", "expect": "too_large"}, "N2"),
        invalid("data_one_over_max", {"op": "unpack", "bytes": header(length=MAX_BINARY_BYTES + 1)
                                      + b"\x00" * (MAX_BINARY_BYTES + 1), "expect": "too_large"}, "N2"),
    ]
    # Wrong Python types at the function's own entry (a caller bug, but the gate must not crash on it).
    for name, value in (("none", None), ("str", "x" * 30), ("int", 7), ("list", [1, 2]), ("dict", {})):
        out.append(invalid(f"wrong_type_{name}", {"op": "unpack_any", "value": value}, "N1"))
    out.append(invalid("bytearray_ok_or_refused", {"op": "unpack_any", "value": bytearray(frame(b"ab"))}, "N1"))
    # check_pair: a binary frame that does not match its message.
    msg = next(m for m in examples() if m["type"] == "echo")
    for name in ("type", "id", "ts", "length"):
        out.append(invalid(f"pair_mismatch_{name}", {"op": "pair", "msg": msg, "mutate": name, "expect": "bad_binary"}, "N6"))
    return out


def gen_hostile(g: Gen) -> list[Case]:
    out = []
    for i in range(g.n(30, 600)):  # random bytes, and random mutations of a valid frame
        n = g.rng.choice([0, 1, 5, 19, 20, 21, 64, 1000])
        out.append(hostile(f"random_bytes_{i}", {"op": "unpack_any", "value": g.rng.randbytes(n)}, "N7"))
    base = bytearray(frame(b"abcd", code=4, msg_id=9, ts=77))
    for i in range(g.n(30, 600)):
        mutated = bytearray(base)
        for _ in range(g.rng.randint(1, 3)):
            mutated[g.rng.randrange(len(mutated))] = g.rng.randrange(256)
        out.append(hostile(f"mutated_frame_{i}", {"op": "unpack_any", "value": bytes(mutated)}, "N7"))
    out.append(hostile("text_as_binary", {"op": "unpack_any", "value": '{"v":0,"type":"hello"}'.encode()}, "N8"))
    out.append(hostile("replayed_frame_twice", {"op": "twice", "bytes": frame(b"ab", code=4, msg_id=3)}, "N9"))
    out.append(hostile("header_claims_8mib_sends_20", {"op": "unpack", "bytes": header(length=MAX_BINARY_BYTES),
                                                       "expect": "bad_binary"}, "N7"))
    return out


def gen_failure(g: Gen) -> list[Case]:
    return [failure("memoryview_input", {"op": "unpack_any", "value": "MEMORYVIEW"}, "N14"),
            failure("huge_garbage_8mib_zero", {"op": "unpack_any", "value": b"\x00" * (MAX_BINARY_BYTES + 100)}, "N11")]


def _pair(ctx: Ctx, d: dict[str, Any]) -> str:
    msg = copy.deepcopy(d["msg"])
    sender = sender_of(msg)
    if sender is None:
        raise Violation("no_crash", "an example message has no single sender")
    length = msg["payload"]["byte_length"]
    data = b"x" * length
    ts = msg[p.ts_field(sender)]
    mtype, msg_id = msg["type"], msg["id"]
    mutate = d["mutate"]
    if mutate == "type":
        mtype = "image" if mtype != "image" else "echo"
    elif mutate == "id":
        msg_id = msg_id % MAX_ID + 1
    elif mutate == "ts":
        ts += 1
    elif mutate == "length":
        data = data + b"y"
    try:
        p.check_pair(msg, p.BinaryFrame(mtype, msg_id, ts, data), sender=sender)
    except p.ProtocolError as e:
        ctx.expect(e.code == "bad_binary" and e.fatal, "validate_before_use", f"mismatch gave {e.code}, fatal={e.fatal}")
        ctx.expect(len(e.message) <= 500 and not _CONTROL.search(e.message), "validate_before_use", "message not bounded")
        return e.code
    return "ok"


@experiment(
    id="P0-05.binary.frames", card="P0-05",
    entries=("binary_type.*", "gate.brain.transport.protocol.unpack_binary", "gate.brain.transport.protocol.check_pair"),
    hypothesis="Every byte string is either parsed into a BinaryFrame or refused with a ProtocolError whose code is "
               "bad_binary, unsupported_version or too_large and whose message is bounded; check_pair accepts "
               "exactly the frame that matches its message; nothing else is ever raised.",
    blast_radius="pure", valid=gen_valid, invalid=gen_invalid, hostile=gen_hostile, failure=gen_failure)
def check(case: Case, ctx: Ctx) -> Any:
    d = case.data
    op = d["op"]
    if op == "pair":
        outcome = _pair(ctx, d)
        ctx.expect(outcome == d["expect"], "validate_before_use", f"pair gave {outcome}, expected {d['expect']}")
        return outcome
    if op == "twice":  # a replayed frame is parsed again, identically (no hidden state in the parser)
        a, b = p.unpack_binary(d["bytes"]), p.unpack_binary(d["bytes"])
        ctx.expect(a == b, "validate_before_use", "the same bytes parsed differently twice")
        return "same"
    value = d["bytes"] if op == "unpack" else d["value"]
    if value == "MEMORYVIEW":  # built here: a memoryview's repr holds an address, which would change the case digest
        value = memoryview(frame(b"ab"))
    try:
        f = p.unpack_binary(value)
        outcome = "ok"
        ctx.expect(isinstance(f, p.BinaryFrame) and len(f.data) <= MAX_BINARY_BYTES, "bounded_resources", "bad frame")
        ctx.expect(f.type in p.BINARY_TYPE_CODES and 1 <= f.id <= MAX_ID and 0 <= f.ts <= MAX_TS, "validate_before_use",
                   "a parsed frame holds a value outside the protocol's range")
    except p.ProtocolError as e:
        ctx.expect(e.code in ("bad_binary", "unsupported_version", "too_large"), "validate_before_use", f"code {e.code}")
        ctx.expect(len(e.message) <= 500 and not _CONTROL.search(e.message), "validate_before_use", "message not bounded")
        outcome = e.code
    except (TypeError, struct.error) as e:
        # The gate's contract is bytes. A non-bytes value is a caller bug, but it must still be a plain refusal.
        outcome = f"wrong_type:{type(e).__name__}"
        ctx.expect(op == "unpack_any" and not isinstance(value, (bytes, bytearray, memoryview)), "no_crash",
                   f"unpack_binary raised {type(e).__name__} on bytes-like input")
    if "expect" in d:
        ctx.expect(outcome == d["expect"], "validate_before_use", f"outcome {outcome!r}, expected {d['expect']!r}")
    return outcome
