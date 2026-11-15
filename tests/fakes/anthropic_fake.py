"""FakeAnthropic: the model API at the HTTP level, in memory (card P0-07 tests; CONVENTIONS C6, C8).

It is an httpx2 MockTransport handed to `brain.egress.build_anthropic_client(..., transport=fake.transport)`,
so the REAL SDK builds and sends each request and parses each streamed reply, while no socket is opened.
A script is a list of steps, one per request:

    FakeAnthropic([sse_reply(text("Hello")), error(529), sse_reply(tool("get_body_status", {}))])

Steps: `sse_reply(*blocks, stop=..., usage=..., chunk=...)` a well-formed stream (text and tool_use blocks;
tool input JSON is split into `chunk`-character deltas); `error(status, ...)` an HTTP error; `raw(bytes)` any
bytes as the stream body (malformed or cut short); `stall(seconds, head)` sends `head` then goes silent.
Every request is recorded (method, path, headers, JSON body) for the tests to inspect.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx2

MODEL = "claude-haiku-5-5"


def text(t: str) -> dict[str, Any]:
    return {"kind": "text", "text": t}


def tool(name: str, args: Any, call_id: str = "toolu_01", raw_json: str | None = None) -> dict[str, Any]:
    return {"kind": "tool", "name": name, "args": args, "id": call_id, "raw": raw_json}


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def sse_events(*blocks: dict[str, Any], stop: str = "end_turn", usage: dict[str, int] | None = None, chunk: int = 7,
               model: str = MODEL, stop_event: bool = True) -> bytes:
    u = {"input_tokens": 120, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    u.update(usage or {})
    out = [_sse("message_start", {"type": "message_start", "message": {
        "id": "msg_fake", "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None,
        "stop_sequence": None, "usage": {k: v for k, v in u.items() if k != "output_tokens"} | {"output_tokens": 1}}})]
    for i, b in enumerate(blocks):
        if b["kind"] == "text":
            out.append(_sse("content_block_start", {"type": "content_block_start", "index": i,
                                                    "content_block": {"type": "text", "text": ""}}))
            for j in range(0, len(b["text"]), chunk):
                out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                                        "delta": {"type": "text_delta", "text": b["text"][j:j + chunk]}}))
        else:
            out.append(_sse("content_block_start", {"type": "content_block_start", "index": i, "content_block": {
                "type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}}))
            js = b["raw"] if b["raw"] is not None else json.dumps(b["args"])
            for j in range(0, len(js), chunk):
                out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                                        "delta": {"type": "input_json_delta", "partial_json": js[j:j + chunk]}}))
        out.append(_sse("content_block_stop", {"type": "content_block_stop", "index": i}))
    out.append(_sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                      "usage": {"output_tokens": u["output_tokens"]}}))
    if stop_event:
        out.append(_sse("message_stop", {"type": "message_stop"}))
    return b"".join(out)


@dataclass
class Step:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    delay_s: float = 0.0
    stall_after_s: float | None = None  # send the body, then hang this long before ending the stream
    json_body: Any = None


def sse_reply(*blocks: dict[str, Any], **kw: Any) -> Step:
    return Step(200, sse_events(*blocks, **kw), {"content-type": "text/event-stream"})


def error(status: int, kind: str = "overloaded_error", headers: dict[str, str] | None = None) -> Step:
    body = json.dumps({"type": "error", "error": {"type": kind, "message": "scripted failure"}}).encode()
    return Step(status, body, {"content-type": "application/json", **(headers or {})})


def raw(body: bytes) -> Step:
    return Step(200, body, {"content-type": "text/event-stream"})


def stall(seconds: float, head: bytes = b"") -> Step:
    return Step(200, head, {"content-type": "text/event-stream"}, stall_after_s=seconds)


class FakeAnthropic:
    """The scripted API. `models` is what GET /v1/models lists."""

    def __init__(self, steps: list[Step] | None = None, models: tuple[str, ...] = (MODEL, "claude-sonnet-5-5")):
        self.steps = list(steps or [])
        self.models = models
        self.requests: list[dict[str, Any]] = []
        self.transport = httpx2.MockTransport(self._handle)

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        body = await request.aread()
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = None
        self.requests.append({"method": request.method, "path": request.url.path, "host": request.url.host,
                              "headers": dict(request.headers), "json": parsed, "raw": body})
        if request.url.path.startswith("/v1/models"):
            data = [{"id": m, "type": "model", "display_name": m, "created_at": "2026-01-01T00:00:00Z"} for m in self.models]
            return httpx2.Response(200, json={"data": data, "has_more": False, "first_id": data[0]["id"] if data else None,
                                              "last_id": data[-1]["id"] if data else None})
        if not self.steps:
            return httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "no script"}})
        step = self.steps.pop(0)
        if step.delay_s:
            await asyncio.sleep(step.delay_s)
        if step.stall_after_s is not None:
            async def stream() -> Any:
                yield step.body
                await asyncio.sleep(step.stall_after_s)
            return httpx2.Response(step.status, headers=step.headers, content=stream())
        return httpx2.Response(step.status, headers=step.headers, content=step.body)

    @property
    def message_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["path"] == "/v1/messages"]
