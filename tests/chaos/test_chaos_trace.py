"""tools/trace.py (docs/testing/50 section 3.3 'Its own tests'): golden documents, a planted done card
with planned-only tests, a planted duplicate risk, a mapped test that no longer exists, waivers."""

import pytest

from tools import trace

pytestmark = pytest.mark.chaos

DOC = """## 6. Card P0-05: protocol
| ID | Test | Class | Status |
| --- | --- | --- | --- |
| T-P0-05-001 | `test_a` | U | Existing |
| T-P0-05-002 | something | U | Planned |
| ID | Risk | Cat | L | I | Score | Mitigation | Tests |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R-FND-90 | a big risk | Sec | 4 | 4 | 16 | x | T-P0-05-002 |
| R-FND-91 | a small risk | Sec | 2 | 2 | 4 | x | T-P0-05-001 |
## 7. Card P0-09: nothing built
| T-P0-09-001 | planned | U | Planned |
"""
COLLECTED = [{"node": "tests/test_x.py::test_a", "markers": [], "doc": ""},
             {"node": "tests/test_x.py::test_b", "markers": [{"name": "trace", "args": ["T-P0-05-002"],
                                                              "kwargs": {"risks": ["R-FND-90"]}}], "doc": ""},
             {"node": "tests/test_x.py::test_c", "markers": [], "doc": "T-P0-05-001 P3 R-FND-91: header form"}]
MAP = """schema = 1
[status]
"P0-05" = "done"
"P0-09" = "done"
[[test]]
node = "tests/test_x.py::test_a"
ids = ["T-P0-05-001"]
"""


@pytest.fixture
def doc(tmp_path):
    p = tmp_path / "10-golden.md"
    p.write_text(DOC, encoding="utf-8")
    return p


def test_ids_resolve_through_map_marker_and_docstring(doc):
    m = trace.build(COLLECTED, [doc], MAP)
    assert m.claims["T-P0-05-001"] == {"tests/test_x.py::test_a", "tests/test_x.py::test_c"}
    assert m.claims["T-P0-05-002"] == {"tests/test_x.py::test_b"}
    v = trace.card_view(m, "P0-05")
    assert v["existing"] == ["T-P0-05-001", "T-P0-05-002"]
    assert [r["id"] for r in v["risks"]] == ["R-FND-90", "R-FND-91"] and v["risks"][0]["score"] == 16


def test_a_done_card_with_planned_only_tests_is_refused(doc):
    failures = trace.check(trace.build(COLLECTED, [doc], MAP))
    assert any(f.startswith("P0-09: marked done with planned-only tests") for f in failures), failures
    assert not any(f.startswith("P0-05: marked done") for f in failures)


def test_a_high_risk_without_an_existing_test_fails_rule_2(doc):
    collected = [c for c in COLLECTED if not c["node"].endswith("test_b")]
    failures = trace.check(trace.build(collected, [doc], MAP))
    assert any("rule 2: R-FND-90" in f for f in failures), failures


def test_a_waiver_lifts_a_rule_until_it_expires(doc):
    waived = MAP + '[[waiver]]\ncard = "P0-09"\nrule = "0"\nitem = "*"\nreason = "x"\nowner = "Jeff"\nexpires = "2999-01-01"\n'
    assert not any(f.startswith("P0-09: marked done") for f in trace.check(trace.build(COLLECTED, [doc], waived)))
    expired = waived.replace("2999-01-01", "2000-01-01")
    assert any(f.startswith("P0-09: marked done") for f in trace.check(trace.build(COLLECTED, [doc], expired)))


def test_a_mapped_test_that_no_longer_exists_fails(doc):
    gone = MAP + '[[test]]\nnode = "tests/test_x.py::test_renamed"\nids = ["T-P0-05-002"]\n'
    assert any("matches no collected test" in f for f in trace.check(trace.build(COLLECTED, [doc], gone)))


def test_a_risk_defined_twice_with_different_scores_is_flagged(doc, tmp_path):
    other = tmp_path / "20-other.md"
    other.write_text("| R-FND-90 | the same risk | Sec | 3 | 3 | 9 | x | |\n", encoding="utf-8")
    m = trace.build(COLLECTED, [doc, other], MAP)
    assert any("R-FND-90 is defined twice" in p for p in m.problems)


def test_card_ids_of_every_scope():
    assert [trace.card_of(i) for i in ("T-P0-05-071", "T-X01-005", "T-AND-003", "T-GATE11-004", "T-RB02-031",
                                       "T-SYS-010")] == ["P0-05", "X-01", "AND", "G11", "R-02", "SYS"]


def test_the_real_map_parses_and_links_existing_tests():
    import tomllib

    doc = tomllib.loads(trace.MAP.read_text(encoding="utf-8"))
    assert len(doc["test"]) >= 40 and doc["status"]["X-01"] == "in_progress"
