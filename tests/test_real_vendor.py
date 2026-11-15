"""Card P0-07 T-P0-07-008: ONE tiny real call to the model API, with its own spend cap.

Deselected in every normal run (CONVENTIONS C8). It runs only when ALL of these hold:
  1. it is asked for:  .\\.venv\\Scripts\\python -m pytest tests\\test_real_vendor.py --run-real-vendor
  2. ANTHROPIC_API_KEY is in .env or the environment (otherwise it is skipped, saying so);
  3. its own cap: LIVE_TEST_USD_CAP below (session, daily and hard stop), far under the vendor-console limit.
It swaps the suite's loopback-only egress policy for one that allows only api.anthropic.com:443, for this test only,
and checks that this was the only outside destination (the egress checklist's "one real call" item).
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from brain import egress, obs
from brain.agent.body_tools import register_body_tools
from brain.agent.budget import SpendGuard, load_prices
from brain.agent.loop import Agent, AgentSettings, TurnInput, load_system_prompt
from brain.agent.model import AnthropicModel
from brain.agent.tools import InputSource, ToolRegistry
from brain.body.sim import SimBody
from brain.config import CapsConfig
from brain.egress import EgressPolicy, build_anthropic_client
from fakes import run
from fakes.egress_policy import testing_guard

LIVE_TEST_USD_CAP = 0.02  # doc 10 T-P0-07-008: the most this test may spend
MODEL = "claude-haiku-5-5"


@pytest.mark.real_vendor
def test_one_tiny_haiku_call_is_metered_and_goes_only_to_the_api():
    key = egress.anthropic_api_key()
    if not key:
        pytest.skip("no ANTHROPIC_API_KEY in .env or the environment: the real-vendor test needs one")
    destinations: list[str] = []
    recording = {"on": False}

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if recording["on"] and event == "socket.getaddrinfo":
            destinations.append(f"{args[0]}:{args[1]}")
        elif recording["on"] and event == "socket.connect" and isinstance(args[1], tuple):
            destinations.append(f"connect:{args[1][1]}")

    sys.addaudithook(hook)  # runs after the guard's hook: it sees only what the guard let through

    async def go() -> Any:
        caps = CapsConfig(session_usd=LIVE_TEST_USD_CAP, daily_usd=LIVE_TEST_USD_CAP, hard_stop_usd=LIVE_TEST_USD_CAP,
                          requests_per_hour=5, max_tokens=64)
        guard = SpendGuard(caps, load_prices(), None)
        guard.start()
        model = AnthropicModel(build_anthropic_client(key))
        try:
            listed = await model.list_models()
            assert MODEL in listed, "the configured live model is not offered for this key"
            body = SimBody()
            registry = ToolRegistry()
            register_body_tools(registry, body)
            agent = Agent(registry, model, guard, settings=AgentSettings(MODEL, MODEL, 64), prompt=load_system_prompt())
            events: list[dict[str, Any]] = []
            obs.LOG.reset_limits()
            obs.LOG.add_sink(events.append)
            try:
                async with body:
                    result = await agent.run_turn(TurnInput("Say hello in three words.", InputSource.LOCAL_CONSOLE))
            finally:
                obs.LOG.remove_sink(events.append)
            return result, guard, events
        finally:
            await model.aclose()

    with testing_guard(EgressPolicy.from_hosts(["api.anthropic.com"])):
        recording["on"] = True
        try:
            result, guard, events = run(go())
        finally:
            recording["on"] = False
    assert result.outcome == "ok" and result.reply
    end = [e for e in events if e["type"] == "model.call_end"][0]["f"]
    assert end["ok"] and end["in_tok"] > 0 and end["out_tok"] > 0
    cost = [e for e in events if e["type"] == "cost.update"]
    assert cost and 0 < guard.counters.session <= LIVE_TEST_USD_CAP and cost[-1]["f"]["usd"] > 0
    names = {d.split(":")[0] for d in destinations if not d.startswith("connect:")}
    assert names <= {"api.anthropic.com"} and all(d.endswith(":443") for d in destinations if d.startswith("connect:"))
