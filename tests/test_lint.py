"""tools/lint.py: E1a (raw types in boundary signatures) and E1b (every gate has its edge tests), each against its
baseline (tests/lint_baseline.toml), and each proved by a planted violation (docs/testing/00 section 4.3)."""

from __future__ import annotations

import textwrap
from collections import Counter
from pathlib import Path

import pytest

from tools import lint

pytestmark = pytest.mark.chaos


def test_e1a_matches_its_baseline() -> None:
    assert lint.e1a_problems() == []


def test_e1a_catches_each_raw_type_in_a_planted_module(tmp_path: Path) -> None:
    src = tmp_path / "planted.py"
    src.write_text(textwrap.dedent('''
        from typing import Any
        from collections.abc import Mapping
        def a(x: Any): ...
        def b(x: dict[str, int]): ...
        def c(x: Mapping[str, int] | None): ...
        def d(x: bytes): ...
        def e(x: list[bytes]): ...
        def f(x: int, y: str): ...
        def _private(x: Any): ...
        class K:
            def m(self, x: object): ...
            @property
            def p(self) -> int: ...
        class Child(K):
            def m(self, x: object): ...
    '''), encoding="utf-8")
    found = lint.scan_e1a([src])
    symbols = {s for (_, s) in found}
    assert symbols == {"a", "b", "c", "d", "e", "K.m"}


def test_a_new_violation_a_stale_entry_and_a_rising_count_all_fail() -> None:
    base = {"meta": {"max_entries": 1}, "entry": [{"check": "E1a", "file": "f.py", "symbol": "g", "count": 1}]}
    assert lint.compare(Counter({("f.py", "g"): 1}), base, "E1a") == []
    assert any("new violation" in p for p in lint.compare(Counter({("f.py", "g"): 1, ("f.py", "h"): 1}), base, "E1a"))
    assert any("counts may only fall" in p for p in lint.compare(Counter({("f.py", "g"): 2}), base, "E1a"))
    assert any("stale" in p for p in lint.compare(Counter(), base, "E1a"))
    assert any("max_entries" in p for p in lint.compare(Counter({("f.py", "g"): 1}), {**base, "meta": {"max_entries": 5}}, "E1a"))


def test_e1b_every_gate_has_its_edge_tests_or_is_in_the_baseline(request: pytest.FixtureRequest) -> None:
    items = request.session.items
    if len(items) < 800:
        pytest.skip("needs the whole suite collected (run `pytest` with no file arguments)")
    markers = []
    for item in items:
        for m in item.iter_markers("gate"):
            if m.args:
                markers.append((str(m.args[0]), m.kwargs.get("limit"), m.kwargs.get("edge")))
    assert lint.e1b_problems(markers) == []


def test_e1b_counts_a_missing_edge_and_a_missing_wrong_type_test() -> None:
    gates = [{"name": "g.one", "limits": ["L"]}]
    assert lint.scan_e1b([], gates)[("tools/chaos/manifest.toml", "g.one")] == 4
    full = [("g.one", "L", e) for e in lint.EDGES] + [("g.one", None, "wrong_type")]
    assert not lint.scan_e1b(full, gates)
