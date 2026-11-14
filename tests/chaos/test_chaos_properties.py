"""Property tests (Hypothesis; plan card X-01 step 4). Deterministic in a normal run (profile chaos-quick,
derandomized, no example database written); HYPOTHESIS_PROFILE=chaos-full runs many more examples.
A failing example is printed with `print_blob` so it can be replayed with @reproduce_failure."""

import asyncio
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from brain.agent import tools as T
from brain.agent.tools import CONSOLE, ToolRegistry, ToolResult
from brain.egress import EgressPolicy
from brain.transport import auth
from brain.transport import protocol as p
from tools.chaos.cases.registry import reference_problem

pytestmark = pytest.mark.chaos

scalars = st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=40)
json_like = st.recursive(scalars, lambda kids: st.lists(kids, max_size=6) | st.dictionaries(st.text(max_size=8), kids,
                                                                                              max_size=6),
                         max_leaves=60)
deep = st.integers(min_value=0, max_value=3000).map(lambda n: _nest(n))


def _nest(n):
    v = 1
    for _ in range(n):
        v = {"a": v}
    return {"x": v}


@given(st.dictionaries(st.text(max_size=10), json_like, max_size=8) | json_like | deep)
def test_argument_gate_never_raises_and_matches_its_contract(value):
    problem = T._plain_json_problem(value)
    assert problem is None or isinstance(problem, str)
    assert (problem is not None) == reference_problem(value)


@given(st.text(max_size=300))
def test_decode_raises_only_protocol_errors(text):
    try:
        p.decode(text)
    except p.ProtocolError as e:
        assert e.code == "bad_json"


@given(json_like, st.sampled_from([None, p.BODY, p.BRAIN]))
def test_validate_raises_only_protocol_errors_with_v0_codes(value, sender):
    try:
        p.validate(value, sender=sender)
    except p.ProtocolError as e:
        assert e.code in p.ERROR_CODES and len(e.message) <= 500


@given(st.text(max_size=80))
def test_token_parser_accepts_only_64_hex(text):
    try:
        token = auth.parse_token(text)
    except ValueError:
        return
    assert len(token) == auth.TOKEN_BYTES and len(text.strip()) == 64


@given(st.text(max_size=80) | st.integers() | st.none())
def test_egress_entries_are_accepted_or_refused_with_value_error(entry):
    try:
        EgressPolicy.from_hosts([entry])
    except ValueError:
        pass


_REGISTRY = ToolRegistry()


@given(st.sampled_from(sorted(_REGISTRY.catalogue.tools) + ["run_command", "delete_files"]), json_like)
def test_the_registry_never_raises(name, args):
    r = asyncio.run(_REGISTRY.call(name, args, CONSOLE))
    assert isinstance(r, ToolResult) and r.status != "error"
    json.dumps(r.to_json())
