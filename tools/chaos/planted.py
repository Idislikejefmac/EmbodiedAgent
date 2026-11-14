"""Planted bugs: the harness's own acceptance test (plan card X-01 steps 2 and 4; T-X01-005).

Each entry reintroduces, IN MEMORY ONLY and only while its context manager is open, a defect that the
P0-05 and P0-06 reviews found and fixed (or a defect class the harness must catch). Production files
are never edited. The harness must fail with the plant active and pass without it, and the failure
must reproduce from the printed seed:

    python -m brain chaos --quick --plant recursion      (fails, prints seeds and case ids)
    python -m brain chaos --seed 1 --only <case id> --plant recursion   (fails again)
    python -m brain chaos --seed 1 --only <case id>      (passes)
"""

from __future__ import annotations

import copy
import functools
import json
import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any

from brain import egress
from brain.agent import tools as tools_mod
from brain.transport import protocol
from brain.transport import server as server_mod

from . import lints

_REAL_SCAN = lints.scan


@dataclass(frozen=True)
class Plant:
    name: str
    finding: str  # what the real defect was
    caught_by: str  # the experiment (or static check) expected to catch it
    patches: Callable[[], list[tuple[object, str, object]]]  # (target, attribute, new value)


# (i) The registry's argument walk before the depth guard: recursive, so deep nesting raised RecursionError.
def _recursive_plain_json_problem(args: object) -> str | None:
    if type(args) is not dict:
        return "arguments must be an object"
    count = [0]

    def walk(value: object) -> str | None:
        count[0] += 1
        if count[0] > tools_mod.MAX_ARGS_NODES:
            return "too many values"
        if type(value) is dict:
            for k, v in value.items():
                if type(k) is not str:
                    return "argument names must be strings"
                found = walk(v)
                if found:
                    return found
        elif type(value) is list:
            for v in value:
                found = walk(v)
                if found:
                    return found
        elif type(value) is float and not math.isfinite(value):
            return "numbers must be finite"
        elif type(value) is str and len(value) > tools_mod.MAX_ARGS_BYTES:
            return "too large"
        elif type(value) not in (dict, list, float, str, int, bool, type(None)):
            return "not plain JSON"
        return None

    found = walk(args)
    if found:
        return found
    return "too large" if len(json.dumps(args)) > tools_mod.MAX_ARGS_BYTES else None


# (ii) An unbounded log line: every argument name in the refusal log and detail (the fix shows 5).
def _all_keys(keys: Any, total: int) -> str:
    return str([str(k) for k in keys])


# (iii) The loopback proxy hole: loopback treated as always local, and proxy variables left in place.
_real_check_address = egress._check_address


def _leaky_check_address(policy: Any, family: int, address: object) -> None:
    if isinstance(address, tuple) and len(address) >= 2 and egress.is_local(address[0]):
        return
    _real_check_address(policy, family, address)


# (iv) Log forgery: the control-character ban on peer text removed from the schema.
@functools.lru_cache(maxsize=1)
def _schema_without_text_pattern() -> Any:
    s = copy.deepcopy(protocol.schema())
    s["$defs"]["text"].pop("pattern", None)
    return protocol._StrictValidator(s)


# (v) Handshake lockout: no eviction (a full pending table refuses newcomers) and idle sockets kept long.
_real_handle = server_mod.BrainServer._handle


async def _handle_without_eviction(self: Any, ws: Any) -> None:
    if len(self._pending) >= server_mod.MAX_PENDING_HANDSHAKES:
        await ws.close(1013, "too many pending handshakes")
        return
    await _real_handle(self, ws)


# (vi) A validation done with assert: a new module that the E2 lint must see.
PLANTED_ASSERT_SOURCE = ("CODES = ('a', 'b')\n\n\ndef check_code(code):\n"
                         "    assert code in CODES, code  # vanishes under python -O\n    return code\n")


# (vii) A new entry point nobody registered: a tool added to the catalogue.
_real_load_catalogue = tools_mod.load_catalogue


def _catalogue_with_new_tool() -> tools_mod.Catalogue:
    cat = _real_load_catalogue()
    spec = replace(cat.tools["stop"], name="teleport", description="Planted by the harness self-test.")
    return replace(cat, tools={**cat.tools, "teleport": spec})


def _scan_with_planted_assert(roots: Any = None) -> Any:
    """The real scan, plus the planted module's asserts (parsed from text: nothing is written to disk)."""
    import ast
    from collections import Counter

    n = sum(isinstance(node, ast.Assert) for node in ast.walk(ast.parse(PLANTED_ASSERT_SOURCE)))
    return _REAL_SCAN(roots) + Counter({("brain/planted_validation.py", "check_code"): n})


PLANTS: dict[str, Plant] = {p.name: p for p in (
    Plant("recursion", "P0-06 review: ToolRegistry.call raised RecursionError on deeply nested arguments",
          "P0-06.registry.args_gate", lambda: [(tools_mod, "_plain_json_problem", _recursive_plain_json_problem)]),
    Plant("unbounded_log", "P0-05/P0-06 review: a log line held every key of peer-supplied data",
          "P0-06.registry.call", lambda: [(tools_mod, "_show_keys", _all_keys)]),
    Plant("proxy_bypass", "P0-06 review: a loopback proxy (HTTP_PROXY to 127.0.0.1) carried traffic past the guard",
          "P0-06.egress.guard", lambda: [(egress, "_check_address", _leaky_check_address),
                                         (egress, "strip_proxy_environment", lambda: [])]),
    Plant("log_forgery", "P0-05 review: a control character in hello.software forged a log line",
          "P0-05.server.session", lambda: [(protocol, "_validator", _schema_without_text_pattern)]),
    Plant("handshake_lockout", "P0-05 review: idle unauthenticated sockets locked the real body out",
          "P0-05.server.session", lambda: [(server_mod.BrainServer, "_handle", _handle_without_eviction),
                                           (server_mod, "FIRST_FRAME_TIMEOUT_S", 30.0)]),
    Plant("assert_validation", "10-F01: a validation written as assert vanishes under python -O",
          "static: E2 assert lint", lambda: [(lints, "scan", _scan_with_planted_assert)]),
    Plant("unregistered_entry_point", "a new tool added without registering it with the harness",
          "static: manifest drift", lambda: [(tools_mod, "load_catalogue", _catalogue_with_new_tool)]),
)}


@contextmanager
def active(name: str) -> Iterator[Plant]:
    """Apply one plant for the length of the block, then restore every attribute exactly."""
    if name not in PLANTS:
        raise KeyError(f"unknown plant {name!r}; known: {sorted(PLANTS)}")
    plant = PLANTS[name]
    saved: list[tuple[object, str, object]] = []
    try:
        for target, attr, value in plant.patches():
            saved.append((target, attr, getattr(target, attr)))
            setattr(target, attr, value)
        yield plant
    finally:
        for target, attr, old in reversed(saved):
            setattr(target, attr, old)
