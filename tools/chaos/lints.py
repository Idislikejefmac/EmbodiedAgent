"""E2 (docs/testing/00 section 4.3): no `assert` in product code, because `python -O` removes it.

A minimal version for the X-01 harness: it scans brain/ and tools/ and compares what it finds with
the recorded baseline below (the seven asserts verified on 13 Nov 2026, remediation item H-06). A new
assert fails; a baseline entry that no longer matches fails as stale, so the list only shrinks. The
full lint module (tools/lint.py with tests/lint_baseline.toml, E1a, E4, E5, E9) is separate planned
work; when it lands this check moves there.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from brain.config import REPO_ROOT

SCAN_ROOTS: list[Path] = [REPO_ROOT / "brain", REPO_ROOT / "tools"]

# (file, qualified function) -> number of asserts allowed until H-06 lands.
BASELINE: dict[tuple[str, str], int] = {}  # empty since H-06 landed (batch 1)
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


# ---- E4 (H-24): broad and silent excepts carry a reviewed marker --------------------------------------------------

BROAD_MARKER = re.compile(r"#\s*broad-except:\s*(\S.{9,})")  # a reason of at least 10 characters
SWALLOW_MARKER = re.compile(r"#\s*swallowed:\s*(\S.{9,})")
EXCEPT_ROOTS: list[Path] = [REPO_ROOT / "brain"]


def _caught_names(handler: ast.ExceptHandler) -> list[str]:
    if handler.type is None:
        return ["<bare>"]
    parts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return [ast.unparse(p) for p in parts]


def _is_silent(handler: ast.ExceptHandler) -> bool:
    """The body does nothing: only pass, `...` or a bare string."""
    return all(isinstance(b, ast.Pass) or (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))
               for b in handler.body)


def except_problems(roots: Iterable[Path] | None = None) -> list[str]:
    """Handlers that catch Exception, BaseException or everything (bare), or that do nothing, and that lack
    `# broad-except: <reason>` (broad) or `# swallowed: <reason>` (silent), on the `except` line or the comment
    line just above it. A site is one problem; the reason must say why swallowing is right there."""
    problems: list[str] = []
    for root in roots or EXCEPT_ROOTS:
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in files:
            if "__pycache__" in path.parts:
                continue
            source = path.read_text(encoding="utf-8-sig")
            lines = source.splitlines()
            for node in ast.walk(ast.parse(source, filename=str(path))):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                broad = any(n in ("Exception", "BaseException", "<bare>") for n in _caught_names(node))
                silent = _is_silent(node)
                if not (broad or silent):
                    continue
                here = lines[node.lineno - 1]
                above = lines[node.lineno - 2].strip() if node.lineno >= 2 else ""
                text = here + "\n" + (above if above.startswith("#") else "")
                if silent and not SWALLOW_MARKER.search(text) and not (broad and BROAD_MARKER.search(text)):
                    kind = "does nothing"
                elif broad and not silent and not BROAD_MARKER.search(text):
                    kind = "catches everything"
                elif broad and silent and not (BROAD_MARKER.search(text) or SWALLOW_MARKER.search(text)):
                    kind = "catches everything and does nothing"
                else:
                    continue
                marker = "# swallowed: <reason>" if silent and not broad else "# broad-except: <reason>"
                problems.append(f"{_rel(path)}:{node.lineno}: `except {', '.join(_caught_names(node))}` {kind}; "
                                f"add {marker} after review")
    return problems
