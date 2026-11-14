"""C8 (docs/testing/CONVENTIONS.md): the test suite runs under a loopback-only egress policy.

T-P0-06-076, T-P0-06-077 (doc 10 F-21). Before X-01 the suite installed config.toml's allowlist, so a
real call to the model API from a test would have left the machine once a key was in .env.
"""

import asyncio
import socket

import pytest

from brain import egress
from brain.config import load_config
from brain.egress import EgressDenied, EgressPolicy


def test_the_suite_policy_allows_no_outside_destination():
    assert egress.guard_active()
    assert egress._policy is not None and egress._policy.allowed == frozenset()
    assert EgressPolicy.from_config(load_config()).allowed  # config.toml still lists the API; the suite ignores it


@pytest.mark.parametrize("host", ["api.anthropic.com", "api.example.org"])
def test_a_real_call_from_a_test_is_refused_before_any_lookup(host):
    with pytest.raises(EgressDenied):
        socket.getaddrinfo(host, 443)  # refused before the name reaches a DNS server
    with pytest.raises(EgressDenied):
        socket.create_connection((host, 443), timeout=2)

    async def go():
        with pytest.raises(EgressDenied):
            await asyncio.open_connection(host, 443)

    asyncio.run(go())


@pytest.mark.real_vendor
def test_marked_real_vendor_would_fail_if_it_ran():
    """Proof that real_vendor tests are deselected: if this one ever runs, it fails."""
    pytest.fail("a real_vendor test was collected into a normal run")
