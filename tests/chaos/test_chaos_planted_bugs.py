"""The harness's acceptance (plan card X-01, stage 1; T-X01-005, T-X01-007, T-X01-008): each defect the
P0-05 and P0-06 reviews found, reintroduced in memory only, is caught by the harness, reproduced from
its printed seed and case id, and gone again once the plant is removed."""

import pytest

from tools.chaos import planted, runner

pytestmark = pytest.mark.chaos

DYNAMIC = ["recursion", "unbounded_log", "proxy_bypass", "log_forgery", "handshake_lockout"]
STATIC = {"assert_validation": "E2 brain/planted_validation.py:check_code",
          "unregistered_entry_point": "tool.teleport"}


@pytest.mark.parametrize("name", DYNAMIC)
def test_planted_bug_is_caught_and_replays_from_its_seed(name, tmp_path):
    plant = planted.PLANTS[name]
    caught = runner.run("quick", only=[plant.caught_by], plant=name, sandbox_parent=tmp_path)
    failures = [r for r in caught.failures if r.case_id.startswith(plant.caught_by + "/")]
    assert failures, f"{name}: the harness did not notice the planted bug"
    first = failures[0]
    assert first.repro == f"python -m brain chaos --seed 1 --only {first.case_id} --plant {name}"
    again = runner.run("quick", seeds=[1], only=[first.case_id], plant=name, sandbox_parent=tmp_path)
    assert [r.status for r in again.results] == [first.status] and again.results[0].violations, name
    clean = runner.run("quick", seeds=[1], only=[first.case_id], sandbox_parent=tmp_path)
    assert [r.status for r in clean.results] == ["pass"], (name, clean.results[0].violations)


@pytest.mark.parametrize("name", sorted(STATIC))
def test_planted_static_weakness_fails_the_static_stage(name):
    with planted.active(name):
        problems, _ = runner.static_checks(runner.load_cases())
    assert any(STATIC[name] in p for p in problems), problems
    problems, _ = runner.static_checks(runner.load_cases())
    assert problems == []


def test_every_plant_restores_what_it_patched():
    from brain.agent import tools
    from brain.transport import protocol, server

    before = (tools._plain_json_problem, tools._show_keys, protocol._validator, server.BrainServer._handle,
              server.FIRST_FRAME_TIMEOUT_S, tools.load_catalogue)
    for name in planted.PLANTS:
        with planted.active(name):
            pass
    assert before == (tools._plain_json_problem, tools._show_keys, protocol._validator, server.BrainServer._handle,
                      server.FIRST_FRAME_TIMEOUT_S, tools.load_catalogue)
