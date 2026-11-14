"""E2 (docs/testing/00 section 4.3): no `assert` in product code, because `python -O` removes it.

A minimal version for the X-01 harness: it scans brain/ and tools/ and compares what it finds with
the recorded baseline below (the seven asserts verified on 13 Nov 2026, remediation item H-06). A new
assert fails; a baseline entry that no longer matches fails as stale, so the list only shrinks. The
full lint module (tools/lint.py with tests/lint_baseline.toml, E1a, E4, E5, E9) is separate planned
work; when it lands this check moves there.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from brain.config import REPO_ROOT

SCAN_ROOTS: list[Path] = [REPO_ROOT / "brain", REPO_ROOT / "tools"]

# (file, qualified function) -> number of asserts allowed until H-06 lands.
BASELINE: dict[tuple[str, str], int] = {
    ("brain/transport/protocol.py", "ProtocolError.__init__"): 1,  # a real validation (10-F01)
    ("brain/agent/tools.py", "_plain_json_problem"): 4,  # type narrowing
    ("brain/agent/tools.py", "ToolRegistry._argument_problem"): 1,  # inside a validation helper
    ("brain/agent/tools.py", "ToolRegistry._call"): 1,  # type narrowing
}
BASELINE_FIX = "H-06"


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def asserts_in(path: Path) -> Counter[tuple[str, str]]:
    found: Counter[tuple[str, str]] = Counter()
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))

    def visit(node: ast.AST, qual: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, f"{qual}.{child.name}" if qual else child.name)
            else:
                if isinstance(child, ast.Assert):
                    found[(_rel(path), qual or "<module>")] += 1
                visit(child, qual)

    visit(tree, "")
    return found


def scan(roots: Iterable[Path] | None = None) -> Counter[tuple[str, str]]:
    total: Counter[tuple[str, str]] = Counter()
    for root in roots or SCAN_ROOTS:
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for f in files:
            if "__pycache__" not in f.parts:
                total += asserts_in(f)
    return total


def assert_problems(found: Counter[tuple[str, str]] | None = None,
                    baseline: dict[tuple[str, str], int] | None = None) -> tuple[list[str], list[str]]:
    """(new violations, stale baseline entries). Both fail."""
    found = scan() if found is None else found
    base = BASELINE if baseline is None else baseline
    new = [f"{f}:{q}: {n} assert(s) used for checks (removed under python -O)" + (f", baseline {base[(f, q)]}"
                                                                                    if (f, q) in base else "")
           for (f, q), n in sorted(found.items()) if n > base.get((f, q), 0)]
    stale = [f"{f}:{q}: baseline allows {n} assert(s) but {found.get((f, q), 0)} remain; lower the baseline "
             f"({BASELINE_FIX} is landing)" for (f, q), n in sorted(base.items()) if found.get((f, q), 0) < n]
    return new, stale
