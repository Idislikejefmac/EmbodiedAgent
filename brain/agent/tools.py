"""The agent's tool registry: a default-deny allowlist (plan: "Command boundary"; card P0-06 step 8).

The agent has no generic tools: no shell, no filesystem, no network send, no config access. A tool
exists only if it is listed in tools.v0.json. Everything else the model (or anything speaking through
the body) asks for reaches `ToolRegistry.call`, is refused and logged, and touches nothing. The
enforcement is this code, never a prompt.

What the registry checks, in order, for every call:

1. the name is on the allowlist (else `refused / unknown_tool`);
2. the caller's channel may use it (`refused / channel_not_permitted`);
3. a sensitive tool has a confirmed identity resting on both face and voice, unless the call comes
   from the local console (`refused / identity_not_confirmed`);
4. the tool has been built (a reserved name, or a listed tool with no handler yet, gives
   `unavailable / not_built`);
5. the arguments are plain JSON of modest size and depth and match the tool's closed schema exactly,
   and their strings carry no control or invisible characters (`refused / invalid_arguments`).

Only then does the handler run, under the tool's timeout. The answer is always a `ToolResult`: plain
data with a `message` the model can say to the user. `call` does not raise, whatever it is given
(cancellation and interpreter exit aside). Nothing here depends on any LLM SDK; the agent loop (card
P0-07) turns `model_tools()` and `ToolResult` into whatever its SDK wants.

**Who is calling** is never taken from the model. The agent loop builds the `Caller` for each turn
with `Caller.for_turn(sources, ...)` from where the content in the model's context came from; see
`turn_channel` for the rule.

**Handlers must not block.** A handler is a coroutine that awaits; the registry abandons it at the
timeout and answers `timeout` whatever the handler does afterwards, but a handler that blocks the
event loop (a long computation, a blocking call) stalls everything, and no timeout can interrupt it.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, validators

from ..body.base import BodyBusy, BodyError, BodyTimeout, BodyUnavailable, NotSupported, NotYetImplemented
from ..body.types import IDENTIFIER, ConfidenceBand, Image

log = logging.getLogger("brain.agent.tools")

CATALOGUE_PATH = Path(__file__).with_name("tools.v0.json")
TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}(?!\n)$")
TOOL_CLASSES = ("query", "teaching", "motion", "staged_delete")
STRING_KINDS = ("id", "name", "spoken_text", "reason_text")
# A parameter may never be one of these, whatever it is called elsewhere (matched on the words of its name).
FORBIDDEN_PARAMETER_WORDS = frozenset({
    "path", "file", "filename", "folder", "dir", "directory", "cmd", "command", "shell", "script", "exec", "url",
    "uri", "host", "hostname", "address", "sql", "code",
})
# The only JSON Schema keywords an input schema may use. Anything else (anyOf, $ref, patternProperties,
# prefixItems, ...) could describe a string these rules never look at, so the catalogue is refused.
INPUT_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "description", "title", "default", "enum", "minimum",
    "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "pattern", "items", "minItems",
    "maxItems", "uniqueItems", "x-kind",
})
JSON_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
ID_PATTERN = r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}(?!\n)$"
DEFAULT_TIMEOUT_S = 15.0
MAX_ARGS_BYTES = 64 * 1024  # arguments larger than this as JSON are refused before validation
MAX_ARGS_DEPTH = 8
MAX_ARGS_NODES = 2000
MAX_KEYS_SHOWN = 5  # argument names shown in a log line or a detail; the rest are counted
EVIDENCE = frozenset({"voice", "face"})
_CODE_WORD = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_INVISIBLE = ("Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp")  # control, format, private, surrogate, unassigned, separators


class Channel(StrEnum):
    """Where a request came from (plan: "Command boundary")."""

    APP = "app"  # anything that arrives through the body: speech, images, text, buttons. Untrusted for control.
    CONSOLE = "console"  # Jeff typing at the brain's own console on the laptop (local stdin). Nothing else.


class InputSource(StrEnum):
    """Where a piece of content in the model's context came from."""

    LOCAL_CONSOLE = "local_console"  # typed at the brain's stdin on the laptop
    REMOTE_CONSOLE = "remote_console"  # an operator session over the network; not privileged until hardened
    BODY = "body"  # speech, pictures, text, button presses, events: anything through a body
    TOOL_RESULT = "tool_result"  # a tool's output: it carries the world's content (pictures, transcripts, notes)


def turn_channel(sources: Iterable[InputSource | str]) -> Channel:
    """The channel of a turn: that of the LEAST privileged content the model can see.

    CONSOLE only if every piece of content in the model's context for this turn was typed at the
    local console. One spoken sentence, picture, button event, remote-console line or tool result in
    the context makes the whole turn APP: text in a photo can steer the model, so a call made after
    the model has seen such content cannot be credited to the console. An empty list is APP. The
    networked "direct connection over WiFi" is REMOTE_CONSOLE and stays APP until its authentication
    is hardened. Privileged console work therefore runs in a context with no app content in it.
    """
    listed = [InputSource(s) for s in sources]
    return Channel.CONSOLE if listed and all(s is InputSource.LOCAL_CONSOLE for s in listed) else Channel.APP


@dataclass(frozen=True)
class Caller:
    """Who is asking, as far as the code knows. Built by the agent loop, never from model output.

    `band`, `evidence` and `person_id` come from the identity layer (card P3-10): `evidence` holds
    "voice" and/or "face"; `person_id` is the registry id of the identified person, or None.
    Identity never widens the allowlist.
    """

    channel: Channel
    band: ConfidenceBand = ConfidenceBand.UNKNOWN
    evidence: frozenset[str] = frozenset()
    person_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "channel", Channel(self.channel))
        object.__setattr__(self, "band", ConfidenceBand(self.band))
        if isinstance(self.evidence, (str, bytes)) or not frozenset(self.evidence) <= EVIDENCE:
            raise ValueError("evidence must be a set holding 'voice' and/or 'face'")
        object.__setattr__(self, "evidence", frozenset(self.evidence))
        if self.person_id is not None and not (isinstance(self.person_id, str) and IDENTIFIER.match(self.person_id)):
            raise ValueError("person_id must be an identifier or None")

    @classmethod
    def for_turn(cls, sources: Iterable[InputSource | str], *, band: ConfidenceBand = ConfidenceBand.UNKNOWN,
                 evidence: Iterable[str] = (), person_id: str | None = None) -> Caller:
        """The caller for one turn, with the channel worked out by `turn_channel`."""
        return cls(turn_channel(sources), band, frozenset(evidence), person_id)

    @property
    def confirmed_by_face_and_voice(self) -> bool:
        return self.band is ConfidenceBand.CONFIRMED and self.evidence >= EVIDENCE


APP = Caller(Channel.APP)
CONSOLE = Caller(Channel.CONSOLE)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    tool_class: str
    channels: frozenset[Channel]
    sensitive: bool
    uses_body: bool = False
    description: str = ""
    input_schema: dict[str, Any] | None = None  # None for a reserved name
    output_schema: dict[str, Any] | None = None
    card: str | None = None  # the card that builds a reserved tool
    timeout_s: float | None = None  # None: the registry's default
    timeout_plus_arg: str | None = None  # a numeric argument (seconds) added to the timeout

    @property
    def reserved(self) -> bool:
        return self.input_schema is None


@dataclass(frozen=True)
class ToolOutput:
    """What a handler returns when it has more than a JSON object: images for the model to see."""

    content: dict[str, Any]
    attachments: tuple[Image, ...] = ()


@dataclass(frozen=True)
class ToolResult:
    """The registry's answer to one call.

    `status`: "ok", "refused" (the boundary said no), "unavailable" (allowed, but cannot be done now)
    or "error" (it broke). `code` says why in one word. `message` is a plain sentence the model can
    say or paraphrase to the user. `content` is the tool's JSON output on success. `detail` is a
    short fact written by the registry itself (which argument, which capability); it never carries
    text from a handler, an adapter or an exception. `attachments` are images for the model; they are
    not part of `to_json()`.
    """

    tool: str
    status: str
    code: str
    message: str
    content: dict[str, Any] | None = None
    detail: str | None = None
    attachments: tuple[Image, ...] = field(default=(), repr=False)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_json(self) -> dict[str, Any]:
        """What the model is given as the tool's result (together with any attachments)."""
        d: dict[str, Any] = {"ok": self.ok, "status": self.status, "code": self.code}
        if self.ok:
            d["result"] = self.content
        else:
            d["message"] = self.message
            if self.detail:
                d["detail"] = self.detail
        return d


Handler = Callable[[dict[str, Any], Caller], Awaitable["dict[str, Any] | ToolOutput"]]

MESSAGES = {
    "unknown_tool": "I can't do that. It is outside what I am able to do: I can talk, look, listen, report on my "
                    "own body, and learn about the people and places around me. I cannot act on, or look into, the "
                    "computer I run on.",
    "channel_not_permitted": "I can't do that when asked this way. It can only be done from the console.",
    "identity_not_confirmed": "I can't do that until I am sure who is asking, by both face and voice.",
    "invalid_arguments": "I could not do that because the request was not in a form I accept.",
    "not_built": "I can't do that yet; that ability has not been built.",
    "body_not_supported": "My body cannot do that.",
    "body_unavailable": "I can't reach my body right now.",
    "busy": "My body is busy; try again in a moment.",
    "timeout": "That took too long and I gave up.",
    "body_error": "My body could not do that.",
    "internal": "Something went wrong inside me while doing that.",
}
STATUS_OF = {
    "ok": "ok", "unknown_tool": "refused", "channel_not_permitted": "refused", "identity_not_confirmed": "refused",
    "invalid_arguments": "refused", "not_built": "unavailable", "body_not_supported": "unavailable",
    "body_unavailable": "unavailable", "busy": "unavailable", "timeout": "unavailable", "body_error": "error",
    "internal": "error",
}


# ---- the catalogue -------------------------------------------------------------------------------------

_StrictValidator = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda checker, x: isinstance(x, int) and not isinstance(x, bool)))


def _types_of(schema: dict[str, Any]) -> list[str]:
    types = schema.get("type")
    return [types] if isinstance(types, str) else [t for t in types] if isinstance(types, list) else []


def _schema_problems(tool: str, schema: object, where: str, *, is_input: bool) -> list[str]:
    """Rules every tool schema must meet (see the catalogue's comment)."""
    if not isinstance(schema, dict):
        return [f"{tool}: {where} is not a schema object"]
    bad: list[str] = []
    types = _types_of(schema)
    if is_input:
        unknown = sorted(set(schema) - INPUT_KEYWORDS)
        if unknown:
            bad.append(f"{tool}: {where} uses schema keyword(s) {unknown} that input schemas may not use")
        if "enum" not in schema and (not types or any(t not in JSON_TYPES for t in types)):
            bad.append(f"{tool}: {where} must state its type")
    if "object" in types or "properties" in schema:
        if schema.get("additionalProperties") is not False:
            bad.append(f"{tool}: {where} must set additionalProperties to false")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return bad + [f"{tool}: {where}.properties is not an object"]
        for name, sub in properties.items():
            if is_input and FORBIDDEN_PARAMETER_WORDS & set(str(name).lower().split("_")):
                bad.append(f"{tool}: parameter {name!r} looks like a path, command or address")
            bad += _schema_problems(tool, sub, f"{where}.{name}", is_input=is_input)
    if "array" in types or "items" in schema:
        bad += _schema_problems(tool, schema.get("items"), f"{where}[]", is_input=is_input)
    if is_input and "string" in types and "enum" not in schema:
        kind = schema.get("x-kind")
        if kind not in STRING_KINDS:
            bad.append(f"{tool}: string {where} must declare x-kind as one of {STRING_KINDS}")
        if not isinstance(schema.get("maxLength"), int):
            bad.append(f"{tool}: string {where} must have a maxLength")
        if kind == "id" and schema.get("pattern") != ID_PATTERN:
            bad.append(f"{tool}: id {where} must use the identifier pattern")
        if kind == "name" and "pattern" not in schema:
            bad.append(f"{tool}: name {where} must have a pattern that excludes path characters")
    return bad


def catalogue_problems(raw: object) -> list[str]:
    """Everything wrong with a catalogue document; empty when it is acceptable."""
    if not isinstance(raw, dict) or not isinstance(raw.get("tools"), list) or not isinstance(raw.get("reserved"), list):
        return ["catalogue must be an object with 'tools' and 'reserved' lists"]
    bad: list[str] = []
    seen: set[str] = set()
    for entry in raw["tools"] + raw["reserved"]:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not TOOL_NAME.match(name):
            bad.append(f"bad tool name {name!r}")
            continue
        if name in seen:
            bad.append(f"{name}: listed twice")
        seen.add(name)
        if entry.get("class") not in TOOL_CLASSES:
            bad.append(f"{name}: class must be one of {TOOL_CLASSES}")
        channels = entry.get("channels")
        if not isinstance(channels, list) or not channels or any(c not in ("app", "console") for c in channels):
            bad.append(f"{name}: channels must be a non-empty list of 'app' and 'console'")
        elif "console" not in channels:
            bad.append(f"{name}: the console may use every tool")
        if not isinstance(entry.get("sensitive"), bool):
            bad.append(f"{name}: sensitive must be true or false")
    for entry in raw["tools"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        name = entry["name"]
        if not isinstance(entry.get("description"), str) or len(entry["description"]) < 20:
            bad.append(f"{name}: needs a description")
        schemas_ok = True
        for key, is_input in (("input_schema", True), ("output_schema", False)):
            schema = entry.get(key)
            if not isinstance(schema, dict) or schema.get("type") != "object":
                bad.append(f"{name}: {key} must be an object schema")
                schemas_ok = False
                continue
            try:
                Draft202012Validator.check_schema(schema)
            except Exception as e:  # jsonschema.SchemaError
                bad.append(f"{name}: {key} is not a valid JSON Schema: {str(e)[:200]}")
                schemas_ok = False
                continue
            bad += _schema_problems(name, schema, key, is_input=is_input)
        timeout = entry.get("timeout_s")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or not 0 < timeout <= 600):
            bad.append(f"{name}: timeout_s must be a number of seconds up to 600")
        plus = entry.get("timeout_plus_arg")
        if plus is not None and schemas_ok:
            arg = entry["input_schema"].get("properties", {}).get(plus)
            if (not isinstance(arg, dict) or arg.get("type") not in ("number", "integer")
                    or not isinstance(arg.get("maximum"), (int, float))):
                bad.append(f"{name}: timeout_plus_arg must name a numeric argument that has a maximum")
    return bad


@dataclass(frozen=True)
class Catalogue:
    version: int
    tools: dict[str, ToolSpec]  # listed with schemas
    reserved: dict[str, ToolSpec]  # names kept for later cards

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Catalogue:
        problems = catalogue_problems(raw)
        if problems:
            raise ValueError("tool catalogue rejected: " + "; ".join(problems))

        def spec(e: dict[str, Any]) -> ToolSpec:
            timeout = e.get("timeout_s")
            return ToolSpec(e["name"], e["class"], frozenset(Channel(c) for c in e["channels"]), e["sensitive"],
                            bool(e.get("uses_body", False)), e.get("description", ""), e.get("input_schema"),
                            e.get("output_schema"), e.get("card"), None if timeout is None else float(timeout),
                            e.get("timeout_plus_arg"))

        return cls(int(raw.get("version", 0)), {e["name"]: spec(e) for e in raw["tools"]},
                   {e["name"]: spec(e) for e in raw["reserved"]})


def load_catalogue() -> Catalogue:
    """Read the allowlist that ships with the code. Done once, when the registry is built."""
    return Catalogue.from_json(json.loads(CATALOGUE_PATH.read_text(encoding="utf-8")))


def _strip_private(schema: Any) -> Any:
    """A copy of a schema without the x- annotations (they are for this code, not for the model)."""
    if isinstance(schema, dict):
        return {k: _strip_private(v) for k, v in schema.items() if not k.startswith("x-")}
    if isinstance(schema, list):
        return [_strip_private(v) for v in schema]
    return schema


# ---- checking arguments ----------------------------------------------------------------------------------


def _show_keys(keys: Iterable[object], total: int) -> str:
    """The first few argument names, shortened, and how many more there are. Never values."""
    shown = [str(k)[:40] for k in itertools.islice(keys, MAX_KEYS_SHOWN)]
    return f"{shown}" + (f" and {total - len(shown)} more" if total > len(shown) else "")


def _plain_json_problem(args: object) -> str | None:
    """Why `args` is not modest, plain JSON, or None. Only exact dict, list, str, int, float, bool and
    None are accepted (a subclass could run code when it is looked at), within fixed depth and size."""
    if type(args) is not dict:
        return "arguments must be an object"
    nodes = 0
    stack: list[tuple[object, int]] = [(args, 1)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_ARGS_NODES:
            return f"arguments have more than {MAX_ARGS_NODES} values"
        kind = type(value)
        if kind is dict or kind is list:
            if depth > MAX_ARGS_DEPTH:
                return f"arguments are nested deeper than {MAX_ARGS_DEPTH}"
            if kind is dict:
                assert isinstance(value, dict)
                if any(type(k) is not str for k in dict.keys(value)):
                    return "argument names must be strings"
                stack.extend((v, depth + 1) for v in dict.values(value))
            else:
                assert isinstance(value, list)
                stack.extend((v, depth + 1) for v in list.__iter__(value))
        elif kind is float:
            assert isinstance(value, float)
            if value != value or value in (float("inf"), float("-inf")):
                return "numbers must be finite"
        elif kind is str:
            assert isinstance(value, str)
            if len(value) > MAX_ARGS_BYTES:
                return f"arguments are larger than {MAX_ARGS_BYTES} bytes"
        elif kind not in (int, bool, type(None)):
            return "arguments must be plain JSON values"
    if len(json.dumps(args, allow_nan=False)) > MAX_ARGS_BYTES:
        return f"arguments are larger than {MAX_ARGS_BYTES} bytes"
    return None


def _string_problem(schema: dict[str, Any], value: object, where: str) -> str | None:
    """Characters the schema patterns cannot express: no control characters anywhere (new lines and
    tabs are allowed in text to be spoken); ids and names must also be NFKC-normalised and free of
    invisible format characters (such as the right-to-left override) and of '%'."""
    if isinstance(value, dict):
        for key, sub in value.items():
            found = _string_problem(schema.get("properties", {}).get(key, {}), sub, f"{where}/{key}".lstrip("/"))
            if found:
                return found
    elif isinstance(value, list):
        for i, sub in enumerate(value):
            found = _string_problem(schema.get("items", {}), sub, f"{where}/{i}".lstrip("/"))
            if found:
                return found
    elif isinstance(value, str):
        kind = schema.get("x-kind")
        strict = kind in ("id", "name") or kind is None
        allowed_controls = "" if strict else "\n\t"
        for ch in value:
            category = unicodedata.category(ch)
            if (category in _INVISIBLE if strict else category == "Cc") and ch not in allowed_controls:
                return f"{where}: contains a control or invisible character"
        if strict and (unicodedata.normalize("NFKC", value) != value or "%" in value):
            return f"{where}: must be plain normalised text without '%'"
    return None


# ---- the registry --------------------------------------------------------------------------------------


class ToolRegistry:
    def __init__(self, catalogue: Catalogue | None = None, *, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.catalogue = load_catalogue() if catalogue is None else catalogue
        self.timeout_s = timeout_s
        self._handlers: dict[str, Handler] = {}
        self._validators: dict[str, Any] = {name: _StrictValidator(spec.input_schema)
                                            for name, spec in self.catalogue.tools.items()}
        self._out_validators: dict[str, Any] = {name: _StrictValidator(spec.output_schema)
                                                for name, spec in self.catalogue.tools.items()}
        self._abandoned: set[asyncio.Task[Any]] = set()
        self.refusals = 0  # calls refused at the boundary since start

    def register(self, name: str, handler: Handler) -> None:
        """Give a listed tool its handler. Code cannot add a tool that is not in the catalogue."""
        if name not in self.catalogue.tools:
            raise KeyError(f"{name!r} is not on the tool allowlist; add it to tools.v0.json first")
        self._handlers[name] = handler

    def available(self, channel: Channel) -> list[str]:
        """The tools that have a handler and that this channel may call."""
        return [n for n, s in self.catalogue.tools.items() if n in self._handlers and Channel(channel) in s.channels]

    def model_tools(self, channel: Channel) -> list[dict[str, Any]]:
        """Tool definitions to show a model: `name`, `description`, `input_schema` (JSON Schema)."""
        return [{"name": n, "description": self.catalogue.tools[n].description,
                 "input_schema": _strip_private(self.catalogue.tools[n].input_schema)}
                for n in self.available(channel)]

    def timeout_for(self, name: str, args: dict[str, Any]) -> float:
        """Seconds a call may take: the tool's own timeout (or the default), plus the value of its
        `timeout_plus_arg` argument (or that argument's default) when it has one."""
        spec = self.catalogue.tools[name]
        timeout = self.timeout_s if spec.timeout_s is None else spec.timeout_s
        if spec.timeout_plus_arg and spec.input_schema is not None:
            arg_schema = spec.input_schema["properties"][spec.timeout_plus_arg]
            extra = args.get(spec.timeout_plus_arg, arg_schema.get("default", arg_schema["maximum"]))
            timeout += float(extra)
        return timeout

    def _result(self, tool: str, code: str, *, content: dict[str, Any] | None = None, detail: str | None = None,
                attachments: tuple[Image, ...] = ()) -> ToolResult:
        return ToolResult(tool, STATUS_OF[code], code, "" if code == "ok" else MESSAGES[code], content, detail,
                          attachments)

    def _refuse(self, shown: str, code: str, caller: Caller, args: object, detail: str | None = None) -> ToolResult:
        """A refusal at the boundary: counted and logged. Argument values are never logged, only the
        first few names."""
        self.refusals += 1
        try:
            keys = _show_keys(dict.keys(args), dict.__len__(args)) if isinstance(args, dict) else type(args).__name__
        except Exception:
            keys = "<unreadable>"
        log.warning("REFUSED tool=%r channel=%s band=%s code=%s arg_keys=%s%s", shown, caller.channel.value,
                    caller.band.value, code, keys, f" detail={detail!r}" if detail else "")
        return self._result(shown, code, detail=detail)

    def _argument_problem(self, name: str, spec: ToolSpec, args: object) -> str | None:
        """Why the arguments are not acceptable, as a short fact with no argument value in it, or None."""
        problem = _plain_json_problem(args)
        if problem is not None:
            return problem
        assert isinstance(args, dict) and spec.input_schema is not None
        first = next(iter(self._validators[name].iter_errors(args)), None)
        if first is not None:
            if first.validator == "additionalProperties":
                extra = sorted(set(args) - set(spec.input_schema.get("properties", {})))
                return f"unexpected argument(s) {_show_keys(extra, len(extra))}"
            where = "/".join(str(p)[:40] for p in list(first.absolute_path)[:MAX_ARGS_DEPTH]) or "arguments"
            return f"{where}: does not satisfy '{first.validator}'"
        return _string_problem(spec.input_schema, args, "")

    async def call(self, name: object, args: object, caller: Caller) -> ToolResult:
        """Run one tool call, or refuse it. Does not raise (cancellation and interpreter exit aside)."""
        shown = name[:64] if isinstance(name, str) else f"<{type(name).__name__}>"
        try:
            return await self._call(name, shown, args, caller)
        except Exception:
            log.exception("tool registry failed handling %r", shown)
            return self._result(shown, "internal")

    async def _call(self, name: object, shown: str, args: object, caller: Caller) -> ToolResult:
        cat = self.catalogue
        if not isinstance(name, str) or (name not in cat.tools and name not in cat.reserved):
            return self._refuse(shown, "unknown_tool", caller, args)
        spec = cat.tools.get(name) or cat.reserved[name]
        if caller.channel not in spec.channels:
            return self._refuse(name, "channel_not_permitted", caller, args)
        if spec.sensitive and caller.channel is not Channel.CONSOLE and not caller.confirmed_by_face_and_voice:
            return self._refuse(name, "identity_not_confirmed", caller, args)
        if spec.reserved:
            log.info("tool %s asked for but not built yet (card %s)", name, spec.card)
            return self._result(name, "not_built")
        if args is None:
            args = {}
        try:
            problem = self._argument_problem(name, spec, args)
        except Exception as e:  # arguments so odd that checking them failed: refused, never raised
            problem = f"arguments could not be checked ({type(e).__name__})"
        if problem is not None:
            return self._refuse(name, "invalid_arguments", caller, args, problem)
        assert isinstance(args, dict)
        handler = self._handlers.get(name)
        if handler is None:
            log.info("tool %s asked for but has no handler yet", name)
            return self._result(name, "not_built")
        task: asyncio.Task[Any] = asyncio.ensure_future(handler(args, caller))
        try:
            done, _ = await asyncio.wait({task}, timeout=self.timeout_for(name, args))
        except BaseException:
            task.cancel()
            raise
        if not done:
            # Answer now, whatever the handler does next: its late result is discarded.
            task.cancel()
            self._abandoned.add(task)
            task.add_done_callback(self._forget)
            log.warning("tool %s timed out", name)
            return self._result(name, "timeout")
        try:
            out = task.result()
        except NotYetImplemented as e:
            log.info("tool %s: not built for this body: %s", name, e)
            return self._result(name, "not_built", detail=e.capability.value)
        except NotSupported as e:
            log.info("tool %s: body does not support it: %s", name, e)
            return self._result(name, "body_not_supported", detail=e.capability.value)
        except BodyUnavailable as e:
            log.info("tool %s: body unavailable: %s", name, e)
            return self._result(name, "body_unavailable")
        except BodyBusy:
            return self._result(name, "busy")
        except (BodyTimeout, TimeoutError):
            log.warning("tool %s timed out", name)
            return self._result(name, "timeout")
        except BodyError as e:
            log.warning("tool %s: body error %s: %s", name, e.code, e)
            code = e.code if isinstance(e.code, str) and _CODE_WORD.match(e.code) else None
            return self._result(name, "body_error", detail=code)
        except asyncio.CancelledError:
            log.warning("tool %s: its handler was cancelled", name)
            return self._result(name, "internal")
        except Exception:
            log.exception("tool %s failed", name)
            return self._result(name, "internal")
        content, attachments = (out.content, out.attachments) if isinstance(out, ToolOutput) else (out, ())
        if (not isinstance(content, dict) or not isinstance(attachments, (tuple, list))
                or not all(isinstance(a, Image) for a in attachments)):
            log.error("tool %s returned something that is not a JSON object with Image attachments", name)
            return self._result(name, "internal")
        problem_out = next(iter(self._out_validators[name].iter_errors(content)), None)
        if problem_out is not None:
            log.error("tool %s returned output that does not match its schema: %s", name, problem_out.message[:200])
            return self._result(name, "internal")
        log.info("tool %s ok (channel %s)", name, caller.channel.value)
        return self._result(name, "ok", content=content, attachments=tuple(attachments))

    def _forget(self, task: asyncio.Task[Any]) -> None:
        self._abandoned.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.info("abandoned tool handler ended with %s", type(task.exception()).__name__)
