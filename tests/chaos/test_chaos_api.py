"""The plug-in API refuses misuse (hard to misuse is a requirement), and the E2 assert lint works."""

from collections import Counter

import pytest

from tools.chaos import api, lints, runner
from tools.chaos.api import CaseClass, Experiment, failure, hostile, invalid, not_applicable, valid
from tools.chaos.matrix import MISSING, Spec, boundary_values, matrix_cases

pytestmark = pytest.mark.chaos


def gens(**over):
    g = {CaseClass.VALID: lambda g: [valid("v")], CaseClass.INVALID: lambda g: [invalid("i", 1, "N1")],
         CaseClass.HOSTILE: not_applicable("no hostile inputs here"), CaseClass.FAILURE: not_applicable("pure, no I/O")}
    g.update(over)
    return g


def make(**kw):
    args = dict(id="X-01.selftest.api", card="X-01", entries=("tool.speak",),
                hypothesis="a hypothesis long enough to count", blast_radius="pure", generators=gens(),
                check=lambda c, x: None)
    args.update(kw)
    return Experiment(**args)


@pytest.mark.parametrize("kw, why", [
    ({"id": "bad id"}, "must look like"),
    ({"card": "P0-05"}, "prefix"),
    ({"entries": ()}, "entries"),
    ({"entries": "tool.speak"}, "entries"),
    ({"hypothesis": "short"}, "hypothesis"),
    ({"generators": {CaseClass.VALID: lambda g: []}}, "every class"),
    ({"generators": gens(**{CaseClass.INVALID: not_applicable("nothing to refuse here")})}, "negative class"),
    ({"blast_radius": "the internet"}, "BlastRadius"),
])
def test_experiment_refuses_misuse(kw, why):
    with pytest.raises(ValueError, match=why):
        make(**kw)


@pytest.mark.parametrize("build, why", [
    (lambda: invalid("x", 1), "needs at least one"),
    (lambda: invalid("x", 1, "N7"), "at least one"),
    (lambda: hostile("x", 1, "N1"), "at least one"),
    (lambda: failure("x", 1, "N6"), "at least one"),
    (lambda: valid("Bad Name"), "case name"),
    (lambda: not_applicable("short"), "reason"),
])
def test_case_refuses_misuse(build, why):
    with pytest.raises(ValueError, match=why):
        build()


def test_runner_refuses_a_generator_that_yields_the_wrong_class():
    exp = make(generators=gens(**{CaseClass.INVALID: lambda g: [valid("oops")]}))
    with pytest.raises(runner.HarnessError, match="not a invalid Case"):
        runner.generate(exp, 1, "quick")


def test_runner_refuses_an_empty_generator_without_a_reason():
    exp = make(generators=gens(**{CaseClass.INVALID: lambda g: []}))
    with pytest.raises(runner.HarnessError, match="made no cases"):
        runner.generate(exp, 1, "quick")


def test_an_experiment_naming_an_unknown_entry_fails_the_static_stage():
    exp = make(entries=("tool.no_such_tool",))
    problems, _ = runner.static_checks({exp.id: exp})
    assert any("matches no manifest entry" in p for p in problems)


def test_registering_two_experiments_with_one_id_from_different_modules_fails():
    def check(c, x):
        return None

    check.__module__ = "case_module_a"
    deco = api.experiment(id="X-01.selftest.dup", card="X-01", entries=("tool.speak",),
                          hypothesis="a hypothesis long enough to count", blast_radius="pure",
                          valid=lambda g: [valid("v")], invalid=lambda g: [invalid("i", 1, "N1")],
                          hostile=not_applicable("no hostile inputs"), failure=not_applicable("pure function"))
    deco(check)
    try:
        def other(c, x):
            return None
        other.__module__ = "case_module_b"
        with pytest.raises(ValueError, match="registered twice"):
            deco(other)
    finally:
        api._unregister_for_tests("X-01.selftest.dup")


def test_matrix_generates_the_standard_boundaries():
    names = {b.name: b.accept for b in boundary_values(Spec("integer", minimum=1, maximum=10))}
    assert names["minimum"] is True and names["maximum"] is True
    assert names["one_under_minimum"] is False and names["one_over_maximum"] is False
    assert names["nan"] is False and names["missing"] is False and names["null"] is False and names["zero"] is False
    s = {b.name: b for b in boundary_values(Spec("string", min_length=1, max_length=5, required=False))}
    assert s["missing"].value is MISSING and s["missing"].accept is True
    assert s["empty"].accept is False and s["max_length"].accept is True and s["one_over_max_length"].accept is False
    assert len(s["oversized_1mib"].value) == 1_048_576
    cases = matrix_cases("Some Input!", Spec("boolean"), make=lambda b: b.value)
    assert {c.cls for c in cases} == {CaseClass.VALID, CaseClass.INVALID}
    assert all(c.name.startswith("some_input") for c in cases)


def test_e2_lint_matches_its_baseline_and_catches_a_new_assert(tmp_path):
    new, stale = lints.assert_problems()
    assert new == [] and stale == []
    (tmp_path / "m.py").write_text("def check(x):\n    assert x > 0\n    return x\n", encoding="utf-8")
    found = lints.scan([tmp_path])
    assert sum(found.values()) == 1
    new, _ = lints.assert_problems(found + lints.scan())
    assert any("check" in line for line in new)
    _, stale = lints.assert_problems(Counter(), {("brain/x.py", "f"): 1})
    assert stale
