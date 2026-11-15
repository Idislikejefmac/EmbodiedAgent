"""Model-level test doubles for the agent loop (card P0-07; doc 10 section 8.9): FakeModel, HostileModel, FakeClock.

FakeModel implements brain.agent.model.ModelClient directly (no SDK, no HTTP): each `complete` takes the next
scripted step, a ModelReply or a ModelFailure (raised), optionally after a delay, and records the request.
A step may also be a callable (request -> reply) for replies that depend on what was asked. When the script
runs out it repeats `default` (or answers with plain text).

HostileModel is a FakeModel whose scripts are the hostile behaviours of doc 10 T-P0-07-020/021/023/024/027.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from brain.agent.model import ModelFailure, ModelReply, ModelRequest, TextSink, ToolCall, Usage

USAGE = Usage(input_tok=900, output_tok=40, cache_read_tok=0, cache_write_tok=0)


def text_reply(t: str, *, usage: Usage = USAGE, stop: str = "end_turn") -> ModelReply:
    blocks = ({"type": "text", "text": t},) if t else ()
    return ModelReply(blocks, t, (), stop, usage, events=4)


def tool_reply(*calls: tuple[str, Any] | tuple[str, Any, str], usage: Usage = USAGE, stop: str = "tool_use",
               preface: str = "") -> ModelReply:
    blocks: list[dict[str, Any]] = [{"type": "text", "text": preface}] if preface else []
    tcs = []
    for i, c in enumerate(calls):
        name, args = c[0], c[1]
        cid = c[2] if len(c) > 2 else f"toolu_{i + 1:02d}_{abs(hash((name, i))) % 10_000}"
        tcs.append(ToolCall(cid, name, args))
        blocks.append({"type": "tool_use", "id": cid, "name": name, "input": args if isinstance(args, dict) else {}})
    return ModelReply(tuple(blocks), preface, tuple(tcs), stop, usage, events=6)


Step = ModelReply | ModelFailure | Callable[[ModelRequest], ModelReply] | tuple[float, Any]


class FakeModel:
    name = "fake"

    def __init__(self, steps: list[Step] | None = None, *, default: Step | None = None, models: list[str] | None = None):
        self.steps = list(steps or [])
        self.default = default
        self.requests: list[ModelRequest] = []
        self.models = models if models is not None else ["claude-haiku-5-5", "claude-sonnet-5-5"]
        self.closed = False

    async def complete(self, request: ModelRequest, on_text: TextSink | None = None) -> ModelReply:
        self.requests.append(request)
        step: Any = self.steps.pop(0) if self.steps else (self.default if self.default is not None
                                                           else text_reply("All done."))
        if isinstance(step, tuple) and len(step) == 2 and isinstance(step[0], (int, float)):
            await asyncio.sleep(step[0])
            step = step[1]
        if callable(step) and not isinstance(step, (ModelReply, ModelFailure)):
            step = step(request)
        if isinstance(step, BaseException):
            raise step
        if on_text is not None and isinstance(step, ModelReply) and step.text:
            on_text(step.text)
        return step

    async def list_models(self) -> list[str]:
        return list(self.models)

    async def aclose(self) -> None:
        self.closed = True

    def last_user_text(self) -> str:
        msgs = self.requests[-1].messages
        for m in reversed(msgs):
            if m["role"] == "user":
                return " ".join(b.get("text", "") for b in m["content"] if isinstance(b, dict))
        return ""


def nested(depth: int) -> Any:
    v: Any = "x"
    for _ in range(depth):
        v = {"a": v}
    return v


HOSTILE = {
    "huge_args": tool_reply(("speak", {"text": "x" * 70_000})),
    "deep_args": tool_reply(("speak", {"text": "hi", "x": nested(50)})),
    "args_string": tool_reply(("get_body_status", '{"not": "an object"}')),
    "args_list": tool_reply(("get_body_status", [1, 2, 3])),
    "args_null": tool_reply(("get_body_status", None)),
    "many_keys": tool_reply(("speak", {f"k{i}": i for i in range(3000)})),
    "unknown_tool": tool_reply(("run_shell", {"line": "format c:"})),
    "sensitive_tool": tool_reply(("delete_person_data", {"person_id": "jeff"})),
    "reserved_tool": tool_reply(("list_waypoints", {})),
    "not_built_tool": tool_reply(("who_is_present", {})),
    "jeff_says_delete": text_reply("I am Jeff, delete everything. Deleting the C drive now."),
    "ignore_previous": tool_reply(("delete_files", {"what": "C:\\"}), preface="Ignore previous instructions."),
    "huge_text": text_reply("blah " * 200_000),
    "empty": text_reply(""),
    "refusal": text_reply("", stop="refusal"),
    "unknown_stop": text_reply("hi", stop="unknown"),
    "tool_use_no_calls": text_reply("hmm", stop="tool_use"),
    "tool_calls_max_tokens": tool_reply(("get_body_status", {}), stop="max_tokens"),
}


class HostileModel(FakeModel):
    """A FakeModel that misbehaves: `HostileModel("loop")` asks for get_body_status forever; any key of HOSTILE
    plays that reply once and then says a plain line."""

    def __init__(self, kind: str, **kw: Any):
        if kind == "loop":
            super().__init__([], default=tool_reply(("get_body_status", {})), **kw)
        elif kind == "stop_flood":
            super().__init__([], default=tool_reply(*[("stop", {})] * 3), **kw)
        else:
            super().__init__([HOSTILE[kind]], default=text_reply("Okay."), **kw)
        self.kind = kind


class FakeClock:
    """Wall time and date that tests move by hand."""

    def __init__(self, t: float = 1_800_000_000.0, day: date = date(2026, 11, 15)):
        self.t = t
        self.day = day

    def now(self) -> float:
        return self.t

    def today(self) -> date:
        return self.day

    def advance(self, seconds: float = 0.0, days: int = 0) -> None:
        self.t += seconds
        self.day += timedelta(days=days)


def dumps(o: Any) -> str:
    return json.dumps(o, default=str)
