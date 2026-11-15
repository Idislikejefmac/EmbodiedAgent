"""The model side of the agent loop (card P0-07): request and reply types, and the streamed-reply gate.

The agent loop talks to a `ModelClient`: `AnthropicModel` (the real API, through the client built in
brain/egress.py), `ScriptedModel` (brain/agent/scripted.py, for `chat --fake-model`), or a test double.
Every one of them returns a `ModelReply` or raises `ModelFailure`; nothing else leaves `complete`.

**The model's output is untrusted input.** The streamed events are checked here before anything uses them
(`_Accumulator`): known event and block types only (others are dropped and counted); block indexes in
range and at most MAX_MODEL_BLOCKS; tool-call ids of a fixed shape and unique; tool-call names are passed on
as data (the registry's allowlist decides); tool-call JSON at most MAX_ARGS_BYTES before it is parsed, and a
call whose JSON does not parse is handed to the registry as a string, which refuses it; text cut at
MAX_MODEL_TEXT_CHARS; at most MODEL_STREAM_EVENTS_MAX events; usage numbers must be whole numbers in range;
and a stream that ends without `message_stop` is a truncated reply (`ModelFailure("truncated")`), never a
silently shortened tool call. Timeouts: MODEL_STREAM_IDLE_S between events (the loop also bounds the call).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from ..limits import (MAX_ARGS_BYTES, MAX_MODEL_BLOCKS, MAX_MODEL_TEXT_CHARS, MODEL_BLOCK_MAX_BYTES,
                      MODEL_STREAM_EVENTS_MAX, MODEL_STREAM_IDLE_S, MODELS_LISTED_MAX)

if TYPE_CHECKING:
    from ..egress import AnthropicClient

log = logging.getLogger("brain.agent.model")

TOOL_CALL_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}(?!\n)$")
STOP_REASONS = frozenset({"end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "pause_turn"})
USAGE_MAX = 10**9  # a usage number above this is not believable
KEPT_BLOCKS = ("text", "tool_use", "thinking", "redacted_thinking")

TextSink = Callable[[str], None]


@dataclass(frozen=True)
class Usage:
    """Token counts from the API's usage fields (input, output, cache read, cache write)."""

    input_tok: int = 0
    output_tok: int = 0
    cache_read_tok: int = 0
    cache_write_tok: int = 0

    @property
    def total_tok(self) -> int:
        return self.input_tok + self.output_tok + self.cache_read_tok + self.cache_write_tok


@dataclass(frozen=True)
class ToolCall:
    """One tool call the model asked for. `arguments` is untrusted: the registry checks it (a call whose JSON
    did not parse carries the raw text, which the registry refuses as not an object)."""

    call_id: str
    name: str
    arguments: object


@dataclass(frozen=True)
class ModelRequest:
    """One request. `system_blocks`, `tools` and `messages` are in the Messages API shape (plain JSON)."""

    model: str
    max_tokens: int
    system_blocks: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...]
    messages: tuple[dict[str, Any], ...]
    effort: str | None = None


@dataclass(frozen=True)
class ModelReply:
    """A complete, checked reply. `blocks` is the assistant content to keep in the conversation (text,
    tool_use and thinking blocks, cleaned); `text` the reply's text (cut at MAX_MODEL_TEXT_CHARS)."""

    blocks: tuple[dict[str, Any], ...]
    text: str
    tool_calls: tuple[ToolCall, ...]
    stop_reason: str
    usage: Usage
    events: int = 0
    dropped: int = 0  # events or blocks of unknown types that were ignored
    text_cut: bool = False


class ModelFailure(Exception):
    """A model call did not give a usable reply. `code` is one word; `usage` is what was billed as far as
    known (a stream cut short still bills its input). No vendor text is carried."""

    def __init__(self, code: str, *, retryable: bool = False, usage: Usage | None = None,
                 retry_after_s: float | None = None, status: int | None = None):
        super().__init__(f"model call failed: {code}")
        self.code = code
        self.retryable = retryable
        self.usage = usage
        self.retry_after_s = retry_after_s
        self.status = status


class ModelClient(Protocol):
    """What the agent loop needs from a model."""

    name: str  # for events: "anthropic", "scripted", "fake"

    async def complete(self, request: ModelRequest, on_text: TextSink | None = None) -> ModelReply: ...

    async def list_models(self) -> list[str]: ...

    async def aclose(self) -> None: ...


# ---- the streamed-reply gate ------------------------------------------------------------------------------


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= USAGE_MAX else None


@dataclass
class _Block:
    kind: str
    call_id: str = ""
    name: str = ""
    parts: list[str] = field(default_factory=list)
    size: int = 0
    signature: str = ""
    data: str = ""
    closed: bool = False
    too_large: bool = False


class _Accumulator:
    """Builds a ModelReply from streamed event dicts, refusing what is malformed (see the module docstring)."""

    def __init__(self, on_text: TextSink | None = None):
        self.on_text = on_text
        self.blocks: dict[int, _Block] = {}
        self.usage = {"input_tok": 0, "output_tok": 0, "cache_read_tok": 0, "cache_write_tok": 0}
        self.stop_reason: str | None = None
        self.started = False
        self.stopped = False
        self.events = 0
        self.dropped = 0
        self.text_chars = 0
        self.text_cut = False

    def usage_so_far(self) -> Usage:
        return Usage(**self.usage)

    def fail(self, code: str, *, retryable: bool = False) -> ModelFailure:
        return ModelFailure(code, retryable=retryable, usage=self.usage_so_far())

    def _take_usage(self, raw: object) -> None:
        if not isinstance(raw, dict):
            return
        for src, dst in (("input_tokens", "input_tok"), ("output_tokens", "output_tok"),
                         ("cache_read_input_tokens", "cache_read_tok"), ("cache_creation_input_tokens", "cache_write_tok")):
            if src in raw and raw[src] is not None:
                n = _count(raw[src])
                if n is None:
                    raise self.fail("malformed")
                self.usage[dst] = max(self.usage[dst], n)  # streamed usage is cumulative

    def feed(self, ev: object) -> None:
        self.events += 1
        if self.events > MODEL_STREAM_EVENTS_MAX:
            raise self.fail("too_large")
        if not isinstance(ev, dict) or not isinstance(ev.get("type"), str):
            raise self.fail("malformed")
        if self.stopped:
            self.dropped += 1  # nothing counts after message_stop
            return
        kind = ev["type"]
        if kind == "message_start":
            msg = ev.get("message")
            if self.started or not isinstance(msg, dict):
                raise self.fail("malformed")
            self.started = True
            self._take_usage(msg.get("usage"))
            return
        if not self.started:
            raise self.fail("malformed")
        if kind == "content_block_start":
            self._start(ev)
        elif kind == "content_block_delta":
            self._delta(ev)
        elif kind == "content_block_stop":
            block = self._block(ev)
            if block is not None:
                block.closed = True
        elif kind == "message_delta":
            delta = ev.get("delta")
            if isinstance(delta, dict) and delta.get("stop_reason") is not None:
                reason = delta["stop_reason"]
                self.stop_reason = reason if isinstance(reason, str) and reason in STOP_REASONS else "unknown"
            self._take_usage(ev.get("usage"))
        elif kind == "message_stop":
            self.stopped = True
        else:
            self.dropped += 1  # ping, or a type this code does not know: ignored

    def _index(self, ev: dict[str, Any]) -> int:
        i = ev.get("index")
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < MAX_MODEL_BLOCKS:
            raise self.fail("malformed")
        return i

    def _block(self, ev: dict[str, Any]) -> _Block | None:
        i = self._index(ev)
        block = self.blocks.get(i)
        if block is None:
            raise self.fail("malformed")  # a delta or stop for a block that never started
        return None if block.kind == "dropped" else block

    def _start(self, ev: dict[str, Any]) -> None:
        i = self._index(ev)
        cb = ev.get("content_block")
        if i in self.blocks or not isinstance(cb, dict):
            raise self.fail("malformed")
        kind = cb.get("type")
        if kind not in KEPT_BLOCKS:
            self.blocks[i] = _Block("dropped")
            self.dropped += 1
            return
        block = _Block(str(kind))
        if kind == "tool_use":
            cid, name = cb.get("id"), cb.get("name")
            if not isinstance(cid, str) or not TOOL_CALL_ID.match(cid) or not isinstance(name, str):
                raise self.fail("malformed")
            if any(b.kind == "tool_use" and b.call_id == cid for b in self.blocks.values()):
                raise self.fail("malformed")  # duplicate id: results could not be matched to calls
            block.call_id, block.name = cid, name[:MAX_ARGS_BYTES]
            initial = cb.get("input")
            if isinstance(initial, dict) and initial:  # a non-streamed input (not usual when streaming)
                block.parts.append(json.dumps(initial)[: MAX_ARGS_BYTES + 1])
        elif kind == "text" and isinstance(cb.get("text"), str) and cb["text"]:
            self._text(block, cb["text"])
        elif kind == "redacted_thinking":
            data = cb.get("data")
            block.data = data if isinstance(data, str) and len(data) <= MODEL_BLOCK_MAX_BYTES else ""
            block.too_large = not block.data
        elif kind == "thinking":
            for key in ("thinking", "signature"):
                if isinstance(cb.get(key), str):
                    self._append(block, cb[key], signature=key == "signature")
        self.blocks[i] = block

    def _append(self, block: _Block, piece: str, *, signature: bool = False) -> None:
        block.size += len(piece)
        if block.size > MODEL_BLOCK_MAX_BYTES:
            block.too_large = True
            return
        if signature:
            block.signature += piece
        else:
            block.parts.append(piece)

    def _text(self, block: _Block, piece: str) -> None:
        room = MAX_MODEL_TEXT_CHARS - self.text_chars
        if room <= 0:
            self.text_cut = True
            return
        if len(piece) > room:
            piece, self.text_cut = piece[:room], True
        self.text_chars += len(piece)
        block.parts.append(piece)
        if self.on_text is not None and piece:
            try:
                self.on_text(piece)
            except Exception:  # broad-except: a display callback must never break the model call
                log.warning("the text display callback failed; the reply continues")

    def _delta(self, ev: dict[str, Any]) -> None:
        block = self._block(ev)
        if block is None:
            return  # a delta of a dropped block
        delta = ev.get("delta")
        if not isinstance(delta, dict) or block.closed:
            raise self.fail("malformed")
        kind = delta.get("type")
        if kind == "text_delta" and block.kind == "text" and isinstance(delta.get("text"), str):
            self._text(block, delta["text"])
        elif kind == "input_json_delta" and block.kind == "tool_use" and isinstance(delta.get("partial_json"), str):
            piece = delta["partial_json"]
            block.size += len(piece)
            if block.size > MAX_ARGS_BYTES:
                block.too_large = True  # stop keeping it; the call is refused below
            else:
                block.parts.append(piece)
        elif kind == "thinking_delta" and block.kind == "thinking" and isinstance(delta.get("thinking"), str):
            self._append(block, delta["thinking"])
        elif kind == "signature_delta" and block.kind == "thinking" and isinstance(delta.get("signature"), str):
            self._append(block, delta["signature"], signature=True)
        else:
            self.dropped += 1  # citations and other delta kinds: not used here

    def finish(self) -> ModelReply:
        if not self.stopped:
            raise self.fail("truncated", retryable=True)
        if any(not b.closed for b in self.blocks.values() if b.kind != "dropped"):
            raise self.fail("truncated", retryable=True)
        stop = self.stop_reason or "unknown"
        kept: list[dict[str, Any]] = []
        calls: list[ToolCall] = []
        texts: list[str] = []
        for i in sorted(self.blocks):
            b = self.blocks[i]
            if b.kind == "text":
                t = "".join(b.parts)
                texts.append(t)
                if t:
                    kept.append({"type": "text", "text": t})
            elif b.kind == "tool_use":
                raw = "".join(b.parts) or "{}"
                args: object
                if b.too_large:
                    args = "<arguments larger than the limit>"
                else:
                    try:
                        args = json.loads(raw)
                    except (ValueError, RecursionError):
                        args = raw  # not JSON: the registry refuses a string as arguments
                calls.append(ToolCall(b.call_id, b.name, args))
                kept.append({"type": "tool_use", "id": b.call_id, "name": b.name,
                             "input": args if isinstance(args, dict) else {}})
            elif b.kind == "thinking":
                if b.too_large:
                    raise self.fail("too_large")
                kept.append({"type": "thinking", "thinking": "".join(b.parts), "signature": b.signature})
            elif b.kind == "redacted_thinking":
                if b.too_large:
                    raise self.fail("too_large")
                kept.append({"type": "redacted_thinking", "data": b.data})
        return ModelReply(tuple(kept), "".join(texts), tuple(calls), stop, self.usage_so_far(), self.events,
                          self.dropped, self.text_cut)


async def _read_stream(events: AsyncIterator[dict[str, Any]], on_text: TextSink | None = None,
                      idle_s: float = MODEL_STREAM_IDLE_S) -> ModelReply:
    """Turn a stream of event dicts into a checked ModelReply (raises ModelFailure). A stream that is silent
    for `idle_s` seconds is abandoned as `stalled`."""
    acc = _Accumulator(on_text)
    iterator = aiter(events)
    try:
        while True:
            try:
                ev = await asyncio.wait_for(anext(iterator), idle_s)
            except StopAsyncIteration:
                break
            except TimeoutError:
                raise acc.fail("stalled", retryable=True) from None
            acc.feed(ev)
        return acc.finish()
    finally:
        closer = getattr(iterator, "aclose", None)
        if closer is not None:
            try:
                await closer()
            except Exception:  # broad-except: closing an abandoned stream must not hide the real outcome
                log.debug("closing the model stream failed")


# ---- the real API ---------------------------------------------------------------------------------------------


class AnthropicModel:
    """ModelClient over the API client built by brain/egress.py (`build_anthropic_client`)."""

    name = "anthropic"

    def __init__(self, client: AnthropicClient):
        self._client = client  # plain dicts in and out, VendorError on failure

    @staticmethod
    def params(request: ModelRequest) -> dict[str, Any]:
        """The request as Messages API parameters (also what the prompt capture records)."""
        params: dict[str, Any] = {"model": request.model, "max_tokens": request.max_tokens,
                                  "system": list(request.system_blocks), "messages": list(request.messages)}
        if request.tools:
            params["tools"] = list(request.tools)
        if request.effort:
            params["output_config"] = {"effort": request.effort}
        return params

    async def complete(self, request: ModelRequest, on_text: TextSink | None = None) -> ModelReply:
        from ..egress import VendorError

        try:
            return await _read_stream(self._client.stream_events(self.params(request)), on_text)
        except VendorError as e:
            raise ModelFailure(e.code, retryable=e.retryable, retry_after_s=e.retry_after_s, status=e.status) from None

    async def list_models(self) -> list[str]:
        from ..egress import VendorError

        try:
            ids: list[str] = await self._client.list_model_ids(MODELS_LISTED_MAX)
        except VendorError as e:
            raise ModelFailure(e.code, retryable=e.retryable, status=e.status) from None
        return ids

    async def aclose(self) -> None:
        await self._client.aclose()


def describe_failure(e: ModelFailure) -> dict[str, Any]:
    """Event fields for a failure: code, status, retryable. Never vendor text."""
    obs_fields: dict[str, Any] = {"code": e.code, "retryable": e.retryable}
    if e.status is not None:
        obs_fields["status"] = e.status
    return obs_fields


__all__ = ["AnthropicModel", "ModelClient", "ModelFailure", "ModelReply", "ModelRequest", "ToolCall", "Usage",
           "describe_failure"]
