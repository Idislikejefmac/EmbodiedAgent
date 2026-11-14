"""Card X-02 steps 4, 5 and 8: the health panel (each source forced into each state), the viewer (reads only
event files and health.json), and the persisted state file (atomic, checked, quarantined when bad)."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from brain.limits import (OBS_DISK_LOW_BYTES, OBS_DISK_MIN_BYTES, OBS_HEALTH_FILE_MAX_BYTES, OBS_HEALTH_INTERVAL_S,
                          OBS_HEALTH_STALE_S, OBS_VIEW_LINE_MAX_BYTES, OBS_VIEW_POLL_S, STATE_FILE_MAX_BYTES,
                          STATE_PENDING_MAX, STATE_QUARANTINE_KEPT)
from brain.obs import health as H
from brain.obs.events import correlate
from brain.obs.health import HealthBoard, format_panel, read_snapshot, write_snapshot
from brain.obs.pipeline import EventLog, EventWriter
from brain.obs.runtime import ObsRuntime
from brain.obs.state import BrainState, StateStore
from brain.obs.viewer import Filter, Follower, format_event, parse_since, read_events, watch


def board_after(*events: tuple[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    b = HealthBoard()
    for etype, f in events:
        b.on_event({"type": etype, "f": f, "msg": "m", "session": "s-1"})
    return b.snapshot()["sources"]


# ---- step 5: every source can be forced into each state --------------------------------------------------------

CASES = [
    ("body_link", "up", [("link.up", {"software": "body/1"})]),
    ("body_link", "down", [("link.up", {}), ("link.down", {"active": True, "reason": "liveness"})]),
    ("body_link", "down", [("body.link", {"connected": False, "body": "sim"})]),
    ("body_link", "unknown", []),
    ("camera", "up", [("body.capabilities", {"capabilities": ["image"], "connected": True})]),
    ("camera", "down", [("body.capabilities", {"capabilities": ["pose"], "connected": True})]),
    ("camera", "down", [("body.capabilities", {"capabilities": [], "connected": False})]),
    ("camera", "unknown", []),
    ("microphone", "up", [("body.capabilities", {"capabilities": ["audio_in"]}), ("body.audio", {"seq": 0})]),
    ("microphone", "down", [("body.capabilities", {"capabilities": [], "connected": False})]),
    ("speaker", "up", [("body.capabilities", {"capabilities": ["speak_text"]})]),
    ("speaker", "down", [("body.capabilities", {"capabilities": ["image"]})]),
    ("clock", "up", [("clock.estimate", {"offset_ms": 1.0, "best_rtt_ms": 2.0})]),
    ("clock", "degraded", [("clock.estimate", {}), ("clock.reset", {})]),
    ("clock", "unknown", [("clock.estimate", {}), ("link.down", {"active": True})]),
    ("battery", "up", [("body.status", {"connected": True, "battery_pct": 80.0})]),
    ("battery", "degraded", [("body.status", {"connected": True, "battery_pct": 10.0})]),
    ("battery", "down", [("body.status", {"connected": True, "battery_pct": 2.0})]),
    ("battery", "degraded", [("body.status", {"connected": True, "stale": True})]),
    ("battery", "unknown", [("body.status", {"connected": False})]),
    ("temperature", "up", [("body.status", {"connected": True, "temperature_c": 30.0})]),
    ("temperature", "degraded", [("body.status", {"connected": True, "temperature_c": 50.0})]),
    ("temperature", "down", [("body.status", {"connected": True, "temperature_c": 60.0})]),
    ("model_vendor", "up", [("model.call_end", {"ok": True})]),
    ("model_vendor", "degraded", [("model.call_end", {"ok": False, "code": "overloaded"})]),
    ("model_vendor", "unknown", []),
    ("speech_vendors", "degraded", [("speech.stt", {"ok": False, "code": "timeout"})]),
    ("budget", "up", [("budget.check", {"headroom_usd": 0.5})]),
    ("budget", "down", [("budget.stop", {"cap": "session"})]),
    ("budget", "unknown", []),
    ("config", "up", [("config.loaded", {"file": "config.toml"})]),
    ("config", "down", [("config.error", {})]),
    ("log_sink", "down", [("obs.sink_failed", {"reason": "disk full"})]),
    ("log_sink", "up", [("obs.sink_failed", {}), ("obs.sink_recovered", {})]),
    ("supervisor", "up", [("supervisor.started", {"component": "transport"})]),
    ("supervisor", "degraded", [("supervisor.restart", {"component": "transport"})]),
    ("supervisor", "down", [("supervisor.gave_up", {"component": "transport"})]),
    ("supervisor", "down", [("supervisor.fatal", {"component": "transport"})]),
]


@pytest.mark.parametrize("source, state, events", CASES, ids=[f"{c[0]}-{c[1]}-{i}" for i, c in enumerate(CASES)])
def test_each_source_can_be_forced_into_each_state(source: str, state: str, events: list[Any]):
    got = board_after(*events)[source]
    assert got["state"] == state, got
    assert got["reason"]


def test_a_missing_camera_is_visible_with_its_reason_not_silent():
    s = board_after(("body.capabilities", {"capabilities": ["pose", "audio_in"], "connected": True}))
    assert s["camera"]["state"] == "down" and "image" in s["camera"]["reason"]
    assert s["microphone"]["state"] == "up"


@pytest.mark.parametrize("free, state", [(OBS_DISK_LOW_BYTES, "up"), (OBS_DISK_LOW_BYTES - 1, "degraded"),
                                         (OBS_DISK_MIN_BYTES, "degraded"), (OBS_DISK_MIN_BYTES - 1, "down")])
def test_disk_space_states(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, free: int, state: str):
    monkeypatch.setattr(H.shutil, "disk_usage", lambda p: type("U", (), {"free": free})())
    b = HealthBoard()
    assert b.snapshot(data_dir=tmp_path)["sources"]["disk"]["state"] == state


def test_queue_drops_show_as_degraded():
    b = HealthBoard()
    b.on_event({"type": "queue.drop", "f": {"new_drops": 3}})
    snap = b.snapshot(obs_stats={"writer_state": "up", "writer_dropped": 0, "writer_queue": 0})
    assert snap["sources"]["queues"]["state"] == "degraded" and "dropped 3" in snap["sources"]["queues"]["reason"]


def test_state_changes_are_coalesced_per_source():
    b = HealthBoard()
    for up in (True, False, True, False):
        b.on_event({"type": "body.link", "f": {"connected": up}})
    changes = b.take_changes()
    assert [c[:2] for c in changes] == [("body_link", "down")] and b.take_changes() == []


def test_the_snapshot_file_is_atomic_and_read_back(tmp_path: Path):
    path = tmp_path / "health.json"
    b = HealthBoard()
    write_snapshot(path, b.snapshot())
    snap, problem = read_snapshot(path)
    assert snap is not None and problem == "" and "camera" in snap["sources"]
    assert not list(tmp_path.glob(".*.tmp"))  # the temporary file was replaced, not left behind


def test_a_stale_snapshot_says_so():
    b = HealthBoard()
    snap = b.snapshot()
    assert "STALE" not in format_panel(snap)
    assert "STALE" in format_panel(snap, now=snap["written_at"] + OBS_HEALTH_STALE_S + 1)
    assert OBS_HEALTH_INTERVAL_S < OBS_HEALTH_STALE_S


@pytest.mark.parametrize("content, problem", [
    (None, "missing"), (b"\x00garbage", "not valid JSON"), (b"[]", "not a health snapshot"),
    (b'{"sources": []}', "not a health snapshot"), (b" " * (OBS_HEALTH_FILE_MAX_BYTES + 1), "larger than")])
def test_a_bad_health_file_is_refused_not_raised(tmp_path: Path, content: bytes | None, problem: str):
    path = tmp_path / "health.json"
    if content is not None:
        path.write_bytes(content)
    snap, why = read_snapshot(path)
    assert snap is None and problem in why


def test_the_panel_cleans_what_it_prints():
    text = format_panel({"written_at": time.time(), "session": "\x1b[2J", "sources": {
        "cam\nera": {"state": "\x07up", "reason": "\u202eevil\r\nFORGED"}}})
    assert "\x1b" not in text and "\x07" not in text and "\u202e" not in text and "\r" not in text
    assert all(not line.startswith("FORGED") for line in text.splitlines())


def test_runtime_writes_health_and_state_and_survives_a_failing_disk(tmp_path: Path):
    log = EventLog()
    rt = ObsRuntime(tmp_path, log=log, board=HealthBoard(), session_id="s-test")
    rt.start()
    try:
        assert rt.write_health()
        snap, _ = read_snapshot(tmp_path / "health.json")
        assert snap is not None and snap["state_file"]["starts"] == 1 and snap["budget"]["requests_this_hour"] == 0
    finally:
        rt.stop()
    blocked = tmp_path / "blocked"
    blocked.write_text("x", encoding="utf-8")
    rt2 = ObsRuntime(blocked, log=EventLog(), board=HealthBoard())
    rt2.start()  # events, state and health all fail: reported, never raised
    assert rt2.write_health() is False and rt2.health_failures == 1
    rt2.stop()


# ---- step 4: the viewer ---------------------------------------------------------------------------------------


def _write_events(folder: Path, n: int = 5, **over: Any) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    log = EventLog(burst=10**6, rate_per_s=10**6)
    w = EventWriter(folder, run="v")
    log.writer = w
    w.start()
    for i in range(n):
        with correlate(session="s-1", turn=f"t-{i % 2}"):
            log.emit("tool.result", f"call {i}", tool="get_pose", i=i, **over)
    log.emit("link.up", "up", software="x")
    log.stop_files()
    return next(folder.glob("events-*.jsonl"))


def test_watch_replays_a_saved_file_with_filters(tmp_path: Path):
    path = _write_events(tmp_path / "logs")
    out: list[str] = []
    c = watch(path.parent, Filter(), out=out.append)
    assert c.shown == 6 and len(out) == 6
    out.clear()
    assert watch(path.parent, Filter(components=frozenset({"transport"})), out=out.append).shown == 1
    assert watch(path.parent, Filter(turn="t-1"), out=out.append).shown == 2
    assert watch(path.parent, Filter(types=("tool.*",)), out=out.append).shown == 5
    assert watch(path.parent, Filter(min_level=30), out=out.append).shown == 0
    assert watch(path, Filter(session="s-1"), out=out.append).shown == 5  # one file
    detail: list[str] = []
    watch(path, Filter(types=("link.up",)), detail=True, out=detail.append)
    assert "software" in detail[0] and "seq=" in detail[0]


def test_since_filters_by_time():
    now = time.time()
    assert abs(parse_since("10m", now) - (now - 600)) < 1e-6 and parse_since("2h", now) == now - 7200
    with pytest.raises(ValueError):
        parse_since("yesterday")
    assert Filter(since=now + 60).matches({"ts": "2026-11-13T10:00:00Z"}) is False


def test_watch_refuses_anything_but_an_event_file(tmp_path: Path):
    other = tmp_path / "config.toml"
    other.write_text("secret = 1", encoding="utf-8")
    with pytest.raises(ValueError):
        watch(other, Filter(), out=lambda s: None)
    (tmp_path / "events-x.jsonl.bak").write_text("{}", encoding="utf-8")
    assert list(read_events([other, tmp_path / "events-x.jsonl.bak"])) == []


def test_the_viewer_opens_only_event_files_and_the_health_file(tmp_path: Path):
    """Every file the viewer opens is checked with an audit hook while it runs."""
    logs = tmp_path / "logs"
    _write_events(logs)
    (logs / "notes.txt").write_text("not for the viewer", encoding="utf-8")
    (logs / "serve-20261113.log").write_text("text log", encoding="utf-8")
    health = tmp_path / "health.json"
    write_snapshot(health, HealthBoard().snapshot())
    opened: list[str] = []
    active = threading.local()

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if event == "open" and getattr(active, "on", False) and isinstance(args[0], (str, os.PathLike)):
            opened.append(os.fsdecode(args[0]))

    sys.addaudithook(hook)
    active.on = True
    try:
        from brain.obs.viewer import health_view

        watch(logs, Filter(), out=lambda s: None)
        health_view(health, out=lambda s: None)
    finally:
        active.on = False
    names = {Path(p).name for p in opened}
    assert names and all(n == "health.json" or (n.startswith("events-") and n.endswith(".jsonl")) for n in names), names


def test_the_viewer_skips_bad_lines_and_prints_no_control_characters(tmp_path: Path):
    path = tmp_path / "events-bad-0001.jsonl"
    good = {"ts": "2026-11-13T10:00:00.000000Z", "level": "INFO", "comp": "\x1b[31m", "type": "tool.result",
            "msg": "\x1b]0;title\x07 hi\r\nFORGED", "f": {"k\n": "v\u2028"}}
    path.write_bytes(b"not json\n" + json.dumps(good).encode() + b"\n" + b"x" * (OBS_VIEW_LINE_MAX_BYTES + 10)
                     + b"\n\xff\xfe\n[]\n")
    out: list[str] = []
    c = watch(tmp_path, Filter(), out=out.append)
    assert c.shown == 1 and c.skipped_bad == 4
    assert all(ord(ch) >= 32 for line in out for ch in line)
    assert "\u2028" not in out[0]


def test_follow_reads_new_lines_across_rotation_and_two_writers(tmp_path: Path):
    assert OBS_VIEW_POLL_S == 0.25
    f = Follower(tmp_path)
    log = EventLog(burst=10**6, rate_per_s=10**6)
    w = EventWriter(tmp_path, run="a", max_bytes=1500)
    log.writer = w
    w.start()
    other = EventLog(burst=10**6, rate_per_s=10**6)
    w2 = EventWriter(tmp_path, run="b")
    other.writer = w2
    w2.start()
    seen = 0
    for i in range(60):
        log.emit("demo.step", "a", i=i)
        if i % 10 == 0:
            other.emit("launcher.start", "b", attempt=i)
        if i % 7 == 0:
            time.sleep(0.05)
            seen += len(f.poll())
    log.stop_files()
    other.stop_files()
    seen += len(f.poll())
    assert len(list(tmp_path.glob("events-a-*.jsonl"))) > 2
    assert seen == 60 + 6


def test_format_event_is_one_line():
    line = format_event({"ts": "2026-11-13T10:00:00.123456Z", "level": "INFO", "comp": "agent", "type": "tool.call",
                         "session": "s-1", "turn": "t-1", "msg": "x", "f": {"tool": "speak"}})
    assert "\n" not in line and "tool.call" in line and "[s-1|t-1]" in line and "tool=speak" in line


# ---- step 8: the state file -------------------------------------------------------------------------------------


def _store(tmp_path: Path) -> tuple[StateStore, list[tuple[str, dict[str, Any]]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    return StateStore(tmp_path, lambda t, m, **f: events.append((t, f))), events


def test_state_round_trips_and_counts_starts(tmp_path: Path):
    s, ev = _store(tmp_path)
    assert s.load() == BrainState() and ev[0][0] == "state.loaded"
    s.update(starts=1, session_id="s-1", requests_this_hour=3, pending_request_ids=("r-1",), spend_usd_total=0.25)
    assert s.save()
    s2, _ = _store(tmp_path)
    st = s2.load()
    assert (st.starts, st.session_id, st.requests_this_hour, st.pending_request_ids, st.spend_usd_total) == \
        (1, "s-1", 3, ("r-1",), 0.25)


def test_a_bad_change_is_refused(tmp_path: Path):
    s, _ = _store(tmp_path)
    with pytest.raises(ValueError):
        s.update(pending_request_ids=tuple(f"r{i}" for i in range(STATE_PENDING_MAX + 1)))
    with pytest.raises(ValueError):
        s.update(session_id="..\\..\\x")


@pytest.mark.parametrize("raw", [
    b'{"schema": 1, "starts": 3, "session_id": "s-1"',  # a writer killed mid-write (without the atomic replace)
    b"", b"\xff\xfe garbage", b"[1]", b'{"schema": 2}', b'{"schema": 1, "starts": "3"}',
    b'{"schema": 1, "spend_usd_total": NaN}', b'{"schema": 1, "token": "x"}', b"[" * 50_000,
    b" " * (STATE_FILE_MAX_BYTES + 1)])
def test_a_corrupt_state_file_is_quarantined_and_defaults_used(tmp_path: Path, raw: bytes):
    (tmp_path / "state.json").write_bytes(raw)
    s, ev = _store(tmp_path)
    assert s.load() == BrainState()
    assert [t for t, _ in ev] == ["state.quarantined"]
    assert not (tmp_path / "state.json").exists() and len(list(tmp_path.glob("state.corrupt-*.json"))) == 1


def test_a_kill_during_save_leaves_the_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    s, _ = _store(tmp_path)
    s.load()
    s.update(starts=5)
    assert s.save()
    from brain.obs import files

    def killed(src: object, dst: object) -> None:  # the process dies between writing the temporary file and replacing
        raise OSError("simulated kill before the replace")

    monkeypatch.setattr(files.os, "replace", killed)
    s.update(starts=6)
    assert s.save() is False and s.save_failures == 1
    monkeypatch.undo()
    (tmp_path / ".state.json.99999.tmp").write_text('{"half', encoding="utf-8")  # debris from a real kill
    s2, ev = _store(tmp_path)
    assert s2.load().starts == 5 and ev[0][0] == "state.loaded"


def test_quarantined_copies_are_bounded(tmp_path: Path):
    for i in range(STATE_QUARANTINE_KEPT + 4):
        (tmp_path / "state.json").write_bytes(b"bad")
        s, _ = _store(tmp_path)
        s.load()
    assert len(list(tmp_path.glob("state.corrupt-*.json"))) == STATE_QUARANTINE_KEPT
