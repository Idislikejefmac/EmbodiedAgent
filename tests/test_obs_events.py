"""Card X-02 steps 1 to 3 and 9: the event schema, redaction at the source, and the pipeline that can never
harm the brain (bounded, non-blocking, a failing sink swallowed and reported once)."""

from __future__ import annotations

import ast
import asyncio
import fnmatch
import json
import os
import stat
import time
from pathlib import Path

import pytest

from brain import obs
from brain.limits import (OBS_EMIT_BUDGET_US, OBS_EVENT_MAX_BYTES, OBS_FIELD_MAX_CHARS, OBS_FIELDS_MAX,
                          OBS_FILE_MAX_BYTES, OBS_FILES_MAX, OBS_LIST_ITEMS_MAX, OBS_MESSAGE_MAX_CHARS,
                          OBS_NEST_DEPTH_MAX, OBS_RING_EVENTS, OBS_SINK_RETRY_S, OBS_TYPE_BURST, OBS_TYPE_RATE_PER_S,
                          OBS_WRITER_CLOSE_TIMEOUT_S, OBS_WRITER_QUEUE_MAX)
from brain.obs import redact
from brain.obs.events import (COMPONENTS, CRASH_TYPES, ENTRY_COVERAGE, ENTRY_EXEMPT, EVENT_TYPES, LEVELS,
                              SCHEMA_VERSION, correlate)
from brain.obs.pipeline import EventLog, EventWriter, event_line

REPO = Path(__file__).resolve().parent.parent
BRAIN = REPO / "brain"
CANARIES = {"token": "c4n4ry" + "ab" * 29, "key": "sk-ant-api03-CANARYcanaryCANARYcanary0123456789",
            "password": "hunter2-canary-password"}


def fresh(**kw: object) -> EventLog:
    return EventLog(**kw)  # type: ignore[arg-type]


def wait_until(cond, timeout: float = 3.0) -> bool:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


# ---- step 1: the schema and the catalogue ------------------------------------------------------------------


def test_an_event_has_every_schema_field_and_is_one_json_line():
    log = fresh()
    with correlate(session="s-1", turn="t-2"):
        e = log.emit("tool.result", "get_pose ok", tool="get_pose", status="ok", ms=1.25)
    assert e is not None
    assert set(e) == {"v", "seq", "ts", "mono", "level", "comp", "type", "session", "turn", "msg", "f"}
    assert e["v"] == SCHEMA_VERSION and e["seq"] == 1 and e["level"] == "INFO" and e["comp"] == "agent"
    assert e["session"] == "s-1" and e["turn"] == "t-2" and e["ts"].endswith("Z")
    assert e["f"] == {"tool": "get_pose", "status": "ok", "ms": 1.25}
    line = event_line(e)
    assert "\n" not in line and json.loads(line) == e


def test_correlation_ids_follow_tasks_started_inside_the_block():
    log = fresh()

    async def go() -> list[object]:
        with correlate(session="s-9"):
            t = asyncio.create_task(asyncio.sleep(0, result=None))
            await t
            inner = asyncio.create_task(_emit_later(log))
        out = await inner
        return [out["session"] if out else None, log.emit("demo.step", "outside")["session"]]  # type: ignore[index]

    assert asyncio.run(go()) == ["s-9", None]


async def _emit_later(log: EventLog) -> dict[str, object] | None:
    await asyncio.sleep(0)
    return log.emit("demo.step", "inside")


def test_the_catalogue_is_well_formed():
    assert len(EVENT_TYPES) >= 60
    for name, t in EVENT_TYPES.items():
        assert name == t.name and "." in name and t.component in COMPONENTS and t.level in LEVELS, name
        assert t.status in ("live", "reserved") and len(t.about) >= 20, name
    reserved = {n for n, t in EVENT_TYPES.items() if t.status == "reserved"}
    # Named for P0-07 so it only has to emit them (model calls, cost and budget, prompt capture reference).
    assert {"model.call_start", "model.call_end", "cost.update", "budget.check", "budget.stop",
            "prompt.capture_ref"} <= reserved
    assert {"turn.start", "turn.end"} <= set(EVENT_TYPES)
    assert CRASH_TYPES == {"tool.failed", "process.crash", "supervisor.crashed"}


def _emit_calls() -> dict[str, set[str]]:
    """{event type: files} for every emit-like call with a constant first argument under brain/."""
    found: dict[str, set[str]] = {}
    for path in BRAIN.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            first = node.args[0]
            if name in ("emit", "em", "_event") and isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.setdefault(first.value, set()).add(path.relative_to(REPO).as_posix())
    return found


def test_every_emitted_type_is_in_the_catalogue_and_every_live_type_is_emitted():
    calls = _emit_calls()
    assert set(calls) <= set(EVENT_TYPES), sorted(set(calls) - set(EVENT_TYPES))
    other_text = "\n".join(p.read_text(encoding="utf-8") for p in BRAIN.rglob("*.py")
                           if p.name not in ("events.py", "health.py"))
    live = {n for n, t in EVENT_TYPES.items() if t.status == "live"}
    never = sorted(n for n in live if f'"{n}"' not in other_text)
    assert never == [], f"live event types no code emits: {never}"
    reserved_emitted = sorted(n for n, t in EVENT_TYPES.items() if t.status == "reserved" and n in calls)
    assert reserved_emitted == [], "a reserved type is emitted: mark it live in brain/obs/events.py"


def test_an_unknown_type_is_recorded_as_a_bad_event_not_raised():
    log = fresh()
    e = log.emit("no.such.type", "x")
    assert e is not None and e["type"] == "obs.bad_event" and e["f"]["asked_type"] == "no.such.type"
    assert log.emit(42, "x")["type"] == "obs.bad_event"  # type: ignore[arg-type,index]


def test_levels_below_the_minimum_are_not_recorded():
    log = fresh(min_level="WARNING")
    assert log.emit("demo.step", "info") is None
    assert log.emit("demo.step", "warn", level="WARNING") is not None
    assert log.emit("link.connect", "debug by default") is None


# ---- step 3: redaction at the source ----------------------------------------------------------------------


def test_canary_secrets_never_reach_an_event_the_ring_or_the_file(tmp_path: Path):
    log = fresh(burst=1000, rate_per_s=1000)
    w = log.start_files(tmp_path)
    all_secrets = " ".join(CANARIES.values())
    log.emit("demo.step", f"message holding {all_secrets}", token=CANARIES["token"], api_key=CANARIES["key"],
             password=CANARIES["password"], note=all_secrets, nested={"auth": CANARIES["token"], "ok": "fine"},
             items=[CANARIES["key"], "x"])
    log.emit("tool.call", "x", **{"prompt": all_secrets, "text": all_secrets, "transcript": all_secrets,
                                  "embedding": [0.1, 0.2], "args": {"path": "c:/"}})
    log.stop_files()
    texts = [event_line(e) for e in log.ring] + [p.read_text(encoding="utf-8") for p in tmp_path.glob("*.jsonl")]
    blob = "\n".join(texts).lower()
    for label, secret in CANARIES.items():
        assert secret.lower() not in blob, label
    assert w.written >= 2
    e = next(e for e in log.ring if e["type"] == "demo.step")
    assert e["f"]["token"] == redact.REDACTED and e["f"]["note"] == redact.REDACTED_SECRET
    assert e["f"]["nested"] == {"auth": redact.REDACTED, "ok": "fine"}


def test_tool_arguments_are_summarised_by_name_type_and_size_never_value():
    count, shape = redact.arg_summary({"text": "my secret words", "interrupt": True, "n": [1, 2, 3]})
    assert count == 3 and shape == ["text:str:15", "interrupt:bool", "n:list:3"]
    assert redact.arg_summary("not a dict") == (None, ["<str>"])

    class Hostile(dict):  # type: ignore[type-arg]
        def keys(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("no")

    assert redact.arg_summary(Hostile(a=1))[0] == 1  # dict.keys is used, not the override


def test_control_characters_and_line_separators_are_escaped_so_no_line_can_be_forged():
    log = fresh()
    e = log.emit("link.refused", "ok\n{\"type\": \"tool.result\"}\r", code="x\x1b[2J\u2028\u202e\ud800y")
    assert e is not None
    line = event_line(e)
    assert line.count("\n") == 0 and "\x1b" not in line and "\u2028" not in line and "\u202e" not in line
    assert "\\x0a" in e["msg"] and "\\u2028" in e["f"]["code"] and "\\ud800" in e["f"]["code"]
    line.encode("utf-8")  # a lone surrogate would raise here and take the event file down


def test_every_field_and_the_whole_event_are_bounded():
    log = fresh()
    e = log.emit("demo.step", "m" * 10_000, big="x" * 100_000, many=list(range(10_000)),
                 deep={"a": {"b": {"c": {"d": 1}}}}, **{f"f{i}": i for i in range(100)})
    assert e is not None
    assert len(e["msg"]) <= OBS_MESSAGE_MAX_CHARS + 3
    assert len(event_line(e).encode("utf-8")) <= OBS_EVENT_MAX_BYTES
    e2 = log.emit("demo.step", "x", big="x" * 100_000, many=list(range(10_000)), deep={"a": {"b": {"c": 1}}})
    assert e2 is not None
    assert len(e2["f"]["big"]) <= OBS_FIELD_MAX_CHARS + 3
    assert len(e2["f"]["many"]) == OBS_LIST_ITEMS_MAX + 1 and e2["f"]["many"][-1].endswith("more")
    assert OBS_NEST_DEPTH_MAX == 2 and e2["f"]["deep"] == {"a": {"b": "<dict>"}}  # two levels below a field
    e3 = log.emit("demo.step", "x", **{f"f{i}": i for i in range(100)})
    assert e3 is not None and len(e3["f"]) == OBS_FIELDS_MAX + 1 and e3["f"]["_more_fields"] == 100 - OBS_FIELDS_MAX


def test_hostile_values_never_make_emit_raise():
    class BadRepr:
        def __repr__(self) -> str:
            raise RuntimeError("no repr")

    class BadMapping(dict):  # type: ignore[type-arg]
        def items(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("no items")

    log = fresh()
    e = log.emit("demo.step", "x", r=BadRepr(), m=BadMapping(a=1), nan=float("nan"), big=10**500, b=b"\x00\x01",
                 o=object(), s={1, 2})
    assert e is not None
    assert e["f"]["r"] == "<BadRepr>" and e["f"]["m"] == "<BadMapping>" and e["f"]["nan"] == "nan"
    assert e["f"]["b"] == "<bytes:2>" and e["f"]["big"].startswith("<int of")
    assert redact.peer(BadRepr()) == "<BadRepr>"
    assert log.emit("demo.step", BadRepr()) is not None  # type: ignore[arg-type]


# ---- step 2: the pipeline ------------------------------------------------------------------------------------


def test_the_ring_buffer_is_bounded():
    assert OBS_RING_EVENTS == 2000
    log = fresh(ring=50, burst=10_000, rate_per_s=10_000)
    for i in range(500):
        log.emit("demo.step", "x", i=i)
    assert len(log.ring) == 50 and log.ring[-1]["f"]["i"] == 499


def test_the_per_type_limit_counts_what_it_holds_back_and_says_so_later():
    clock = [100.0]
    log = fresh(burst=OBS_TYPE_BURST, rate_per_s=OBS_TYPE_RATE_PER_S)
    shown = [log.emit("demo.step", "x") for _ in range(OBS_TYPE_BURST + 15)]
    assert sum(e is not None for e in shown) == OBS_TYPE_BURST
    assert log.counts["suppressed"] == 15 and log.suppressed["demo.step"] == 15
    assert log.emit("body.close", "another type is not affected") is not None
    time.sleep(1.0 / OBS_TYPE_RATE_PER_S + 0.05)
    e = log.emit("demo.step", "after a pause")
    assert e is not None and e["f"]["suppressed_before"] == 15
    del clock


def test_crash_events_are_never_held_back():
    log = fresh(burst=1, rate_per_s=0.001)
    got = [log.emit("supervisor.crashed", "boom", component="c") for _ in range(30)]
    assert all(e is not None for e in got)


def test_a_full_writer_queue_drops_and_counts_and_never_blocks(tmp_path: Path):
    assert OBS_WRITER_QUEUE_MAX == 10_000
    w = EventWriter(tmp_path, run="t", queue_max=10)
    t0 = time.perf_counter()
    for i in range(1000):
        w.offer(f'{{"i": {i}}}')  # the thread is not started: nothing drains the queue
    assert time.perf_counter() - t0 < 0.5
    assert w.dropped == 990 and w.depth == 10


def test_a_sink_that_raises_is_counted_and_removed_after_three_failures():
    log = fresh()
    calls: list[int] = []

    def bad(e: object) -> None:
        calls.append(1)
        raise ValueError("sink bug")

    log.add_sink(bad)
    for _ in range(5):
        assert log.emit("demo.step", "x") is not None
    assert len(calls) == 3 and bad not in log.sinks and log.counts["sink_errors"] == 3


def test_a_sink_that_emits_cannot_recurse():
    log = fresh()
    log.add_sink(lambda e: log.emit("demo.step", "from inside a sink"))
    assert log.emit("demo.step", "outer") is not None
    assert log.counts["reentrant"] == 1 and len(log.ring) == 1


def _writer_failure(tmp_path: Path, how: str) -> tuple[EventLog, EventWriter]:
    target = tmp_path / "logs"
    if how == "folder_is_a_file":
        target.write_text("x", encoding="utf-8")
    log = fresh(burst=10_000, rate_per_s=10_000)
    w = log.start_files(target)
    if how == "disk_full":
        assert wait_until(lambda: w.state == "up")

        class FullDisk:
            def write(self, data: str) -> int:
                raise OSError(28, "No space left on device")

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        real, w._fh = w._fh, FullDisk()
        real.close()
    if how == "read_only_file":
        assert wait_until(lambda: w.current is not None)
        log.stop_files()
        assert w.current is not None
        os.chmod(w.current, stat.S_IREAD)
        w = EventWriter(target, run=w.run, report=log._writer_report)
        log.writer = w
        w.start()
    return log, w


@pytest.mark.parametrize("how", ["folder_is_a_file", "disk_full", "read_only_file"])
def test_a_failing_event_file_is_swallowed_counted_and_reported_once(tmp_path: Path, how: str):
    log, w = _writer_failure(tmp_path, how)
    try:
        for i in range(200):
            assert log.emit("demo.step", "x", i=i) is not None  # never raises, never blocks
            if i % 50 == 0:
                time.sleep(0.25)
        assert wait_until(lambda: w.state == "down")
        assert w.failures >= 1 and w.dropped > 0 and w.reason
        reports = [e for e in log.ring if e["type"] == "obs.sink_failed"]
        assert len(reports) == 1, f"reported {len(reports)} times"
        assert log.stats()["writer_state"] == "down"
    finally:
        log.stop_files(OBS_WRITER_CLOSE_TIMEOUT_S)
        for p in tmp_path.rglob("*.jsonl"):
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)


def test_the_event_file_recovers_and_says_how_much_was_lost(tmp_path: Path):
    assert OBS_SINK_RETRY_S == 5.0
    target = tmp_path / "logs"
    target.write_text("x", encoding="utf-8")
    log = fresh(burst=10_000, rate_per_s=10_000)
    w = EventWriter(target, run="r", report=log._writer_report, retry_s=0.2)
    log.writer = w
    w.start()
    try:
        log.emit("demo.step", "while down")
        assert wait_until(lambda: w.state == "down")
        target.unlink()  # the problem goes away
        time.sleep(0.3)
        log.emit("demo.step", "after")
        assert wait_until(lambda: w.state == "up")
        assert [e["type"] for e in log.ring if e["type"].startswith("obs.sink")] == ["obs.sink_failed",
                                                                                     "obs.sink_recovered"]
    finally:
        log.stop_files()


def test_files_rotate_without_renaming_and_old_ones_are_pruned(tmp_path: Path):
    assert OBS_FILE_MAX_BYTES == 5 * 1024 * 1024 and OBS_FILES_MAX == 20
    log = fresh(burst=10**9, rate_per_s=10**9)
    w = EventWriter(tmp_path, run="rot", report=log._writer_report, max_bytes=2000, max_files=3)
    log.writer = w
    w.start()
    for i in range(400):
        log.emit("demo.step", "rotation", i=i)
    log.stop_files()
    files = sorted(tmp_path.glob("events-*.jsonl"))
    assert 1 <= len(files) <= 3
    assert files[-1].name.startswith("events-rot-") and int(files[-1].stem.rsplit("-", 1)[1]) > 3
    last = [json.loads(line) for line in files[-1].read_text(encoding="utf-8").splitlines()]
    assert last[-1]["f"]["i"] == 399


def test_events_emitted_before_the_files_start_are_written_first(tmp_path: Path):
    log = fresh()
    log.emit("config.loaded", "early", file="config.toml")
    log.start_files(tmp_path)
    log.stop_files()
    lines = (next(tmp_path.glob("events-*.jsonl"))).read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["type"] == "config.loaded" and json.loads(lines[1])["type"] == "obs.started"


def test_emit_overhead_is_within_its_budget(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """OBS_EMIT_BUDGET_US per event, with the file writer running (the real configuration)."""
    log = fresh(burst=10**9, rate_per_s=10**9)
    log.start_files(tmp_path)
    try:
        for _ in range(200):  # warm up
            log.emit("tool.result", "warm", tool="get_pose", status="ok", code="ok", ms=1.0)
        n = 5000
        t0 = time.perf_counter()
        for i in range(n):
            log.emit("tool.result", "get_pose ok", tool="get_pose", status="ok", code="ok", ms=1.25, i=i)
        per_event_us = (time.perf_counter() - t0) / n * 1e6
    finally:
        log.stop_files()
    with capsys.disabled():
        print(f"\nemit() overhead: {per_event_us:.1f} us per event (budget {OBS_EMIT_BUDGET_US} us)")
    assert per_event_us < OBS_EMIT_BUDGET_US


# ---- step 9: every manifest entry point is covered by an emitted event type --------------------------------


def test_every_tool_message_body_method_and_cli_command_has_an_event_or_a_documented_exemption():
    from tools.chaos.manifest import load_manifest

    m = load_manifest()
    emitted = set(_emit_calls()) | {"obs.bad_event"}
    problems = []
    for eid, e in sorted(m.entries.items()):
        if e["kind"] not in ("tool", "reserved_tool", "message", "body_method", "cli_command"):
            continue
        covered = [types for pat, types in ENTRY_COVERAGE.items() if fnmatch.fnmatchcase(eid, pat)]
        exempt = [why for pat, why in ENTRY_EXEMPT.items() if fnmatch.fnmatchcase(eid, pat)]
        if not covered and not exempt:
            problems.append(f"{eid}: no event type and no exemption (brain/obs/events.py ENTRY_COVERAGE)")
        for types in covered:
            missing = [t for t in types if t not in EVENT_TYPES or t not in emitted]
            if missing:
                problems.append(f"{eid}: {missing} are not emitted by any code")
    assert problems == []
    stale = [p for p in list(ENTRY_COVERAGE) + list(ENTRY_EXEMPT)
             if not any(fnmatch.fnmatchcase(i, p) for i in m.entries)]
    assert stale == [], f"coverage patterns that match no manifest entry: {stale}"


def test_every_tool_call_through_the_registry_is_traced(sim_events: list[dict[str, object]]):
    from brain.agent.body_tools import register_body_tools
    from brain.agent.tools import APP, ToolRegistry, load_catalogue
    from brain.body.sim import SimBody

    names = list(load_catalogue().tools) + list(load_catalogue().reserved)

    async def go() -> None:
        async with SimBody() as body:
            reg = ToolRegistry()
            register_body_tools(reg, body)
            for name in names:
                obs.LOG.reset_limits()
                await reg.call(name, {}, APP)

    asyncio.run(go())
    called = [e["f"]["tool"] for e in sim_events if e["type"] == "tool.call"]  # type: ignore[index]
    ended = [e["f"]["tool"] for e in sim_events if e["type"] in ("tool.result", "tool.refused")]  # type: ignore[index]
    assert called == names and ended == names


@pytest.fixture
def sim_events():  # type: ignore[no-untyped-def]
    got: list[dict[str, object]] = []
    obs.LOG.reset_limits()
    obs.LOG.add_sink(got.append)
    try:
        yield got
    finally:
        obs.LOG.remove_sink(got.append)
