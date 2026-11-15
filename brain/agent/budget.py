"""The interim spend guard (card P0-07 step 9; docs/cost-estimate.md; full guard in card P1-04).

Every model request asks `SpendGuard.reserve` first and reports back with `settle`. The guard enforces, in
code (never in the prompt):

- **money caps** from config.toml [caps]: `session_usd` (this run of the brain), `daily_usd` (the local
  calendar day), `hard_stop_usd` (everything recorded in data/state.json: it persists across restarts and
  never resets by itself; raising it is a console edit of config.toml);
- **requests_per_hour** (a fixed one-hour window, persisted);
- `max_tokens` per request (the loop sends [caps] max_tokens on every request), at most MAX_TURN_TOOL_CALLS
  tool calls per turn and MAX_IMAGES_IN_CONTEXT images in context (both in brain/agent/loop.py).

**Reservation before sending (DEC-055).** `reserve` charges the request's worst case (its estimated input
billed at the dearer of the input and cache-write rates, plus `max_tokens` of output, times the long-prompt
multiplier when the estimate is over the model's threshold) against every money cap, counting everything
already reserved and not yet settled, so concurrent or runaway calls cannot overshoot. `settle` replaces the
reservation with the real cost from the API's usage fields (input, output, cache read, cache write).

At BUDGET_WARN_FRACTION (80 percent) of a cap: one warning (a `budget.check` event at WARNING and a spoken
line for the loop to say). At 100 percent: every further request is refused (`budget.stop`, "budget
reached") and keeps being refused; the session cap ends with the run, the daily cap with the day.

Prices: brain/agent/prices.json, with a `verified_on` date; older than PRICE_TABLE_MAX_AGE_DAYS gives a
warning at start. A model that is not in the table is refused (its cost could not be bounded).

Counters persist in data/state.json (brain/obs/state.py, atomic). If that file was corrupt it is quarantined
and the counters start from zero; the vendor-console limit on the key ($25 a month) is the outer wall for
that case. Only one brain command should run at a time: two would each keep their own copy of the counters.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .. import obs
from ..config import CapsConfig
from ..limits import BUDGET_WARN_FRACTION, PRICE_TABLE_MAX_AGE_DAYS, PRICE_TABLE_MAX_BYTES
from ..obs.files import read_capped
from ..obs.state import SPEND_TOTAL_SANITY_MAX, StateStore
from .model import Usage

log = logging.getLogger("brain.agent.budget")

PRICES_PATH = Path(__file__).with_name("prices.json")
PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")
PRICE_SANITY_MAX = 1000.0  # USD per million tokens: no real price is near this
HOUR_S = 3600.0
CAPS = ("session", "daily", "hard_stop")
SPOKEN = {
    "warn": "Heads up: I have used most of my spending allowance.",
    "stop": "I have reached my spending limit, so I can't think about that right now.",
    "rate": "I am being asked too much too fast, so I am pausing for a while.",
}


class PriceError(ValueError):
    """prices.json cannot be used (the message names the problem, never more than that)."""


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write: float
    long_context_tokens: int | None = None
    long_context_multiplier: float = 1.0


@dataclass(frozen=True)
class PriceTable:
    verified_on: date
    models: dict[str, Price]

    def age_days(self, today: date) -> int:
        return (today - self.verified_on).days

    def stale(self, today: date) -> bool:
        return self.age_days(today) > PRICE_TABLE_MAX_AGE_DAYS


def _money(v: object, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= PRICE_SANITY_MAX:
        raise PriceError(f"{what} must be a number from 0 to {PRICE_SANITY_MAX:g}")
    return float(v)


def parse_prices(text: str) -> PriceTable:
    """The price table from its JSON text; PriceError if anything is wrong."""
    try:
        raw = json.loads(text)
    except (ValueError, RecursionError):
        raise PriceError("prices.json is not valid JSON") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("models"), dict) or not raw["models"]:
        raise PriceError("prices.json must hold a non-empty 'models' table")
    try:
        verified = date.fromisoformat(str(raw.get("verified_on")))
    except ValueError:
        raise PriceError("prices.json verified_on must be a date such as 2026-11-13") from None
    models: dict[str, Price] = {}
    for name, entry in raw["models"].items():
        if not isinstance(name, str) or not 1 <= len(name) <= 128 or not isinstance(entry, dict):
            raise PriceError("each model must be a name with a table of prices")
        if set(entry) - set(PRICE_FIELDS) - {"long_context_tokens", "long_context_multiplier"}:
            raise PriceError(f"{name[:40]}: unknown price field")
        rates = [_money(entry.get(f), f"{name[:40]}.{f}") for f in PRICE_FIELDS]
        threshold = entry.get("long_context_tokens")
        if threshold is not None and (isinstance(threshold, bool) or not isinstance(threshold, int) or threshold <= 0):
            raise PriceError(f"{name[:40]}.long_context_tokens must be a positive whole number")
        mult = _money(entry.get("long_context_multiplier", 1.0), f"{name[:40]}.long_context_multiplier")
        if mult < 1.0:
            raise PriceError(f"{name[:40]}.long_context_multiplier must be at least 1")
        models[name] = Price(rates[0], rates[1], rates[2], rates[3], threshold, mult)
    return PriceTable(verified, models)


def load_prices(path: Path | None = None) -> PriceTable:
    """Read and check brain/agent/prices.json (or `path`). PriceError if it is missing, too large or wrong."""
    data, why = read_capped(path or PRICES_PATH, PRICE_TABLE_MAX_BYTES)
    if data is None:
        raise PriceError(f"prices.json cannot be used ({why})")
    try:
        return parse_prices(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise PriceError("prices.json is not UTF-8") from None


def _rate(price: Price, input_like: int) -> float:
    long = price.long_context_tokens is not None and input_like > price.long_context_tokens
    return price.long_context_multiplier if long else 1.0


def usage_cost(price: Price, usage: Usage) -> float:
    """USD for one request's usage (all input-like tokens count toward the long-prompt threshold)."""
    mult = _rate(price, usage.input_tok + usage.cache_read_tok + usage.cache_write_tok)
    return mult * (usage.input_tok * price.input + usage.output_tok * price.output
                   + usage.cache_read_tok * price.cache_read + usage.cache_write_tok * price.cache_write) / 1e6


def worst_case_cost(price: Price, input_tok: int, max_tokens: int) -> float:
    """The most one request could cost: every input token at the dearer of input and cache write, plus
    max_tokens of output, with the long-prompt multiplier if the input is over the threshold."""
    mult = _rate(price, input_tok)
    return mult * (input_tok * max(price.input, price.cache_write) + max_tokens * price.output) / 1e6


@dataclass(frozen=True)
class Reservation:
    rid: int
    model: str
    usd: float  # the worst case held against the caps until settled


@dataclass(frozen=True)
class Refusal:
    """A request the guard will not allow. `cap` names it (session, daily, hard_stop, requests_per_hour,
    unknown_price); `spoken` is a line the loop may say."""

    cap: str
    spoken: str


@dataclass
class _Counters:
    session: float = 0.0
    daily: float = 0.0
    hard_stop: float = 0.0
    day: str = ""
    hour_start: float = 0.0
    requests_this_hour: int = 0


@dataclass
class SpendGuard:
    """See the module docstring. `store` holds the persisted counters (None, or `persist=False`: in memory only,
    for the scripted model, whose spend is not real). `now` is wall time in seconds; `today` the local date."""

    caps: CapsConfig
    prices: PriceTable
    store: StateStore | None = None
    persist: bool = True
    now: Callable[[], float] = time.time
    today: Callable[[], date] = date.today
    counters: _Counters = field(default_factory=_Counters)
    reserved: dict[int, Reservation] = field(default_factory=dict)
    warned: set[str] = field(default_factory=set)
    stopped_by: str | None = None
    _next_id: int = 0

    def start(self) -> None:
        """Load the persisted counters (the session counter starts at zero) and report the price table's age."""
        c = self.counters
        if self.store is not None:
            st = self.store.state
            c.daily, c.hard_stop, c.day = st.spend_usd_today, st.spend_usd_total, st.day
            c.hour_start, c.requests_this_hour = st.hour_start, st.requests_this_hour
        c.session = 0.0
        self._roll()
        self._save()
        today = self.today()
        if self.prices.stale(today):
            log.warning("the price table was verified %d days ago (more than %d): costs may be wrong; re-check "
                        "the pricing page and update brain/agent/prices.json", self.prices.age_days(today),
                        PRICE_TABLE_MAX_AGE_DAYS)
        obs.emit("budget.check", "spend guard ready", cap="all", headroom_usd=round(self.headroom(), 6),
                 price_age_days=self.prices.age_days(today), price_stale=self.prices.stale(today),
                 level="WARNING" if self.prices.stale(today) else None)

    # ---- the caps ---------------------------------------------------------------------------------------

    def cap_usd(self, cap: str) -> float:
        return {"session": self.caps.session_usd, "daily": self.caps.daily_usd,
                "hard_stop": self.caps.hard_stop_usd}[cap]

    def spent(self, cap: str) -> float:
        return float(getattr(self.counters, cap))

    def outstanding(self) -> float:
        return sum(r.usd for r in self.reserved.values())

    def headroom(self) -> float:
        return max(0.0, min(self.cap_usd(c) - self.spent(c) - self.outstanding() for c in CAPS))

    def _roll(self) -> None:
        c = self.counters
        day = self.today().isoformat()
        if c.day != day:
            c.day, c.daily = day, 0.0
            self.warned.discard("daily")
            if self.stopped_by == "daily":
                self.stopped_by = None
        now = self.now()
        if not 0 <= now - c.hour_start < HOUR_S:
            c.hour_start, c.requests_this_hour = now, 0

    def _save(self) -> None:
        if self.store is None or not self.persist:
            return
        c = self.counters
        try:
            self.store.update(spend_usd_session=min(c.session, SPEND_TOTAL_SANITY_MAX),
                              spend_usd_today=min(c.daily, SPEND_TOTAL_SANITY_MAX),
                              spend_usd_total=min(c.hard_stop, SPEND_TOTAL_SANITY_MAX), day=c.day,
                              hour_start=max(0.0, min(c.hour_start, 1e11)), requests_this_hour=c.requests_this_hour)
        except ValueError:
            log.warning("the spend counters could not be recorded in the state file (refused by its checks)")
            return
        self.store.save()

    def _refuse(self, cap: str, spoken: str) -> Refusal:
        obs.emit("budget.stop", f"request refused: the {cap} cap", cap=cap,
                 spent_usd=round(self.spent(cap), 6) if cap in CAPS else None,
                 cap_usd=self.cap_usd(cap) if cap in CAPS else None, requests_this_hour=self.counters.requests_this_hour)
        return Refusal(cap, spoken)

    def reserve(self, model: str, input_tok: int, max_tokens: int) -> Reservation | Refusal:
        """Allow one request (holding its worst-case cost) or refuse it. Never raises."""
        self._roll()
        price = self.prices.models.get(model)
        if price is None:
            return self._refuse("unknown_price", SPOKEN["stop"])
        if self.stopped_by in CAPS:
            return self._refuse(self.stopped_by, SPOKEN["stop"])
        if self.counters.requests_this_hour >= self.caps.requests_per_hour:  # counts requests in flight too
            return self._refuse("requests_per_hour", SPOKEN["rate"])
        worst = worst_case_cost(price, max(0, input_tok), max(0, max_tokens))
        for cap in CAPS:
            if self.spent(cap) + self.outstanding() + worst > self.cap_usd(cap):
                if self.spent(cap) >= self.cap_usd(cap) or self.outstanding() == 0:
                    self.stopped_by = cap  # the cap is reached (or this request alone cannot fit): keep refusing
                return self._refuse(cap, SPOKEN["stop"])
        self._next_id += 1
        r = Reservation(self._next_id, model, worst)
        self.reserved[r.rid] = r
        self.counters.requests_this_hour += 1
        self._save()
        obs.emit("budget.check", "request allowed", cap="all", model=model, worst_usd=round(worst, 6),
                 headroom_usd=round(self.headroom(), 6), requests_this_hour=self.counters.requests_this_hour)
        return r

    def settle(self, reservation: Reservation, usage: Usage | None) -> tuple[float, str | None]:
        """Replace the reservation with the real cost (`usage` None: nothing was billed as far as is known).
        Returns (cost in USD, a spoken warning line the first time a cap passes BUDGET_WARN_FRACTION, or None).
        Never raises."""
        self.reserved.pop(reservation.rid, None)
        price = self.prices.models.get(reservation.model)
        cost = usage_cost(price, usage) if price is not None and usage is not None else 0.0
        c = self.counters
        c.session += cost
        c.daily += cost
        c.hard_stop += cost
        self._save()
        warning = None
        for cap in CAPS:
            limit = self.cap_usd(cap)
            if limit > 0 and self.spent(cap) >= BUDGET_WARN_FRACTION * limit and cap not in self.warned:
                self.warned.add(cap)
                warning = SPOKEN["warn"]
                obs.emit("budget.check", f"the {cap} cap is {BUDGET_WARN_FRACTION:.0%} used", cap=cap, warn=True,
                         spent_usd=round(self.spent(cap), 6), cap_usd=limit, headroom_usd=round(self.headroom(), 6),
                         level="WARNING")
            if self.spent(cap) >= limit:
                self.stopped_by = self.stopped_by or cap
        obs.emit("cost.update", f"spend after a request: {cost:.6f} USD", model=reservation.model,
                 usd=round(cost, 8), session_usd=round(c.session, 6), today_usd=round(c.daily, 6),
                 total_usd=round(c.hard_stop, 6), in_tok=usage.input_tok if usage else 0,
                 out_tok=usage.output_tok if usage else 0, cache_read_tok=usage.cache_read_tok if usage else 0,
                 cache_write_tok=usage.cache_write_tok if usage else 0)
        return cost, warning

    def summary(self) -> str:
        c = self.counters
        return (f"session ${c.session:.4f} of ${self.caps.session_usd:g}; today ${c.daily:.4f} of "
                f"${self.caps.daily_usd:g}; total ${c.hard_stop:.4f} of ${self.caps.hard_stop_usd:g}; "
                f"requests this hour {c.requests_this_hour} of {self.caps.requests_per_hour}")
