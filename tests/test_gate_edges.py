"""E1b gate edge tests (docs/testing/00 section 4.4): for a gate listed in tools/chaos/manifest.toml and each limit it
enforces, one test below, one at and one above the limit, plus one wrong-type test per gate. The marker
`gate(name, limit=, edge=)` is what tools/lint.py counts; gates still without all their edges are in
tests/lint_baseline.toml (E1b entries, counts that only fall)."""

from __future__ import annotations

import json
import math
import struct

import pytest

from brain.agent import tools as T
from brain.body.types import AudioChunk
from brain.config import ConfigError, parse_config
from brain.limits import (AUDIO_CHANNELS_MAX, AUDIO_SAMPLE_RATE_HZ_MAX, AUDIO_SAMPLE_RATE_HZ_MIN, MAX_ARGS_BYTES, MAX_ARGS_DEPTH,
                          MAX_ARGS_NODES, MAX_BINARY_BYTES, MAX_TS, PORT_MAX, PORT_MIN, TOKEN_BYTES)
from brain.transport import auth
from brain.transport import protocol as p

UNPACK = "brain.transport.protocol.unpack_binary"
TOKEN = "brain.transport.auth.parse_token"
ARGS = "brain.agent.tools._plain_json_problem"
CONFIG = "brain.config.parse_config"
AUDIO = "brain.body.types.AudioChunk"


def frame(length: int = 0, ts: int = 5, data: bytes = b"") -> bytes:
    return p.BINARY_HEADER.pack(0, 1, 0, 1, ts, length) + data


def refused(fn, *a, **k) -> bool:  # type: ignore[no-untyped-def]
    try:
        fn(*a, **k)
    except (p.ProtocolError, ValueError, ConfigError):
        return True
    return False


# ---- unpack_binary ----------------------------------------------------------------------------------------------------


@pytest.mark.gate(UNPACK, limit="MAX_BINARY_BYTES", edge="below")
def test_unpack_one_byte_under_the_data_limit() -> None:
    assert len(p.unpack_binary(frame(MAX_BINARY_BYTES - 1, data=b"\0" * (MAX_BINARY_BYTES - 1))).data) == MAX_BINARY_BYTES - 1


@pytest.mark.gate(UNPACK, limit="MAX_BINARY_BYTES", edge="at")
def test_unpack_exactly_the_data_limit() -> None:
    assert len(p.unpack_binary(frame(MAX_BINARY_BYTES, data=b"\0" * MAX_BINARY_BYTES)).data) == MAX_BINARY_BYTES


@pytest.mark.gate(UNPACK, limit="MAX_BINARY_BYTES", edge="above")
def test_unpack_one_byte_over_the_data_limit_is_too_large() -> None:
    with pytest.raises(p.ProtocolError) as e:
        p.unpack_binary(frame(MAX_BINARY_BYTES + 1, data=b"\0" * (MAX_BINARY_BYTES + 1)))
    assert e.value.code == "too_large"


@pytest.mark.gate(UNPACK, limit="MAX_TS", edge="below")
def test_unpack_timestamp_below_the_limit() -> None:
    assert p.unpack_binary(frame(ts=MAX_TS - 1)).ts == MAX_TS - 1


@pytest.mark.gate(UNPACK, limit="MAX_TS", edge="at")
def test_unpack_timestamp_at_the_limit() -> None:
    assert p.unpack_binary(frame(ts=MAX_TS)).ts == MAX_TS


@pytest.mark.gate(UNPACK, limit="MAX_TS", edge="above")
def test_unpack_timestamp_over_the_limit_is_refused() -> None:
    with pytest.raises(p.ProtocolError) as e:
        p.unpack_binary(frame(ts=MAX_TS + 1))
    assert e.value.code == "bad_binary"


@pytest.mark.gate(UNPACK, edge="wrong_type")
@pytest.mark.parametrize("value", [None, 7, [1], {"a": 1}])
def test_unpack_refuses_a_value_that_is_not_bytes(value: object) -> None:
    with pytest.raises((TypeError, struct.error, p.ProtocolError)):
        p.unpack_binary(value)  # type: ignore[arg-type]


# ---- parse_token ------------------------------------------------------------------------------------------------------


@pytest.mark.gate(TOKEN, limit="TOKEN_BYTES", edge="below")
def test_token_one_character_short_is_refused() -> None:
    assert refused(auth.parse_token, "a" * (TOKEN_BYTES * 2 - 1))


@pytest.mark.gate(TOKEN, limit="TOKEN_BYTES", edge="at")
def test_token_of_exactly_64_hex_characters_parses() -> None:
    assert auth.parse_token("a" * (TOKEN_BYTES * 2)) == b"\xaa" * TOKEN_BYTES


@pytest.mark.gate(TOKEN, limit="TOKEN_BYTES", edge="above")
def test_token_one_character_long_is_refused() -> None:
    assert refused(auth.parse_token, "a" * (TOKEN_BYTES * 2 + 1))


@pytest.mark.gate(TOKEN, edge="wrong_type")
@pytest.mark.parametrize("value", [None, 7, b"a" * 64, ["a"]])
def test_token_refuses_a_value_that_is_not_text(value: object) -> None:
    with pytest.raises((TypeError, AttributeError, ValueError)):
        auth.parse_token(value)  # type: ignore[arg-type]


# ---- the tool-argument gate -------------------------------------------------------------------------------------------


def nested(depth: int) -> dict[str, object]:
    value: object = 1
    for _ in range(depth - 1):
        value = {"a": value}
    return {"a": value} if depth >= 1 else {}


@pytest.mark.gate(ARGS, limit="MAX_ARGS_DEPTH", edge="below")
def test_args_one_level_under_the_depth_limit() -> None:
    assert T._plain_json_problem(nested(MAX_ARGS_DEPTH - 1)) is None


@pytest.mark.gate(ARGS, limit="MAX_ARGS_DEPTH", edge="at")
def test_args_exactly_at_the_depth_limit() -> None:
    assert T._plain_json_problem(nested(MAX_ARGS_DEPTH)) is None


@pytest.mark.gate(ARGS, limit="MAX_ARGS_DEPTH", edge="above")
def test_args_one_level_over_the_depth_limit_is_refused() -> None:
    assert T._plain_json_problem(nested(MAX_ARGS_DEPTH + 1)) is not None


@pytest.mark.gate(ARGS, limit="MAX_ARGS_NODES", edge="below")
def test_args_under_the_node_limit() -> None:
    assert T._plain_json_problem({"x": list(range(MAX_ARGS_NODES - 3))}) is None


@pytest.mark.gate(ARGS, limit="MAX_ARGS_NODES", edge="at")
def test_args_exactly_at_the_node_limit() -> None:
    assert T._plain_json_problem({"x": list(range(MAX_ARGS_NODES - 2))}) is None


@pytest.mark.gate(ARGS, limit="MAX_ARGS_NODES", edge="above")
def test_args_over_the_node_limit_are_refused() -> None:
    assert T._plain_json_problem({"x": list(range(MAX_ARGS_NODES))}) is not None


@pytest.mark.gate(ARGS, limit="MAX_ARGS_BYTES", edge="below")
def test_args_under_the_byte_limit() -> None:
    assert T._plain_json_problem({"x": "a" * (MAX_ARGS_BYTES - 100)}) is None


@pytest.mark.gate(ARGS, limit="MAX_ARGS_BYTES", edge="at")
def test_args_whose_json_is_exactly_the_byte_limit() -> None:
    n = MAX_ARGS_BYTES - len(json.dumps({"x": ""}))  # the JSON around the string costs a few bytes: the total is what counts
    assert len(json.dumps({"x": "a" * n})) == MAX_ARGS_BYTES
    assert T._plain_json_problem({"x": "a" * n}) is None


@pytest.mark.gate(ARGS, limit="MAX_ARGS_BYTES", edge="above")
def test_args_over_the_byte_limit_are_refused() -> None:
    assert T._plain_json_problem({"x": "a" * (MAX_ARGS_BYTES + 1)}) is not None


@pytest.mark.gate(ARGS, edge="wrong_type")
@pytest.mark.parametrize("value", [None, "{}", [1], 7, b"{}", ("a",), {1: 2}, {"x": math.nan}, {"x": b"b"}])
def test_args_gate_refuses_what_is_not_plain_json(value: object) -> None:
    assert T._plain_json_problem(value) is not None


# ---- parse_config (the port limits) -----------------------------------------------------------------------------------


@pytest.mark.gate(CONFIG, limit="PORT_MIN", edge="below")
def test_config_port_below_the_minimum_is_refused() -> None:
    assert refused(parse_config, {"network": {"port": PORT_MIN - 1}})


@pytest.mark.gate(CONFIG, limit="PORT_MIN", edge="at")
def test_config_port_at_the_minimum() -> None:
    assert parse_config({"network": {"port": PORT_MIN}}).network.port == PORT_MIN


@pytest.mark.gate(CONFIG, limit="PORT_MIN", edge="above")
def test_config_port_above_the_minimum() -> None:
    assert parse_config({"network": {"port": PORT_MIN + 1}}).network.port == PORT_MIN + 1


@pytest.mark.gate(CONFIG, limit="PORT_MAX", edge="below")
def test_config_port_below_the_maximum() -> None:
    assert parse_config({"network": {"port": PORT_MAX - 1}}).network.port == PORT_MAX - 1


@pytest.mark.gate(CONFIG, limit="PORT_MAX", edge="at")
def test_config_port_at_the_maximum() -> None:
    assert parse_config({"network": {"port": PORT_MAX}}).network.port == PORT_MAX


@pytest.mark.gate(CONFIG, limit="PORT_MAX", edge="above")
def test_config_port_above_the_maximum_is_refused() -> None:
    assert refused(parse_config, {"network": {"port": PORT_MAX + 1}})


@pytest.mark.gate(CONFIG, edge="wrong_type")
@pytest.mark.parametrize("value", [None, 7, "x", [1], b"b"])
def test_config_refuses_a_table_that_is_not_a_table(value: object) -> None:
    with pytest.raises(ConfigError):
        parse_config(value)  # type: ignore[arg-type]


# ---- AudioChunk (sample rate and channels) ----------------------------------------------------------------------------


def chunk(rate: int = 16000, channels: int = 1) -> AudioChunk:
    return AudioChunk(stream_id=0, seq=0, sample_rate_hz=rate, channels=channels, data=b"\0\0" * channels, t_us=0)


@pytest.mark.gate(AUDIO, limit="AUDIO_SAMPLE_RATE_HZ_MIN", edge="below")
def test_audio_rate_below_the_minimum_is_refused() -> None:
    assert refused(chunk, AUDIO_SAMPLE_RATE_HZ_MIN - 1)


@pytest.mark.gate(AUDIO, limit="AUDIO_SAMPLE_RATE_HZ_MIN", edge="at")
def test_audio_rate_at_the_minimum() -> None:
    assert chunk(AUDIO_SAMPLE_RATE_HZ_MIN).sample_rate_hz == AUDIO_SAMPLE_RATE_HZ_MIN


@pytest.mark.gate(AUDIO, limit="AUDIO_SAMPLE_RATE_HZ_MIN", edge="above")
def test_audio_rate_above_the_minimum() -> None:
    assert chunk(AUDIO_SAMPLE_RATE_HZ_MIN + 1).sample_rate_hz == AUDIO_SAMPLE_RATE_HZ_MIN + 1


@pytest.mark.gate(AUDIO, limit="AUDIO_SAMPLE_RATE_HZ_MAX", edge="below")
def test_audio_rate_below_the_maximum() -> None:
    assert chunk(AUDIO_SAMPLE_RATE_HZ_MAX - 1).sample_rate_hz == AUDIO_SAMPLE_RATE_HZ_MAX - 1


@pytest.mark.gate(AUDIO, limit="AUDIO_SAMPLE_RATE_HZ_MAX", edge="at")
def test_audio_rate_at_the_maximum() -> None:
    assert chunk(AUDIO_SAMPLE_RATE_HZ_MAX).sample_rate_hz == AUDIO_SAMPLE_RATE_HZ_MAX


@pytest.mark.gate(AUDIO, limit="AUDIO_SAMPLE_RATE_HZ_MAX", edge="above")
def test_audio_rate_above_the_maximum_is_refused() -> None:
    assert refused(chunk, AUDIO_SAMPLE_RATE_HZ_MAX + 1)


@pytest.mark.gate(AUDIO, limit="AUDIO_CHANNELS_MAX", edge="below")
def test_audio_channels_below_the_maximum() -> None:
    assert chunk(channels=AUDIO_CHANNELS_MAX - 1).channels == AUDIO_CHANNELS_MAX - 1


@pytest.mark.gate(AUDIO, limit="AUDIO_CHANNELS_MAX", edge="at")
def test_audio_channels_at_the_maximum() -> None:
    assert chunk(channels=AUDIO_CHANNELS_MAX).channels == AUDIO_CHANNELS_MAX


@pytest.mark.gate(AUDIO, limit="AUDIO_CHANNELS_MAX", edge="above")
def test_audio_channels_above_the_maximum_are_refused() -> None:
    assert refused(chunk, channels=AUDIO_CHANNELS_MAX + 1)


@pytest.mark.gate(AUDIO, limit="MAX_BINARY_BYTES", edge="below")
def test_audio_data_below_the_wire_limit() -> None:
    assert len(AudioChunk(0, 0, 16000, 1, b"\0" * (MAX_BINARY_BYTES - 2)).data) == MAX_BINARY_BYTES - 2


@pytest.mark.gate(AUDIO, limit="MAX_BINARY_BYTES", edge="at")
def test_audio_data_at_the_wire_limit() -> None:
    assert len(AudioChunk(0, 0, 16000, 1, b"\0" * MAX_BINARY_BYTES).data) == MAX_BINARY_BYTES


@pytest.mark.gate(AUDIO, limit="MAX_BINARY_BYTES", edge="above")
def test_audio_data_over_the_wire_limit_is_refused() -> None:
    assert refused(AudioChunk, 0, 0, 16000, 1, b"\0" * (MAX_BINARY_BYTES + 2))


@pytest.mark.gate(AUDIO, edge="wrong_type")
@pytest.mark.parametrize("kwargs", [{"sample_rate_hz": "16000"}, {"sample_rate_hz": 16000.5}, {"channels": None}, {"data": "ab"}, {"seq": True}])
def test_audio_chunk_refuses_wrong_types(kwargs: dict[str, object]) -> None:
    base: dict[str, object] = dict(stream_id=0, seq=0, sample_rate_hz=16000, channels=1, data=b"\0\0")
    with pytest.raises(ValueError):
        AudioChunk(**{**base, **kwargs})  # type: ignore[arg-type]
