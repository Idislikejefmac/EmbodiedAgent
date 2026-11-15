"""`python -m brain chat`: the agent loop with a typed console (card P0-07; docs/agent_loop.md).

    python -m brain chat                 # SimBody, the scripted model (no key, no network, no spend)
    python -m brain chat --as-app        # the same lines as APP-channel turns (body content, untrusted)
    python -m brain chat --live          # the real model API (needs the key, [models] allow_live_calls, caps)
    python -m brain chat --body phone    # the body over the link (speech needs the body's speak_local)

Typed lines are LOCAL_CONSOLE turns (the privileged channel), unless --as-app. Console commands:
`/poke` (SimBody: raise the body's poke event; a real body has its own button), `/look <question>` (a
careful-look turn: the code takes a picture and uses the careful-looking model), `/quit`. End of input
also ends the session. The live trace of events is shown unless --quiet (the event files are written too:
`python -m brain watch --follow` in another console shows the same).

This module is a wiring point: it may name SimBody (for /poke). It never names a specific device body.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import unicodedata
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import obs
from .agent.budget import PriceTable, SpendGuard
from .agent.loop import Agent, PokeGate, TurnInput, TurnResult
from .agent.model import ModelClient
from .agent.tools import Caller, InputSource
from .config import Config, ConfigError
from .body.base import Body
from .body.types import ADAPTER_EVENT_NAMES, Image
from .limits import CONSOLE_MAX_CHARS
from .obs.viewer import format_event

log = logging.getLogger("brain.chat")

HELP = "commands: /poke  /look <question>  /quit   (anything else is said to the agent)"
STDIN_READ_CHARS = CONSOLE_MAX_CHARS * 4  # one read; a longer line is cut here and its rest discarded


def printable(text: str) -> str:
    """Model or body text made safe for the terminal: control and invisible characters removed (new lines kept)."""
    return "".join(c for c in text if c == "\n" or unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn"))


def stdin_lines(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[str | None]) -> threading.Thread:
    """Read lines from stdin in a daemon thread (so a blocked read never keeps the process alive); each line
    goes to `queue`, at most STDIN_READ_CHARS of it; None marks the end of input."""

    def read() -> None:
        try:
            while True:
                chunk = sys.stdin.readline(STDIN_READ_CHARS)
                if not chunk:
                    break
                if not chunk.endswith("\n") and len(chunk) >= STDIN_READ_CHARS:
                    while True:  # discard the rest of an over-long line
                        more = sys.stdin.readline(STDIN_READ_CHARS)
                        if not more or more.endswith("\n"):
                            break
                loop.call_soon_threadsafe(queue.put_nowait, chunk.rstrip("\r\n"))
        except (OSError, ValueError, UnicodeDecodeError) as e:
            log.warning("console input ended: %s", type(e).__name__)
        finally:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, None)
            except RuntimeError:  # swallowed: the event loop already closed; nothing waits for input any more
                pass

    thread = threading.Thread(target=read, name="console-input", daemon=True)
    thread.start()
    return thread


@dataclass
class ChatOptions:
    as_app: bool = False
    trace: bool = True
    live: bool = False


class ChatSession:
    """Feeds console lines and body pokes to the agent and shows what happens."""

    def __init__(self, agent: Agent, body: Body, guard: SpendGuard, *, options: ChatOptions,
                 out: Callable[[str], None] = print, poke_gate: PokeGate | None = None):
        self.agent = agent
        self.body = body
        self.guard = guard
        self.options = options
        self.out = out
        self.poke_gate = poke_gate or PokeGate()
        self.results: list[TurnResult] = []
        self._mid_line = False
        self._streamed = False  # reply text was shown as it streamed in this turn
        self._poke_tasks: set[asyncio.Task[None]] = set()
        agent.on_text = self.show_text

    # ---- output ------------------------------------------------------------------------------------------

    def _line(self, text: str) -> None:
        if self._mid_line:
            self.out("")
            self._mid_line = False
        self.out(text)

    def show_text(self, piece: str) -> None:
        """Streamed reply text, shown as it arrives."""
        text = printable(piece)
        if not text:
            return
        self._streamed = True
        if not self._mid_line:
            sys.stdout.write("agent> ")
            self._mid_line = True
        sys.stdout.write(text)
        sys.stdout.flush()

    def show_event(self, event: Mapping[str, Any]) -> None:
        if self.options.trace:
            self._line("  " + format_event(event))

    def show_result(self, r: TurnResult) -> None:
        if r.outcome != "ok" or not self._streamed:
            self._line(f"agent> {printable(r.reply)}")
        elif self._mid_line:
            self._line("")
        self._streamed = False
        spend = "scripted model: no real spend" if not self.options.live else self.guard.summary()
        self.out(f"  [{r.turn_id} {r.outcome}{f' ({r.code})' if r.code else ''}; {r.tool_calls} tool call(s); "
                 f"{'spoken' if r.spoken else 'not spoken'}; channel {r.channel}] cost ${r.cost_usd:.6f}; {spend}")

    # ---- input -------------------------------------------------------------------------------------------

    def _source(self) -> InputSource:
        return InputSource.BODY if self.options.as_app else InputSource.LOCAL_CONSOLE

    async def turn(self, inp: TurnInput) -> TurnResult:
        r = await self.agent.run_turn(inp)
        self.results.append(r)
        self.show_result(r)
        return r

    async def _look(self, question: str) -> TurnInput:
        """A careful-look turn: the code (not the model) takes the picture, through the registry."""
        r = await self.agent.registry.call("capture_image", {"reason": "a careful look was asked for"},
                                           Caller.for_turn([self._source()]))
        images: tuple[Image, ...] = r.attachments if r.ok else ()
        if not r.ok:
            self._line(f"  (no picture: {r.message})")
        return TurnInput(question or "What do you see?", self._source(), "look", images, careful=True)

    async def line(self, text: str) -> bool:
        """Handle one console line. False when the session should end."""
        cmd = text.strip()
        if not cmd:
            return True
        if cmd in ("/quit", "/exit"):
            return False
        if cmd == "/help":
            self.out(HELP)
            return True
        if cmd == "/poke":
            inject = getattr(self.body, "inject_event", None)
            if callable(inject):
                inject("poke", {})
                await asyncio.sleep(0.05)  # let the body's event reach the poke handler
            else:
                self.out("  (this body has its own Poke button)")
            return True
        if cmd == "/look" or cmd.startswith("/look "):
            await self.turn(await self._look(cmd[5:].strip()))
            return True
        await self.turn(TurnInput(text, self._source()))
        return True

    async def _poke(self) -> None:
        try:
            await self.turn(TurnInput("", InputSource.BODY, "poke"))
        finally:
            self.poke_gate.done()

    async def on_body_event(self, name: str, source: str) -> None:
        """A body event: a Poke starts a turn through the gate (at most one per POKE_MIN_INTERVAL_S)."""
        if name != "poke" or source == "adapter" or name in ADAPTER_EVENT_NAMES:
            return
        if not self.poke_gate.admit():
            log.info("poke dropped by the gate (%d dropped so far)", self.poke_gate.dropped)
            return
        task = asyncio.create_task(self._poke())
        self._poke_tasks.add(task)
        task.add_done_callback(self._poke_tasks.discard)

    async def watch_body(self) -> None:
        stream = self.body.events()
        try:
            async for ev in stream:
                await self.on_body_event(ev.name, ev.source)
        finally:
            await stream.aclose()

    async def run(self, lines: AsyncIterator[str | None]) -> int:
        """Until /quit or the end of input; then waits for a running Poke turn and returns 0."""
        watcher = asyncio.create_task(self.watch_body())
        try:
            async for text in lines:
                if text is None or not await self.line(text):
                    break
        finally:
            if self._poke_tasks:
                await asyncio.wait(set(self._poke_tasks), timeout=90)
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        return 0


async def queue_lines(queue: asyncio.Queue[str | None]) -> AsyncIterator[str | None]:
    while True:
        item = await queue.get()
        yield item
        if item is None:
            return


class StartRefused(Exception):
    """A deliberate fail-closed refusal to start the chat (the message says why and what to do)."""


async def live_model(config: Config, prices: PriceTable) -> tuple[ModelClient, str]:
    """The real model client for --live, after every start check; StartRefused if any fails. Returns the client
    and the key (for the prompt capture's redaction list; never shown)."""
    from .agent.model import AnthropicModel, ModelFailure
    from .egress import EgressDenied, anthropic_api_key, build_anthropic_client

    if not config.models.allow_live_calls:
        raise StartRefused("live model calls are not allowed: set [models] allow_live_calls = true in config.toml "
                           "to consent to paid calls (spend is capped by [caps])")
    profile = config.profile
    try:  # a named profile reads its own .env only (never the environment or the root .env): docs/profiles.md
        key = anthropic_api_key() if profile.is_default else profile.api_key("ANTHROPIC_API_KEY")
    except ConfigError as e:
        raise StartRefused(f"the .env file cannot be read: {e}") from None
    if not key:
        raise StartRefused("no ANTHROPIC_API_KEY in .env or the environment; add one (see docs/cost-estimate.md)"
                           if profile.is_default else
                           f"no ANTHROPIC_API_KEY in {profile.env_path}; put your key there (see docs/profiles.md). "
                           "A key set elsewhere on this computer is not used for a profile")
    for model_id in (config.models.live_conversation, config.models.careful_looking):
        if model_id not in prices.models:
            raise StartRefused(f"model {model_id!r} has no price in brain/agent/prices.json, so its cost cannot be "
                               "capped; add its prices first")
    try:
        client = build_anthropic_client(key)
    except EgressDenied:
        raise StartRefused("api.anthropic.com is not in config.toml [egress] allowed_hosts") from None
    model = AnthropicModel(client)
    try:
        listed = await model.list_models()
    except ModelFailure as e:
        await model.aclose()
        raise StartRefused(f"the model list could not be read ({e.code}); not starting the live loop") from None
    missing = [m for m in (config.models.live_conversation, config.models.careful_looking) if m not in listed]
    if missing:
        await model.aclose()
        raise StartRefused(f"configured model(s) {missing} are not offered by the API for this key; check [models]")
    return model, key


async def run_chat(config: Config, body: Body, *, options: ChatOptions,
                   lines: AsyncIterator[str | None] | None = None, out: Callable[[str], None] = print) -> int:
    """Build the agent (model, spend guard, capture, registry over `body`) and run the console session.
    Returns 0, or EXIT_REFUSED (2) after a fail-closed refusal at start."""
    from .agent.body_tools import register_body_tools
    from .agent.budget import PriceError, load_prices
    from .agent.capture import PromptCapture
    from .agent.loop import AgentSettings, PromptError, load_system_prompt
    from .agent.scripted import ScriptedModel
    from .agent.tools import ToolRegistry
    from .obs.runtime import ObsRuntime

    try:
        prompt = load_system_prompt()
        prices = load_prices()
    except (PromptError, PriceError) as e:
        obs.emit("startup.refused", "the agent cannot start", reason=type(e).__name__)
        out(f"chat refused to start: {e}")
        return 2
    secrets: list[str] = []
    model: ModelClient
    if options.live:
        try:
            model, key = await live_model(config, prices)
        except StartRefused as e:
            obs.emit("startup.refused", "the live model loop refused to start", reason="live_check")
            out(f"chat refused to start: {e}")
            return 2
        secrets.append(key)
    else:
        model = ScriptedModel()
    data_dir = config.paths.data_dir
    rt = ObsRuntime(data_dir, log=obs.LOG, board=obs.HEALTH, session_id="")
    rt.start()
    guard = SpendGuard(config.caps, prices, rt.state, persist=options.live)
    guard.start()
    capture = PromptCapture(data_dir, enabled=config.logging.full_prompts, max_mb=config.logging.capture_max_mb,
                            max_days=config.logging.capture_max_days, secrets=secrets)
    registry = ToolRegistry()
    register_body_tools(registry, body)
    agent = Agent(registry, model, guard, settings=AgentSettings.from_config(config), prompt=prompt, capture=capture)
    session = ChatSession(agent, body, guard, options=options, out=out)
    obs.LOG.add_sink(session.show_event)
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    try:
        async with body:
            out(f"chat: model {'live ' + config.models.live_conversation if options.live else 'scripted (no key, no spend)'}"
                f"; body {body.name}; typed lines are {'APP' if options.as_app else 'CONSOLE'} turns; prompt capture "
                f"{'on (data/capture, private)' if capture.enabled else 'off'}")
            out(HELP)
            if lines is None:
                stdin_lines(asyncio.get_running_loop(), queue)
                lines = queue_lines(queue)
            return await session.run(lines)
    finally:
        obs.LOG.remove_sink(session.show_event)
        await model.aclose()
        rt.stop()
        if options.live:
            out(f"spend: {guard.summary()}")
