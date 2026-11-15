"""The agent loop (card P0-07): one turn in, a spoken reply out, tools in between. docs/agent_loop.md.

    agent = Agent(registry, model, guard, settings=AgentSettings.from_config(config), prompt=load_system_prompt())
    result = await agent.run_turn(TurnInput("what is your battery level?", InputSource.LOCAL_CONSOLE))

`run_turn` never raises (cancellation aside) and always returns a TurnResult; every failure becomes an
outcome code, an event and a short spoken line. Turns are serialised (one at a time).

**The channel rule** (docs/body_tools_egress_v0.md section 2): the `Caller` for every tool call is built by
`Caller.for_turn` from the sources of ALL the content in the model's context at that moment (earlier turns
kept in the conversation, this turn's input, pictures, and every tool result so far), never from anything
the model says. Only content typed at the local console is CONSOLE; one piece of body content (speech, a
picture, a Poke) or one tool result makes the turn APP. The model's choice of tool and arguments is
untrusted input: it goes to `ToolRegistry.call`, whose allowlist, channel rules and argument gate decide.

**Bounds** (brain/limits.py): MAX_TURN_TOOL_CALLS tool calls per turn (the loop breaker); MAX_TURN_SECONDS
per turn and MODEL_CALL_TIMEOUT_S per model call; MODEL_RETRIES_MAX retries of a retryable failure, each
through the spend guard; [caps] max_tokens on every request; at most MAX_IMAGES_IN_CONTEXT pictures in the
context (older ones become a short note); HISTORY_TURNS_MAX turns kept; CONSOLE_MAX_CHARS of input;
TOOL_RESULT_MAX_BYTES per tool result; the reply is cut to what the speak tool accepts.

**Models are chosen by code**: [models] live_conversation (low effort) for every turn, careful_looking only
for a turn the caller marks `careful` (an explicit "look carefully" request). The model never chooses.

**Caching**: the system prompt, the tool definitions and the memory block (an empty placeholder until card
P4-08) carry `cache_control`; the cost events show the cache reads and writes.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
import json
import logging
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import obs
from ..body.types import Image
from ..config import Config
from ..limits import (CONSOLE_MAX_CHARS, HISTORY_TURNS_MAX, MAX_IMAGES_IN_CONTEXT, MAX_TURN_SECONDS,
                      MAX_TURN_TOOL_CALLS, MODEL_CALL_TIMEOUT_S, MODEL_RETRIES_MAX, MODEL_RETRY_BACKOFF_S,
                      POKE_MIN_INTERVAL_S, SYSTEM_PROMPT_MAX_BYTES, TOOL_RESULT_MAX_BYTES)
from ..obs.files import read_capped
from .budget import Refusal, SpendGuard
from .capture import PromptCapture
from .model import ModelClient, ModelFailure, ModelReply, ModelRequest, TextSink, ToolCall, describe_failure
from .tools import Caller, InputSource, ToolRegistry, ToolResult

log = logging.getLogger("brain.agent.loop")

SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "system_v0.md"
MEMORY_PLACEHOLDER = "Memory notes about people and places: none yet."
IMAGE_TOKENS_WORST = 3136  # (1568 / 28) squared: the most one picture can cost in input tokens
CHARS_PER_TOKEN_LOW = 3  # a cautious chars-per-token ratio for the reservation estimate (real text is about 4)
IMAGE_REMOVED_NOTE = "(An earlier picture was removed from this conversation to keep its cost down.)"
TURN_GRACE_S = 5.0  # the backstop allows this long past MAX_TURN_SECONDS for the turn to wind down

FALLBACK = {
    "budget": "I have reached my spending limit, so I can't think about that right now.",
    "loop_limit": "Sorry, I got stuck going round in circles, so I stopped.",
    "timeout": "Sorry, that took me too long, so I stopped.",
    "refusal": "Sorry, I can't help with that.",
    "empty": "Sorry, I don't have an answer for that.",
    "truncated": "Sorry, my answer got cut off. Please ask again.",
    "malformed": "Sorry, something went wrong with my thinking. Please ask again.",
    "model_error": "Sorry, I can't think right now. Please try again in a moment.",
    "internal": "Sorry, something went wrong inside me.",
}
MODEL_FAILURE_OUTCOME = {"truncated": "truncated", "malformed": "malformed", "too_large": "malformed",
                         "stalled": "timeout", "timeout": "timeout"}
_LABEL = {InputSource.LOCAL_CONSOLE: "[console]", InputSource.REMOTE_CONSOLE: "[remote console]",
          InputSource.BODY: "[app | unknown speaker]", InputSource.TOOL_RESULT: "[tool result]"}
_MARKUP = re.compile(r"[*#_`~>|\\]+|\[|\]|\(|\)|https?://\S+")
_SPACES = re.compile(r"\s+")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


class PromptError(ValueError):
    """The system prompt file cannot be used; the agent does not start (fail closed)."""


@dataclass(frozen=True)
class SystemPrompt:
    text: str
    sha256: str
    name: str


def load_system_prompt(path: Path | None = None) -> SystemPrompt:
    """Read and check the system prompt file (one file under version control). Missing, empty, larger than
    SYSTEM_PROMPT_MAX_BYTES, not UTF-8, or holding control characters other than new lines: PromptError."""
    p = path or SYSTEM_PROMPT_PATH
    data, why = read_capped(p, SYSTEM_PROMPT_MAX_BYTES)
    if data is None:
        raise PromptError(f"the system prompt {p.name} cannot be used ({why})")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise PromptError(f"the system prompt {p.name} is not UTF-8 text") from None
    if not text.strip():
        raise PromptError(f"the system prompt {p.name} is empty")
    if any(unicodedata.category(c) in ("Cc", "Cf") and c not in "\n\r\t" for c in text):
        raise PromptError(f"the system prompt {p.name} holds control or invisible characters")
    return SystemPrompt(text.replace("\r\n", "\n").strip(), hashlib.sha256(data).hexdigest(), p.name)


# ---- input and output text ---------------------------------------------------------------------------------


def clean_input(text: str) -> tuple[str, bool]:
    """Typed or body text as model input: control and invisible characters removed (new lines kept as spaces),
    square brackets turned into round ones (a label is the system's alone, so none can be forged in the text),
    cut to CONSOLE_MAX_CHARS. Returns (text, was it cut)."""
    if not isinstance(text, str):
        return "", False
    kept = "".join(" " if c in "\n\r\t" else c for c in text
                   if c in "\n\r\t" or unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"))
    kept = kept.replace("[", "(").replace("]", ")").strip()
    return kept[:CONSOLE_MAX_CHARS], len(kept) > CONSOLE_MAX_CHARS


def speech_text(text: str, max_chars: int) -> str:
    """Model text as words to speak: markup symbols, links and control characters removed, white space
    collapsed, cut at the last sentence end within `max_chars` (else at `max_chars`)."""
    kept = "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn") or c in "\n\t")
    kept = _SPACES.sub(" ", _MARKUP.sub(" ", kept)).strip()
    if len(kept) <= max_chars:
        return kept
    head = kept[:max_chars]
    ends = [m.end() for m in _SENTENCE_END.finditer(head)]
    return head[: ends[-1]] if ends else head


# ---- turns ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnInput:
    """One thing that starts a turn. `source` is where it came from (never from a model); `kind` is text,
    poke or look; `careful` selects the careful-looking model (code decides, never the model)."""

    text: str
    source: InputSource
    kind: str = "text"
    images: tuple[Image, ...] = ()
    careful: bool = False


@dataclass(frozen=True)
class TurnResult:
    turn_id: str
    outcome: str  # ok, budget, loop_limit, timeout, refusal, empty, truncated, malformed, model_error, internal
    reply: str  # what was said (or printed when it could not be said)
    spoken: bool
    tool_calls: int
    model_calls: int
    channel: str
    cost_usd: float
    code: str | None = None  # the failure's code (a model error code, the cap that stopped it)
    warning: str | None = None  # a spend warning said with the reply


@dataclass
class AgentSettings:
    live_model: str = "claude-haiku-5-5"
    careful_model: str = "claude-sonnet-5-5"
    max_tokens: int = 1024
    live_effort: str | None = "low"
    careful_effort: str | None = "medium"

    @classmethod
    def from_config(cls, config: Config) -> AgentSettings:
        return cls(config.models.live_conversation, config.models.careful_looking, config.caps.max_tokens)


class PokeGate:
    """At most one Poke turn per POKE_MIN_INTERVAL_S, and none while one is waiting or running; the rest are
    dropped and counted (doc 10 PokeGate)."""

    def __init__(self, min_interval_s: float = POKE_MIN_INTERVAL_S, now: Callable[[], float] = time.monotonic):
        self.min_interval_s = min_interval_s
        self._now = now
        self._last: float | None = None
        self.busy = False
        self.admitted = 0
        self.dropped = 0

    def admit(self) -> bool:
        t = self._now()
        if self.busy or (self._last is not None and t - self._last < self.min_interval_s):
            self.dropped += 1
            return False
        self._last, self.busy = t, True
        self.admitted += 1
        return True

    def done(self) -> None:
        self.busy = False


@dataclass
class _Turn:
    turn_id: str
    inp: TurnInput
    deadline: float
    messages: list[dict[str, Any]] = field(default_factory=list)
    sources: list[InputSource] = field(default_factory=list)
    tool_calls: int = 0
    model_calls: int = 0
    cost: float = 0.0
    spoke: bool = False
    warning: str | None = None

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


@dataclass
class _Kept:
    messages: list[dict[str, Any]]
    sources: list[InputSource]


def _image_block(image: Image) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": image.media_type,
                                        "data": base64.b64encode(image.data).decode("ascii")}}


def _walk_strings(value: Any, depth: int = 0) -> tuple[int, int]:
    """(characters of text, number of images) in a request part, without looking at image data."""
    if depth > 40:
        return 0, 0
    if isinstance(value, dict):
        if value.get("type") == "image":
            return 0, 1
        chars, images = 0, 0
        for k, v in value.items():
            c, i = _walk_strings(v, depth + 1)
            chars, images = chars + c + len(str(k)), images + i
        return chars, images
    if isinstance(value, list | tuple):
        chars, images = 0, 0
        for v in value:
            c, i = _walk_strings(v, depth + 1)
            chars, images = chars + c, images + i
        return chars, images
    return len(str(value)), 0


def estimate_input_tokens(request: ModelRequest) -> int:
    """A cautious upper estimate of a request's input tokens, for the spend reservation."""
    chars, images = _walk_strings([request.system_blocks, request.tools, request.messages])
    return chars // CHARS_PER_TOKEN_LOW + 1 + images * IMAGE_TOKENS_WORST


class Agent:
    """See the module docstring. `on_text` gets the reply's text as it streams (for a console display)."""

    def __init__(self, registry: ToolRegistry, model: ModelClient, guard: SpendGuard, *, settings: AgentSettings,
                 prompt: SystemPrompt, capture: PromptCapture | None = None, on_text: TextSink | None = None):
        self.registry = registry
        self.model = model
        self.guard = guard
        self.settings = settings
        self.prompt = prompt
        self.capture = capture
        self.on_text = on_text
        self._kept: list[_Kept] = []
        self._lock = asyncio.Lock()
        self._turn_numbers = itertools.count(1)
        self._call_numbers = itertools.count(1)
        spec = registry.catalogue.tools.get("speak")
        schema = spec.input_schema if spec is not None and spec.input_schema else {}
        self.speak_max_chars = int(schema.get("properties", {}).get("text", {}).get("maxLength", 2000))
        log.info("agent ready: system prompt %s sha256 %s (%d chars); live model %s; careful model %s",
                 prompt.name, prompt.sha256[:16], len(prompt.text), settings.live_model, settings.careful_model)

    # ---- context ----------------------------------------------------------------------------------------

    @property
    def history_turns(self) -> int:
        return len(self._kept)

    def context_sources(self) -> list[InputSource]:
        """Sources of everything kept in the conversation (earlier turns)."""
        return [s for k in self._kept for s in k.sources]

    def reset(self) -> None:
        """Forget the conversation (used after a request the API refused as malformed)."""
        self._kept.clear()

    def _system_blocks(self) -> tuple[dict[str, Any], ...]:
        return ({"type": "text", "text": self.prompt.text},
                {"type": "text", "text": MEMORY_PLACEHOLDER, "cache_control": {"type": "ephemeral"}})

    def _tools(self, caller: Caller) -> tuple[dict[str, Any], ...]:
        tools = [dict(t) for t in self.registry.model_tools(caller.channel)]
        if tools:
            tools[-1]["cache_control"] = {"type": "ephemeral"}
        return tuple(tools)

    def _enforce_image_rule(self, messages: list[dict[str, Any]]) -> int:
        """Keep the newest MAX_IMAGES_IN_CONTEXT pictures; replace older ones (in place) with a short note.
        Returns how many pictures remain."""
        seen = 0

        def visit(blocks: list[Any]) -> None:
            nonlocal seen
            for i in range(len(blocks) - 1, -1, -1):
                b = blocks[i]
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "image":
                    seen += 1
                    if seen > MAX_IMAGES_IN_CONTEXT:
                        blocks[i] = {"type": "text", "text": IMAGE_REMOVED_NOTE}
                elif b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                    visit(b["content"])

        for msg in reversed(messages):
            if isinstance(msg.get("content"), list):
                visit(msg["content"])
        return min(seen, MAX_IMAGES_IN_CONTEXT)

    def _request(self, t: _Turn, caller: Caller) -> ModelRequest:
        messages = [m for k in self._kept for m in k.messages] + t.messages
        self._enforce_image_rule(messages)
        careful = t.inp.careful
        s = self.settings
        return ModelRequest(s.careful_model if careful else s.live_model, s.max_tokens, self._system_blocks(),
                            self._tools(caller), tuple(messages), s.careful_effort if careful else s.live_effort)

    def _user_message(self, inp: TurnInput) -> tuple[dict[str, Any], int]:
        if inp.kind == "poke":
            text, cut = "The poke button on my body was pressed.", False
            label = "[app event]"
        else:
            text, cut = clean_input(inp.text)
            label = _LABEL.get(inp.source, "[app | unknown speaker]")
        content: list[dict[str, Any]] = [{"type": "text", "text": f"{label}: {text or '(nothing was said)'}"}]
        content += [_image_block(img) for img in inp.images]
        return {"role": "user", "content": content}, len(text) + (1 if cut else 0)

    # ---- one turn ---------------------------------------------------------------------------------------

    async def run_turn(self, inp: TurnInput) -> TurnResult:
        """Run one turn to its end. Never raises (cancellation aside)."""
        async with self._lock:
            turn_id = f"t-{next(self._turn_numbers)}"
            with obs.correlate(turn=turn_id):
                return await self._guarded(turn_id, inp)

    async def _guarded(self, turn_id: str, inp: TurnInput) -> TurnResult:
        t0 = time.monotonic()
        t = _Turn(turn_id, inp, t0 + MAX_TURN_SECONDS)
        try:
            source = InputSource(inp.source)
        except ValueError:
            source = InputSource.BODY  # an unknown source is treated as the least privileged
        images = tuple(i for i in inp.images if isinstance(i, Image))
        t.inp = TurnInput(inp.text if isinstance(inp.text, str) else "", source, inp.kind, images, bool(inp.careful))
        t.sources = [source] + [InputSource.BODY] * len(images)
        obs.emit("turn.start", f"turn {turn_id} ({t.inp.kind})", turn_id=turn_id, kind=t.inp.kind,
                 sources=sorted({s.value for s in self.context_sources() + t.sources}), images=len(images),
                 chars=len(t.inp.text), careful=t.inp.careful, history_turns=len(self._kept))
        try:
            result = await asyncio.wait_for(self._turn(t), MAX_TURN_SECONDS + TURN_GRACE_S)
        except TimeoutError:
            result = await self._fail(t, "timeout", "turn_deadline")
        except Exception as e:  # broad-except: a turn must end with a result and a spoken line, never an exception
            log.exception("turn %s failed unexpectedly", turn_id)
            result = await self._fail(t, "internal", type(e).__name__)
        obs.emit("turn.end", f"turn {turn_id} {result.outcome}", turn_id=turn_id, outcome=result.outcome,
                 code=result.code, tool_calls=result.tool_calls, model_calls=result.model_calls, spoken=result.spoken,
                 channel=result.channel, cost_usd=round(result.cost_usd, 8),
                 ms=round((time.monotonic() - t0) * 1000, 1), level="INFO" if result.outcome == "ok" else "WARNING")
        return result

    def _caller(self, t: _Turn) -> Caller:
        return Caller.for_turn(self.context_sources() + t.sources)

    async def _turn(self, t: _Turn) -> TurnResult:
        message, _ = self._user_message(t.inp)
        t.messages.append(message)
        while True:
            if t.remaining() <= 0:
                return await self._fail(t, "timeout", "turn_deadline")
            caller = self._caller(t)
            request = self._request(t, caller)
            reply = await self._call_model(t, request)
            if not isinstance(reply, ModelReply):
                return await self._fail(t, *reply)
            calls = reply.tool_calls
            if calls and reply.stop_reason != "tool_use":
                return await self._fail(t, "truncated" if reply.stop_reason == "max_tokens" else "malformed",
                                        f"tool calls with stop reason {reply.stop_reason}")
            if reply.stop_reason == "tool_use":
                if not calls:
                    return await self._fail(t, "malformed", "tool_use without a tool call")
                if t.tool_calls + len(calls) > MAX_TURN_TOOL_CALLS:
                    log.warning("turn %s: the model asked for more than %d tool calls; the turn stops", t.turn_id,
                                MAX_TURN_TOOL_CALLS)
                    return await self._fail(t, "loop_limit", f"{t.tool_calls + len(calls)} calls asked")
                t.messages.append({"role": "assistant", "content": list(reply.blocks)})
                results = []
                for call in calls:
                    result = await self._run_tool(t, call)
                    if result is None:
                        return await self._fail(t, "timeout", "turn_deadline")
                    results.append(result)
                t.messages.append({"role": "user", "content": results})
                continue
            if reply.stop_reason == "refusal":
                return await self._fail(t, "refusal", "refusal")
            if reply.stop_reason not in ("end_turn", "stop_sequence", "pause_turn", "max_tokens"):
                return await self._fail(t, "malformed", f"stop reason {reply.stop_reason}")
            words = speech_text(reply.text, self.speak_max_chars)
            if not words and not t.spoke:
                return await self._fail(t, "empty" if reply.stop_reason != "max_tokens" else "truncated", "no text")
            if reply.blocks:
                t.messages.append({"role": "assistant", "content": list(reply.blocks)})
            else:
                t.messages.append({"role": "assistant", "content": [{"type": "text", "text": "(no words)"}]})
            self._commit(t)
            spoken = t.spoke
            if words:
                spoken = await self._say(t, words + (f" {t.warning}" if t.warning else ""))
            return TurnResult(t.turn_id, "ok", words, spoken, t.tool_calls, t.model_calls,
                              self._caller(t).channel.value, t.cost, None, t.warning)

    def _commit(self, t: _Turn) -> None:
        self._kept.append(_Kept(t.messages, t.sources))
        while len(self._kept) > HISTORY_TURNS_MAX:
            self._kept.pop(0)

    async def _run_tool(self, t: _Turn, call: ToolCall) -> dict[str, Any] | None:
        """Run one tool call through the registry with the turn's Caller. None when the turn ran out of time."""
        t.tool_calls += 1
        caller = self._caller(t)
        remaining = t.remaining()
        if remaining <= 0:
            return None
        try:
            r = await asyncio.wait_for(self.registry.call(call.name, call.arguments, caller), remaining)
        except TimeoutError:
            obs.emit("tool.timeout", "a tool call outlasted the turn", tool=call.name[:64], timeout_s=round(remaining, 2))
            return None
        t.sources.append(InputSource.TOOL_RESULT)
        if call.name == "speak" and r.ok:
            t.spoke = True
        return self._tool_result_block(call, r)

    @staticmethod
    def _tool_result_block(call: ToolCall, r: ToolResult) -> dict[str, Any]:
        text = json.dumps(r.to_json(), ensure_ascii=False)
        failed = not r.ok
        if len(text.encode("utf-8")) > TOOL_RESULT_MAX_BYTES:
            failed = True
            text = json.dumps({"ok": False, "status": "error", "code": "result_too_large",
                               "message": "That result was too large to use."})
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        content += [_image_block(a) for a in r.attachments[:MAX_IMAGES_IN_CONTEXT]]
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call.call_id, "content": content}
        if failed:
            block["is_error"] = True
        return block

    async def _call_model(self, t: _Turn, request: ModelRequest) -> ModelReply | tuple[str, str]:
        """One model call with at most MODEL_RETRIES_MAX retries; (outcome, code) when it gives no reply."""
        attempt = 0
        while True:
            est = estimate_input_tokens(request)
            res = self.guard.reserve(request.model, est, request.max_tokens)
            if isinstance(res, Refusal):
                t.warning = None
                return "budget", res.cap
            call_id = f"m-{next(self._call_numbers)}"
            t.model_calls += 1
            obs.emit("model.call_start", f"{request.model} request", model=request.model, call=call_id,
                     attempt=attempt, est_in_tok=est, max_out_tok=request.max_tokens, tools=len(request.tools),
                     messages=len(request.messages), client=self.model.name)
            if self.capture is not None:
                self.capture.request(call_id, request)
            t0 = time.monotonic()
            failure: ModelFailure | None = None
            reply: ModelReply | None = None
            timeout = min(MODEL_CALL_TIMEOUT_S, t.remaining())
            try:
                if timeout <= 0:
                    raise ModelFailure("timeout")
                reply = await asyncio.wait_for(self.model.complete(request, self.on_text), timeout)
                if not isinstance(reply, ModelReply):
                    raise ModelFailure("malformed")
            except ModelFailure as e:
                failure = e
            except TimeoutError:
                failure = ModelFailure("timeout")
            except Exception as e:  # broad-except: a model client bug is one failed call, not a crashed turn
                log.warning("the model client raised %s; treated as a failed call", type(e).__name__)
                failure = ModelFailure("internal")
            cost, warning = self.guard.settle(res, reply.usage if reply is not None else failure.usage if failure else None)
            t.cost += cost
            if warning and not t.warning:
                t.warning = warning
            ms = round((time.monotonic() - t0) * 1000, 1)
            if reply is not None:
                u = reply.usage
                obs.emit("model.call_end", f"{request.model} {reply.stop_reason}", model=request.model, call=call_id,
                         ok=True, stop=reply.stop_reason, in_tok=u.input_tok, out_tok=u.output_tok,
                         cache_read_tok=u.cache_read_tok, cache_write_tok=u.cache_write_tok, ms=ms,
                         blocks=len(reply.blocks), tool_calls=len(reply.tool_calls), dropped=reply.dropped,
                         text_cut=reply.text_cut)
                if self.capture is not None:
                    self.capture.reply(call_id, reply)
                return reply
            if failure is None:  # cannot happen; keeps the types honest
                failure = ModelFailure("internal")
            obs.emit("model.call_end", f"{request.model} failed: {failure.code}", model=request.model, call=call_id,
                     ok=False, ms=ms, attempt=attempt, level="WARNING", **describe_failure(failure))
            if self.capture is not None:
                self.capture.failure(call_id, failure)
            if failure.code == "bad_request":
                self.reset()  # the conversation itself may be what the API refused: start it afresh
            wait = min(max(failure.retry_after_s or 0.0, MODEL_RETRY_BACKOFF_S), MODEL_RETRY_BACKOFF_S * 4)
            if failure.retryable and attempt < MODEL_RETRIES_MAX and t.remaining() > wait + 1.0:
                attempt += 1
                await asyncio.sleep(wait)
                continue
            return MODEL_FAILURE_OUTCOME.get(failure.code, "model_error"), failure.code

    async def _say(self, t: _Turn, words: str) -> bool:
        """Speak through the speak tool (the same path, schema and boundary as a model's call). True if said."""
        if not words:
            return False
        r = await self.registry.call("speak", {"text": words[: self.speak_max_chars]}, self._caller(t))
        if not r.ok:
            log.info("turn %s: the reply could not be spoken (%s); it is shown on the console only", t.turn_id, r.code)
        return r.ok

    async def _fail(self, t: _Turn, outcome: str, code: str) -> TurnResult:
        """End a turn that did not finish: nothing of it is kept in the conversation, a fixed line is said."""
        line = FALLBACK.get(outcome, FALLBACK["internal"])
        if outcome == "budget":
            line = FALLBACK["budget"]
        log.warning("turn %s ended: %s (%s)", t.turn_id, outcome, code)
        t.messages = []
        caller = Caller.for_turn(self.context_sources() + t.sources)
        spoken = False
        try:
            spoken = (await asyncio.wait_for(self.registry.call("speak", {"text": line}, caller),
                                             MODEL_CALL_TIMEOUT_S)).ok
        except TimeoutError:
            spoken = False
        return TurnResult(t.turn_id, outcome, line, spoken, t.tool_calls, t.model_calls, caller.channel.value,
                          t.cost, code, None)
