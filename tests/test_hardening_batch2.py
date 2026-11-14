"""Hardening batch 2 (docs/testing/50 section 4.A): H-10, H-34, H-01 and H-33, H-09 and H-04, and edge tests
for named limits that had none (H-11, E5). The config gate (H-05) is in test_config_gate.py and the
limits home itself in test_limits.py. Each test here failed before its fix."""

import asyncio
import gzip
import io
import json
import logging
import os
import tarfile
import time
import tracemalloc

import pytest
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from brain.agent import tools as tools_mod
from brain.agent.tools import CONSOLE, ToolRegistry, _plain_json_problem, _show_keys
from brain.body.types import AudioChunk, Image, Pose
from brain.limits import (AUDIO_CHANNELS_MAX, AUDIO_IN_MAX_CHUNK_BYTES, AUDIO_SAMPLE_RATE_HZ_MAX,
                          AUDIO_SAMPLE_RATE_HZ_MIN, ARCHIVE_MAX_BYTES, ARCHIVE_MEMBERS_MAX, ARCHIVE_UNPACKED_MAX_BYTES,
                          ECHO_TEST_MAX_BYTES, ERROR_MESSAGE_MAX_CHARS, EVENT_MAX_BYTES, HANDLER_QUEUE,
                          HANDLER_QUEUE_MAX_BYTES, HANDSHAKE_TIMEOUT_S, LINK_TIMEOUT_S_MAX, LOG_KEY_MAX_CHARS,
                          LOG_PEER_ITEM_MAX_CHARS, LOG_PEER_KEYS_SHOWN, MAX_ARGS_BYTES, MAX_ARGS_NODES,
                          MAX_BINARY_BYTES, MAX_FRAME_BYTES, MAX_IMAGE_BYTES, MAX_KEYS_SHOWN, MAX_PEER_ERRORS_PER_MIN,
                          PASSPHRASE_MIN_CHARS, PEER_ECHO_MAX_PER_S, PEER_EXCESS_MAX_PER_MIN, PEER_HEARTBEAT_MAX_PER_S,
                          PEER_LOG_LINES_PER_MIN_MAX, PEER_MSG_BURST, PEER_MSG_MAX_PER_S, QUAT_NORM_TOL,
                          SCRYPT_LOG_N, SEND_TIMEOUT_S, BINARY_PAIR_TIMEOUT_S, REQUEST_TIMEOUT_S, TOOL_DEFAULT_TIMEOUT_S,
                          TOOL_NAME_MAX_CHARS, TOOL_TIMEOUT_MAX_S, WIRE_IMAGE_EDGE_MAX_PX, WS_RECV_QUEUE_FRAMES)
from brain.logsafe import short_items
from brain.transport import protocol as p
from brain.transport import server as server_mod
from brain.transport.flood import FloodGuard, HandlerBudget, PeerLogLimit, size_problem
from brain.transport.link import Link
from brain.transport.server import BrainServer
from fakes import TOKEN, body, collect, error_codes, raw, raw_handshake, run, the_link, wait_for, with_server
from tools import migrate

PASS = "correct horse battery staple"


# ---- H-10: the argument walk stops as soon as the size is passed (T-P0-06-061) ---------------------------------


def test_h10_a_string_heavy_argument_is_refused_without_walking_131_mb():
    """Before: up to MAX_ARGS_NODES strings of MAX_ARGS_BYTES each were looked at and then dumped to JSON
    (about 131 MB) before the size check. Now the walk stops at the second string."""
    big = "x" * MAX_ARGS_BYTES
    args = {f"k{i}": big for i in range(MAX_ARGS_NODES - 1)}  # one string object, referenced 1,999 times
    tracemalloc.start()
    try:
        t0 = time.perf_counter()
        problem = _plain_json_problem(args)
        elapsed = time.perf_counter() - t0
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert problem == f"arguments are larger than {MAX_ARGS_BYTES} bytes"
    assert peak < 1024 * 1024, peak  # was about 131 MB of JSON text
    assert elapsed < 1.0, elapsed


def test_h10_argument_names_are_counted_during_the_walk():
    assert _plain_json_problem({"k" * (MAX_ARGS_BYTES + 1): 1}) == f"arguments are larger than {MAX_ARGS_BYTES} bytes"
    many_long_names = {f"{i}" + "n" * 1000: 1 for i in range(100)}  # 100 KB of names, tiny values
    assert "larger than" in (_plain_json_problem(many_long_names) or "")


def test_h10_size_edges_match_json():
    at = {"t": "x" * (MAX_ARGS_BYTES - 9)}  # json.dumps gives exactly MAX_ARGS_BYTES
    assert len(json.dumps(at)) == MAX_ARGS_BYTES and _plain_json_problem(at) is None
    over = {"t": "x" * (MAX_ARGS_BYTES - 8)}
    assert _plain_json_problem(over) == f"arguments are larger than {MAX_ARGS_BYTES} bytes"


def test_h10_registry_refuses_the_hostile_argument_quickly():
    registry = ToolRegistry()
    registry.register("get_body_status", lambda args, caller: {"connected": False})
    big = "y" * MAX_ARGS_BYTES

    async def go():
        t0 = time.perf_counter()
        r = await registry.call("get_body_status", {f"k{i}": big for i in range(MAX_ARGS_NODES - 1)}, CONSOLE)
        return r, time.perf_counter() - t0
    r, elapsed = asyncio.run(go())
    assert (r.status, r.code) == ("refused", "invalid_arguments") and elapsed < 1.0


# ---- H-34: Image and AudioChunk enforce the wire's upper bounds (T-P0-06-031) ---------------------------------


@pytest.mark.parametrize("edge, ok", [(0, False), (1, True), (WIRE_IMAGE_EDGE_MAX_PX, True), (WIRE_IMAGE_EDGE_MAX_PX + 1, False)])
def test_h34_image_edge_bounds(edge, ok):
    for kw in ({"width_px": edge, "height_px": 1}, {"width_px": 1, "height_px": edge}):
        if ok:
            Image("i-1", "jpeg", data=b"", **kw)
        else:
            with pytest.raises(ValueError, match="_px must be an integer from 1 to"):
                Image("i-1", "jpeg", data=b"", **kw)


@pytest.mark.parametrize("rate, ok", [(AUDIO_SAMPLE_RATE_HZ_MIN - 1, False), (AUDIO_SAMPLE_RATE_HZ_MIN, True),
                                      (AUDIO_SAMPLE_RATE_HZ_MAX, True), (AUDIO_SAMPLE_RATE_HZ_MAX + 1, False),
                                      (2**64, False), (True, False)])
def test_h34_audio_sample_rate_bounds(rate, ok):
    """The recorded chaos defect (audio_chunk.sample_rate_hz.one_over_maximum) is this edge."""
    make = lambda: AudioChunk(0, 0, rate, 1, b"\x00\x00")  # noqa: E731
    if ok:
        assert make().sample_rate_hz == rate
    else:
        with pytest.raises(ValueError, match="sample_rate_hz"):
            make()


@pytest.mark.parametrize("channels, ok", [(0, False), (1, True), (AUDIO_CHANNELS_MAX, True), (AUDIO_CHANNELS_MAX + 1, False)])
def test_h34_audio_channel_bounds(channels, ok):
    data = b"\x00\x00" * max(channels, 1)
    if ok:
        AudioChunk(0, 0, 16_000, channels, data)
    else:
        with pytest.raises(ValueError, match="channels"):
            AudioChunk(0, 0, 16_000, channels, data)


def test_h34_data_length_caps():
    Image("i-1", "jpeg", 1, 1, bytes(MAX_BINARY_BYTES))
    AudioChunk(0, 0, 16_000, 1, bytes(MAX_BINARY_BYTES))
    with pytest.raises(ValueError, match="at most"):
        Image("i-1", "jpeg", 1, 1, bytes(MAX_BINARY_BYTES + 1))
    with pytest.raises(ValueError, match="at most"):
        AudioChunk(0, 0, 16_000, 2, bytes(MAX_BINARY_BYTES + 4))
    with pytest.raises(ValueError, match="bytes"):
        Image("i-1", "jpeg", 1, 1, "not bytes")  # type: ignore[arg-type]


# ---- H-01: restore refuses before spending memory; H-33: restore never silently overwrites ---------------------


def make_src(root):
    (root / "data" / "people" / "ann").mkdir(parents=True)
    (root / "data" / "people" / "ann" / "face.bin").write_bytes(b"ann's face")
    (root / "data" / "other.txt").write_text("keep me", encoding="utf-8")
    (root / ".env").write_text("ANTHROPIC_API_KEY=abc\n", encoding="utf-8")
    return root


@pytest.fixture
def archive(tmp_path):
    out = tmp_path / "a.eamig"
    migrate.export_data(make_src(tmp_path / "src"), out, PASS)
    return out


def authentic(tmp_path, payload: bytes, name="odd.eamig"):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, nonce = os.urandom(16), os.urandom(12)
    header = migrate.MAGIC + bytes([SCRYPT_LOG_N]) + salt + nonce
    path = tmp_path / name
    path.write_bytes(header + AESGCM(migrate._key(PASS, salt, SCRYPT_LOG_N)).encrypt(nonce, payload, header))
    return path


def tar_of(entries) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for info, data in entries:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def file_info(name, size):
    info = tarfile.TarInfo(name)
    info.size = size
    return info


@pytest.mark.parametrize("log_n", [0, 14, SCRYPT_LOG_N - 1, SCRYPT_LOG_N + 1, 22, 255])
def test_h01_any_other_key_cost_is_refused_before_scrypt(tmp_path, archive, monkeypatch, log_n):
    """T-P0-04-024: a crafted header could ask scrypt for about 4 GiB (log_n 22) before authentication."""
    blob = bytearray(archive.read_bytes())
    blob[len(migrate.MAGIC)] = log_n
    archive.write_bytes(bytes(blob))
    monkeypatch.setattr(migrate, "_key", lambda *a: pytest.fail("scrypt ran on an unauthenticated header"))
    with pytest.raises(ValueError, match="Unsupported archive header"):
        migrate.restore_data(archive, tmp_path / "dest", PASS)
    with pytest.raises(ValueError, match="Unsupported archive header"):
        migrate.verify_archive(archive, PASS)
    assert not (tmp_path / "dest").exists()


def test_h01_an_archive_over_the_limit_is_refused_before_it_is_read(tmp_path, archive, monkeypatch):
    monkeypatch.setattr(migrate, "ARCHIVE_MAX_BYTES", archive.stat().st_size - 1)
    real_open = open
    monkeypatch.setattr("builtins.open", lambda f, *a, **k: pytest.fail("read") if str(f) == str(archive)
                        else real_open(f, *a, **k))
    with pytest.raises(ValueError, match="larger than"):
        migrate.restore_data(archive, tmp_path / "dest", PASS)
    assert ARCHIVE_MAX_BYTES == 8 * 1024**3  # the real limit is far above any test archive


@pytest.mark.parametrize("damage", ["bit_flip_body", "bit_flip_header_salt", "truncated_tag", "truncated_header",
                                    "empty", "magic_only"])
def test_h01_damaged_archives_are_refused(tmp_path, archive, damage):
    blob = archive.read_bytes()
    if damage == "bit_flip_body":
        blob = blob[:-20] + bytes([blob[-20] ^ 1]) + blob[-19:]
    elif damage == "bit_flip_header_salt":
        blob = blob[:10] + bytes([blob[10] ^ 0x80]) + blob[11:]
    elif damage == "truncated_tag":
        blob = blob[:-5]
    elif damage == "truncated_header":
        blob = blob[: migrate.HEADER_LEN - 3]
    elif damage == "empty":
        blob = b""
    else:
        blob = migrate.MAGIC
    archive.write_bytes(blob)
    with pytest.raises(ValueError):
        migrate.restore_data(archive, tmp_path / "dest", PASS)
    assert not (tmp_path / "dest").exists()


def test_h01_wrong_passphrase_is_refused(tmp_path, archive):
    with pytest.raises(ValueError, match="Wrong passphrase"):
        migrate.verify_archive(archive, PASS + "x")


def test_h01_a_gzip_bomb_stops_at_the_unpacked_limit_without_writing(tmp_path, monkeypatch):
    zeros = bytes(20 * 1024 * 1024)  # 20 MiB of zeros compresses to about 20 KB
    bomb = authentic(tmp_path, tar_of([(file_info("data/zeros.bin", len(zeros)), zeros)]))
    assert bomb.stat().st_size < 200_000
    monkeypatch.setattr(migrate, "ARCHIVE_UNPACKED_MAX_BYTES", 10 * 1024 * 1024)
    with pytest.raises(ValueError, match="unpacks to more than"):
        migrate.restore_data(bomb, tmp_path / "dest", PASS)
    assert not (tmp_path / "dest").exists()
    assert ARCHIVE_UNPACKED_MAX_BYTES == 32 * 1024**3


def test_h01_member_count_is_bounded(tmp_path, monkeypatch):
    many = authentic(tmp_path, tar_of([(file_info(f"data/f{i}", 1), b"x") for i in range(5)]))
    monkeypatch.setattr(migrate, "ARCHIVE_MEMBERS_MAX", 4)
    with pytest.raises(ValueError, match="more than 4 entries"):
        migrate.verify_archive(many, PASS)
    monkeypatch.setattr(migrate, "ARCHIVE_MEMBERS_MAX", 5)
    assert len(migrate.verify_archive(many, PASS)) == 5
    assert ARCHIVE_MEMBERS_MAX >= 100_000


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "chardev", "absolute", "parent", "drive"])
def test_h01_links_special_entries_and_escapes_are_refused(tmp_path, kind):
    info = file_info({"absolute": "/etc/x", "parent": "../x", "drive": "C:/x"}.get(kind, "data/x"), 0)
    info.type = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE, "fifo": tarfile.FIFOTYPE,
                 "chardev": tarfile.CHRTYPE}.get(kind, tarfile.REGTYPE)
    if kind in ("symlink", "hardlink"):
        info.linkname = "../../.env"
    evil = authentic(tmp_path, tar_of([(file_info("data/ok", 1), b"x"), (info, b"" if info.isreg() else None)]))
    with pytest.raises(ValueError, match="Unsafe path|Unsupported entry"):
        migrate.restore_data(evil, tmp_path / "dest", PASS)
    assert not (tmp_path / "dest" / "data" / "ok").exists()  # every check passes before anything is written


def test_h33_restore_refuses_a_non_empty_destination_and_leaves_it_alone(tmp_path, archive):
    dest = tmp_path / "dest"
    (dest / "data").mkdir(parents=True)
    (dest / "data" / "other.txt").write_text("newer", encoding="utf-8")
    with pytest.raises(ValueError, match="not empty"):
        migrate.restore_data(archive, dest, PASS)
    assert (dest / "data" / "other.txt").read_text(encoding="utf-8") == "newer"


def test_h33_force_lists_what_it_replaces_before_writing(tmp_path, archive):
    dest = tmp_path / "dest"
    (dest / "data").mkdir(parents=True)
    (dest / "data" / "other.txt").write_text("newer", encoding="utf-8")
    seen = []

    def on_replace(name):
        seen.append((name, (dest / name).read_text(encoding="utf-8")))  # still the old content: nothing written yet
    names = migrate.restore_data(archive, dest, PASS, force=True, on_replace=on_replace)
    assert seen == [("data/other.txt", "newer")] and set(names) == {"data", ".env"}
    assert (dest / "data" / "other.txt").read_text(encoding="utf-8") == "keep me"


def test_h33_an_empty_destination_folder_is_fine(tmp_path, archive):
    (tmp_path / "dest").mkdir()
    assert set(migrate.restore_data(archive, tmp_path / "dest", PASS)) == {"data", ".env"}


def test_h33_the_deletion_log_hook_keeps_deleted_data_deleted(tmp_path, archive):
    """The hook point (DELETION_LOG; TODO P3-01, P4-08): entries the destination's log lists are not
    re-created, and the destination's log is kept (the archive's copy is merged into it)."""
    assert migrate.DELETION_LOG == "data/deletions.log" and callable(migrate.deleted_by_record)
    dest = tmp_path / "dest"
    (dest / "data").mkdir(parents=True)
    (dest / migrate.DELETION_LOG).write_text("# deleted after the backup\ndata/people/ann\n", encoding="utf-8")
    names = migrate.restore_data(archive, dest, PASS, force=True)
    assert not (dest / "data" / "people" / "ann").exists() and (dest / "data" / "other.txt").exists()
    assert "data" in names and "data/people/ann" in (dest / migrate.DELETION_LOG).read_text(encoding="utf-8")
    assert migrate.deleted_by_record("data/people/ann/face.bin", {"data/people/ann"})
    assert not migrate.deleted_by_record("data/people/anna/face.bin", {"data/people/ann"})


def test_h33_the_archive_deletion_log_is_merged_not_overwritten(tmp_path):
    src = make_src(tmp_path / "src")
    (src / migrate.DELETION_LOG).write_text("data/people/bob\n", encoding="utf-8")
    out = tmp_path / "a.eamig"
    migrate.export_data(src, out, PASS)
    dest = tmp_path / "dest"
    (dest / "data").mkdir(parents=True)
    (dest / migrate.DELETION_LOG).write_text("data/people/ann\n", encoding="utf-8")
    migrate.restore_data(out, dest, PASS, force=True)
    log_text = (dest / migrate.DELETION_LOG).read_text(encoding="utf-8")
    assert "data/people/ann" in log_text and "data/people/bob" in log_text


def test_h01_verify_lists_entries_and_extracts_nothing(tmp_path, archive, monkeypatch, capsys):
    entries = migrate.verify_archive(archive, PASS)
    assert ("data/people/ann/face.bin", 10, False) in entries and ("data", 0, True) in entries
    monkeypatch.setattr(migrate.getpass, "getpass", lambda _="": PASS)
    monkeypatch.chdir(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    assert migrate.main(["verify", str(archive)]) == 0
    out = capsys.readouterr().out
    assert "data/people/ann/face.bin  10 bytes" in out and "nothing extracted" in out
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_h33_cli_restore_needs_force_and_says_what_it_replaces(tmp_path, archive, monkeypatch, capsys):
    monkeypatch.setattr(migrate.getpass, "getpass", lambda _="": PASS)
    dest = tmp_path / "dest"
    (dest / "data").mkdir(parents=True)
    (dest / "data" / "other.txt").write_text("newer", encoding="utf-8")
    assert migrate.main(["restore", str(archive), "--dest", str(dest)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error:") and "--force" in err and "Traceback" not in err
    assert migrate.main(["restore", str(archive), "--dest", str(dest), "--force"]) == 0
    out = capsys.readouterr().out
    assert out.index("Replacing: data/other.txt") < out.index("Restored into")


def test_passphrase_edge():
    with pytest.raises(ValueError, match=str(PASSPHRASE_MIN_CHARS)):
        migrate.export_data(migrate.Path("."), migrate.Path("x.eamig"), "p" * (PASSPHRASE_MIN_CHARS - 1))


# ---- H-09 and H-04: floods of valid frames and the handler queue (T-P0-05-040 to 042, T-P0-05-083) -------------


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_flood_guard_buckets_per_type_and_sustained_excess():
    clock = FakeClock()
    g = FloodGuard(clock=clock)
    assert all(g.admit("event") for _ in range(PEER_MSG_BURST)) and not g.admit("event")
    assert all(g.admit("heartbeat") for _ in range(PEER_HEARTBEAT_MAX_PER_S)) and not g.admit("heartbeat")
    assert all(g.admit("echo") for _ in range(PEER_ECHO_MAX_PER_S)) and not g.admit("echo")
    clock.t += 1.5 / PEER_MSG_MAX_PER_S  # one and a half tokens back
    assert g.admit("event") and not g.admit("event")
    assert not g.flooding
    for _ in range(PEER_EXCESS_MAX_PER_MIN - 3):
        g.admit("pose")  # first PEER_MSG_BURST pass, the rest are excess
    assert not g.flooding
    for _ in range(PEER_MSG_BURST + 1):
        g.admit("pose")
    assert g.flooding and g.limited["pose"] > 0 and g.limited["event"] == 2


def test_handler_budget_counts_messages_and_bytes():
    b = HandlerBudget(max_messages=3, max_bytes=100)
    assert b.reserve(60) and b.reserve(40) and not b.reserve(1)
    b.release(60)
    assert b.reserve(10) and b.reserve(10) and not b.reserve(10)  # three messages now
    assert HandlerBudget().max_bytes == HANDLER_QUEUE_MAX_BYTES and HandlerBudget().max_messages == HANDLER_QUEUE


def test_size_caps_by_type():
    assert size_problem("event", EVENT_MAX_BYTES, 0) is None and size_problem("event", EVENT_MAX_BYTES + 1, 0)
    assert size_problem("echo", 100, ECHO_TEST_MAX_BYTES) is None and size_problem("echo", 100, ECHO_TEST_MAX_BYTES + 1)
    assert size_problem("image", 100, MAX_IMAGE_BYTES) is None and size_problem("image", 100, MAX_IMAGE_BYTES + 1)
    assert size_problem("audio_in", 100, AUDIO_IN_MAX_CHUNK_BYTES) is None
    assert size_problem("audio_in", 100, AUDIO_IN_MAX_CHUNK_BYTES + 1)
    assert size_problem("pose", 10**6, 0) is None


def test_peer_log_limit_caps_lines_per_minute_and_says_how_many_it_suppressed(caplog):
    clock = FakeClock()
    logger = logging.getLogger("brain.transport.test-limit")
    limit = PeerLogLimit(logger, clock=clock)
    logger.addFilter(limit)
    try:
        caplog.set_level(logging.INFO, logger=logger.name)
        for i in range(200):
            logger.info("line %d", i)
        assert len(caplog.records) == PEER_LOG_LINES_PER_MIN_MAX - 1 and limit.suppressed == 200 - len(caplog.records)
        clock.t += 61
        logger.info("next minute")
        msgs = [r.getMessage() for r in caplog.records]
        assert msgs[-2].startswith(f"{200 - (PEER_LOG_LINES_PER_MIN_MAX - 1)} log lines from this connection "
                                   "suppressed") and msgs[-1] == "next minute"
        logger.info("summary", extra={"log_limit_exempt": True})
        assert caplog.records[-1].getMessage() == "summary"
    finally:
        logger.removeFilter(limit)


def _hb(i):
    return raw("heartbeat", i, {})


def test_h09_a_heartbeat_flood_is_answered_busy_and_the_link_stays_up():
    async def go(server, url):
        async with ws_connect(url) as ws:
            await raw_handshake(ws)
            for i in range(3, 3 + 40):
                await ws.send(_hb(i))
            got = await collect(ws, 0.5)
            codes = error_codes(got)
            replies = [m for m in got if m["type"] == "heartbeat" and m["reply_to"] is not None]
            assert ws.close_code is None, "a heartbeat flood must not close the link"
            assert codes.count("busy") >= 40 - PEER_HEARTBEAT_MAX_PER_S - 2 and len(replies) >= PEER_HEARTBEAT_MAX_PER_S
            assert all(not m["payload"]["fatal"] for m in got if m["type"] == "error")
            await asyncio.sleep(1.0)  # the bucket refills: a heartbeat is answered again
            await ws.send(_hb(100))
            later = await collect(ws, 0.5)
            assert any(m["type"] == "heartbeat" and m["reply_to"] == 100 for m in later)
    run(with_server(go))


def test_h09_events_over_the_rate_are_dropped_and_counted_not_handled(caplog):
    handled = []

    async def on_message(link, r):
        handled.append(r.msg["id"])

    async def go(server, url):
        async with ws_connect(url) as ws:
            await raw_handshake(ws)
            for i in range(3, 3 + 600):
                await ws.send(raw("event", i, {"name": "tick", "data": {"n": i}}))
            got = await collect(ws, 0.5)
            assert error_codes(got) == [] and ws.close_code is None  # dropped quietly, not answered
        await wait_for(lambda: not server.links)
    caplog.set_level(logging.INFO)
    run(with_server(go, on_message=on_message))
    # PEER_MSG_BURST at once, then PEER_MSG_MAX_PER_S a second while the 600 arrive; the rest is dropped
    assert PEER_MSG_BURST <= len(handled) <= 600 - 200, len(handled)
    end = [r.getMessage() for r in caplog.records if r.getMessage().startswith("session end")]
    assert end and "rate_limited={'event': " in end[0]


def test_h09_sustained_flooding_closes_the_link_with_1002():
    async def go(server, url):
        async with ws_connect(url) as ws:
            await raw_handshake(ws)
            # Far more than PEER_MSG_BURST + PEER_EXCESS_MAX_PER_MIN, so the refill while sending never matters.
            for i in range(3, 3 + 3 * (PEER_MSG_BURST + PEER_EXCESS_MAX_PER_MIN)):
                try:
                    await ws.send(raw("pose", i, {"frame_id": "w-1", "position_m": [0, 0, 0],
                                                  "orientation_xyzw": [0, 0, 0, 1], "tracking": "ok"}))
                except ConnectionClosed:  # the brain closed it: that is the point
                    break
            got = await collect(ws, 2.0)
            fatal = [m for m in got if m["type"] == "error" and m["payload"]["fatal"]]
            assert fatal and fatal[-1]["payload"]["code"] == "busy", got[-3:]
            assert ws.close_code == p.CLOSE_PROTOCOL_ERROR
        await wait_for(lambda: not server.links)
        async with body(url) as c:  # the brain itself is fine: a body connects again
            assert (await c.echo_test(count=2, size=10)).ok
    run(with_server(go))


def test_h09_flood_log_lines_are_capped_per_connection(caplog):
    async def go(server, url):
        async with ws_connect(url) as ws:
            await raw_handshake(ws)
            for i in range(3, 3 + 400):
                await ws.send(_hb(i))  # 390 busy refusals, each a WARNING before the fix
            await collect(ws, 0.5)
        await wait_for(lambda: not server.links)
    caplog.set_level(logging.INFO)
    run(with_server(go))
    session = [r for r in caplog.records if r.name.startswith("brain.transport.s-")]
    exempt = [r for r in session if getattr(r, "log_limit_exempt", False)]
    assert len(session) - len(exempt) <= PEER_LOG_LINES_PER_MIN_MAX - 1 and len(exempt) <= 3, len(session)
    assert any("suppressed in the last minute" in r.getMessage() for r in exempt)


def test_h04_oversize_payloads_by_type_are_refused_not_fatal():
    async def go(server, url):
        async with body(url) as c:
            blob = bytes(ECHO_TEST_MAX_BYTES + 1)
            await c.link.send("echo", {"seq": 0, "text": "", "byte_length": len(blob), "crc32": p.crc32(blob)}, data=blob)
            r = await c.next_message()
            assert (r.type, r.msg["payload"]["code"], r.msg["payload"]["fatal"]) == ("error", "too_large", False)
            assert (await c.echo_test(count=1, size=ECHO_TEST_MAX_BYTES)).ok  # at the limit: fine
            await c.link.send("event", {"name": "big", "data": {"s": "x" * EVENT_MAX_BYTES}})
            r = await c.next_message()
            assert r.msg["payload"]["code"] == "too_large"
            img = bytes(MAX_IMAGE_BYTES + 1)
            await c.link.send("image", {"encoding": "jpeg", "width": 1, "height": 1, "byte_length": len(img)}, data=img)
            assert (await c.next_message()).msg["payload"]["code"] == "too_large"
            chunk = bytes(AUDIO_IN_MAX_CHUNK_BYTES + 2)
            await c.link.send("audio_in", {"stream_id": 1, "seq": 0, "encoding": "pcm_s16le", "sample_rate": 16000,
                                           "channels": 1, "byte_length": len(chunk)}, data=chunk)
            assert (await c.next_message()).msg["payload"]["code"] == "too_large"
            assert (await c.echo_test(count=2, size=10)).ok  # still usable
    run(with_server(go))


def test_h04_the_handler_queue_is_bounded_in_bytes_with_a_slow_handler():
    """T-P0-05-083: a handler that does not keep up. Before, 256 queued messages of up to 8 MiB each (about
    2 GiB); now the queue stops at handler_queue_max_bytes and the body gets `busy`; memory stays bounded."""
    release = asyncio.Event()
    handled = []

    async def slow(link, r):
        await release.wait()
        handled.append(r.type)

    budget = 4 * 1024 * 1024
    img = bytes(MAX_IMAGE_BYTES - 1000)  # one object, sent 30 times: about 60 MiB in total

    async def go(server, url):
        async with body(url) as c:
            tracemalloc.start()
            try:
                start, _ = tracemalloc.get_traced_memory()
                for _ in range(30):
                    await c.link.send("image", {"encoding": "jpeg", "width": 1, "height": 1, "byte_length": len(img)},
                                      data=img)
                await asyncio.sleep(0.5)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            busy = []
            while not c.inbox.empty():
                m = c.inbox.get_nowait()
                busy += [m.msg["payload"]["code"]] if m.type == "error" else []
            assert busy and set(busy) == {"busy"}, busy
            assert peak - start < budget + 4 * MAX_FRAME_BYTES + 16 * 1024 * 1024, (peak - start) / 2**20
            release.set()
            await asyncio.sleep(0.2)
            assert 1 <= len(handled) <= budget // len(img) + 1
            assert (await c.echo_test(count=2, size=10)).ok
    run(with_server(go, on_message=slow, handler_queue_max_bytes=budget))


def test_h04_the_handler_queue_still_stops_at_handler_queue_messages():
    release = asyncio.Event()

    async def slow(link, r):
        await release.wait()

    async def go(server, url):
        async with ws_connect(url) as ws:
            await raw_handshake(ws)
            for i in range(3, 3 + HANDLER_QUEUE + 20):
                await ws.send(raw("event", i, {"name": "tick", "data": {}}))
            codes = error_codes(await collect(ws, 0.5))
            assert 18 <= codes.count("busy") <= 20, codes.count("busy")  # one is being handled, 256 wait
            release.set()
    run(with_server(go, on_message=slow, peer_msg_max_per_s=10_000, peer_msg_burst=10_000))


def test_h09_a_flood_of_one_type_does_not_starve_the_body():
    """Heartbeats keep flowing (liveness holds) and the brain's own requests are answered during an event flood."""
    async def go(server, url):
        async with body(url, heartbeat_s=0.2, liveness_s=1.0) as c:
            link = the_link(server)
            flood = asyncio.create_task(_flood_events(c.link, 600))
            ack = await link.request("command", {"name": "get_status", "args": {}}, expect="ack", timeout=5)
            await flood
            await asyncio.sleep(1.5)  # longer than liveness: heartbeats were not starved
            assert ack.msg["payload"]["status"] == "ok" and link.closed_reason is None
            assert (await c.echo_test(count=3, size=1000)).ok
    run(with_server(go, heartbeat_s=0.2, liveness_s=1.0))


async def _flood_events(link, n):
    for i in range(n):
        await link.send("event", {"name": "tick", "data": {"n": i}})
        if i % 50 == 0:
            await asyncio.sleep(0)


def test_server_flood_knobs_are_range_checked():
    for bad in (0, -1, float("nan"), True, "10", 10**9):
        with pytest.raises(ValueError):
            BrainServer(token=TOKEN, peer_msg_max_per_s=bad)
    for bad in (0, HANDLER_QUEUE_MAX_BYTES + 1):
        with pytest.raises(ValueError):
            BrainServer(token=TOKEN, handler_queue_max_bytes=bad)
    with pytest.raises(TypeError):
        BrainServer(token=TOKEN, handler_queue_max_bytes=1.5)


# ---- named limits that had no test (H-11, E5): one edge each --------------------------------------------------


def test_transport_timeouts_default_to_their_limits_and_are_bounded():
    s = BrainServer(token=TOKEN)
    assert (s.hello_timeout_s, s.send_timeout_s) == (HANDSHAKE_TIMEOUT_S, SEND_TIMEOUT_S)
    link = Link(None, p.BRAIN)
    assert (link.binary_timeout_s, link.send_timeout_s, link.max_peer_errors_per_min) == (
        BINARY_PAIR_TIMEOUT_S, SEND_TIMEOUT_S, MAX_PEER_ERRORS_PER_MIN)
    assert Link.request.__kwdefaults__["timeout"] == REQUEST_TIMEOUT_S
    assert p.check_timeout("t", LINK_TIMEOUT_S_MAX) == LINK_TIMEOUT_S_MAX
    with pytest.raises(ValueError):
        p.check_timeout("t", LINK_TIMEOUT_S_MAX + 1)
    assert server_mod.MAX_FRAME_BYTES == MAX_FRAME_BYTES == MAX_BINARY_BYTES + p.BINARY_HEADER.size
    assert WS_RECV_QUEUE_FRAMES * MAX_FRAME_BYTES <= 40 * 1024 * 1024  # the reviewed websockets buffer (H-04)


def test_error_message_is_cut_at_its_limit():
    assert len(p.ProtocolError("internal", "x" * ERROR_MESSAGE_MAX_CHARS).message) == ERROR_MESSAGE_MAX_CHARS
    assert len(p.ProtocolError("internal", "x" * (ERROR_MESSAGE_MAX_CHARS + 1)).message) == ERROR_MESSAGE_MAX_CHARS


def test_peer_log_items_are_cut_at_their_limits():
    text = short_items(["n" * (LOG_PEER_ITEM_MAX_CHARS + 1)] * (LOG_PEER_KEYS_SHOWN + 1), LOG_PEER_KEYS_SHOWN + 1)
    assert text.count("...") == LOG_PEER_KEYS_SHOWN and text.endswith(" and 1 more")
    assert "n" * (LOG_PEER_ITEM_MAX_CHARS + 1) not in text


def test_quaternion_norm_tolerance_edge():
    Pose("w-1", (0, 0, 0), (0, 0, 0, 1 + QUAT_NORM_TOL * 0.9), "ok", 0)
    with pytest.raises(ValueError, match="unit quaternion"):
        Pose("w-1", (0, 0, 0), (0, 0, 0, 1 + QUAT_NORM_TOL * 1.1), "ok", 0)


def test_tool_registry_limits():
    assert _show_keys(["k" * (LOG_KEY_MAX_CHARS + 5)] * (MAX_KEYS_SHOWN + 2), MAX_KEYS_SHOWN + 2).endswith(" and 2 more")
    assert ("k" * (LOG_KEY_MAX_CHARS + 1)) not in _show_keys(["k" * (LOG_KEY_MAX_CHARS + 5)], 1)
    registry = ToolRegistry()
    assert registry.timeout_s == TOOL_DEFAULT_TIMEOUT_S
    r = asyncio.run(registry.call("z" * (TOOL_NAME_MAX_CHARS + 30), {}, CONSOLE))
    assert r.code == "unknown_tool" and len(r.tool) == TOOL_NAME_MAX_CHARS
    raw_cat = json.loads(tools_mod.CATALOGUE_PATH.read_text(encoding="utf-8"))
    raw_cat["tools"][0]["timeout_s"] = TOOL_TIMEOUT_MAX_S
    assert not [x for x in tools_mod.catalogue_problems(raw_cat) if "timeout_s" in x]
    raw_cat["tools"][0]["timeout_s"] = TOOL_TIMEOUT_MAX_S + 0.5
    assert [x for x in tools_mod.catalogue_problems(raw_cat) if "timeout_s" in x]
