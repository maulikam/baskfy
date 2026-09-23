"""OP12 — the backtest days and runs (``04`` §13, ``06`` OP12), by the live code.

``06`` OP12's pure acceptance criteria:

* Tier 1 runs end to end per sleeve (:class:`TestTierOne` over the fixture days; the full
  backfill is the box's run — STATUS);
* "a planted Tier 2 day reproduces its expected R to 0.01" and "Tier 3 over fixture snapshots
  reproduces the paper path's P&L to the rupee" (:class:`TestAPlantedDay` — a day whose
  snapshots are set by hand, so its R is arithmetic on those numbers);
* "the caveat text equals ``07``'s verbatim" (:class:`TestTheCaveats`);
* "no card mixes tiers or sleeves" (:class:`TestOneSleeveOneTier`);
* the ±25 % sensitivity per threshold (:class:`TestSensitivity`).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from options_scan_fixtures import (
    GAP_HOLD,
    MASTER,
    O2_UP_BREAK,
    QUIET_MONTHLY,
    TREND_WEEKLY,
    at,
    carry_forward,
    flat,
    gap_hold_bars,
    market,
    o2_up_break_bars,
    quiet_monthly_bars,
    snapshot,
    trend_weekly_bars,
)

from baskfy_core.options.backtest import TIER1_CAVEAT, TIER2_CAVEAT, TIER2_O2_CAVEAT, Tier
from baskfy_core.options.backtest_suite import THRESHOLDS, run_tier, tier1_sensitivity
from baskfy_core.options.bars import Bar, closed
from baskfy_core.options.chain import Level, OptionQuote
from baskfy_core.options.config import OptionsCeilings, OptionsConfig, Side, Sleeve
from baskfy_core.options.costs import CostFill, charges
from baskfy_core.options.replay_day import ModelChain, StoredChain, priced_day, tier1_day
from baskfy_core.options.scan import DayContext, Snapshot

CFG = OptionsConfig()
CEIL = OptionsCeilings()
CTX = DayContext()
DOCS = Path(__file__).resolve().parents[4] / "docs"
QUIET = market(QUIET_MONTHLY, quiet_monthly_bars())
TREND = market(TREND_WEEKLY, trend_weekly_bars())
GAP = market(GAP_HOLD, gap_hold_bars())
UP = market(O2_UP_BREAK, o2_up_break_bars())
ALL = (QUIET, TREND, GAP, UP)


def chain(bars: tuple[Bar, ...], minute: dt.datetime) -> Snapshot:
    """The collector's minute at a 14 % flat vol (the OP8 fixture)."""
    spot = closed(bars, minute)[-1].close
    expiries = sorted({c.expiry for c in MASTER if c.expiry >= minute.date()})[:2]
    forwards = {e: carry_forward(spot, minute, e) for e in expiries}
    return snapshot(minute, spot, expiries, flat(0.14), forwards=forwards)


class TestTierOne:
    def test_each_fixture_day_by_the_sleeves_own_gate(self) -> None:
        quiet = tier1_day(Sleeve.O1M, QUIET, CTX, CFG)
        assert quiet is not None and quiet.traded and quiet.trigger_time == dt.time(10, 0)
        trend = tier1_day(Sleeve.O1W, TREND, CTX, CFG)
        assert trend is not None and (trend.traded, trend.skip_reason) == (False, "NOT_CONTAINED")
        gap = tier1_day(Sleeve.O3B, GAP, CTX, CFG)
        assert gap is not None and gap.traded and gap.trigger_time == dt.time(9, 45)

    def test_o2_and_o3a_record_the_index_move_after_the_trigger(self) -> None:
        o2 = tier1_day(Sleeve.O2, UP, CTX, CFG)
        assert o2 is not None and o2.traded and o2.trigger_time == dt.time(10, 4)
        assert o2.mfe_points is not None and o2.mfe_points > 0 and o2.mae_points == 0
        assert o2.net_pnl_inr is None and o2.r_multiple is None  # Tier 1 has no P&L
        o3a = tier1_day(Sleeve.O3A, TREND, CTX, CFG)
        assert o3a is not None and o3a.trigger_time == dt.time(10, 19)

    def test_a_day_that_is_not_the_sleeves_is_not_a_session(self) -> None:
        assert tier1_day(Sleeve.O1M, TREND, CTX, CFG) is None  # a weekly, not the monthly
        assert tier1_day(Sleeve.O3A, UP, CTX, CFG) is None  # a Monday, not an expiry

    def test_an_event_day_is_a_skipped_session(self) -> None:
        ctx = DayContext(event_days=frozenset({GAP_HOLD}))
        outcome = tier1_day(Sleeve.O3B, GAP, ctx, CFG)
        assert outcome is not None and outcome.skip_reason == "EVENT_DAY"

    def test_a_tier_one_run_per_sleeve(self) -> None:
        for sleeve in Sleeve:
            result = run_tier(sleeve, Tier.SIGNALS, ALL, context=CTX, options=CFG, ceilings=CEIL)
            assert result.sleeve is sleeve and result.tier is Tier.SIGNALS
            assert result.net_pnl_inr is None and result.caveats == (TIER1_CAVEAT,)


class TestTierTwo:
    def test_the_model_chain_trades_the_trend_day_to_target(self) -> None:
        outcome = priced_day(Sleeve.O3A, TREND, CTX, ModelChain(TREND, CFG), options=CFG,
                             ceilings=CEIL)  # fmt: skip
        assert outcome is not None and outcome.traded and outcome.closed_reason == "TARGET"
        assert outcome.r_multiple is not None and outcome.r_multiple > 0

    def test_a_tier_two_run_carries_its_caveat_and_o2s_extra_sentence(self) -> None:
        o2 = run_tier(Sleeve.O2, Tier.MODELLED, [UP], context=CTX, options=CFG, ceilings=CEIL,
                      source_for=lambda m: ModelChain(m, CFG))  # fmt: skip
        assert o2.caveats == (TIER2_CAVEAT, TIER2_O2_CAVEAT)
        assert o2.traded == 1 and o2.net_pnl_inr is not None


class TestAPlantedDay:
    """TREND_WEEKLY for O3-A with every chain set by hand: the 10:20 decision chain and the 10:21
    entry chain are the collector's fixture; at 10:30 the spread is planted at a long bid of 95.00
    and a short ask of 10.00 — V = 85.00 ≥ 80 % of the 100-point width — so the rules say TARGET
    at 10:30, and the R is arithmetic on those numbers."""

    def planted(self) -> StoredChain:
        decision, entry, exit_ = (at(TREND_WEEKLY, 10, m) for m in (20, 21, 30))
        at_exit = chain(TREND.bars, exit_)
        planted = {Decimal(25200): (Decimal("95.00"), Decimal("95.20")),
                   Decimal(25300): (Decimal("9.80"), Decimal("10.00"))}  # fmt: skip
        quotes = tuple(
            _set(q, *planted[q.strike]) if _weekly_call(q) and q.strike in planted else q
            for q in at_exit.quotes
        )
        return StoredChain({
            decision: chain(TREND.bars, decision),
            entry: chain(TREND.bars, entry),
            exit_: replace(at_exit, quotes=quotes),
        })  # fmt: skip

    def test_the_planted_day_reproduces_its_r_to_the_paisa(self) -> None:
        source = self.planted()
        outcome = priced_day(Sleeve.O3A, TREND, CTX, source, options=CFG, ceilings=CEIL)
        assert outcome is not None and outcome.traded and outcome.closed_reason == "TARGET"
        # By hand. The plan prices from 10:20: debit = long ask - short bid, R per lot = debit x 65
        # + the ₹500 reserve (04 §5.5). The entry fills at 10:21's resting levels, the exit at the
        # planted 10:30 levels (04 §8.4).
        d, e = source.at(at(TREND_WEEKLY, 10, 20)), source.at(at(TREND_WEEKLY, 10, 21))
        assert d is not None and e is not None
        long_d, short_d = _leg(d, 25200), _leg(d, 25300)
        long_e, short_e = _leg(e, 25200), _leg(e, 25300)
        assert long_d.ask is not None and short_d.bid is not None
        assert long_e.ask is not None and short_e.bid is not None
        r = (long_d.ask - short_d.bid) * 65 + Decimal(500)
        fills = [
            CostFill(Side.BUY, long_e.ask, 65), CostFill(Side.SELL, short_e.bid, 65),
            CostFill(Side.BUY, Decimal("10.00"), 65), CostFill(Side.SELL, Decimal("95.00"), 65),
        ]  # fmt: skip
        gross = (Decimal("95.00") - Decimal("10.00") - (long_e.ask - short_e.bid)) * 65
        net = (gross - charges(fills, CFG.costs).total).quantize(Decimal("0.01"))
        assert outcome.net_pnl_inr == net
        assert outcome.r_multiple == (net / r).quantize(Decimal("0.01"))

    def test_tier_three_over_the_planted_snapshots_is_the_same_rupees(self) -> None:
        """Tier 3 is the paper path over stored minutes: the run's ₹ is the day's ₹."""
        source = self.planted()
        day = priced_day(Sleeve.O3A, TREND, CTX, source, options=CFG, ceilings=CEIL)
        run = run_tier(Sleeve.O3A, Tier.OBSERVED, [TREND], context=CTX, options=CFG,
                       ceilings=CEIL, source_for=lambda _m: source)  # fmt: skip
        assert day is not None and run.net_pnl_inr == day.net_pnl_inr
        assert run.caveats[0] == "Observed sample: 1 sessions."
        assert run.caveats[1].startswith("Insufficient sample: 1 of 20")


def _weekly_call(q: OptionQuote) -> bool:
    return q.expiry == TREND_WEEKLY and q.option_type.value == "CE"


def _leg(snap: Snapshot, strike: int) -> OptionQuote:
    return next(q for q in snap.quotes if q.strike == strike and _weekly_call(q))


def _set(q: OptionQuote, bid: Decimal, ask: Decimal) -> OptionQuote:
    return replace(q, bid=bid, ask=ask, bids=(Level(bid, 1300),) * 3, asks=(Level(ask, 1300),) * 3)


class TestTheCaveats:
    def test_tier_one_and_two_are_07s_words_verbatim(self) -> None:
        options_07 = (DOCS / "options" / "07-data-reality.md").read_text()
        condor_07 = (DOCS / "condor" / "07-data-reality.md").read_text()
        assert f'"{TIER1_CAVEAT}"' in options_07
        assert f'"{TIER2_CAVEAT}"' in condor_07
        o2_rule = " ".join((DOCS / "options" / "04-business-rules.md").read_text().split())
        assert " ".join(TIER2_O2_CAVEAT.split()) in o2_rule


class TestOneSleeveOneTier:
    def test_a_run_is_one_sleeve_and_one_tier_and_skips_other_days(self) -> None:
        result = run_tier(Sleeve.O3B, Tier.SIGNALS, ALL, context=CTX, options=CFG, ceilings=CEIL)
        # Only expiry days are O3's; the Monday fixture is not counted at all.
        assert (result.sleeve, result.tier) == (Sleeve.O3B, Tier.SIGNALS)
        assert result.sessions == 3 and result.traded == 1


class TestSensitivity:
    def test_every_threshold_at_minus_and_plus_25_percent(self) -> None:
        for sleeve in Sleeve:
            rows = tier1_sensitivity(sleeve, ALL, context=CTX, options=CFG)
            assert len(rows) == 2 * len(THRESHOLDS[sleeve])
            assert {row.factor for row in rows} == {Decimal("0.75"), Decimal("1.25")}

    def test_loosening_a_maximum_never_loses_a_signal(self) -> None:
        rows = {(r.field, r.factor): r for r in tier1_sensitivity(Sleeve.O1W, ALL, context=CTX,
                                                                   options=CFG)}  # fmt: skip
        for field in ("gap_max_pct", "range_max_pct", "er_max"):
            assert rows[(field, Decimal("1.25"))].signals >= rows[(field, Decimal("0.75"))].signals
