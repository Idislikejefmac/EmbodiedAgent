"""Redaction at the source (card X-02 step 3): what may enter a structured event, and in what shape.

Every value an event carries goes through `clean_fields` before the event exists, so nothing later in the
pipeline (ring buffer, sinks, files, viewer) can see more than this lets through:

- **Forbidden fields.** A field whose name contains a secret or content word (token, key, secret, password,
  mac, nonce, auth, cookie, credential, embedding, template, prompt, transcript, text, content, value,
  args, arguments, payload, pixels, pcm, biometric, faceprint, voiceprint) is replaced by `<redacted>`
  whatever its value. Names such as `arg_count`, `chars` or `capabilities` are fine: the check is on whole
  words of the name. Byte strings are never shown, only their length.
- **Secret shapes.** Any string that looks like an API key (`sk-...`), a 64-hex token, a bearer token or a
  `NAME_KEY=...` assignment is replaced by `<redacted:secret>`, whatever field it is in.
- **Control characters** (and the Unicode line separators and bidirectional overrides that can make one
  line look like two) are written as `\\xNN` / `\\uNNNN`, so an event line can never forge another line.
- **Bounds.** Strings are cut to OBS_FIELD_MAX_CHARS, lists to OBS_LIST_ITEMS_MAX items (with the count),
  dicts to OBS_FIELDS_MAX keys and OBS_NEST_DEPTH_MAX levels; anything that is not plain JSON is shown as
  its type name; non-finite floats become strings.

Peer-supplied strings (anything a body or a model said) go through `peer()`, which shows them with %r,
cut, like the text logs (S9). Tool arguments are never put in an event: `arg_summary` gives their names,
types and sizes only.

Nothing here raises: a hostile object whose `str` or `repr` fails is named by its type.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from itertools import islice
from typing import Any

from ..limits import OBS_FIELD_MAX_CHARS, OBS_FIELDS_MAX, OBS_LIST_ITEMS_MAX, OBS_NEST_DEPTH_MAX

REDACTED = "<redacted>"
REDACTED_SECRET = "<redacted:secret>"
FORBIDDEN_WORDS = frozenset({
    "token", "tokens", "key", "apikey", "secret", "secrets", "password", "passwd", "passphrase", "mac", "nonce",
    "auth", "cookie", "credential", "credentials", "embedding", "embeddings", "template", "templates", "prompt",
    "prompts", "transcript", "transcripts", "text", "content", "value", "values", "args", "arguments", "payload",
    "pixels", "pcm", "samples_data", "biometric", "faceprint", "voiceprint",
})
# Shapes of real secrets (the same idea as the chaos harness's log hygiene, tools/chaos/log_hygiene.py).
SECRET_SHAPES = re.compile(
    r"sk-(?:ant|proj|live|test)[A-Za-z0-9_-]{8,}"
    r"|(?<![0-9A-Fa-f])[0-9a-fA-F]{64}(?![0-9A-Fa-f])"
    r"|[Bb]earer\s+[A-Za-z0-9._-]{16,}"
    r"|\b[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)\s*=\s*\S{6,}"
    r"|://[^/\s:@]+:[^/\s@]+@")
# Control characters, C1 controls, line and paragraph separators, bidirectional embeddings and overrides, and
# lone surrogates (they cannot be written as UTF-8: one would take the event file down).
UNSAFE_CHARS = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\u2028\\u2029\\u202a-\\u202e\\u2066-\\u2069\\ufeff\\ud800-\\udfff]")
_WORDS = re.compile(r"[a-z0-9]+")


def _escape(match: re.Match[str]) -> str:
    code = ord(match.group(0))
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def clean_text(text: str, max_chars: int = OBS_FIELD_MAX_CHARS) -> str:
    """`text` cut to max_chars, secret shapes redacted, unsafe characters escaped. Never raises."""
    cut = text[:max_chars]
    if SECRET_SHAPES.search(text[: max_chars + 200]):
        return REDACTED_SECRET
    cut = UNSAFE_CHARS.sub(_escape, cut)
    if len(text) > max_chars:
        cut += "..."
    return cut


def peer(value: object, max_chars: int = OBS_FIELD_MAX_CHARS) -> str:
    """A peer-supplied value as %r, cut (it cannot forge a line or hide a secret). Never raises."""
    try:
        text = repr(value[: max_chars + 2] if isinstance(value, str) else value)
    except Exception:  # broad-except: repr of a hostile object may raise anything; an event must not
        return f"<{type(value).__name__}>"
    return clean_text(text, max_chars)


def forbidden_name(name: str) -> bool:
    """True if a field of this name may hold a secret or personal content (whole words of the name)."""
    low = name.lower()
    return any(word in FORBIDDEN_WORDS for word in _WORDS.findall(low)) or "api_key" in low


def clean_value(value: object, depth: int = 0) -> Any:
    """`value` as bounded, plain JSON (see the module docstring). Never raises."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if -(2**63) <= value < 2**63 else f"<int of {value.bit_length()} bits>"
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<bytes:{len(value)}>"
    if depth >= OBS_NEST_DEPTH_MAX:
        return f"<{type(value).__name__}>"
    if isinstance(value, Mapping):
        try:
            return clean_fields(value, depth + 1)
        except Exception:  # broad-except: a hostile mapping may raise while iterated; the event keeps its type
            return f"<{type(value).__name__}>"
    if isinstance(value, (list, tuple, set, frozenset)):
        try:
            items = list(islice(iter(value), OBS_LIST_ITEMS_MAX))
            total = len(value)
        except Exception:  # broad-except: a hostile container may raise while iterated
            return f"<{type(value).__name__}>"
        out = [clean_value(v, depth + 1) for v in items]
        if total > len(out):
            out.append(f"... {total - len(out)} more")
        return out
    return f"<{type(value).__name__}>"


def clean_fields(fields: Mapping[Any, Any], depth: int = 0) -> dict[str, Any]:
    """A bounded copy of `fields` with forbidden names redacted and every value cleaned. Never raises
    for a plain dict; a hostile mapping is handled by the caller (clean_value)."""
    out: dict[str, Any] = {}
    total = 0
    for key, value in fields.items():
        total += 1
        if len(out) >= OBS_FIELDS_MAX:
            continue
        name = clean_text(key if isinstance(key, str) else f"<{type(key).__name__}>", 40)
        out[name] = REDACTED if forbidden_name(name) else clean_value(value, depth)
    if total > len(out):
        out["_more_fields"] = total - len(out)
    return out


def arg_summary(args: object) -> tuple[int | None, list[str]]:
    """Tool arguments as (count, ["name:type:size", ...]): names, types and sizes, never values, for
    example (2, ["reason:str:12", "max_edge:int"]). The names are cleaned (a model chooses them). Never raises."""
    if not isinstance(args, dict):
        return None, [f"<{type(args).__name__}>"]
    shape: list[str] = []
    try:
        for name in islice(dict.keys(args), OBS_LIST_ITEMS_MAX):
            v = dict.__getitem__(args, name)
            kind = type(v).__name__
            if isinstance(v, (str, list, dict, bytes)):
                kind += f":{len(v)}"
            shape.append(f"{clean_text(name if isinstance(name, str) else '<' + type(name).__name__ + '>', 40)}:{kind}")
        return dict.__len__(args), shape
    except Exception:  # broad-except: a hostile dict subclass may raise; the summary says so
        return None, ["<unreadable>"]
