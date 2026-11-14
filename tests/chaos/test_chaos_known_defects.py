"""Real defects the harness found in code built before X-01, recorded as STRICT expected failures.

Each test asks that the chaos case for the defect passes. Today it does not, so the test is an xfail;
the day the hardening pass fixes the defect the test passes, strict xfail turns that into a failure,
and the commit that fixes the defect also removes the `known_defect` marker from the case and the
xfail here. (The chaos run reports the same cases as `xfail` in its summary.)
"""

import subprocess
import sys

import pytest

from brain.config import REPO_ROOT
from tools.chaos import lints

pytestmark = pytest.mark.chaos

DEFECTS: list[tuple[str, str, str]] = []  # every defect recorded at X-01 stage 1a is fixed (hardening batch 2)

# Fixed in hardening batch 2: each was a strict expected failure above and is now a permanent regression case.
FIXED = [
    ("P0-05.server.session/failure/", "event_flood",
     "H-09 / T-P0-05-047: no limit on valid frames; 150 events gave 150 INFO lines (PEER_LOG_LINES_PER_MIN_MAX 60)"),
    ("P0-04.config.fail_closed/hostile/", "data_dir_absolute_outside",
     "H-05 (10-F09): [paths] data_dir was not confined (absolute path)"),
    ("P0-04.config.fail_closed/hostile/", "data_dir_traversal",
     "H-05 (10-F09): [paths] data_dir was not confined (..\\ traversal)"),
    ("P0-04.matrix.config_keys/invalid/", "network.port.one_over_maximum",
     "H-05: network.port = 65536 was accepted silently"),
    ("P0-04.matrix.config_keys/invalid/", "network.port.null",
     "H-05: a wrong-typed config value ended python -m brain in a traceback, not a clear refusal"),
    ("P0-04.matrix.migrate/invalid/", "log_n_22_reaches_scrypt",
     "H-01: restore_data handed a header log_n of 22 (about 4 GiB) to scrypt before authenticating the archive"),
    ("P0-06.matrix.body_args/invalid/", "audio_chunk.sample_rate_hz.one_over_maximum",
     "Appendix A.10 item 2 / H-34: AudioChunk had no upper bound on sample_rate_hz"),
]


def _case(report, prefix, name):
    hits = [r for r in report.results if r.case_id.startswith(prefix) and r.name == name]
    assert len(hits) == 1, f"no single case {prefix}{name}"
    return hits[0]


@pytest.mark.parametrize("prefix, name, defect", DEFECTS, ids=[d[1] for d in DEFECTS])
def test_recorded_defect_is_fixed(chaos_quick_report, prefix, name, defect, request):
    case = _case(chaos_quick_report, prefix, name)
    assert case.known_defect, f"{name} is no longer marked as a known defect: update this list"
    request.applymarker(pytest.mark.xfail(strict=True, reason=defect))
    assert case.status == "xpass", f"{defect}: {case.violations[:2]}"


@pytest.mark.parametrize("prefix, name, defect", FIXED, ids=[d[1] for d in FIXED])
def test_fixed_defect_stays_fixed(chaos_quick_report, prefix, name, defect):
    """The regression half: the case exists, carries no known_defect marker, and passes."""
    case = _case(chaos_quick_report, prefix, name)
    assert not case.known_defect, f"{name} is marked as a known defect again"
    assert case.status == "pass", f"regression of {defect}: {case.violations[:2]}"


def test_no_case_is_still_a_recorded_defect(chaos_quick_report):
    """Hardening batch 2 cleared every recorded defect; a new one must be added to DEFECTS with its H-item."""
    unlisted = [(r.case_id, r.name) for r in chaos_quick_report.results if r.known_defect
                and not any(r.case_id.startswith(p) and r.name == n for p, n, _ in DEFECTS)]
    assert unlisted == [], unlisted


def test_no_assert_is_used_for_checks():
    assert sum(lints.scan().values()) == 0, dict(lints.scan())


def test_protocol_error_refuses_unknown_codes_even_under_python_O():
    code = ("from brain.transport.protocol import ProtocolError\n"
            "try:\n    ProtocolError('not_a_code', 'x')\nexcept ValueError:\n    print('REFUSED')\n"
            "else:\n    print('ACCEPTED')\n")
    out = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    assert out.stdout.strip() == "REFUSED", out.stdout + out.stderr
