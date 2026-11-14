"""The boundary-value matrix (core; every card gets it for free).

For one input (a tool parameter, a protocol field, a CLI flag, a config key, a method argument) it
generates the standard boundary set from the input's declared limits:

    minimum, maximum, one under the minimum, one over the maximum, zero, empty, null, missing,
    wrong type, wrong encoding, oversized (ten times the limit and far beyond)

and says for each whether the entry point must ACCEPT it, must REFUSE it (with a structured error), or
may do either (only "no crash, no traceback, no hang, no side effect" is checked). The declared limits
come from a JSON Schema (`spec_from_schema`, for the tool catalogue and the protocol schema) or from an
explicit `Spec` (for code that has no schema yet), so a limit changes in one place.

Use from a cases module:

    from tools.chaos.matrix import Spec, boundary_values, matrix_cases
    cases = matrix_cases("max_edge", Spec("integer", minimum=64, maximum=4096), make=lambda v: {...})
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .api import Case, CaseClass

MISSING: Any = type("Missing", (), {"__repr__": lambda self: "<missing>"})()  # the input is left out
OVERSIZE_CHARS = 1_048_576  # "far beyond": a 1 MiB string or list


@dataclass(frozen=True)
class Spec:
    """What an input declares. `type` is a JSON type name, "enum", or "any" (nothing declared)."""

    type: str
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: float | None = None
    exclusive_maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    min_items: int | None = None
    max_items: int | None = None
    enum: tuple[Any, ...] | None = None
    pattern: str | None = None
    required: bool = True
    nullable: bool = False
    item: Spec | None = None  # for arrays
    strict_text: bool = True  # False for free text that may hold any printable character


@dataclass(frozen=True)
class Boundary:
    name: str
    value: Any
    tag: str  # the N-class
    accept: bool | None  # True: must accept; False: must refuse (structured); None: either, no crash


def _resolve(schema: dict[str, Any], root: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    stack = [schema]
    while stack:
        s = stack.pop()
        ref = s.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/") and root is not None:
            stack.append(root["$defs"][ref.split("/")[-1]])
        for sub in s.get("allOf", []) if isinstance(s.get("allOf"), list) else []:
            if isinstance(sub, dict) and "if" not in sub:
                stack.append(sub)
        out.update({k: v for k, v in s.items() if k not in ("$ref", "allOf")})
    return out


def spec_from_schema(schema: dict[str, Any], root: dict[str, Any] | None = None, *, required: bool = True) -> Spec:
    s = _resolve(schema, root)
    if "enum" in s or "const" in s:
        return Spec("enum", enum=tuple(s.get("enum", [s.get("const")])), required=required)
    if "oneOf" in s or "anyOf" in s:
        return Spec("any", required=required)
    t = s.get("type")
    nullable = isinstance(t, list) and "null" in t
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "any")
    item = spec_from_schema(s["items"], root) if isinstance(s.get("items"), dict) else None
    return Spec(t or "any", s.get("minimum"), s.get("maximum"), s.get("exclusiveMinimum"), s.get("exclusiveMaximum"),
                s.get("minLength"), s.get("maxLength"), s.get("minItems"), s.get("maxItems"), None, s.get("pattern"),
                required, nullable, item, strict_text=s.get("x-kind") not in ("spoken_text", "reason_text"))


def _in_range(spec: Spec, x: float) -> bool:
    return ((spec.minimum is None or x >= spec.minimum) and (spec.maximum is None or x <= spec.maximum)
            and (spec.exclusive_minimum is None or x > spec.exclusive_minimum)
            and (spec.exclusive_maximum is None or x < spec.exclusive_maximum))


def boundary_values(spec: Spec) -> list[Boundary]:
    """The boundary set for one input. Deterministic (no randomness)."""
    out: list[Boundary] = []
    add = out.append
    add(Boundary("missing", MISSING, "N3", True if not spec.required else False))
    add(Boundary("null", None, "N1", True if spec.nullable else (None if spec.type == "any" else False)))
    t = spec.type
    if t in ("integer", "number"):
        lo = spec.minimum if spec.minimum is not None else spec.exclusive_minimum
        hi = spec.maximum if spec.maximum is not None else spec.exclusive_maximum
        step = 1 if t == "integer" else 0.001
        if spec.minimum is not None:
            add(Boundary("minimum", spec.minimum, "N6", True))
        if spec.exclusive_minimum is not None:
            add(Boundary("at_exclusive_minimum", spec.exclusive_minimum, "N6", False))
        if lo is not None:
            add(Boundary("one_under_minimum", lo - step, "N6", False))
        if spec.maximum is not None:
            add(Boundary("maximum", spec.maximum, "N6", True))
        if spec.exclusive_maximum is not None:
            add(Boundary("at_exclusive_maximum", spec.exclusive_maximum, "N6", False))
        if hi is not None:
            add(Boundary("one_over_maximum", hi + step, "N6", False))
            add(Boundary("ten_times_maximum", hi * 10 + 1 if hi > 0 else 10**6, "N2", False))
        add(Boundary("zero", 0, "N6", _in_range(spec, 0)))
        add(Boundary("negative_one", -1, "N6", _in_range(spec, -1)))
        add(Boundary("huge", 2**64, "N2", _in_range(spec, 2**64)))
        add(Boundary("nan", math.nan, "N6", False))
        add(Boundary("infinity", math.inf, "N6", False))
        add(Boundary("minus_infinity", -math.inf, "N6", False))
        add(Boundary("bool", True, "N1", False))
        add(Boundary("numeric_string", "1", "N1", False))
        if t == "integer":
            add(Boundary("fraction", 1.5, "N1", False))
    elif t == "string":
        lo, hi = spec.min_length or 0, spec.max_length
        fill = "a"
        pattern_ok: bool | None = None if spec.pattern else True  # a pattern may forbid the filler
        add(Boundary("empty", "", "N3", (lo == 0 and pattern_ok) if lo == 0 else False))
        add(Boundary("one_char", fill, "N3", pattern_ok if lo <= 1 and (hi is None or hi >= 1) else False))
        if hi is not None:
            add(Boundary("max_length", fill * hi, "N2", pattern_ok))
            add(Boundary("one_over_max_length", fill * (hi + 1), "N2", False))
            add(Boundary("ten_times_max_length", fill * (hi * 10 + 1), "N2", False))
        add(Boundary("oversized_1mib", fill * OVERSIZE_CHARS, "N2", False if hi is not None else None))
        add(Boundary("wrong_type_int", 7, "N1", False))
        add(Boundary("wrong_type_list", ["a"], "N1", False))
        add(Boundary("wrong_type_bytes", b"a", "N1", False))
        for name, text in (("nul", "a\x00b"), ("bell", "a\x07b"), ("newline", "a\nb"), ("escape", "a\x1b[2Jb")):
            add(Boundary(f"control_{name}", text, "N5", False if spec.strict_text and spec.pattern else None))
        for name, text in (("lone_surrogate", "a\ud800b"), ("bidi_override", "a‮b"), ("zero_width", "a​b"),
                           ("fullwidth", "ａｂ"), ("bom", "\ufeffab")):
            add(Boundary(f"encoding_{name}", text, "N5", None))
    elif t == "boolean":
        add(Boundary("true", True, "N1", True))
        add(Boundary("false", False, "N1", True))
        for name, v in (("zero", 0), ("one", 1), ("string_true", "true")):
            add(Boundary(f"wrong_type_{name}", v, "N1", False))
    elif t == "enum":
        for i, v in enumerate((spec.enum or ())[:8]):
            add(Boundary(f"enum_{i}", v, "N1", True))
        add(Boundary("not_in_enum", "zz_not_listed", "N1", False))
        add(Boundary("empty", "", "N3", "" in (spec.enum or ())))
        add(Boundary("wrong_type_int", 7, "N1", 7 in (spec.enum or ())))
        add(Boundary("oversized_1mib", "a" * OVERSIZE_CHARS, "N2", False))
    elif t == "array":
        lo, hi = spec.min_items or 0, spec.max_items
        add(Boundary("empty", [], "N3", None if lo == 0 else False))
        if hi is not None:
            add(Boundary("one_over_max_items", [_sample(spec.item)] * (hi + 1), "N2", False))
        add(Boundary("oversized", [_sample(spec.item)] * 100_000, "N2", None if hi is None else False))
        add(Boundary("wrong_type_string", "a", "N1", False))
        add(Boundary("wrong_type_object", {"a": 1}, "N1", False))
    elif t == "object":
        add(Boundary("empty", {}, "N3", None))
        add(Boundary("wrong_type_list", [], "N1", False))
        add(Boundary("deep", _deep(100_000), "N4", None))
        add(Boundary("many_keys", {f"k{i}": i for i in range(5000)}, "N4", None))
    else:  # "any": nothing declared, so only "no crash" can be asked
        for name, v in (("int", 7), ("string", "a"), ("list", []), ("deep", _deep(100_000)), ("nan", math.nan)):
            add(Boundary(f"any_{name}", v, "N1", None))
    return out


def _sample(spec: Spec | None) -> Any:
    if spec is None:
        return 1
    return {"integer": 1, "number": 0.5, "string": "a", "boolean": True}.get(spec.type, 1)


def _deep(depth: int) -> Any:
    value: Any = 1
    for _ in range(depth):
        value = {"a": value}
    return value


def matrix_cases(prefix: str, spec: Spec, make: Callable[[Boundary], Any], *, known: dict[str, str] | None = None,
                 skip: Iterable[str] = (), context: Callable[[Boundary], bool | None] | None = None) -> list[Case]:
    """Cases for one input: `make(boundary)` builds the case data. A boundary the input must accept is a
    VALID case; one it must refuse, or may treat either way, is an INVALID case. `known` maps a boundary
    name (or "*" for all refusals) to a recorded defect, which makes that case a strict expected failure.
    `context(boundary)` may replace a boundary's expectation where a rule outside the input's own
    declaration applies (for example hello.role must equal the sender); return b.accept to keep it."""
    known = known or {}
    out: list[Case] = []
    stem = re.sub(r"[^a-z0-9_.-]+", "_", prefix.lower()).strip("_.-") or "input"
    for b in boundary_values(spec):
        if b.name in skip:
            continue
        if context is not None:
            b = Boundary(b.name, b.value, b.tag, context(b))
        cls = CaseClass.VALID if b.accept is True else CaseClass.INVALID
        defect = known.get(b.name) or (known.get("*") if b.accept is False else None)
        out.append(Case(f"{stem}.{b.name}"[-80:].lstrip("_.-"), cls, make(b), frozenset() if cls is CaseClass.VALID else {b.tag},
                        known_defect=defect))
    return out
