"""Protocol v0: constants, envelope validation and binary frames.

The written spec is protocol/v0.md and the JSON Schema is protocol/v0.schema.json.
This module is body-agnostic transport: it must not know which body is connected.
"""

from __future__ import annotations

import json
import math
import re
import struct
import time
import zlib
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, NoReturn

from jsonschema import Draft202012Validator, validators

from ..config import REPO_ROOT
from ..limits import (HEARTBEAT_MS_MAX, HEARTBEAT_MS_MIN, LINK_TIMEOUT_S_MAX, LIVENESS_TIMEOUT_MS_MAX,
                      LIVENESS_TIMEOUT_MS_MIN)

PROTOCOL_VERSION = 0
SCHEMA_PATH = REPO_ROOT / "protocol" / "v0.schema.json"

BODY = "body"
BRAIN = "brain"

MESSAGE_TYPES = (
    "hello", "auth", "heartbeat", "pose", "tracking_state", "image", "audio_in",
    "audio_out", "speak_local", "event", "command", "ack", "error", "echo",
)

# Who may send each type (protocol/v0.md section 4).
SENDERS: dict[str, frozenset[str]] = {
    "hello": frozenset({BODY, BRAIN}),
    "auth": frozenset({BODY, BRAIN}),
    "heartbeat": frozenset({BODY, BRAIN}),
    "pose": frozenset({BODY}),
    "tracking_state": frozenset({BODY}),
    "image": frozenset({BODY}),
    "audio_in": frozenset({BODY}),
    "audio_out": frozenset({BRAIN}),
    "speak_local": frozenset({BRAIN}),
    "event": frozenset({BODY}),
    "command": frozenset({BRAIN}),
    "ack": frozenset({BODY, BRAIN}),
    "error": frozenset({BODY, BRAIN}),
    "echo": frozenset({BODY, BRAIN}),
}

# The command allowlist (protocol/v0.md section 8). Default deny.
COMMANDS = (
    "get_status", "capture_image", "start_audio_in", "stop_audio_in",
    "stop_audio_out", "set_pose_rate",
)

ERROR_CODES = (
    "bad_json", "bad_envelope", "bad_payload", "unsupported_version", "unknown_type",
    "wrong_direction", "bad_sequence", "bad_binary", "too_large", "unknown_command",
    "bad_args", "not_implemented", "busy", "internal", "unauthorized",
)

# Capability names known in v0 (section 4.1). hello may list others; receivers ignore unknown names.
CAPABILITIES = ("pose", "tracking_state", "image", "audio_in", "audio_out", "speak_local", "event", "echo")

ENVELOPE_FIELDS = ("v", "type", "id", "reply_to", "ts_body", "ts_brain", "payload")

# Binary frames (protocol/v0.md section 5): big-endian, 20-byte header.
# v uint8 | type uint8 | flags uint16 | id uint32 | ts int64 (us) | length uint32
BINARY_HEADER = struct.Struct(">BBHIqI")
BINARY_TYPE_CODES = {"image": 1, "audio_in": 2, "audio_out": 3, "echo": 4}
BINARY_TYPE_NAMES = {code: name for name, code in BINARY_TYPE_CODES.items()}

MAX_TEXT_BYTES = 65_536
MAX_BINARY_BYTES = 8 * 1024 * 1024
MAX_ID = 2**32 - 1
MAX_TS = 2**53 - 1

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

HEARTBEAT_MS = 2000
LIVENESS_TIMEOUT_MS = 6000

def check_seconds(name: str, value: object, low: float, high: float) -> float:
    """value as a float if it is a real, finite number of seconds from low to high (a bool is not one).
    TypeError for the wrong type, ValueError for the wrong range; the message names the argument (H-12)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number of seconds, not {type(value).__name__}")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be from {low:g} to {high:g} seconds, not {str(value)[:30]}")
    return float(value)


def check_link_timing(heartbeat_s: object, liveness_s: object) -> tuple[float, float]:
    """The heartbeat interval and liveness timeout a Link or BrainServer may be built with: the ranges of the
    hello settings in the schema, and liveness strictly above the heartbeat so that a healthy peer is never
    dropped (H-12). Without these a heartbeat of 0 is a busy loop and a liveness below it drops every peer."""
    hb = check_seconds("heartbeat_s", heartbeat_s, HEARTBEAT_MS_MIN / 1000, HEARTBEAT_MS_MAX / 1000)
    lv = check_seconds("liveness_s", liveness_s, LIVENESS_TIMEOUT_MS_MIN / 1000, LIVENESS_TIMEOUT_MS_MAX / 1000)
    if lv <= hb:
        raise ValueError(f"liveness_s ({lv:g}) must be greater than heartbeat_s ({hb:g})")
    return hb, lv


def check_timeout(name: str, value: object) -> float:
    """A handshake, binary or send timeout: above 0 and at most LINK_TIMEOUT_S_MAX seconds."""
    return check_seconds(name, value, 1e-3, LINK_TIMEOUT_S_MAX)


# WebSocket close codes (section 1).
CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_AUTH_FAILED = 1008
CLOSE_REPLACED = 4001


def now_us() -> int:
    """This process's monotonic clock in microseconds (QueryPerformanceCounter on Windows, Python 3.13+)."""
    return time.monotonic_ns() // 1000


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def ts_field(role: str) -> str:
    return "ts_body" if role == BODY else "ts_brain"


def other_role(role: str) -> str:
    return BRAIN if role == BODY else BODY


def valid_id(x: object) -> int | None:
    """x if it is a usable message id (an int from 1 to 2^32-1), else None."""
    return x if isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= MAX_ID else None


class UnknownErrorCode(ValueError):
    """A ProtocolError was built with a code that is not in ERROR_CODES (a bug in the caller, never peer input)."""


class ProtocolError(Exception):
    """A protocol violation to be reported to the peer as an `error` message.

    `silent` marks an error about a received `error` message: it is logged but never answered (section 9).
    """

    def __init__(self, code: str, message: str, *, fatal: bool = False, reply_to: int | None = None,
                 silent: bool = False):
        if code not in ERROR_CODES:
            raise UnknownErrorCode(f"{str(code)[:64]!r} is not a protocol error code")
        message = _CONTROL.sub("?", message)[:500]  # error.message may not carry control characters
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.fatal = fatal
        self.reply_to = valid_id(reply_to)
        self.silent = silent


@lru_cache(maxsize=1)
def schema() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return loaded


def _is_strict_integer(checker: object, instance: object) -> bool:
    """JSON Schema counts 1.0 as an integer; v0 does not: ids, timestamps and counts must be written as
    integers, so `1.0` is refused instead of being echoed back as `reply_to: 1.0`."""
    return isinstance(instance, int) and not isinstance(instance, bool)


_StrictValidator = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", _is_strict_integer))


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    s = schema()
    Draft202012Validator.check_schema(s)
    return _StrictValidator(s)


def schema_errors(msg: object) -> list[str]:
    """All schema violations for one message, as readable strings (empty if valid)."""
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in _validator().iter_errors(msg)
    ]


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not a finite number")


def _finite_float(text: str) -> float:
    """parse_float hook: a literal such as 1e400 overflows to infinity, which B1 forbids like NaN (X01-F01)."""
    value = float(text)
    if value in (float("inf"), float("-inf")):
        raise ValueError("a number is too large to be finite")
    return value


def decode(text: str) -> object:
    """json.loads that rejects NaN and Infinity and survives deep nesting. Raises ProtocolError(bad_json)."""
    try:
        return json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, RecursionError) as e:
        raise ProtocolError("bad_json", f"not JSON: {type(e).__name__}: {str(e)[:200]}") from None


def encode(msg: dict[str, Any]) -> str:
    return json.dumps(msg, separators=(",", ":"), allow_nan=False)


def validate(msg: object, *, sender: str | None = None) -> dict[str, Any]:
    """Check one decoded JSON message against v0. Returns it, or raises ProtocolError.

    `sender` is the role the message came from; when given, direction, the sender's own
    timestamp (section 6) and hello.role are checked too.
    """
    if not isinstance(msg, dict):
        raise ProtocolError("bad_json", "message is not a JSON object")
    msg_id = valid_id(msg.get("id"))
    silent = msg.get("type") == "error"  # never answer an error with an error
    if "v" not in msg:
        raise ProtocolError("bad_envelope", "missing field 'v'", reply_to=msg_id, silent=silent)
    if msg["v"] != PROTOCOL_VERSION or not isinstance(msg["v"], int) or isinstance(msg["v"], bool):
        raise ProtocolError("unsupported_version", f"v={str(msg.get('v'))[:32]!r}; this side speaks v0",
                            fatal=True, reply_to=msg_id, silent=silent)
    mtype = msg.get("type")
    if mtype not in MESSAGE_TYPES:
        raise ProtocolError("unknown_type", f"unknown type {str(mtype)[:64]!r}", reply_to=msg_id)
    if mtype == "command":
        payload = msg.get("payload")
        name = payload.get("name") if isinstance(payload, dict) else None
        if name not in COMMANDS:
            raise ProtocolError("unknown_command",
                                f"command {str(name)[:64]!r} is not in the v0 allowlist", reply_to=msg_id)
    try:
        errors = list(_validator().iter_errors(msg))
    except RecursionError:
        raise ProtocolError("bad_payload", "nested too deeply", reply_to=msg_id, silent=silent) from None
    if errors:
        first = errors[0]
        path = [str(p) for p in first.absolute_path]
        where = "/".join(path) or "<root>"
        if mtype == "command" and path[:2] == ["payload", "args"]:
            code = "bad_args"
        elif path[:1] == ["payload"]:
            code = "bad_payload"
        else:
            code = "bad_envelope"
        raise ProtocolError(code, f"{where}: {first.message[:300]}", reply_to=msg_id, silent=silent)
    if sender is not None:
        if sender not in SENDERS[mtype]:
            raise ProtocolError("wrong_direction", f"{mtype!r} may not be sent by the {sender}", reply_to=msg_id,
                                silent=silent)
        if msg[ts_field(sender)] is None:
            raise ProtocolError("bad_envelope", f"{ts_field(sender)} must be the {sender}'s own timestamp",
                                reply_to=msg_id, silent=silent)
        if mtype == "hello" and msg["payload"]["role"] != sender:
            raise ProtocolError("bad_payload", f"hello.role must be {sender!r}", fatal=True, reply_to=msg_id)
    return msg


def make_message(mtype: str, msg_id: int, payload: dict[str, Any], *, sender: str, ts: int,
                 reply_to: int | None = None, other_ts: int | None = None) -> dict[str, Any]:
    """Build an envelope. The sender stamps its own clock; `other_ts` copies the peer's (replies)."""
    msg = {
        "v": PROTOCOL_VERSION, "type": mtype, "id": msg_id, "reply_to": reply_to,
        "ts_body": None, "ts_brain": None, "payload": payload,
    }
    msg[ts_field(sender)] = ts
    msg[ts_field(other_role(sender))] = other_ts
    return msg


def has_binary(msg: dict[str, Any]) -> bool:
    """True when a binary frame must follow this JSON message (section 5.2)."""
    return msg["type"] in BINARY_TYPE_CODES and msg["payload"].get("byte_length", 0) > 0


@dataclass(frozen=True)
class BinaryFrame:
    type: str
    id: int
    ts: int
    data: bytes
    flags: int = 0


class FrameValueError(ValueError):
    """A message id, timestamp or binary length outside what protocol v0 allows (caller bug, never peer input)."""


class FrameTypeError(TypeError):
    """A message field of the wrong Python type (a bool is not an int here; text is not bytes)."""


def check_timestamp(ts: object) -> int:
    """ts if it is an int from 0 to 2^53-1 microseconds, else FrameTypeError or FrameValueError."""
    if not isinstance(ts, int) or isinstance(ts, bool):
        raise FrameTypeError(f"ts must be an int, not {type(ts).__name__}")
    if not 0 <= ts <= MAX_TS:
        raise FrameValueError("ts must be 0 to 2^53-1 microseconds")
    return ts


def check_message_id(msg_id: object) -> int:
    """msg_id if it is an int from 1 to 2^32-1, else FrameTypeError or FrameValueError."""
    if not isinstance(msg_id, int) or isinstance(msg_id, bool):
        raise FrameTypeError(f"msg_id must be an int, not {type(msg_id).__name__}")
    if not 1 <= msg_id <= MAX_ID:
        raise FrameValueError("msg_id must be 1 to 2^32-1")
    return msg_id


def check_data(data: object) -> bytes | bytearray | memoryview:
    """data if it is bytes-like (bytes, bytearray or memoryview), else FrameTypeError."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise FrameTypeError(f"binary data must be bytes, not {type(data).__name__}")
    return data


def pack_binary(mtype: str, msg_id: int, ts: int, data: bytes) -> bytes:
    if mtype not in BINARY_TYPE_CODES:
        raise FrameValueError(f"type {mtype!r} has no binary frame")
    check_message_id(msg_id)
    check_timestamp(ts)
    check_data(data)
    if len(data) > MAX_BINARY_BYTES:
        raise FrameValueError("binary data over 8 MiB")
    return BINARY_HEADER.pack(PROTOCOL_VERSION, BINARY_TYPE_CODES[mtype], 0, msg_id, ts, len(data)) + data


def unpack_binary(frame: bytes) -> BinaryFrame:
    """Parse one binary frame; raises ProtocolError(bad_binary or too_large) if malformed."""
    if len(frame) < BINARY_HEADER.size:
        raise ProtocolError("bad_binary", f"frame of {len(frame)} bytes is shorter than the 20-byte header",
                            fatal=True)
    v, code, flags, msg_id, ts, length = BINARY_HEADER.unpack_from(frame)
    if v != PROTOCOL_VERSION:
        raise ProtocolError("unsupported_version", f"binary frame v={v}", fatal=True)
    if code not in BINARY_TYPE_NAMES:
        raise ProtocolError("bad_binary", f"unknown binary type code {code}", fatal=True)
    if not 0 <= ts <= MAX_TS:
        raise ProtocolError("bad_binary", f"timestamp {ts} outside 0 to 2^53-1", fatal=True)
    if not 1 <= msg_id <= MAX_ID:
        raise ProtocolError("bad_binary", f"message id {msg_id} outside 1 to 2^32-1", fatal=True)
    if length > MAX_BINARY_BYTES:
        raise ProtocolError("too_large", f"binary length {length} over 8 MiB", fatal=True)
    if length != len(frame) - BINARY_HEADER.size:
        raise ProtocolError("bad_binary", f"header length {length} but {len(frame) - BINARY_HEADER.size} data bytes",
                            fatal=True)
    return BinaryFrame(BINARY_TYPE_NAMES[code], msg_id, ts, bytes(frame[BINARY_HEADER.size:]), flags)


def check_pair(msg: dict[str, Any], frame: BinaryFrame, *, sender: str) -> None:
    """The binary frame must match the JSON message it follows (section 5.2)."""
    want_ts = msg[ts_field(sender)]
    if (frame.type != msg["type"] or frame.id != msg["id"] or frame.ts != want_ts
            or len(frame.data) != msg["payload"]["byte_length"]):
        raise ProtocolError(
            "bad_binary",
            f"binary frame ({frame.type}, id {frame.id}, ts {frame.ts}, {len(frame.data)} bytes) does not match "
            f"message ({msg['type']}, id {msg['id']}, ts {want_ts}, {msg['payload']['byte_length']} bytes)",
            fatal=True, reply_to=msg["id"])
