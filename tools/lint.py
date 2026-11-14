"""Static lints from docs/testing/00 sections 4.3 to 4.5 that guard the self-checking function standard.

    python tools/lint.py e1a [--write-baseline]   E1a (conservative subset): raw types in boundary signatures
    python tools/lint.py e1b                      E1b: every manifest gate has below/at/above and wrong_type edge tests

Both read tests/lint_baseline.toml (existing violations, keyed by SYMBOL, never by line number). Rules (4.5):
a violation not in the baseline fails; a baseline entry that no longer matches fails as stale (delete it); a
symbol's count may not rise; meta.max_entries equals the number of entries, so the file can only shrink.
Run by tests/test_lint.py. Stdlib only.

E1a here is the conservative subset (see docs/chaos.md "What the lints do not cover"): in public functions and
methods of the boundary modules, a parameter annotated `Any`, `object`, `dict`, `Mapping` (any form), `bytes`,
`bytearray` or `memoryview` (also inside a union, `Optional` or a container). NOT covered: missing annotations,
bare `str` with a vocabulary name (text, path, host, ...), NewType forgery outside a gate module, `@trusted_input`
markers (the decorator does not exist yet), methods overriding a base class (skipped to avoid noise), private
functions, Kotlin.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
BASELINE = REPO / "tests" / "lint_baseline.toml"
MANIFEST = REPO / "tools" / "chaos" / "manifest.toml"
BOUNDARY_ROOTS = ["brain/transport", "brain/agent", "brain/body", "brain/identity", "brain/memory", "brain/spatial",
                  "brain/voice", "brain/recorder", "brain/config.py", "brain/__main__.py", "tools/migrate.py"]
RAW_NAMES = frozenset({"Any", "object", "dict", "Dict", "Mapping", "MutableMapping", "bytes", "bytearray", "memoryview"})
EDGES = ("below", "at", "above")


def _files() -> list[Path]:
    out: list[Path] = []
    for rel in BOUNDARY_ROOTS:
        p = REPO / rel
        out += [p] if p.is_file() else sorted(p.rglob("*.py")) if p.exists() else []
    return [f for f in out if "__pycache__" not in f.parts]


def _raw_in(annotation: ast.AST | None) -> list[str]:
    """Names from RAW_NAMES that occur anywhere in an annotation (unions, subscripts, string annotations)."""
    if annotation is None:
        return []
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return []
    found = []
    for node in ast.walk(annotation):
        name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
        if name in RAW_NAMES:
            found.append(name)
    return found


def _is_property(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any((isinstance(d, ast.Name) and d.id in ("property", "cached_property", "overload"))
               or (isinstance(d, ast.Attribute) and d.attr in ("setter", "getter", "deleter", "overload")) for d in fn.decorator_list)


def scan_e1a(files: list[Path] | None = None, gates: set[str] | None = None) -> Counter[tuple[str, str]]:
    """(file, qualified symbol) -> number of raw-typed parameters, over the boundary modules."""
    found: Counter[tuple[str, str]] = Counter()
    for path in files if files is not None else _files():
        rel = path.resolve().relative_to(REPO).as_posix() if path.is_absolute() and REPO in path.resolve().parents else path.as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))

        def visit(node: ast.AST, qual: str, in_class_with_bases: bool) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    if child.name.startswith("_"):
                        continue
                    bases = any(not (isinstance(b, ast.Name) and b.id in ("object", "Exception")) for b in child.bases)
                    visit(child, f"{qual}.{child.name}" if qual else child.name, bases)
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = child.name
                    public = not name.startswith("_") or name in ("__init__", "__call__")
                    symbol = f"{qual}.{name}" if qual else name
                    if not public or _is_property(child) or (qual and in_class_with_bases and name != "__init__") \
                            or (gates and f"{rel.removesuffix('.py').replace('/', '.')}.{symbol}" in gates):
                        continue
                    args = child.args
                    params = [*args.posonlyargs, *args.args, *args.kwonlyargs] + ([args.vararg] if args.vararg else []) \
                        + ([args.kwarg] if args.kwarg else [])
                    n = sum(1 for a in params if a.arg not in ("self", "cls") and _raw_in(a.annotation))
                    if n:
                        found[(rel, symbol)] += n

        visit(tree, "", False)
    return found


def load_baseline(path: Path = BASELINE) -> dict[str, Any]:
    if not path.exists():
        return {"meta": {"max_entries": 0}, "entry": []}
    doc: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    return doc


def _gate_names() -> set[str]:
    doc = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    return {g["name"] for g in doc.get("gate", [])}


def compare(found: Counter[tuple[str, str]], baseline: dict[str, Any], check: str) -> list[str]:
    """Problems for one check: new violations, count rises, stale entries, and a max_entries that is not the entry count."""
    base = {(e["file"], e["symbol"]): int(e["count"]) for e in baseline.get("entry", []) if e["check"] == check}
    out = []
    for key, n in sorted(found.items()):
        if key not in base:
            out.append(f"{check} {key[0]}: {key[1]}: {n} new violation(s) (not in tests/lint_baseline.toml; fix the code)")
        elif n > base[key]:
            out.append(f"{check} {key[0]}: {key[1]}: {n} violations, baseline allows {base[key]} (counts may only fall)")
    for key, n in sorted(base.items()):
        if found.get(key, 0) < n:
            out.append(f"{check} {key[0]}: {key[1]}: baseline lists {n} but {found.get(key, 0)} remain (stale: lower or delete the entry)")
    meta = baseline.get("meta", {}).get("max_entries")
    total = len(baseline.get("entry", []))
    if meta != total:
        out.append(f"baseline meta.max_entries is {meta} but the file has {total} entries (set it to {total}; it may only go down)")
    return out


def e1a_problems() -> list[str]:
    return compare(scan_e1a(gates=_gate_names()), load_baseline(), "E1a")


# ---- E1b -----------------------------------------------------------------------------------------------------------


def gate_edges_present(markers: list[tuple[str, str | None, str | None]]) -> set[tuple[str, str | None, str | None]]:
    return set(markers)


def scan_e1b(markers: list[tuple[str, str | None, str | None]], gates: list[dict[str, Any]]) -> Counter[tuple[str, str]]:
    """(manifest, gate name) -> number of missing edge tests. `markers` are (gate, limit, edge) from @pytest.mark.gate."""
    have = set(markers)
    missing: Counter[tuple[str, str]] = Counter()
    for g in gates:
        name = g["name"]
        n = sum(1 for lim in g.get("limits", []) for e in EDGES if (name, lim, e) not in have)
        n += 0 if any(m[0] == name and m[2] == "wrong_type" for m in have) else 1
        if n:
            missing[("tools/chaos/manifest.toml", name)] = n
    return missing


def e1b_problems(markers: list[tuple[str, str | None, str | None]]) -> list[str]:
    doc = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    return compare(scan_e1b(markers, doc.get("gate", [])), load_baseline(), "E1b")


def markers_from_collected(rows: list[dict[str, Any]] | None = None, collect: bool = False) -> list[tuple[str, str | None, str | None]]:
    """(gate, limit, edge) for every @pytest.mark.gate in the collected tests (data/trace/collected.json, tools/trace.py)."""
    import json

    if rows is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("ea_trace", REPO / "tools" / "trace.py")  # not the stdlib `trace`
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load tools/trace.py")
        tr = importlib.util.module_from_spec(spec)
        sys.modules["ea_trace"] = tr
        spec.loader.exec_module(tr)

        path = tr.collect() if collect else tr.COLLECTED
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for row in rows:
        for m in row.get("markers", []):
            if m["name"] == "gate" and m["args"]:
                out.append((m["args"][0], m["kwargs"].get("limit"), m["kwargs"].get("edge")))
    return out


def write_baseline(found_a: Counter[tuple[str, str]], found_b: Counter[tuple[str, str]]) -> None:
    lines = ["# Adoption baseline for tools/lint.py (docs/testing/00 section 4.5). Keyed by symbol, never by line.",
             "# A new violation fails; an entry that no longer matches fails as stale (delete it); counts and",
             "# max_entries may only fall. Written by `python tools/lint.py e1a --write-baseline`; changes by hand",
             "# only to LOWER a count or delete an entry.", "", "[meta]", f"max_entries = {len(found_a) + len(found_b)}", ""]
    for check, found, fix, card in (("E1a", found_a, "typed boundary objects (H-02 follow-up)", "X-01"),
                                    ("E1b", found_b, "gate edge tests (stage 1b adds the first ones)", "X-01")):
        for (file, symbol), n in sorted(found.items()):
            lines += ["[[entry]]", f'check = "{check}"', f'file = "{file}"', f'symbol = "{symbol}"', f"count = {n}",
                      f'fix = "{fix}"', f'card = "{card}"', ""]
    BASELINE.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("e1a", "e1b"):
        print(__doc__)
        return 2
    if argv[0] == "e1a":
        if "--write-baseline" in argv:
            old_b = Counter({(e["file"], e["symbol"]): int(e["count"]) for e in load_baseline().get("entry", []) if e["check"] == "E1b"})
            write_baseline(scan_e1a(gates=_gate_names()), old_b)
            print(f"wrote {BASELINE}")
            return 0
        problems = e1a_problems()
    else:
        markers = markers_from_collected(collect=True)
        if "--write-baseline" in argv:
            old_a = Counter({(e["file"], e["symbol"]): int(e["count"]) for e in load_baseline().get("entry", []) if e["check"] == "E1a"})
            doc = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
            write_baseline(old_a, scan_e1b(markers, doc.get("gate", [])))
            print(f"wrote {BASELINE}")
            return 0
        problems = e1b_problems(markers)
    for p in problems:
        print(p)
    print(f"{argv[0]}: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
