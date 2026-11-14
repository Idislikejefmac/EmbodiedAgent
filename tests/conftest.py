"""Test-wide setup: the whole suite runs under the egress guard with the repo's own allowlist, so no
test can reach the network by accident (loopback, which the transport tests use, is always allowed)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so tests can import body_contract

from brain import egress  # noqa: E402
from brain.config import load_config  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def egress_guard():
    egress.install_guard(egress.EgressPolicy.from_config(load_config()))
    yield
