"""The plug-in API of the chaos harness (plan card X-01): how a card registers an experiment.

A card adds two things and nothing else:

1. Its entry points, as records in ``tools/chaos/manifest.toml`` (the drift check tells it which).
2. A cases module in ``tools/chaos/cases/`` that registers one or more experiments with ``@experiment``.

An experiment is the chaos-engineering unit (Principles of Chaos): a measurable **steady-state
hypothesis**, the **entry points** it disturbs, its **blast radius**, an **abort condition**, and
four **case generators**, one per class:

    valid    inputs the entry point must accept and handle correctly
    invalid  N1 wrong type, N2 oversize, N3 empty, N4 structure and depth, N5 encoding, N6 numeric edge
    hostile  N7 injection, N8 order and sequence, N9 replay and spoof, N16 authority
    failure  N10 concurrency, N11 exhaustion, N12 timing, N13 corrupt state, N14 dependency, N15 environment

Every class must be given. A class that genuinely does not apply is given as
``not_applicable("why")``, which the run summary prints, so a gap is a stated decision and never a
silent omission.

The ``check`` function runs one case and raises ``Violation`` when the steady state does not hold
(``ctx.expect(...)`` does it for you). It may be a plain function or a coroutine; each case runs in a
fresh event loop in its own thread, under every monitor (no crash, side-effect audit, resource
bounds, log bounds, secret scan). Whatever it returns is scanned for secrets and kept in the report
(shortened), so return the thing the entry point produced.

Minimal example (see README.md for the walk-through)::

    from tools.chaos.api import CaseClass, experiment, hostile, invalid, not_applicable, valid

    def gen_valid(g):
        yield valid("plain", {"text": "hello"})

    def gen_invalid(g):
        yield invalid("too_long", {"text": "x" * 5000}, "N2")

    @experiment(id="P9-99.example.speak", card="P9-99", entries=("tool.speak",),
                hypothesis="speak accepts text up to its limit and refuses anything longer, never raising",
                blast_radius="sim", valid=gen_valid, invalid=gen_invalid,
                hostile=not_applicable("covered by P0-06.registry.call"),
                failure=not_applicable("covered by P0-06.registry.call"))
    async def check(case, ctx):
        result = await run_the_entry_point(case.data)
        ctx.expect(result.ok == (case.cls is CaseClass.VALID), "validate_before_use", f"got {result.status}")
        return result
"""

from __future__ import annotations

import inspect
import random
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---- vocabulary --------------------------------------------------------------------------------------


class CaseClass(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    HOSTILE = "hostile"
    FAILURE = "failure"


CLASS_ORDER = (CaseClass.VALID, CaseClass.INVALID, CaseClass.HOSTILE, CaseClass.FAILURE)

# The negative-test classes of docs/testing/00 section 5.1 that belong to each case class.
CLASS_TAGS: dict[CaseClass, frozenset[str]] = {
    CaseClass.VALID: frozenset(),
    CaseClass.INVALID: frozenset({"N1", "N2", "N3", "N4", "N5", "N6"}),
    CaseClass.HOSTILE: frozenset({"N7", "N8", "N9", "N16"}),
    CaseClass.FAILURE: frozenset({"N10", "N11", "N12", "N13", "N14", "N15"}),
}
# Classes from 5.1 that cut across the four groups; any case may carry them in addition.
EXTRA_TAGS = frozenset({"N17", "N18", "N19", "N20", "N21"})


class BlastRadius(StrEnum):
    """What an experiment may touch. Everything runs inside the harness sandbox (a temporary data
    folder, canary secrets); nothing may reach the real phone, the real data folder or the network."""

    PURE = "pure"  # in-process computation only
    SANDBOX = "sandbox"  # may read and write the sandbox's temporary data folder
    SIM = "sim"  # SimBody in-process, plus the sandbox
    LOOPBACK = "loopback"  # loopback sockets this process opens itself, plus SimBody and the sandbox


# The seven invariants of plan card X-01 ("What it tests"). A Violation names one of them.
INVARIANTS = (
    "no_crash",  # 1
    "no_forbidden_side_effect",  # 2
    "validate_before_use",  # 3
    "bounded_resources",  # 4
    "no_secret_in_output",  # 5
    "right_to_refuse",  # 6
    "fail_closed",  # 7
)


class Violation(AssertionError):
    """The steady state did not hold. Raised by a check (or a monitor) for one invariant."""

    def __init__(self, invariant: str, message: str):
        if invariant not in INVARIANTS:
            raise ValueError(f"unknown invariant {invariant!r}; expected one of {INVARIANTS}")
        super().__init__(f"{invariant}: {message}")
        self.invariant = invariant
        self.message = message


# ---- cases -------------------------------------------------------------------------------------------

_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")


@dataclass(frozen=True)
class Case:
    """One input for one experiment. Build it with valid(), invalid(), hostile() or failure().

    `data` is anything the check needs; it is regenerated from the seed, never stored. `known_defect`
    marks a case that is EXPECTED to break the steady state because of a recorded, unfixed defect
    (for example "H-08: ..."): the run reports it as xfail, and as a failure once it starts passing
    (strict), so the marker is removed in the commit that fixes the defect.
    """

    name: str
    cls: CaseClass
    data: Any = None
    tags: frozenset[str] = frozenset()
    known_defect: str | None = None
    timeout_s: float | None = None  # None: the mode's per-case timeout
    max_log_lines: int | None = None  # None: PEER_LOG_LINES_PER_MIN_MAX
    # True only when the case itself injects an exception into a component (a handler that raises) and
    # an isolation point is expected to log its traceback. Any other logged traceback is a crash.
    isolated_fault: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.match(self.name):
            raise ValueError(f"case name {self.name!r}: lower case letters, digits, _ . - (max 80)")
        cls = CaseClass(self.cls)
        object.__setattr__(self, "cls", cls)
        tags = frozenset(self.tags)
        object.__setattr__(self, "tags", tags)
        own = CLASS_TAGS[cls]
        if cls is not CaseClass.VALID and not tags & own:
            raise ValueError(f"case {self.name!r}: a {cls.value} case needs at least one of {sorted(own)}")
        stray = tags - own - EXTRA_TAGS
        if stray:
            raise ValueError(f"case {self.name!r}: tags {sorted(stray)} do not belong to class {cls.value}")
        if self.known_defect is not None and not (isinstance(self.known_defect, str) and self.known_defect.strip()):
            raise ValueError("known_defect must name the defect (for example 'H-08: ...')")


def valid(name: str, data: Any = None, **kw: Any) -> Case:
    return Case(name, CaseClass.VALID, data, frozenset(), **kw)


def invalid(name: str, data: Any, *tags: str, **kw: Any) -> Case:
    return Case(name, CaseClass.INVALID, data, frozenset(tags), **kw)


def hostile(name: str, data: Any, *tags: str, **kw: Any) -> Case:
    return Case(name, CaseClass.HOSTILE, data, frozenset(tags), **kw)


def failure(name: str, data: Any, *tags: str, **kw: Any) -> Case:
    return Case(name, CaseClass.FAILURE, data, frozenset(tags), **kw)


# ---- generation and run contexts ---------------------------------------------------------------------


@dataclass
class Gen:
    """Given to a generator. Use `rng` for every random choice (never `random` or the clock), so the
    same seed always gives the same cases. `n(quick, full)` is how many random cases to make."""

    rng: random.Random
    mode: str  # "quick" or "full"
    seed: int

    def n(self, quick: int, full: int | None = None) -> int:
        return quick if self.mode == "quick" else (full if full is not None else quick * 10)


@dataclass
class Ctx:
    """Given to a check for one case."""

    rng: random.Random  # seeded for this case
    mode: str
    case_seed: int
    sandbox: Any  # tools.chaos.sandbox.Sandbox
    monitors: Any  # tools.chaos.monitors.CaseMonitors: logs captured so far, observe_queue()
    notes: list[str] = field(default_factory=list)

    def expect(self, condition: object, invariant: str, message: str) -> None:
        """Raise Violation(invariant, message) unless `condition` is true."""
        if not condition:
            raise Violation(invariant, message)

    def note(self, text: str) -> None:
        """A short remark kept in the report (for example a measured duration)."""
        self.notes.append(text[:200])

    def log_messages(self) -> list[str]:
        return self.monitors.messages()


GenFn = Callable[[Gen], Iterable[Case]]
CheckFn = Callable[[Case, Ctx], Any]


def not_applicable(reason: str) -> GenFn:
    """The generator for a class that does not apply to this experiment, with the reason."""
    if not isinstance(reason, str) or len(reason.strip()) < 10:
        raise ValueError("not_applicable needs a reason of at least 10 characters")

    def none(g: Gen) -> Iterable[Case]:
        return ()

    none.not_applicable = reason.strip()  # type: ignore[attr-defined]
    return none


def na_reason(gen: GenFn) -> str | None:
    return getattr(gen, "not_applicable", None)


# ---- experiments and their registry --------------------------------------------------------------------

_ID = re.compile(r"^(P\d-\d\d|X-01|SYS|AND)\.[a-z0-9_]+\.[a-z0-9_]+$")
_CARD = re.compile(r"^(P\d-\d\d|X-01|SYS|AND|R-\d\d)$")


@dataclass(frozen=True)
class Experiment:
    id: str  # "<card>.<area>.<name>", e.g. "P0-06.registry.call"
    card: str
    entries: tuple[str, ...]  # manifest ids or glob patterns ("tool.*")
    hypothesis: str  # the steady state, stated so it can be measured
    blast_radius: BlastRadius
    generators: Mapping[CaseClass, GenFn]
    check: CheckFn
    allow_processes: frozenset[str] = frozenset()  # executable names a case may start (default none)
    abort_after_failures: int = 5  # abort condition: stop this experiment (for this seed) after this many
    module: str = ""

    def __post_init__(self) -> None:
        if not _ID.match(self.id):
            raise ValueError(f"experiment id {self.id!r} must look like 'P0-06.area.name'")
        if not _CARD.match(self.card) or not self.id.startswith(self.card + "."):
            raise ValueError(f"experiment {self.id}: card {self.card!r} must be the id's prefix")
        if isinstance(self.entries, str) or not self.entries or not all(isinstance(e, str) and e for e in self.entries):
            raise ValueError(f"experiment {self.id}: entries must be a non-empty tuple of manifest ids")
        object.__setattr__(self, "entries", tuple(self.entries))
        if not isinstance(self.hypothesis, str) or len(self.hypothesis.strip()) < 20:
            raise ValueError(f"experiment {self.id}: state the steady-state hypothesis (at least 20 characters)")
        object.__setattr__(self, "blast_radius", BlastRadius(self.blast_radius))
        gens = {CaseClass(k): v for k, v in dict(self.generators).items()}
        missing = [c.value for c in CLASS_ORDER if c not in gens]
        if missing:
            raise ValueError(f"experiment {self.id}: give a generator for every class; missing {missing} "
                             "(use not_applicable('why') for a class that does not apply)")
        if not all(callable(v) for v in gens.values()):
            raise ValueError(f"experiment {self.id}: generators must be callables")
        if all(na_reason(gens[c]) for c in CLASS_ORDER if c is not CaseClass.VALID) or na_reason(gens[CaseClass.VALID]):
            raise ValueError(f"experiment {self.id}: needs valid cases and at least one negative class")
        object.__setattr__(self, "generators", gens)
        if not callable(self.check):
            raise ValueError(f"experiment {self.id}: check must be callable")
        object.__setattr__(self, "allow_processes", frozenset(p.lower() for p in self.allow_processes))
        if not isinstance(self.abort_after_failures, int) or self.abort_after_failures < 1:
            raise ValueError(f"experiment {self.id}: abort_after_failures must be a positive int")

    @property
    def is_async(self) -> bool:
        return inspect.iscoroutinefunction(self.check)


_REGISTRY: dict[str, Experiment] = {}


def experiment(*, id: str, card: str, entries: Iterable[str], hypothesis: str, blast_radius: str | BlastRadius,
               valid: GenFn, invalid: GenFn, hostile: GenFn, failure: GenFn,
               allow_processes: Iterable[str] = (), abort_after_failures: int = 5) -> Callable[[CheckFn], CheckFn]:
    """Decorator on the check function: build the Experiment and register it (once per id)."""

    def register(check: CheckFn) -> CheckFn:
        exp = Experiment(id, card, tuple(entries), hypothesis, BlastRadius(blast_radius),
                         {CaseClass.VALID: valid, CaseClass.INVALID: invalid, CaseClass.HOSTILE: hostile,
                          CaseClass.FAILURE: failure}, check, frozenset(allow_processes), abort_after_failures,
                         getattr(check, "__module__", ""))
        old = _REGISTRY.get(exp.id)
        if old is not None and old.module != exp.module:
            raise ValueError(f"experiment id {exp.id!r} is registered twice ({old.module} and {exp.module})")
        _REGISTRY[exp.id] = exp
        return check

    return register


def registered() -> dict[str, Experiment]:
    """Every registered experiment by id (import tools.chaos.cases first; the runner does)."""
    return dict(sorted(_REGISTRY.items()))


def _unregister_for_tests(exp_id: str) -> None:
    _REGISTRY.pop(exp_id, None)
