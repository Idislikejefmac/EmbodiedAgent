"""H-11 (T-P0-05-076, T-X01-039): brain/limits.py is the one home of the brain's named limits.

E5 (docs/testing/00 section 4.3) as far as it is practical today: every constant in brain/limits.py is used
by product code (a validator or the bound it enforces) and named by at least one test, with the
exceptions listed below and their reasons; the old scattered constants are gone or are re-exports of
the limits; and the limits the body app shares are pinned against protocol/v0.md, the schema and the
Kotlin constants by regular expression (Limits.kt is planned for hardening batch 3).
"""

import ast
import re
from pathlib import Path

import pytest

from brain import limits as L
from brain.transport import protocol as p

REPO = Path(__file__).resolve().parent.parent
LIMITS_FILE = REPO / "brain" / "limits.py"
DOC = REPO / "protocol" / "v0.md"
KOTLIN_DIR = REPO / "body-android" / "app" / "src" / "main" / "java" / "com" / "jmd" / "embodied" / "body"

# Constants no test names directly, and why that is acceptable for now. Each is still used by product code
# (checked below). A stale entry (the constant is now named by a test, or no longer exists) fails.
NOT_IN_A_TEST = {
    "BINARY_HEADER_BYTES": "fixed by the spec; protocol.py refuses to import if the struct size differs",
    "CLOSE_TIMEOUT_S": "a close-handshake wait; exercised by every transport test that closes a link",
    "CLOSE_REASON_MAX_CHARS": "truncates our own close reason; exercised by the closing tests",
    "CLOCK_HISTORY_MAX": "a default argument; test_clock_history_is_bounded passes its own cap",
    "ACL_TOOL_TIMEOUT_S": "the icacls subprocess timeout; the token-store tests run icacls",
    "ADB_LINK_RECHECK_S": "a loop period in the phone body; needs the phone (no test reaches it)",
    "LOG_FILE_MAX_BYTES": "rotating log handler size; test_h08 checks the handler is installed",
    "LOG_FILE_BACKUPS": "rotating log handler count; as LOG_FILE_MAX_BYTES",
    "REQUEST_LOG_RETENTION_DAYS": "the default of [logging] capture_max_days; the capture itself is card P0-07",
    "NONCE_BYTES": "nonce length; every handshake test makes nonces",
    "RECENT_IDS_KEPT": "replay window; test_transport_loopback exercises duplicate ids below it",
    "PEER_RATE_SANITY_MAX": "the range of a BrainServer test-only knob",
    "CLOCK_MAX_FIT_PPM": "a sanity bound inside the clock estimator; pinned against the body app below",
    "EVENT_QUEUE": "body stream queue size; the body contract's slow-consumer check exercises the drop policy",
    "AUDIO_QUEUE": "body stream queue size; as EVENT_QUEUE",
    "POSE_WAIT_S": "how long get_pose waits; the tool tests cover a body with and without poses",
}
# The chaos harness's boundary matrix (tools/chaos/cases/) counts as a boundary test: it generates the
# below / at / above cases from these names (config keys, body arguments, CLI flags, migration).
CHAOS_CASES = REPO / "tools" / "chaos" / "cases"


def limit_names() -> list[str]:
    tree = ast.parse(LIMITS_FILE.read_text(encoding="utf-8"))
    return [t.id for node in tree.body if isinstance(node, ast.Assign) for t in node.targets
            if isinstance(t, ast.Name) and t.id.isupper()]


def _texts(files):
    return {f: f.read_text(encoding="utf-8-sig") for f in files}


PRODUCT = _texts([f for f in sorted((REPO / "brain").rglob("*.py")) if f != LIMITS_FILE] + [REPO / "tools" / "migrate.py"])
TESTS = _texts([f for f in sorted((REPO / "tests").rglob("*.py")) if f.name != "test_limits.py"])
BOUNDARY_CASES = _texts(sorted(CHAOS_CASES.glob("*.py")))


def _named(name, texts):
    word = re.compile(rf"\b{name}\b")
    return [f for f, t in texts.items() if word.search(t)]


def test_limits_module_is_only_constants_and_imports_nothing():
    tree = ast.parse(LIMITS_FILE.read_text(encoding="utf-8"))
    body = tree.body[1:] if isinstance(tree.body[0], ast.Expr) else tree.body  # the docstring
    assert all(isinstance(n, ast.Assign) for n in body), [type(n).__name__ for n in body]
    names = limit_names()
    assert len(names) == len(set(names)), "a limit is defined twice"
    for name in names:
        value = getattr(L, name)
        assert isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0, name


@pytest.mark.parametrize("name", limit_names())
def test_every_limit_is_used_by_product_code(name):
    assert _named(name, PRODUCT), f"{name} is in brain/limits.py but no product module uses it"


@pytest.mark.parametrize("name", limit_names())
def test_every_limit_is_named_by_a_test_or_listed_with_a_reason(name):
    tested = _named(name, TESTS) or _named(name, BOUNDARY_CASES)
    if name in NOT_IN_A_TEST:
        assert not tested, f"{name} is now named by a test: remove it from NOT_IN_A_TEST"
    else:
        assert tested, f"{name} is named by no test (add an edge test, or list it in NOT_IN_A_TEST with a reason)"


def test_exception_list_names_only_real_limits():
    assert set(NOT_IN_A_TEST) <= set(limit_names()), set(NOT_IN_A_TEST) - set(limit_names())


# Numeric literals that were limits and must not come back (Appendix A "Literal" rows, H-11).
OLD_LITERALS = [
    ("brain/transport/server.py", r"max_queue=\d"), ("brain/transport/client.py", r"max_queue=\d"),
    ("brain/transport/link.py", r"timeout_s: float = \d"), ("brain/transport/link.py", r"reason\[:\d"),
    ("brain/transport/server.py", r"timeout_s: float = \d"), ("brain/transport/protocol.py", r"\[:500\]"),
    ("brain/transport/auth.py", r"timeout=\d"), ("brain/agent/tools.py", r"<= 600\b"),
    ("brain/agent/tools.py", r"\[:40\]"), ("brain/agent/tools.py", r"\[:64\]"), ("brain/body/types.py", r"> 1e-3"),
    ("brain/__main__.py", r"maxBytes=\d"), ("tools/migrate.py", r"14 <= log_n"),
]


@pytest.mark.parametrize("path, pattern", OLD_LITERALS)
def test_former_literal_limits_are_named(path, pattern):
    assert not re.search(pattern, (REPO / path).read_text(encoding="utf-8")), (path, pattern)


def test_one_constant_for_the_link_recheck_period():
    """Appendix A.10 item 7: LINK_RECHECK_S and PHONE_LINK_RECHECK_S were one limit in two constants."""
    for text in PRODUCT.values():
        assert not re.search(r"^\s*(PHONE_)?LINK_RECHECK_S\s*=", text, re.M)
    assert L.ADB_LINK_RECHECK_S == 60


def test_old_module_names_are_the_limits():
    from brain.agent import body_tools, tools
    from brain.body import base, streams
    from brain.transport import auth, clock, server
    from tools import migrate

    assert (p.MAX_TEXT_BYTES, p.MAX_BINARY_BYTES, p.MAX_ID, p.MAX_TS) == (L.MAX_TEXT_BYTES, L.MAX_BINARY_BYTES,
                                                                          L.MAX_ID, L.MAX_TS)
    assert (p.HEARTBEAT_MS, p.LIVENESS_TIMEOUT_MS) == (L.HEARTBEAT_MS, L.LIVENESS_TIMEOUT_MS)
    assert (server.HANDLER_QUEUE, server.PRE_AUTH_MAX_BYTES, server.MAX_FRAME_BYTES) == (
        L.HANDLER_QUEUE, L.PRE_AUTH_MAX_BYTES, L.MAX_BINARY_BYTES + p.BINARY_HEADER.size)
    assert (auth.TOKEN_BYTES, clock.MAX_FIT_PPM) == (L.TOKEN_BYTES, L.CLOCK_MAX_FIT_PPM)
    assert (tools.MAX_ARGS_BYTES, tools.DEFAULT_TIMEOUT_S, body_tools.POSE_WAIT_S) == (
        L.MAX_ARGS_BYTES, L.TOOL_DEFAULT_TIMEOUT_S, L.POSE_WAIT_S)
    assert base.MAX_EDGE_PX_RANGE == (L.CAPTURE_MAX_EDGE_PX_MIN, L.CAPTURE_MAX_EDGE_PX_MAX)
    assert streams.EVENT_QUEUE == L.EVENT_QUEUE
    assert (migrate.SCRYPT_LOG_N, migrate.MIN_PASSPHRASE) == (L.SCRYPT_LOG_N, L.PASSPHRASE_MIN_CHARS)


# ---- cross-language pins: the body app, the spec and the schema ------------------------------------------------


def kotlin_value(file: str, name: str) -> float:
    m = re.search(rf"const val {name} = ([0-9_ *.L]+)", (KOTLIN_DIR / file).read_text(encoding="utf-8-sig"))
    assert m, f"{file}: no const val {name}"
    value = 1.0
    for factor in m.group(1).replace("_", "").replace("L", "").split("*"):
        value *= float(factor)
    return value


KOTLIN_PINS = [  # (limit, Kotlin file, Kotlin name, Kotlin value per unit of the limit)
    ("MAX_TEXT_BYTES", "Protocol.kt", "MAX_TEXT_BYTES", 1),
    ("MAX_TS", "Protocol.kt", "MAX_TS", 1),
    ("MAX_BINARY_BYTES", "BinaryFrame.kt", "MAX_DATA", 1),
    ("MAX_ID", "BinaryFrame.kt", "MAX_ID", 1),
    ("TOKEN_BYTES", "LinkAuth.kt", "TOKEN_BYTES", 1),
    ("HEARTBEAT_MS", "BrainLink.kt", "DEFAULT_HEARTBEAT_MS", 1),
    ("LIVENESS_TIMEOUT_MS", "BrainLink.kt", "DEFAULT_LIVENESS_MS", 1),
    ("HANDSHAKE_TIMEOUT_S", "BrainLink.kt", "HANDSHAKE_TIMEOUT_MS", 1000),
    ("MAX_PEER_ERRORS_PER_MIN", "BrainLink.kt", "MAX_PEER_ERRORS_PER_MIN", 1),
    ("ECHO_TEST_MAX_COUNT", "BrainLink.kt", "MAX_ECHO_COUNT", 1),
    ("ECHO_TEST_MAX_BYTES", "BrainLink.kt", "MAX_ECHO_BYTES", 1),
    ("CLOCK_MAX_FIT_PPM", "ClockOffset.kt", "MAX_FIT_PPM", 1),
]


@pytest.mark.parametrize("name, file, kotlin_name, scale", KOTLIN_PINS, ids=[k[0] for k in KOTLIN_PINS])
def test_shared_limits_match_the_body_app(name, file, kotlin_name, scale):
    assert kotlin_value(file, kotlin_name) == getattr(L, name) * scale


DOC_PINS = [  # (text that must appear in protocol/v0.md, built from the limits)
    f"at most {L.MAX_TEXT_BYTES:,} **bytes**",
    f"Largest data size: {L.MAX_BINARY_BYTES:,} bytes",
    f"every {L.HEARTBEAT_MS:,} ms",
    f"no **valid** frame for {L.LIVENESS_TIMEOUT_MS:,} ms",
    f"must complete within {L.HANDSHAKE_TIMEOUT_S:g} s",
    f"within {L.SEND_TIMEOUT_S:g} s also aborts the connection",
    f"More than {L.MAX_PEER_ERRORS_PER_MIN} protocol errors",
    f"{L.AUDIO_SAMPLE_RATE_HZ_MIN:,} to {L.AUDIO_SAMPLE_RATE_HZ_MAX:,} Hz",
    f"at most {L.ERROR_MESSAGE_MAX_CHARS} characters",
    f"fresh {L.NONCE_BYTES}-byte nonce",
    f"If {L.HANDLER_QUEUE} messages or {L.HANDLER_QUEUE_MAX_BYTES // 2**20} MiB are waiting",
    f"{L.PEER_MSG_MAX_PER_S} a second by default (a burst of {L.PEER_MSG_BURST})",
    f"heartbeat requests {L.PEER_HEARTBEAT_MAX_PER_S} a second, echo requests {L.PEER_ECHO_MAX_PER_S:,} a second",
    f"More than {L.PEER_EXCESS_MAX_PER_MIN:,} messages over the limit within one minute",
    f"an `event` frame at most {L.EVENT_MAX_BYTES // 1024} KiB, `echo` data at most {L.ECHO_TEST_MAX_BYTES:,} bytes, "
    f"`image` data at most {L.MAX_IMAGE_BYTES // 2**20} MiB, `audio_in` data at most {L.AUDIO_IN_MAX_CHUNK_BYTES:,} bytes",
    f"at most {L.PEER_LOG_LINES_PER_MIN_MAX} lines a minute per connection",
]


@pytest.mark.parametrize("text", DOC_PINS)
def test_spec_states_the_limits(text):
    assert text in " ".join(DOC.read_text(encoding="utf-8").split()), text


def test_schema_states_the_limits():
    s = p.schema()["$defs"]
    assert s["image"]["properties"]["width"]["maximum"] == L.WIRE_IMAGE_EDGE_MAX_PX
    assert s["image"]["properties"]["height"]["maximum"] == L.WIRE_IMAGE_EDGE_MAX_PX
    assert s["image"]["properties"]["byte_length"]["maximum"] == L.MAX_BINARY_BYTES
    rate = s["pcm"]["properties"]["sample_rate"]
    assert (rate["minimum"], rate["maximum"]) == (L.AUDIO_SAMPLE_RATE_HZ_MIN, L.AUDIO_SAMPLE_RATE_HZ_MAX)
    assert max(s["pcm"]["properties"]["channels"]["enum"]) == L.AUDIO_CHANNELS_MAX
    assert s["byte_length"]["maximum"] == L.MAX_BINARY_BYTES
    assert s["ts"]["maximum"] == L.MAX_TS
    hello = s["hello"]["properties"]
    assert (hello["heartbeat_ms"]["minimum"], hello["heartbeat_ms"]["maximum"]) == (L.HEARTBEAT_MS_MIN, L.HEARTBEAT_MS_MAX)
    assert (hello["liveness_timeout_ms"]["minimum"], hello["liveness_timeout_ms"]["maximum"]) == (
        L.LIVENESS_TIMEOUT_MS_MIN, L.LIVENESS_TIMEOUT_MS_MAX)
    cmd = p.schema()["$defs"]["command"]["allOf"]
    edge = cmd[0]["then"]["properties"]["args"]["properties"]["max_edge"]
    hz = cmd[1]["then"]["properties"]["args"]["properties"]["hz"]
    assert (edge["minimum"], edge["maximum"]) == (L.CAPTURE_MAX_EDGE_PX_MIN, L.CAPTURE_MAX_EDGE_PX_MAX)
    assert hz["maximum"] == L.POSE_RATE_MAX_HZ
    assert s["error"]["properties"]["message"]["maxLength"] == L.ERROR_MESSAGE_MAX_CHARS
