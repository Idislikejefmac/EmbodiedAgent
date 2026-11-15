"""Card P0-07 step 10: development prompt capture (brain/agent/capture.py; DEC-025).

On and off; bounded in record size, file size, total size and age; images as hashes only; never in the event
stream (only prompt.capture_ref with an id and a byte count); never holds the key (canary); a failing folder does
not stop the agent.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from brain import obs
from brain.agent import capture as C
from brain.agent.body_tools import register_body_tools
from brain.agent.budget import SpendGuard, load_prices
from brain.agent.capture import PromptCapture
from brain.agent.loop import Agent, AgentSettings, TurnInput, load_system_prompt
from brain.agent.model import ModelFailure, ModelRequest
from brain.agent.tools import InputSource, ToolRegistry
from brain.body.sim import SimBody
from brain.config import CapsConfig
from brain.limits import CAPTURE_FILE_MAX_BYTES, CAPTURE_RECORD_MAX_BYTES
from fakes import run
from fakes.models import FakeClock, FakeModel, text_reply, tool_reply

pytestmark = pytest.mark.trace("T-P0-07-012", "T-P0-07-013", risks=("R-FND-40", "R-FND-41"))

CANARY = "sk-ant-api03-canary" + "Z" * 30


def files(root: Path) -> list[Path]:
    return sorted((root / C.CAPTURE_DIR).glob("prompts-*.jsonl"))


def records(root: Path) -> list[dict]:
    return [json.loads(line) for f in files(root) for line in f.read_text(encoding="utf-8").splitlines()]


def agent_with(capture: PromptCapture, model: FakeModel) -> tuple[Agent, SimBody]:
    body = SimBody()
    registry = ToolRegistry()
    register_body_tools(registry, body)
    clock = FakeClock()
    guard = SpendGuard(CapsConfig(), load_prices(), None, now=clock.now, today=clock.today)
    return Agent(registry, model, guard, settings=AgentSettings(), prompt=load_system_prompt(), capture=capture), body


def test_on_it_keeps_full_requests_and_replies_and_the_events_only_carry_a_reference(tmp_path):
    async def go():
        cap = PromptCapture(tmp_path, enabled=True, max_mb=5, max_days=14, secrets=[CANARY])
        agent, body = agent_with(cap, FakeModel([tool_reply(("capture_image", {"reason": "look"})),
                                                 text_reply("A grey square.")]))
        events: list[dict] = []
        obs.LOG.reset_limits()
        obs.LOG.add_sink(events.append)
        try:
            async with body:
                r = await agent.run_turn(TurnInput(f"what do you see? my key is {CANARY}", InputSource.LOCAL_CONSOLE))
        finally:
            obs.LOG.remove_sink(events.append)
        assert r.outcome == "ok"
        recs = records(tmp_path)
        assert [x["kind"] for x in recs] == ["request", "response", "request", "response"]
        first = json.dumps(recs[0])
        assert agent.prompt.text[:60] in first and "what do you see?" in first  # the full prompt is there
        assert CANARY not in "".join(f.read_text(encoding="utf-8") for f in files(tmp_path))
        image = [b for b in recs[2]["payload"]["messages"][-1]["content"][0]["content"] if b["type"] == "image"][0]
        assert set(image) == {"type", "media_type", "bytes", "sha256"} and image["bytes"] > 0  # no pixels
        refs = [e for e in events if e["type"] == "prompt.capture_ref"]
        assert len(refs) == 4 and all(set(e["f"]) <= {"record", "kind", "call", "bytes", "ok"} for e in refs)
        whole = json.dumps(events)
        assert "what do you see" not in whole and agent.prompt.text[:60] not in whole and "A grey square" not in whole

    run(go())


def test_off_it_writes_nothing(tmp_path):
    async def go():
        cap = PromptCapture(tmp_path, enabled=False, max_mb=5, max_days=14)
        agent, body = agent_with(cap, FakeModel([text_reply("hi")]))
        async with body:
            await agent.run_turn(TurnInput("hello", InputSource.LOCAL_CONSOLE))
        assert not (tmp_path / C.CAPTURE_DIR).exists() and cap.records == 0

    run(go())


def test_a_failure_is_captured_with_its_code_only(tmp_path):
    cap = PromptCapture(tmp_path, enabled=True, max_mb=1, max_days=1)
    cap.failure("m-1", ModelFailure("overloaded", retryable=True, status=529))
    rec = records(tmp_path)[0]
    assert rec["kind"] == "error" and rec["payload"]["code"] == "overloaded" and rec["payload"]["status"] == 529


def test_secret_shapes_are_redacted_even_when_not_given(tmp_path):
    cap = PromptCapture(tmp_path, enabled=True, max_mb=1, max_days=1)
    req = ModelRequest("m", 10, (), (), ({"role": "user", "content": "ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnop"},))
    cap.request("m-1", req)
    text = files(tmp_path)[0].read_text(encoding="utf-8")
    assert "sk-ant-abcdefghijklmnop" not in text and "<redacted:secret>" in text


def test_a_record_over_the_limit_is_cut_and_marked(tmp_path):
    cap = PromptCapture(tmp_path, enabled=True, max_mb=10, max_days=1)
    big = "word " * (CAPTURE_RECORD_MAX_BYTES // 4)
    cap.request("m-1", ModelRequest("m", 10, (), (), ({"role": "user", "content": big},)))
    line = files(tmp_path)[0].read_bytes()
    rec = json.loads(line)
    assert len(line) <= CAPTURE_RECORD_MAX_BYTES + 1 and rec["cut_at_bytes"] == CAPTURE_RECORD_MAX_BYTES
    assert rec["original_bytes"] > CAPTURE_RECORD_MAX_BYTES


def test_files_rotate_and_the_folder_stays_within_its_size(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "CAPTURE_FILE_MAX_BYTES", 300_000)
    assert CAPTURE_FILE_MAX_BYTES > 300_000
    cap = PromptCapture(tmp_path, enabled=True, max_mb=1, max_days=14)
    for i in range(30):
        cap.request(f"m-{i}", ModelRequest("m", 10, (), (), ({"role": "user", "content": "x" * 100_000},)))
    cap.prune()
    total = sum(f.stat().st_size for f in files(tmp_path))
    assert len(files(tmp_path)) >= 2 and total <= 1024 * 1024 + 300_000  # within max_mb, give or take the open file


def test_old_files_are_deleted(tmp_path):
    cap = PromptCapture(tmp_path, enabled=True, max_mb=5, max_days=2)
    folder = tmp_path / C.CAPTURE_DIR
    folder.mkdir(parents=True)
    old = folder / "prompts-20200101-000000-1-1.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    past = time.time() - 3 * 86_400
    os.utime(old, (past, past))
    cap.request("m-1", ModelRequest("m", 10, (), (), ({"role": "user", "content": "hi"},)))
    assert not old.exists() and len(files(tmp_path)) == 1


def test_an_unwritable_capture_folder_never_stops_the_agent(tmp_path):
    async def go():
        blocker = tmp_path / "data"
        blocker.write_text("a file where the folder should be", encoding="utf-8")
        cap = PromptCapture(blocker, enabled=True, max_mb=1, max_days=1)
        agent, body = agent_with(cap, FakeModel([text_reply("still fine")]))
        async with body:
            r = await agent.run_turn(TurnInput("hi", InputSource.LOCAL_CONSOLE))
        assert r.outcome == "ok" and cap.failures >= 1

    run(go())


def test_capture_files_are_owner_only_where_the_platform_has_permission_bits(tmp_path):
    cap = PromptCapture(tmp_path, enabled=True, max_mb=1, max_days=1)
    cap.request("m-1", ModelRequest("m", 10, (), (), ({"role": "user", "content": "hi"},)))
    f = files(tmp_path)[0]
    assert f.parent == tmp_path / "capture"
    if os.name != "nt":  # on Windows the file takes the data folder's access list (the developer's own folder)
        assert f.stat().st_mode & 0o077 == 0
