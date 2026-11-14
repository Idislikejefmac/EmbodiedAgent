"""Binary frame header (protocol/v0.md section 5)."""

import re
from pathlib import Path

import pytest

from brain.transport import protocol as p

DOC = Path(__file__).resolve().parent.parent / "protocol" / "v0.md"

# The same vector is in the spec and in the body's Kotlin unit test (BinaryFrameTest.kt).
GOLDEN_HEX = "00 04 00 00 00 00 00 07 00 00 01 1f 71 fb 04 cb 00 00 00 05 68 65 6c 6c 6f"


def test_header_is_20_bytes_big_endian():
    assert p.BINARY_HEADER.size == 20
    assert p.BINARY_HEADER.format.startswith(">")


def test_golden_vector_packs_and_unpacks():
    frame = p.pack_binary("echo", 7, 1234567890123, b"hello")
    assert frame.hex(" ") == GOLDEN_HEX
    f = p.unpack_binary(frame)
    assert (f.type, f.id, f.ts, f.data, f.flags) == ("echo", 7, 1234567890123, b"hello", 0)


def test_spec_shows_the_same_vector():
    assert GOLDEN_HEX in DOC.read_text(encoding="utf-8")


@pytest.mark.parametrize("mtype", sorted(p.BINARY_TYPE_CODES))
def test_round_trip_every_type_with_100kb(mtype):
    data = bytes(range(256)) * 400  # 102,400 bytes
    f = p.unpack_binary(p.pack_binary(mtype, p.MAX_ID, p.MAX_TS, data))
    assert (f.type, f.id, f.ts, f.data) == (mtype, p.MAX_ID, p.MAX_TS, data)


def test_negative_timestamps_are_invalid():
    """The header field is int64 for layout, but timestamps are 0 to 2^53-1 like the JSON (section 5.1)."""
    with pytest.raises(ValueError):
        p.pack_binary("echo", 1, -5, b"x")
    frame = p.BINARY_HEADER.pack(0, 4, 0, 1, -5, 1) + b"x"
    with pytest.raises(p.ProtocolError) as e:
        p.unpack_binary(frame)
    assert e.value.code == "bad_binary"


def test_empty_data_is_allowed():
    assert p.unpack_binary(p.pack_binary("audio_out", 1, 0, b"")).data == b""


@pytest.mark.parametrize("frame, code", [
    (b"\x00\x04\x00", "bad_binary"),                                         # shorter than header
    (p.pack_binary("echo", 1, 0, b"abc")[:-1], "bad_binary"),                # length mismatch
    (p.pack_binary("echo", 1, 0, b"abc") + b"x", "bad_binary"),              # trailing byte
    (b"\x01" + p.pack_binary("echo", 1, 0, b"abc")[1:], "unsupported_version"),
    (b"\x00\x09" + p.pack_binary("echo", 1, 0, b"abc")[2:], "bad_binary"),   # unknown type code
])
def test_malformed_frames_are_rejected(frame, code):
    with pytest.raises(p.ProtocolError) as e:
        p.unpack_binary(frame)
    assert e.value.code == code and e.value.fatal


def test_pair_check_catches_mismatch():
    msg = p.make_message("echo", 5, {"seq": 0, "text": "", "byte_length": 3, "crc32": p.crc32(b"abc")},
                         sender=p.BODY, ts=100)
    p.check_pair(msg, p.unpack_binary(p.pack_binary("echo", 5, 100, b"abc")), sender=p.BODY)
    for wrong in (p.pack_binary("echo", 6, 100, b"abc"), p.pack_binary("image", 5, 100, b"abc"),
                  p.pack_binary("echo", 5, 101, b"abc"), p.pack_binary("echo", 5, 100, b"abcd")):
        with pytest.raises(p.ProtocolError):
            p.check_pair(msg, p.unpack_binary(wrong), sender=p.BODY)
