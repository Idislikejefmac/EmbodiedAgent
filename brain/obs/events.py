"""The structured event schema and its catalogue (card X-02 step 1; docs/observability.md).

One event is one JSON line:

    {"v": 1, "seq": 42, "ts": "2026-11-13T10:00:00.123456Z", "mono": 1234.567890, "level": "INFO",
     "comp": "agent", "type": "tool.result", "session": "s-20261113-100000-ab12", "turn": "t-3",
     "msg": "get_body_status ok", "f": {"tool": "get_body_status", "status": "ok", "ms": 1.2}}

- `ts` is wall time (UTC, for people), `mono` is time.monotonic() (for ordering and durations; it does not
  jump when the wall clock does). `seq` counts events of this process from 1.
- `level` is DEBUG, INFO, WARNING, ERROR or CRITICAL. `comp` is the component (transport, body, agent,
  config, egress, obs, supervisor, launcher, brain, demo). `type` names the decision point (EVENT_TYPES).
- `session` and `turn` are the correlation ids of the code that emitted the event (contextvars, see
  `correlate`): a body connection's session id, and an agent turn id once the agent loop exists (P0-07).
- `msg` is a short sentence; `f` holds the bounded, redacted fields (brain/obs/redact.py).

Every type is listed in EVENT_TYPES with its component, default level and meaning. A type whose `status`
is "reserved" is named and documented for a later card (P0-07 emits the model, cost, budget, turn and
prompt-capture events) so that card only has to call emit(). A type marked `crash` reports an exception
nothing expected (an isolation point caught it); the chaos harness treats one as a crash.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

SCHEMA_VERSION = 1
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
COMPONENTS = ("transport", "body", "agent", "config", "egress", "obs", "supervisor", "launcher", "brain", "demo",
              "model", "speech", "identity")


@dataclass(frozen=True)
class EventType:
    name: str
    component: str
    level: str
    about: str
    status: str = "live"  # live: emitted by today's code; reserved: named for a later card
    card: str = "X-02"
    crash: bool = False  # an unexpected exception caught by an isolation point


def _t(name: str, component: str, level: str, about: str, *, status: str = "live", card: str = "X-02",
       crash: bool = False) -> EventType:
    return EventType(name, component, level, about, status, card, crash)


_ALL = [
    # ---- transport: the WebSocket link to a body (brain/transport) ----
    _t("link.connect", "transport", "DEBUG", "a connection arrived; the handshake starts (remote address port only)"),
    _t("link.auth", "transport", "INFO", "handshake result: ok, or the refusal code (unauthorized, bad_sequence...)"),
    _t("link.up", "transport", "INFO", "a body authenticated and became the active body (software, capability count)"),
    _t("link.down", "transport", "INFO", "a connection ended (reason, close code); the body link is down if it was active"),
    _t("link.replaced", "transport", "INFO", "a newer body connection replaced the active one (close 4001)"),
    _t("link.evicted", "transport", "WARNING", "a pending handshake was evicted (too many at once)"),
    _t("link.refused", "transport", "WARNING", "a message from the peer was refused (error code, fatal)"),
    _t("link.peer_error", "transport", "WARNING", "the peer sent an error message (code, fatal)"),
    _t("link.unconsumed", "transport", "INFO", "a valid message that nothing in the brain consumes yet (type)"),
    _t("link.session_summary", "transport", "INFO", "counters of a finished connection (rx, tx, refused, limited)"),
    _t("link.forward", "transport", "INFO", "the body's port forward was checked and changed state (ok or not)"),
    _t("watchdog.fired", "transport", "WARNING", "the liveness watchdog dropped a link that went silent"),
    _t("flood.rate_limited", "transport", "WARNING", "a peer went over a message rate; the excess is dropped"),
    _t("flood.closed", "transport", "WARNING", "a peer kept flooding; the link was closed"),
    _t("flood.too_large", "transport", "WARNING", "a message over its type's size limit was refused"),
    _t("queue.busy", "transport", "WARNING", "the handler queue was full; the peer got `busy` (depth, bytes)"),
    _t("queue.drop", "body", "WARNING", "a slow stream consumer lost its oldest items (stream, dropped so far)"),
    _t("clock.estimate", "transport", "INFO", "the body-clock offset estimate (offset_ms, best_rtt_ms, drift)"),
    _t("clock.reset", "transport", "WARNING", "a clock sample disagreed with the estimate; older samples dropped"),
    # ---- body: the Body interface and its adapters (brain/body) ----
    _t("body.start", "body", "INFO", "a body adapter started (name, capabilities)"),
    _t("body.close", "body", "INFO", "a body adapter closed"),
    _t("body.link", "body", "INFO", "the adapter's link to its body went up or down"),
    _t("body.capabilities", "body", "INFO", "the body's capability set changed (the list)"),
    _t("body.status", "body", "INFO", "a status reading (connected, stale, battery, temperature)"),
    _t("body.event", "body", "INFO", "the body reported an event (name and data key count, never values)"),
    _t("body.frame", "body", "INFO", "a camera still arrived (id, encoding, size; never the pixels)"),
    _t("body.audio", "body", "INFO", "microphone audio is flowing (stream, chunk seq; every 25th chunk)"),
    _t("body.stream", "body", "INFO", "a body stream opened or closed (kind)"),
    _t("body.speak", "body", "INFO", "the body was asked to speak (utterance id, character count; never the text)"),
    _t("body.stop", "body", "INFO", "the body was asked to stop (motion, audio)"),
    _t("body.navigate", "body", "INFO", "a navigation goal was started, reached or cancelled"),
    _t("body.not_supported", "body", "INFO", "a capability-gated method was refused (capability, not built yet)"),
    _t("body.request", "body", "WARNING", "a request to the body failed (command, refused or timed out)"),
    # ---- agent: tool registry and channel rules (brain/agent) ----
    _t("tool.call", "agent", "INFO", "a tool call arrived (tool, channel; argument names, types and sizes only)"),
    _t("tool.result", "agent", "INFO", "a tool call ended (status, code, duration)"),
    _t("tool.refused", "agent", "WARNING", "the boundary refused a tool call (code, channel, detail)"),
    _t("tool.timeout", "agent", "WARNING", "a tool handler ran past its timeout and was abandoned"),
    _t("tool.shut_off", "agent", "ERROR", "a tool has too many stuck handlers; it is off until they end"),
    _t("tool.failed", "agent", "ERROR", "a tool handler or the registry raised unexpectedly (type, where)", crash=True),
    _t("channel.decision", "agent", "INFO", "the channel of a turn was worked out from its content sources"),
    _t("sensitivity.decision", "agent", "INFO", "a sensitive tool was allowed or refused, and why"),
    # ---- config, egress, startup and the process ----
    _t("config.loaded", "config", "INFO", "config.toml passed the gate (file name, body kind)"),
    _t("config.error", "config", "ERROR", "config.toml or .env was refused; the brain does not start (fail closed)"),
    _t("startup.self_test", "egress", "INFO", "the egress guard tested itself (ok, or what got through)"),
    _t("startup.refused", "brain", "ERROR", "a deliberate fail-closed refusal to start (reason); not a crash"),
    _t("egress.denied", "egress", "WARNING", "a connection or name lookup outside the allowlist was refused"),
    _t("validation.failed", "body", "WARNING", "a value was refused by a gate (parameter name, problem; never the value)"),
    _t("brain.start", "brain", "INFO", "a brain command started (command, version, pid)"),
    _t("brain.stop", "brain", "INFO", "a brain command ended (exit code, reason)"),
    _t("process.crash", "brain", "CRITICAL", "an unexpected exception reached main(); a crash report was written",
       crash=True),
    _t("probe.result", "brain", "INFO", "the echo probe finished (ok, count)"),
    _t("pair.result", "brain", "INFO", "pairing finished (ok)"),
    # ---- the observability pipeline itself ----
    _t("obs.started", "obs", "INFO", "event files are being written (folder name, file prefix)"),
    _t("obs.sink_failed", "obs", "ERROR", "the event file cannot be written (reason); reported once, retried"),
    _t("obs.sink_recovered", "obs", "INFO", "the event file can be written again (events dropped meanwhile)"),
    _t("obs.dropped", "obs", "WARNING", "events were dropped or suppressed (queue full, per-type limit)"),
    _t("obs.bad_event", "obs", "WARNING", "emit() was called with an unknown type or failed to build the event"),
    _t("health.change", "obs", "INFO", "a health source changed state (source, state, reason)"),
    _t("state.loaded", "obs", "INFO", "data/state.json was read (or started fresh)"),
    _t("state.quarantined", "obs", "WARNING", "data/state.json was corrupt or too large; moved aside, defaults used"),
    _t("state.save_failed", "obs", "WARNING", "data/state.json could not be written (reason); retried"),
    # ---- the crash barrier ----
    _t("supervisor.started", "supervisor", "INFO", "a supervised component started (or restarted)"),
    _t("supervisor.crashed", "supervisor", "ERROR", "a supervised component raised (type, where); contained",
       crash=True),
    _t("supervisor.restart", "supervisor", "WARNING", "a component will restart after a backoff (attempt, delay)"),
    _t("supervisor.gave_up", "supervisor", "ERROR", "a component failed too often; it is stopped and reported"),
    _t("supervisor.fatal", "supervisor", "ERROR", "a component refused to run for a security reason; never retried"),
    _t("supervisor.stopped", "supervisor", "INFO", "a supervised component ended (done, cancelled or stopped)"),
    _t("launcher.start", "launcher", "INFO", "`serve --supervised` started the brain process (attempt)"),
    _t("launcher.exit", "launcher", "WARNING", "the brain process ended (exit code, meaning)"),
    _t("launcher.restart", "launcher", "WARNING", "the brain process will be restarted after a backoff"),
    _t("launcher.gave_up", "launcher", "ERROR", "the brain process failed too often; the launcher stops"),
    # ---- turns: used by demo-trace today, by the agent loop from P0-07 ----
    _t("turn.start", "agent", "INFO", "an agent turn started (turn id, content sources; never the words)",
       card="P0-07"),
    _t("turn.end", "agent", "INFO", "an agent turn ended (outcome, tool calls, duration)", card="P0-07"),
    _t("demo.step", "demo", "INFO", "a step of the scripted demo session (python -m brain demo-trace)"),
    # ---- reserved for later cards: named so they only have to emit them ----
    _t("model.call_start", "model", "INFO", "a model request started (model id, input tokens estimate, tools offered)",
       status="reserved", card="P0-07"),
    _t("model.call_end", "model", "INFO", "a model request ended (stop reason, tokens in and out, ms, ok or error code)",
       status="reserved", card="P0-07"),
    _t("cost.update", "model", "INFO", "spend after a request (usd this session, today, since start)",
       status="reserved", card="P0-07"),
    _t("budget.check", "model", "INFO", "the interim spend guard was consulted (which cap, headroom)",
       status="reserved", card="P0-07"),
    _t("budget.stop", "model", "ERROR", "a spend or rate cap stopped a request (which cap)", status="reserved",
       card="P0-07"),
    _t("prompt.capture_ref", "model", "INFO", "a full prompt and response went to the PRIVATE capture file: "
       "only its record id and byte count are here, never the text", status="reserved", card="P0-07"),
    _t("speech.stt", "speech", "INFO", "a transcript arrived (vendor, ms, character count; never the words)",
       status="reserved", card="P2-02"),
    _t("speech.tts", "speech", "INFO", "speech was synthesized (vendor, ms, character count)", status="reserved",
       card="P2-01"),
    _t("identity.decision", "identity", "INFO", "a recognition decision (band, evidence kinds; never a template)",
       status="reserved", card="P3-10"),
]
EVENT_TYPES: dict[str, EventType] = {t.name: t for t in _ALL}
CRASH_TYPES = frozenset(t.name for t in _ALL if t.crash)
# Never held back by the per-type limit: a crash must not hide behind a flood, and health changes are already
# bounded (at most one per source per health snapshot, about once a second).
UNLIMITED_TYPES = CRASH_TYPES | {"health.change"}

# ---- correlation ids -----------------------------------------------------------------------------------------

SESSION: contextvars.ContextVar[str | None] = contextvars.ContextVar("obs_session", default=None)
TURN: contextvars.ContextVar[str | None] = contextvars.ContextVar("obs_turn", default=None)


@contextmanager
def correlate(*, session: str | None = None, turn: str | None = None) -> Iterator[None]:
    """Events emitted inside the block (and by tasks started inside it) carry these correlation ids."""
    tokens = []
    if session is not None:
        tokens.append((SESSION, SESSION.set(session)))
    if turn is not None:
        tokens.append((TURN, TURN.set(turn)))
    try:
        yield
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


# ---- which event types cover which manifest entry points (the X-02 step 9 coverage test) ----------------------
# Patterns are manifest ids (fnmatch). Every entry of kind tool, reserved_tool, message, body_method and
# cli_command must match a pattern here (or in ENTRY_EXEMPT, with the reason).
ENTRY_COVERAGE: dict[str, tuple[str, ...]] = {
    "tool.*": ("tool.call", "tool.result", "tool.refused"),
    "reserved_tool.*": ("tool.call", "tool.result", "tool.refused"),
    "message.hello": ("link.auth", "link.up"),
    "message.auth": ("link.auth",),
    "message.heartbeat": ("clock.estimate", "watchdog.fired"),
    "message.echo": ("link.session_summary", "link.refused"),
    "message.error": ("link.peer_error", "link.refused"),
    "message.event": ("body.event",),
    "message.ack": ("link.unconsumed",),
    "message.image": ("link.unconsumed",),
    "message.pose": ("link.unconsumed",),
    "message.audio_in": ("link.unconsumed",),
    "message.tracking_state": ("link.unconsumed",),
    "message.command": ("body.status", "body.request"),
    "body_method.start": ("body.start",),
    "body_method.close": ("body.close",),
    "body_method.status": ("body.status",),
    "body_method.events": ("body.event",),
    "body_method.stop": ("body.stop",),
    "body_method.capture_image": ("body.frame", "body.not_supported"),
    "body_method.pose_stream": ("body.stream", "body.not_supported"),
    "body_method.audio_in_stream": ("body.stream", "body.audio", "body.not_supported"),
    "body_method.play_audio": ("body.speak", "body.not_supported"),
    "body_method.speak_text": ("body.speak", "body.not_supported"),
    "body_method.navigate": ("body.navigate", "body.not_supported"),
    "body_method.require": ("body.not_supported",),
    "body_method.capabilities": ("body.capabilities",),
    "body_method.connected": ("body.link",),
    "body_method.clock_sync": ("clock.estimate",),
    "cli_command.brain.serve": ("brain.start", "brain.stop", "supervisor.started"),
    "cli_command.brain.info": ("config.loaded",),
    "cli_command.brain.probe": ("probe.result",),
    "cli_command.brain.pair": ("pair.result",),
    "cli_command.brain.body-check": ("tool.call", "tool.result"),
    "cli_command.brain.demo-trace": ("demo.step", "turn.start", "turn.end"),
}
ENTRY_EXEMPT: dict[str, str] = {
    "message.speak_local": "the brain does not send speak_local yet (card P0-07 step 6 wires speak to it)",
    "message.audio_out": "the brain does not send speaker audio yet (card P2-01)",
    "body_method.name": "a constant label for operators; no decision is made",
    "body_method.supports": "a read-only membership test; the refusal it guards is body.not_supported",
    "body_method.wait_connected": "waits only; the state change it waits for is body.link or link.up",
    "cli_command.brain.chaos": "the harness: its own report and results file are its trace",
    "cli_command.brain.watch": "the read-only viewer of the event files; events of its own would feed itself",
    "cli_command.brain.health": "prints data/health.json; reads only, decides nothing",
    "cli_command.migrate.*": "tools/migrate.py is a separate offline tool with its own report, not the running brain",
}
