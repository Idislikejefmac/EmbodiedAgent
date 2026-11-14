"""Traceability: card -> tests -> risks -> promises (docs/testing/50 sections 3.2 to 3.5). Stdlib only.

    python tools/trace.py collect            collect the pytest items (writes data/trace/collected.json)
    python tools/trace.py report [--card C]  per card: planned/existing/manual ids, risks, promises
    python tools/trace.py check              the done-check: a card marked done must rest on real tests
    python tools/trace.py json               machine-readable form of the report
    python tools/trace.py seed               candidate [[test]] entries from doc 10's "Existing" rows

Where a test gets its ids, in order (a conflict is an error): a @pytest.mark.trace("T-...", promises=,
risks=) marker; a docstring that starts with ids ("T-P0-05-071 P5 R-FND-17: ..."); an entry in
tests/trace_map.toml (the existing flat tests carry no ids and are mapped there without editing them).

Card state comes from docs/testing/status.toml when it exists, else from [status] in tests/trace_map.toml.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs" / "testing"
MAP = REPO / "tests" / "trace_map.toml"
COLLECTED = REPO / "data" / "trace" / "collected.json"
MANIFEST = REPO / "tools" / "chaos" / "manifest.toml"

TEST_ID = re.compile(r"\bT-(?:P\d-\d\d|X-?01|AND|SYS|GATE\d+|G\d+|RB\d\d|R-\d\d)-(?:\d{3}|F\d\d)\b")
RISK_ID = re.compile(r"\bR-(?:FND|EYE|VOX|ID|SPA|MEM|RLZ|ROB|SYS)-\d\d\b")
PROMISE = re.compile(r"^P[1-5]$")
STATUSES = ("Existing", "Planned", "Manual", "Partial", "Deferred")
DONE_STATES = ("planned", "in_progress", "done")


def card_of(test_id: str) -> str:
    body = test_id[2:]
    m = re.match(r"(P\d-\d\d)-", body)
    if m:
        return m.group(1)
    if body.startswith(("X01", "X-01")):
        return "X-01"
    m = re.match(r"RB(\d\d)-", body) or re.match(r"R-(\d\d)-", body)
    if m:
        return f"R-{m.group(1)}"
    m = re.match(r"(?:GATE|G)(\d+)-", body)
    if m:
        return f"G{m.group(1)}"
    return body.split("-")[0]


@dataclass
class DocTest:
    id: str
    status: str
    promises: set[str]
    doc: str
    text: str


@dataclass
class Risk:
    id: str
    score: int | None
    owner: str | None
    tests: set[str]
    doc: str
    defining: bool = False  # from a row with L, I and score (not an index row)


@dataclass
class Model:
    doc_tests: dict[str, DocTest] = field(default_factory=dict)
    risks: dict[str, Risk] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    collected: list[dict[str, Any]] = field(default_factory=list)
    claims: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # T-id -> collected nodes
    node_ids: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # node -> ids
    node_risks: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    node_promises: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    file_cards: dict[str, str] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)
    waivers: list[dict[str, Any]] = field(default_factory=list)
    manual: dict[str, str] = field(default_factory=dict)


# ---- reading the design documents ----------------------------------------------------------------------------


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_docs(paths: list[Path], model: Model) -> None:
    for path in paths:
        owner: str | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            h = re.match(r"^#{2,3} .*?\b(?:Card|card) (P\d-\d\d|X-01|R-\d\d)\b", line)
            if h:
                owner = h.group(1)
            if not line.startswith("|"):
                continue
            cells = _cells(line)
            first = cells[0].strip("*` ")
            if TEST_ID.fullmatch(first):
                status = next((s for c in reversed(cells[1:]) for s in STATUSES if c.startswith(s)), "Planned")
                promises = {p for c in cells[1:] for p in re.split(r"[,\s]+", c) if PROMISE.match(p)}
                if first in model.doc_tests and model.doc_tests[first].status != status:
                    model.problems.append(f"{first} has status {model.doc_tests[first].status} in "
                                          f"{model.doc_tests[first].doc} and {status} in {path.name}")
                model.doc_tests.setdefault(first, DocTest(first, status, promises, path.name, " | ".join(cells[1:])[:200]))
            elif RISK_ID.fullmatch(first):
                ints = [int(c) for c in cells[1:9] if re.fullmatch(r"\d{1,2}", c)]
                # A defining row has L, I and score = L x I; an index row (doc 50) has the score first.
                triple = next((ints[k + 2] for k in range(len(ints) - 2) if ints[k] * ints[k + 1] == ints[k + 2]), None)
                score = triple if triple is not None else (ints[0] if ints else None)
                tests = set(TEST_ID.findall(line))
                risk_owner = owner or (Counter(card_of(t) for t in tests).most_common(1)[0][0] if tests else None)
                old = model.risks.get(first)
                if old is not None and old.score is not None and score is not None and old.score != score \
                        and triple is not None and getattr(old, "defining", False):
                    model.problems.append(f"{first} is defined twice with different scores ({old.score} in {old.doc}, "
                                          f"{score} in {path.name})")
                if old is None or (old.score is None and score is not None):
                    model.risks[first] = Risk(first, score, risk_owner, tests, path.name, triple is not None)
                else:
                    old.tests |= tests


# ---- collected tests and the map ------------------------------------------------------------------------------


def collect(out: Path = COLLECTED) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "pytest", "tests", "--collect-only", "-q", "--trace-out", str(out),
                    "-p", "no:cacheprovider"], cwd=REPO, capture_output=True, text=True, timeout=600)
    if not out.exists():
        raise SystemExit("collection did not write its output; run pytest --collect-only to see why")
    return out


def load_map(model: Model, path: Path = MAP, text: str | None = None) -> dict[str, Any]:
    doc = tomllib.loads(text if text is not None else path.read_text(encoding="utf-8"))
    for f in doc.get("file", []):
        model.file_cards[f["path"]] = f["card"]
    model.status.update(doc.get("status", {}))
    status_file = DOCS / "status.toml"
    if text is None and status_file.exists():
        model.status.update(tomllib.loads(status_file.read_text(encoding="utf-8")).get("cards", {}))
    model.waivers = list(doc.get("waiver", []))
    model.manual = {m["id"]: m.get("record", "") for m in doc.get("manual", [])}
    for card, state in model.status.items():
        if state not in DONE_STATES:
            model.problems.append(f"status of {card} is {state!r}; expected one of {DONE_STATES}")
    return doc


def resolve(model: Model, mapping: dict[str, Any]) -> None:
    nodes = [c["node"] for c in model.collected]
    for row in model.collected:  # markers, then docstring headers
        ids: set[str] = set()
        risks: set[str] = set()
        promises: set[str] = set()
        for m in row.get("markers", []):
            if m["name"] == "trace":
                ids |= {a for a in m["args"] if TEST_ID.fullmatch(a)}
                risks |= set(m["kwargs"].get("risks", []))
                promises |= set(m["kwargs"].get("promises", []))
        head = row.get("doc", "").split(":")[0]
        doc_ids = set(TEST_ID.findall(head)) if TEST_ID.match(head) else set()
        if ids and doc_ids and ids != doc_ids:
            model.problems.append(f"{row['node']}: marker ids {sorted(ids)} differ from docstring ids {sorted(doc_ids)}")
        ids |= doc_ids
        if doc_ids:
            risks |= set(RISK_ID.findall(head))
            promises |= {p for p in head.split() if PROMISE.match(p)}
        _claim(model, row["node"], ids, risks, promises)
    for entry in mapping.get("test", []):
        pattern = entry["node"]
        hits = [n for n in nodes if n == pattern or fnmatch.fnmatchcase(n, pattern)]
        if not hits:
            model.problems.append(f"trace_map: {pattern} matches no collected test (rule 4: renamed or deleted?)")
        for n in hits:
            _claim(model, n, set(entry.get("ids", [])), set(entry.get("risks", [])), set(entry.get("promises", [])))


def _claim(model: Model, node: str, ids: set[str], risks: set[str], promises: set[str]) -> None:
    for i in ids:
        model.claims[i].add(node)
    model.node_ids[node] |= ids
    model.node_risks[node] |= risks
    model.node_promises[node] |= promises


def build(collected: list[dict[str, Any]] | None = None, docs: list[Path] | None = None,
          map_text: str | None = None) -> Model:
    model = Model()
    parse_docs(docs if docs is not None else sorted(DOCS.glob("*.md")), model)
    mapping = load_map(model, text=map_text)
    if collected is None:
        path = COLLECTED if COLLECTED.exists() else collect()
        collected = json.loads(path.read_text(encoding="utf-8"))
    model.collected = [c for c in collected if c.get("selected", True)]
    resolve(model, mapping)
    return model


# ---- per card ------------------------------------------------------------------------------------------------


def card_view(model: Model, card: str) -> dict[str, Any]:
    ids = sorted(i for i in model.doc_tests if card_of(i) == card)
    existing = sorted(i for i in ids if model.claims.get(i))
    claimed_elsewhere = sorted(i for i in model.claims if card_of(i) == card and i not in model.doc_tests)
    manual = sorted(i for i in ids if i in model.manual or model.doc_tests[i].status == "Manual")
    nodes = {n for i in existing + claimed_elsewhere for n in model.claims[i]}
    nodes |= {n for n in model.node_ids if any(n.startswith(p + "::") for p, c in model.file_cards.items() if c == card)}
    risks = sorted((r for r in model.risks.values() if r.owner == card), key=lambda r: -(r.score or 0))
    promise_plan = {p for i in ids for p in model.doc_tests[i].promises}
    promise_have = {p for n in nodes for p in model.node_promises.get(n, set())}
    promise_have |= {p for i in existing for p in model.doc_tests[i].promises}
    risk_rows = []
    for r in risks:
        have = sorted({t for t in r.tests if model.claims.get(t)} |
                      {n for n in nodes if r.id in model.node_risks.get(n, set())})
        risk_rows.append({"id": r.id, "score": r.score, "existing": len(have),
                          "manual": sum(1 for t in r.tests if t in manual)})
    unmapped = sorted(n for n in model.node_ids if not model.node_ids[n]
                      and any(n.startswith(p + "::") for p, c in model.file_cards.items() if c == card))
    return {"card": card, "state": model.status.get(card, "planned"), "ids": len(ids), "existing": existing,
            "planned": len([i for i in ids if i not in existing and i not in manual]), "manual": manual,
            "claimed_without_doc_row": claimed_elsewhere, "promises_planned": sorted(promise_plan),
            "promises_existing": sorted(promise_have), "risks": risk_rows, "tests": sorted(nodes),
            "unmapped_tests": unmapped}


def _waived(model: Model, card: str, rule: str, item: str) -> bool:
    today = date.today().isoformat()
    return any(w.get("card") == card and str(w.get("rule")) == rule and w.get("item") in (item, "*")
               and str(w.get("expires", "9999")) >= today for w in model.waivers)


def check(model: Model) -> list[str]:
    """The done-check (docs/testing/50 section 3.5): failures for every card marked done."""
    out = list(model.problems)
    manifest_cards: Counter[str] = Counter()
    if MANIFEST.exists():
        doc = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
        manifest_cards = Counter(e["card"] for e in doc.get("entry", []))
    for card, state in sorted(model.status.items()):
        if state != "done":
            continue
        v = card_view(model, card)
        if not v["existing"] and not v["claimed_without_doc_row"] and not _waived(model, card, "0", "*"):
            out.append(f"{card}: marked done with planned-only tests ({v['ids']} planned ids, none resolve to a "
                       "collected test)")
        for p in v["promises_planned"]:
            if p not in v["promises_existing"] and not _waived(model, card, "1", p):
                out.append(f"{card}: rule 1: promise {p} has no existing automated test")
        for r in v["risks"]:
            if r["score"] is None:
                continue
            if r["score"] >= 15 and not r["existing"] and not _waived(model, card, "2", r["id"]):
                out.append(f"{card}: rule 2: {r['id']} (score {r['score']}) has no existing automated test")
            elif 8 <= r["score"] < 15 and not r["existing"] and not r["manual"] and not _waived(model, card, "3", r["id"]):
                out.append(f"{card}: rule 3: {r['id']} (score {r['score']}) has neither a test nor a manual record")
        if card.startswith("P") and not manifest_cards.get(card) and not _waived(model, card, "6", "*"):
            out.append(f"{card}: rule 6: no entry points registered in tools/chaos/manifest.toml")
    return out


# ---- seeding the map from doc 10 -------------------------------------------------------------------------------


def seed(model: Model, doc: Path | None = None) -> list[dict[str, Any]]:
    """Candidate [[test]] entries: every doc row marked Existing that names test functions, matched to the
    collected test with that function name (only unique matches are proposed)."""
    by_func: dict[str, list[str]] = defaultdict(list)
    for c in model.collected:
        func = c["node"].split("::")[-1].split("[")[0]
        by_func[func].append(c["node"].split("[")[0])
    out = []
    path = doc or DOCS / "10-phase0-foundations.md"
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = _cells(line) if line.startswith("|") else []
        if not cells or not TEST_ID.fullmatch(cells[0]) or "Existing" not in line:
            continue
        for func in sorted(set(re.findall(r"\b(test_[a-z0-9_]+)\b", line))):
            nodes = sorted(set(by_func.get(func, [])))
            if len(nodes) == 1:
                out.append({"node": nodes[0] + ("*" if any("[" in c["node"] for c in model.collected
                                                          if c["node"].startswith(nodes[0] + "[")) else ""),
                            "ids": [cells[0]]})
    return out


# ---- command line ---------------------------------------------------------------------------------------------


def report_text(model: Model, cards: list[str]) -> str:
    lines = []
    for card in cards:
        v = card_view(model, card)
        lines.append(f"Card {card} [{v['state']}]  ids {v['ids']}: existing {len(v['existing'])}, planned "
                     f"{v['planned']}, manual {len(v['manual'])}; collected tests linked {len(v['tests'])}; "
                     f"unmapped tests in its files {len(v['unmapped_tests'])}")
        lines.append(f"  promises planned {v['promises_planned']} existing {v['promises_existing']}")
        for r in v["risks"][:12]:
            flag = ("FAIL" if (r["score"] or 0) >= 15 and not r["existing"] else
                    "WARN" if 8 <= (r["score"] or 0) < 15 and not (r["existing"] or r["manual"]) else "ok")
            lines.append(f"  {r['id']:10} score {r['score']!s:>4}  existing {r['existing']:3}  manual {r['manual']}  {flag}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python tools/trace.py")
    ap.add_argument("command", choices=("collect", "report", "check", "json", "seed"))
    ap.add_argument("--card", action="append", help="limit report/json to these cards")
    args = ap.parse_args(argv)
    if args.command == "collect":
        print(collect())
        return 0
    model = build()
    cards = args.card or sorted({card_of(i) for i in model.doc_tests} | set(model.status))
    if args.command == "report":
        print(report_text(model, cards))
        for p in model.problems:
            print(f"PROBLEM {p}")
        return 0
    if args.command == "json":
        print(json.dumps({c: card_view(model, c) for c in cards}, indent=1))
        return 0
    if args.command == "seed":
        for e in seed(model):
            print(f'[[test]]\nnode = "{e["node"]}"\nids = {json.dumps(e["ids"])}\n')
        return 0
    failures = check(model)
    for f in failures:
        print(f"FAIL {f}")
    print(f"trace check: {len(failures)} problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
