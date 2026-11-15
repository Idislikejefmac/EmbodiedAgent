"""Card P0-07 step 9: the interim spend guard (brain/agent/budget.py; docs/cost-estimate.md, DEC-055, DEC-068).

Caps, the reservation before each request, restart persistence through data/state.json, the 80 percent warning,
the 100 percent stop, the hour and day windows, the price table, and a scripted runaway stopped at the cap.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from brain import obs
from brain.agent.body_tools import register_body_tools
from brain.agent.budget import (PRICES_PATH, Price, PriceError, Refusal, Reservation, SpendGuard, load_prices,
                                parse_prices, usage_cost, worst_case_cost)
from brain.agent.loop import Agent, AgentSettings, TurnInput, estimate_input_tokens, load_system_prompt
from brain.agent.model import Usage
from brain.agent.tools import InputSource, ToolRegistry
from brain.body.sim import SimBody
from brain.config import CapsConfig
from brain.limits import BUDGET_WARN_FRACTION, MAX_TURN_TOOL_CALLS, PRICE_TABLE_MAX_AGE_DAYS, PRICE_TABLE_MAX_BYTES
from brain.obs.state import StateStore
from fakes import run
from fakes.models import FakeClock, FakeModel, HostileModel, text_reply, tool_reply

pytestmark = pytest.mark.trace("T-P0-07-023", "T-P0-07-029", risks=("R-FND-37",))

HAIKU = "claude-haiku-5-5"
PRICES = load_prices()


def guard(caps: CapsConfig | None = None, *, store: StateStore | None = None, clock: FakeClock | None = None,
          persist: bool = True) -> tuple[SpendGuard, FakeClock]:
    clock = clock or FakeClock()
    g = SpendGuard(caps or CapsConfig(), PRICES, store, persist=persist, now=clock.now, today=clock.today)
    g.start()
    return g, clock


class Events:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def __enter__(self) -> Events:
        obs.LOG.reset_limits()
        obs.LOG.add_sink(self.items.append)
        return self

    def __exit__(self, *exc: object) -> None:
        obs.LOG.remove_sink(self.items.append)

    def of(self, t: str) -> list[dict]:
        return [e for e in self.items if e["type"] == t]


# ---- prices ---------------------------------------------------------------------------------------------------------


def test_the_price_table_ships_with_the_code_and_prices_the_configured_models():
    cfg = CapsConfig()
    assert PRICES.models[HAIKU] == Price(0.10, 0.50, 0.01, 0.125, 100_000, 5.0)
    assert {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"} <= set(PRICES.models)
    assert PRICES.verified_on <= date.today() and cfg.max_tokens == 1024


def test_usage_cost_counts_input_output_and_both_cache_kinds():
    p = PRICES.models[HAIKU]
    u = Usage(input_tok=1_000_000, output_tok=1_000_000, cache_read_tok=1_000_000, cache_write_tok=1_000_000)
    assert usage_cost(p, u) == pytest.approx(5 * (0.10 + 0.50 + 0.01 + 0.125))  # over 100k: the 5x long-prompt rate
    small = Usage(input_tok=1000, output_tok=100, cache_read_tok=4000, cache_write_tok=0)
    assert usage_cost(p, small) == pytest.approx((1000 * 0.10 + 100 * 0.50 + 4000 * 0.01) / 1e6)


def test_the_worst_case_uses_the_dearer_input_rate_and_max_tokens():
    p = PRICES.models["claude-sonnet-5-5"]
    assert worst_case_cost(p, 1000, 1024) == pytest.approx((1000 * 2.5 + 1024 * 10.0) / 1e6)
    haiku = PRICES.models[HAIKU]
    assert worst_case_cost(haiku, 100_001, 0) == pytest.approx(5 * 100_001 * 0.125 / 1e6)


@pytest.mark.parametrize("bad", [
    "not json", "[]", '{"models": {}}', '{"verified_on": "2026-11-13", "models": {"m": {"input": -1, "output": 1, '
    '"cache_read": 1, "cache_write": 1}}}', '{"verified_on": "yesterday", "models": {"m": {}}}',
    '{"verified_on": "2026-11-13", "models": {"m": {"input": 1, "output": 1, "cache_read": 1, "cache_write": 1, '
    '"surprise": 2}}}', '{"verified_on": "2026-11-13", "models": {"m": {"input": 1, "output": 1, "cache_read": 1, '
    '"cache_write": 1, "long_context_multiplier": 0.5}}}', '{"verified_on": "2026-11-13", "models": {"m": {"input": '
    'NaN, "output": 1, "cache_read": 1, "cache_write": 1}}}',
])
def test_a_bad_price_table_is_refused(bad):
    with pytest.raises(PriceError):
        parse_prices(bad)


def test_a_price_table_over_the_size_limit_is_refused_before_parsing(tmp_path):
    p = tmp_path / "prices.json"
    p.write_text(" " * (PRICE_TABLE_MAX_BYTES + 1), encoding="utf-8")
    with pytest.raises(PriceError, match="larger"):
        load_prices(p)
    with pytest.raises(PriceError, match="missing"):
        load_prices(tmp_path / "none.json")


@pytest.mark.parametrize("age, stale", [(PRICE_TABLE_MAX_AGE_DAYS - 1, False), (PRICE_TABLE_MAX_AGE_DAYS, False),
                                        (PRICE_TABLE_MAX_AGE_DAYS + 1, True)])
def test_an_old_price_table_gives_a_warning(age, stale, caplog):
    clock = FakeClock(day=date.fromordinal(PRICES.verified_on.toordinal() + age))
    with Events() as ev:
        guard(clock=clock)
    assert PRICES.stale(clock.today()) is stale
    ready = ev.of("budget.check")[0]
    assert ready["f"]["price_stale"] is stale and (ready["level"] == "WARNING") is stale
    assert ("verified" in caplog.text) is stale


# ---- caps, reservation, warning and stop ---------------------------------------------------------------------------


def test_a_request_is_reserved_at_its_worst_case_and_settled_at_its_real_cost():
    g, _ = guard()
    r = g.reserve(HAIKU, 2000, 1024)
    assert isinstance(r, Reservation) and r.usd == pytest.approx(worst_case_cost(PRICES.models[HAIKU], 2000, 1024))
    assert g.outstanding() == r.usd and g.counters.requests_this_hour == 1
    cost, warn = g.settle(r, Usage(input_tok=2000, output_tok=50))
    assert cost == pytest.approx((2000 * 0.10 + 50 * 0.50) / 1e6) and warn is None
    assert g.outstanding() == 0 and g.counters.session == g.counters.daily == g.counters.hard_stop == cost


def test_reservations_in_flight_count_against_the_cap_so_concurrent_calls_cannot_overshoot():
    caps = CapsConfig(session_usd=0.0015)
    g, _ = guard(caps)
    first = g.reserve(HAIKU, 1000, 1024)  # worst case about 0.00064
    second = g.reserve(HAIKU, 1000, 1024)
    third = g.reserve(HAIKU, 1000, 1024)  # 3 x 0.00064 > 0.0015: refused while two are in flight
    assert isinstance(first, Reservation) and isinstance(second, Reservation)
    assert isinstance(third, Refusal) and third.cap == "session" and g.stopped_by is None  # not reached: in flight
    g.settle(first, Usage(input_tok=10, output_tok=1))
    assert isinstance(g.reserve(HAIKU, 1000, 1024), Reservation)  # room again once the first settled cheaply


def test_eighty_percent_warns_once_and_one_hundred_percent_stops_for_good():
    caps = CapsConfig(session_usd=0.001, daily_usd=5.0, hard_stop_usd=10.0)
    g, _ = guard(caps)
    with Events() as ev:
        r = g.reserve(HAIKU, 100, 10)
        _, warn = g.settle(r, Usage(input_tok=8_100, output_tok=0))  # 0.00081 = 81 percent of the session cap
        r = g.reserve(HAIKU, 10, 10)
        _, again = g.settle(r, Usage(input_tok=10))
        r = g.reserve(HAIKU, 10, 10)
        g.settle(r, Usage(input_tok=2_000))  # past 100 percent
        refusals = [g.reserve(HAIKU, 10, 10) for _ in range(3)]
    assert warn is not None and again is None
    warns = [e for e in ev.of("budget.check") if e["f"].get("warn")]
    assert len(warns) == 1 and warns[0]["level"] == "WARNING" and warns[0]["f"]["cap"] == "session"
    assert all(isinstance(x, Refusal) and x.cap == "session" for x in refusals)
    assert len(ev.of("budget.stop")) == 3 and g.stopped_by == "session"


@pytest.mark.parametrize("fraction, warns", [(BUDGET_WARN_FRACTION - 0.01, False), (BUDGET_WARN_FRACTION, True),
                                             (BUDGET_WARN_FRACTION + 0.01, True)])
def test_the_warning_threshold_edges(fraction, warns):
    g, _ = guard(CapsConfig(session_usd=1.0))
    r = g.reserve(HAIKU, 10, 10)
    _, warn = g.settle(r, Usage(output_tok=round(fraction * 2e6)))  # 0.50 USD per million output tokens
    assert (warn is not None) is warns


def test_a_zero_cap_refuses_everything_and_an_unpriced_model_is_refused():
    g, _ = guard(CapsConfig(session_usd=0.0))
    assert isinstance(g.reserve(HAIKU, 10, 10), Refusal)
    g2, _ = guard()
    refusal = g2.reserve("claude-unknown-9", 10, 10)
    assert isinstance(refusal, Refusal) and refusal.cap == "unknown_price"


def test_requests_per_hour_is_a_window_that_rolls():
    g, clock = guard(CapsConfig(requests_per_hour=3))
    held = [g.reserve(HAIKU, 10, 10) for _ in range(3)]
    assert all(isinstance(h, Reservation) for h in held)
    for h in held:
        g.settle(h, Usage(input_tok=1))  # type: ignore[arg-type]
    over = g.reserve(HAIKU, 10, 10)
    assert isinstance(over, Refusal) and over.cap == "requests_per_hour"
    clock.advance(3599)
    assert isinstance(g.reserve(HAIKU, 10, 10), Refusal)
    clock.advance(2)
    assert isinstance(g.reserve(HAIKU, 10, 10), Reservation)


def test_the_daily_cap_resets_with_the_day_but_the_hard_stop_does_not():
    g, clock = guard(CapsConfig(session_usd=100, daily_usd=0.0001, hard_stop_usd=0.00025))
    r = g.reserve(HAIKU, 10, 10)
    g.settle(r, Usage(input_tok=1_000))  # 0.0001: the daily cap is reached
    assert isinstance(g.reserve(HAIKU, 10, 10), Refusal) and g.stopped_by == "daily"
    clock.advance(days=1)
    r = g.reserve(HAIKU, 10, 10)
    assert isinstance(r, Reservation)
    g.settle(r, Usage(input_tok=1_000))
    clock.advance(days=1)
    r = g.reserve(HAIKU, 10, 10)
    g.settle(r, Usage(input_tok=600))  # total 0.00026: past the hard stop
    clock.advance(days=5)
    stop = g.reserve(HAIKU, 10, 10)
    assert isinstance(stop, Refusal) and stop.cap == "hard_stop"


def test_the_counters_persist_across_a_restart(tmp_path: Path):
    store = StateStore(tmp_path)
    store.load()
    g, clock = guard(CapsConfig(hard_stop_usd=0.0005), store=store)
    r = g.reserve(HAIKU, 10, 10)
    g.settle(r, Usage(input_tok=3_000))  # 0.0003
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["spend_usd_total"] == pytest.approx(0.0003) and state["requests_this_hour"] == 1
    store2 = StateStore(tmp_path)
    store2.load()
    g2, _ = guard(CapsConfig(hard_stop_usd=0.0005), store=store2, clock=clock)
    assert g2.counters.hard_stop == pytest.approx(0.0003) and g2.counters.daily == pytest.approx(0.0003)
    assert g2.counters.session == 0 and g2.counters.requests_this_hour == 1
    r2 = g2.reserve(HAIKU, 10, 10)
    g2.settle(r2, Usage(input_tok=3_000))  # 0.0006 since the state file began: past the hard stop
    store3 = StateStore(tmp_path)
    store3.load()
    g3, _ = guard(CapsConfig(hard_stop_usd=0.0005), store=store3, clock=clock)
    assert isinstance(g3.reserve(HAIKU, 10, 10), Refusal)  # still refused after the restart


def test_scripted_spend_is_not_persisted(tmp_path: Path):
    store = StateStore(tmp_path)
    store.load()
    g, _ = guard(store=store, persist=False)
    g.settle(g.reserve(HAIKU, 10, 10), Usage(input_tok=10_000))  # type: ignore[arg-type]
    assert not (tmp_path / "state.json").exists()


# ---- a scripted runaway is stopped at the cap (plan step 9 check) -----------------------------------------------------


def test_a_runaway_tool_loop_is_stopped_at_the_session_cap():
    """A model that asks for a tool forever, with heavy usage on every call, turn after turn: the per-turn loop
    breaker ends each turn, and the session cap ends the run (and keeps refusing); spend never passes the cap."""
    async def go():
        caps = CapsConfig(session_usd=0.01, daily_usd=5.0, hard_stop_usd=10.0)
        clock = FakeClock()
        g = SpendGuard(caps, PRICES, None, now=clock.now, today=clock.today)
        g.start()
        # every call bills as much as it can: all the input it sent and the full max_tokens of output
        def heavy(req: Any) -> Any:
            return tool_reply(("get_body_status", {}), usage=Usage(input_tok=estimate_input_tokens(req),
                                                                     output_tok=req.max_tokens))

        per_call = usage_cost(PRICES.models[HAIKU], Usage(input_tok=1500, output_tok=1024))
        model = HostileModel("loop")
        model.default = heavy
        body = SimBody()
        registry = ToolRegistry()
        register_body_tools(registry, body)
        agent = Agent(registry, model, g, settings=AgentSettings(), prompt=load_system_prompt())
        outcomes = []
        with Events() as ev:
            async with body:
                for _ in range(12):
                    obs.LOG.reset_limits()
                    outcomes.append((await agent.run_turn(TurnInput("status", InputSource.BODY))).outcome)
        first_stop = outcomes.index("budget")
        assert outcomes[0] == "loop_limit" and set(outcomes[:first_stop]) == {"loop_limit"}
        assert set(outcomes[first_stop:]) == {"budget"} and len(outcomes) - first_stop >= 3  # keeps refusing
        assert g.counters.session <= caps.session_usd and g.stopped_by == "session"
        assert len(model.requests) <= caps.session_usd / per_call + 1
        assert ev.of("budget.stop") and all(e["f"]["cap"] == "session" for e in ev.of("budget.stop"))
        assert len(model.requests) <= (first_stop + 1) * (MAX_TURN_TOOL_CALLS + 1)  # the loop breaker bounds each turn

    run(go())

def test_a_flood_of_cheap_requests_is_stopped_by_the_hourly_cap():
    async def go():
        clock = FakeClock()
        g = SpendGuard(CapsConfig(requests_per_hour=20), PRICES, None, now=clock.now, today=clock.today)
        g.start()
        model = FakeModel([], default=text_reply("ok", usage=Usage(input_tok=10, output_tok=1)))
        body = SimBody()
        registry = ToolRegistry()
        register_body_tools(registry, body)
        agent = Agent(registry, model, g, settings=AgentSettings(), prompt=load_system_prompt())
        async with body:
            results = [await agent.run_turn(TurnInput(f"n{i}", InputSource.LOCAL_CONSOLE)) for i in range(30)]
        assert len(model.requests) == 20 and [r.outcome for r in results[20:]] == ["budget"] * 10
        assert results[-1].code == "requests_per_hour"

    run(go())
