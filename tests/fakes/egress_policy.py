"""Test-only swap of the egress policy (H-07; moved out of brain/egress.py).

`brain.egress.install_guard` is one-shot in production: the policy in force can never be widened. Tests that
need a different policy for a moment use `testing_guard` here. It reaches into the module's private state, which
is why it lives in tests/ and not in the product, and why the lint in tests/test_egress.py refuses any mention
of it under brain/.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from brain import egress


@contextmanager
def testing_guard(policy: egress.EgressPolicy) -> Iterator[None]:
    """Enforce `policy` inside the block, then restore whatever was in force before."""
    previous = egress._policy
    egress._activate(policy)
    try:
        yield
    finally:
        with egress._lock:
            egress._resolved.clear()
            egress._policy = previous


testing_guard.__test__ = False  # type: ignore[attr-defined]  # a helper, not a test, although its name starts with "test"
