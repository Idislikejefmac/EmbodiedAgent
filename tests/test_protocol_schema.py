"""Protocol v0 schema: hand-written examples, every doc example, and the Python and Kotlin constants agree."""

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from brain.transport import protocol as p

DOC = Path(__file__).resolve().parent.parent / "protocol" / "v0.md"
KOTLIN_DIR = DOC.parent.parent / "body-android" / "app" / "src" / "main" / "java" / "com" / "jmd" / "embodied" / "body"


def envelope(**overrides):
    msg = {"v": 0, "type": "heartbeat", "id": 1, "reply_to": None, "ts_body": 1000, "ts_brain": None,
           "payload": {}}
    msg.update(overrides)
    return msg


# Step 1: three hand-written examples.
HAND_WRITTEN = [
    {"v": 0, "type": "hello", "id": 1, "reply_to": None, "ts_body": 5_000_000, "ts_brain": None,
     "payload": {"role": "body", "software": "test-body/1", "capabilities": ["echo"], "commands": ["get_status"]}},
    {"v": 0, "type": "command", "id": 3, "reply_to": None, "ts_body": None, "ts_brain": 9_000_000,
     "payload": {"name": "capture_image", "args": {"max_edge": 640}}},
    {"v": 0, "type": "error", "id": 4, "reply_to": 3, "ts_body": 5_100_000, "ts_brain": None,
     "payload": {"code": "not_implemented", "message": "no camera yet", "fatal": False}},
]


def test_schema_is_valid_draft_2020_12():
    Draft202012Validator.check_schema(p.schema())


@pytest.mark.parametrize("msg", HAND_WRITTEN, ids=lambda m: m["type"])
def test_hand_written_examples_validate(msg):
    assert p.schema_errors(msg) == []
    assert p.validate(msg) is msg


def doc_examples():
    blocks = re.findall(r"```json\n(.*?)```", DOC.read_text(encoding="utf-8"), re.S)
    return [json.loads(b) for b in blocks]


def test_every_doc_example_validates():
    examples = doc_examples()
    assert len(examples) >= len(p.MESSAGE_TYPES)
    for msg in examples:
        assert p.schema_errors(msg) == [], msg
        # The sender is the side whose clock is stamped (a reply stamps both; direction then unchecked).
        sender = p.BODY if msg["ts_brain"] is None else p.BRAIN if msg["ts_body"] is None else None
        p.validate(msg, sender=sender)


def test_every_message_type_has_a_doc_example():
    covered = {m["type"] for m in doc_examples()}
    assert covered == set(p.MESSAGE_TYPES)


def test_doc_examples_stamp_their_own_clock():
    """Section 6: each side stamps its own field; only replies may copy the other."""
    for msg in doc_examples():
        stamped = [f for f in ("ts_body", "ts_brain") if msg[f] is not None]
        assert stamped, msg
        if len(stamped) == 2:
            assert msg["reply_to"] is not None, msg


def test_python_constants_match_schema():
    s = p.schema()
    assert tuple(s["properties"]["type"]["enum"]) == p.MESSAGE_TYPES
    assert tuple(s["$defs"]["command_name"]["enum"]) == p.COMMANDS
    assert tuple(s["$defs"]["error_code"]["enum"]) == p.ERROR_CODES
    assert tuple(s["required"]) == p.ENVELOPE_FIELDS
    assert set(p.SENDERS) == set(p.MESSAGE_TYPES)
    hello = s["$defs"]["hello"]["properties"]
    # Must-ignore: hello lists names, not an enum, so a newer body's capability is not fatal.
    assert hello["capabilities"]["items"] == {"$ref": "#/$defs/name"}
    assert hello["commands"]["items"] == {"$ref": "#/$defs/name"}
    assert s["$defs"]["ts"]["minimum"] == 0 and s["$defs"]["ts"]["maximum"] == p.MAX_TS


def kotlin(name: str) -> str:
    return (KOTLIN_DIR / name).read_text(encoding="utf-8-sig")


def kotlin_string_list(name: str) -> list[str]:
    m = re.search(rf"val {name} = (?:setOf|listOf)\((.*?)\)", kotlin("Protocol.kt"), re.S)
    assert m, name
    return re.findall(r'"([a-z_]+)"', m.group(1))


def kotlin_const(file: str, name: str) -> int:
    m = re.search(rf"const val {name} = ([0-9_ *L]+)", kotlin(file))
    assert m, name
    value = 1
    for factor in m.group(1).replace("_", "").replace("L", "").split("*"):
        value *= int(factor)
    return value


def test_body_constants_match_schema():
    """The Kotlin body's allowlist and type tables must not drift from the spec."""
    assert tuple(kotlin_string_list("COMMANDS")) == p.COMMANDS
    assert set(kotlin_string_list("TYPES")) == set(p.MESSAGE_TYPES)
    assert set(kotlin_string_list("FROM_BRAIN")) == {t for t, s in p.SENDERS.items() if p.BRAIN in s}
    assert set(kotlin_string_list("IMPLEMENTED_COMMANDS")) <= set(p.COMMANDS)


def test_body_binary_codes_sizes_and_timing_match():
    codes = {k: int(v) for k, v in re.findall(r'"([a-z_]+)" to (\d+)', kotlin("BinaryFrame.kt"))}
    assert codes == p.BINARY_TYPE_CODES
    assert kotlin_const("BinaryFrame.kt", "MAX_DATA") == p.MAX_BINARY_BYTES
    assert kotlin_const("Protocol.kt", "MAX_TEXT_BYTES") == p.MAX_TEXT_BYTES
    assert kotlin_const("BrainLink.kt", "DEFAULT_HEARTBEAT_MS") == p.HEARTBEAT_MS
    assert kotlin_const("BrainLink.kt", "DEFAULT_LIVENESS_MS") == p.LIVENESS_TIMEOUT_MS


def test_body_command_argument_bounds_match_schema():
    cmd = p.schema()["$defs"]["command"]["allOf"]
    max_edge = cmd[0]["then"]["properties"]["args"]["properties"]["max_edge"]
    hz = cmd[1]["then"]["properties"]["args"]["properties"]["hz"]
    src = kotlin("Protocol.kt")
    assert f'"max_edge")?.let {{ it in {max_edge["minimum"]}..{max_edge["maximum"]} }}' in src
    assert f'in {float(hz["minimum"])}..{float(hz["maximum"])}' in src


def test_doc_direction_column_matches_senders():
    rows = re.findall(r"^\| `([a-z_]+)` \| ([BR, ]+) \| (?:no|yes|optional) \|", DOC.read_text(encoding="utf-8"),
                      re.M)
    table = {t: frozenset({"B": p.BODY, "R": p.BRAIN}[d.strip()] for d in dirs.split(",")) for t, dirs in rows}
    assert table == p.SENDERS


def test_doc_lists_every_command_error_code_and_capability():
    text = DOC.read_text(encoding="utf-8")
    for name in p.COMMANDS + p.ERROR_CODES:
        assert f"| `{name}` |" in text, name
    for name in p.CAPABILITIES:
        assert f"`{name}`" in text, name


POSE = {"frame_id": "world-1", "position_m": [0, 0, 0], "orientation_xyzw": [0, 0, 0, 1], "tracking": "ok"}


@pytest.mark.parametrize("msg, code", [
    (envelope(extra=1), "bad_envelope"),
    ({k: v for k, v in envelope().items() if k != "reply_to"}, "bad_envelope"),
    (envelope(v=1), "unsupported_version"),
    (envelope(v=1, extra=1), "unsupported_version"),  # version is checked before unknown fields
    (envelope(type="shell"), "unknown_type"),
    (envelope(id=0), "bad_envelope"),
    (envelope(ts_body=1.5), "bad_envelope"),
    (envelope(ts_body=-1), "bad_envelope"),
    (envelope(type="ack", reply_to=None, payload={"status": "ok"}), "bad_envelope"),
    (envelope(type="tracking_state", payload={"state": "great"}), "bad_payload"),
    (envelope(type="command", payload={"name": "run_shell", "args": {"cmd": "del C:\\"}}), "unknown_command"),
    (envelope(type="command", payload={"name": "read_file", "args": {"path": "config.toml"}}), "unknown_command"),
    (envelope(type="command", payload={"name": "get_status", "args": {"path": "C:\\"}}), "bad_args"),
    (envelope(type="command", payload={"name": "capture_image", "args": {"max_edge": 640, "path": "x"}}),
     "bad_args"),
    (envelope(type="command", payload={"name": "capture_image", "args": {"max_edge": "640"}}), "bad_args"),
    (envelope(type="pose", payload={**POSE, "frame_id": "../../etc"}), "bad_payload"),
    (envelope(type="pose", payload={**POSE, "frame_id": "a/b"}), "bad_payload"),
    (envelope(type="pose", payload={**POSE, "frame_id": ".hidden"}), "bad_payload"),
    (envelope(type="hello", payload={"role": "body", "software": "x", "capabilities": [], "boot_id": "b 1"}),
     "bad_payload"),
    (envelope(type="hello", payload={"role": "brain", "software": "x", "capabilities": [],
                                     "wall_time_utc": "yesterday"}), "bad_payload"),
    (envelope(type="hello", payload={"role": "body", "software": "x", "capabilities": ["Not A Name"]}),
     "bad_payload"),
    ([1, 2], "bad_json"),
])
def test_invalid_messages_get_the_right_error_code(msg, code):
    with pytest.raises(p.ProtocolError) as e:
        p.validate(msg)
    assert e.value.code == code


def test_hello_accepts_unknown_capability_and_command_names():
    msg = envelope(type="hello", payload={"role": "body", "software": "x", "versions": [0, 1],
                                          "capabilities": ["echo", "lidar_scan"], "commands": ["get_status", "dock"]})
    assert p.validate(msg, sender=p.BODY) is msg


def test_sender_timestamp_is_required():
    msg = envelope(type="pose", ts_body=None, ts_brain=None, payload=POSE)
    assert p.schema_errors(msg) == []  # the schema alone cannot say whose clock it is
    with pytest.raises(p.ProtocolError) as e:
        p.validate(msg, sender=p.BODY)
    assert e.value.code == "bad_envelope"


def test_nan_and_deep_nesting_are_bad_json():
    for text in ('{"a": NaN}', '{"a": Infinity}', "[" * 5000 + "]" * 5000):
        with pytest.raises(p.ProtocolError) as e:
            p.decode(text)
        assert e.value.code == "bad_json"


def test_errors_about_an_error_are_silent_and_reply_to_is_sanitised():
    with pytest.raises(p.ProtocolError) as e:
        p.validate(envelope(type="error", id=7, payload={"code": "nope", "message": "", "fatal": False}))
    assert e.value.silent
    with pytest.raises(p.ProtocolError) as e:
        p.validate(envelope(id=2**40, extra=1))
    assert e.value.reply_to is None


def test_payload_unknown_fields_are_ignored_but_command_args_are_closed():
    assert p.schema_errors(envelope(payload={"future_field": 1})) == []
    bad = envelope(type="command", ts_body=None, ts_brain=1,
                   payload={"name": "set_pose_rate", "args": {"hz": 10, "also": 1}})
    assert p.schema_errors(bad)


def test_direction_rules():
    cmd = envelope(type="command", ts_body=None, ts_brain=1, payload={"name": "get_status", "args": {}})
    p.validate(cmd, sender=p.BRAIN)
    with pytest.raises(p.ProtocolError) as e:
        p.validate(cmd, sender=p.BODY)
    assert e.value.code == "wrong_direction"


def test_body_error_codes_close_codes_and_auth_labels_match():
    """Kotlin error-code strings, close codes and auth labels are pinned to the spec and the brain."""
    from brain.transport import auth

    src = "".join(kotlin(n) for n in ("Protocol.kt", "BrainLink.kt", "BinaryFrame.kt"))
    assert tuple(kotlin_string_list("ERROR_CODES")) == p.ERROR_CODES
    used = set(re.findall(r'(?:ProtocolException|FrameException)\(\s*"([a-z_]+)"', src))
    assert used and used <= set(p.ERROR_CODES), used - set(p.ERROR_CODES)
    doc = DOC.read_text(encoding="utf-8")
    for name, value in (("CLOSE_NORMAL", 1000), ("CLOSE_GOING_AWAY", 1001), ("CLOSE_PROTOCOL_ERROR", 1002),
                        ("CLOSE_AUTH_FAILED", 1008), ("CLOSE_REPLACED", 4001)):
        assert getattr(p, name) == value
        assert kotlin_const("Protocol.kt", name) == value
        assert f"**{value}**" in doc, value
    labels = dict(re.findall(r'"(body|brain)" to "([^"]+)"', kotlin("LinkAuth.kt")))
    assert {k: v.encode() for k, v in labels.items()} == auth.LABELS
    for label in auth.LABELS.values():
        assert f"`{label.decode()}`" in doc
