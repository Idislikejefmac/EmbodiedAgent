"""The quick chaos run is part of pytest (plan card X-01 step 9), plus the runner's own contract.

T-X01-003 (determinism, replay, reproduction line, results file), T-X01-004 (time budget, no network),
T-X01-015 (no secret in results), T-X01-020 and T-X01-022 (the harness's own inputs).
"""

import json

import pytest

from tools.chaos import runner
from tools.chaos import thresholds as T
from tools.chaos.api import INVARIANTS

pytestmark = pytest.mark.chaos


def test_quick_run_holds_every_invariant(chaos_quick_report):
    r = chaos_quick_report
    problems = [runner.format_failure(f) for f in r.failures] + r.static + r.harness_problems
    assert r.ok, "\n".join(problems)
    verdicts = r.invariants()
    assert set(verdicts) == set(INVARIANTS) and all(v["verdict"] == "held" for v in verdicts.values())


def test_quick_run_covers_every_experiment_and_class(chaos_quick_report):
    ran = {res.case_id.split("/")[0] for res in chaos_quick_report.results}
    assert ran == set(runner.load_cases())
    for exp_id, exp in runner.load_cases().items():
        for cls in ("valid", "invalid", "hostile", "failure"):
            cases = [x for x in chaos_quick_report.results if x.case_id.startswith(f"{exp_id}/{cls}/")]
            assert cases or f"{exp_id}/{cls}" in chaos_quick_report.not_applicable, (exp_id, cls)


def test_quick_run_fits_its_budget(chaos_quick_report):
    """Asserted with a 2x margin (T-X01-004); the measured time is in the results and the summary."""
    assert chaos_quick_report.duration_s < 2 * T.QUICK_SUITE_MAX_S, chaos_quick_report.duration_s


def test_same_seed_same_cases():
    for exp in runner.load_cases().values():
        a, b = runner.generate(exp, 7, "quick"), runner.generate(exp, 7, "quick")
        assert runner.digest(a) == runner.digest(b), exp.id
        assert [i for i, _, _ in a] == [i for i, _, _ in b]


def test_replaying_one_case_by_its_id_gives_the_same_verdict(chaos_quick_report, tmp_path):
    picked = [r for r in chaos_quick_report.results if r.status in ("pass", "xfail")][::97][:4]
    again = runner.run("quick", only=[r.case_id for r in picked], sandbox_parent=tmp_path)
    assert {r.case_id: r.status for r in again.results} == {r.case_id: r.status for r in picked}
    assert all(r.repro == f"python -m brain chaos --seed 1 --only {r.case_id}" for r in again.results)


def test_results_file_has_date_commit_counts_and_no_secret(chaos_quick_report, tmp_path):
    path = runner.write_results(chaos_quick_report, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert {"date", "commit", "mode", "seeds", "counts", "invariants", "case_list_digest"} <= set(data)
    text = path.read_text(encoding="utf-8")
    assert not any(secret.lower() in text.lower() for secret in chaos_quick_report.canaries.values())
    with pytest.raises(runner.HarnessError, match="canary"):  # a planted leak is refused, not written
        runner.write_results(chaos_quick_report, tmp_path, canaries={"x": data["commit"]})


@pytest.mark.parametrize("argv", [["--seed", "x"], ["--seed", "-1"], ["--seed", str(2**64)], ["--quick", "--full"]])
def test_bad_runner_arguments_are_refused_with_usage(argv, capsys):
    with pytest.raises(SystemExit) as e:
        runner.main(argv)
    assert e.value.code == 2 and "usage" in capsys.readouterr().err


def test_the_runner_refuses_to_run_under_a_wider_guard():
    from brain import egress
    from brain.egress import EgressPolicy

    with egress.testing_guard(EgressPolicy.from_hosts(["api.example.org"])):
        with pytest.raises(runner.HarnessError, match="loopback-only"):
            runner.preflight()


@pytest.mark.slow
def test_full_run_holds_every_invariant(tmp_path):
    r = runner.run("full", sandbox_parent=tmp_path)
    assert r.ok, "\n".join(runner.format_failure(f) for f in r.failures[:20]) + "\n".join(r.static)
