"""``docs/fno/04`` §1-§3, §10 as the desk monitor applies them (FO7): the 09:20 re-price of an F1
condor and an F2 future on live quotes, the marks, the evening trail that is never lowered, and
the order of F2's actions on a tick."""

from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal

from baskfy_core.fno.condor import CondorStrikes, LegQuote, LegRole
from baskfy_core.fno.config import DEFAULT_FNO_CEILINGS, DEFAULT_FNO_CONFIG, PlanState
from baskfy_core.fno.exits import ExitReason, stop_from_vol
from baskfy_core.fno.monitor import (
    CondorReprice,
    f1_close_cost,
    f1_pnl_inr,
    f2_action,
    f2_pnl_inr,
    f2_trail_step,
    reprice_condor,
    reprice_future,
)
from baskfy_core.options.config import CostRates, Mode, SizingMode

CFG = DEFAULT_FNO_CONFIG
STRIKES = CondorStrikes(Decimal(26100), Decimal(25800), Decimal(24200), Decimal(23900))
LOT = 65
TWENTY_FIVE_LAKH = Decimal(2500000)


def q(bid: str, ask: str, oi_lots: int = 5000) -> LegQuote:
    return LegQuote(Decimal(bid), Decimal(ask), oi_lots)


def book(**overrides: LegQuote) -> dict[LegRole, LegQuote]:
    base = {
        LegRole.LONG_CALL: q("19.90", "20.10"),
        LegRole.SHORT_CALL: q("59.80", "60.20"),
        LegRole.SHORT_PUT: q("54.80", "55.20"),
        LegRole.LONG_PUT: q("17.90", "18.10"),
    }
    base.update({LegRole(k.upper()): v for k, v in overrides.items()})
    return base


def reprice(quotes: dict[LegRole, LegQuote], capital: Decimal = TWENTY_FIVE_LAKH) -> CondorReprice:
    return reprice_condor(
        strikes=STRIKES, quotes=quotes, lot_size=LOT, capital_inr=capital, mode=Mode.PAPER,
        config=CFG, rates=CostRates(), ceilings=DEFAULT_FNO_CEILINGS,
    )  # fmt: skip


class TestF1Reprice:
    def test_a_clean_plan_prices_on_live_mids_and_sizes_from_the_budget(self) -> None:
        out = reprice(book())
        assert out.state is None, out.reasons
        assert out.credit == Decimal("77.00")  # 60 + 55 - 20 - 18, on mids
        assert out.max_loss_per_unit == Decimal("223.00")  # the 300-point wing less the credit
        assert out.sizing is not None and out.sizing.lots == 1  # ₹25,000 / ₹14,495 per lot
        assert out.cost is not None and out.cost.cost_share_pct is not None
        assert out.cost.cost_share_pct <= CFG.f1.max_cost_share_pct

    def test_a_liquidity_refusal_is_named(self) -> None:
        out = reprice(book(short_call=q("57.00", "63.00", oi_lots=100)))
        assert out.state is PlanState.REJECTED_LIQUIDITY
        joined = " ".join(out.reasons)
        assert "short call 25800" in joined and "OI 100 lots" in joined and "spread" in joined

    def test_a_one_sided_leg_rejects_before_any_price(self) -> None:
        out = reprice(book(long_put=LegQuote(None, Decimal("18"), 5000)))
        assert out.state is PlanState.REJECTED_LIQUIDITY and out.credit is None
        assert any("long put 23900: no two-sided quote" in r for r in out.reasons)

    def test_zero_lots_is_rejected_never_rounded_up(self) -> None:
        out = reprice(book(), capital=Decimal(1000000))  # ₹10,000 < ₹14,495 per lot
        assert out.state is PlanState.REJECTED_SIZE
        assert out.sizing is not None and out.sizing.lots == 0

    def test_a_thin_credit_is_rejected_on_cost(self) -> None:
        thin = book(
            long_call=q("0.95", "1.05"), short_call=q("2.95", "3.05"),
            short_put=q("2.95", "3.05"), long_put=q("0.95", "1.05"),
        )  # fmt: skip
        out = reprice(thin)
        assert out.state is PlanState.REJECTED_COST
        assert any("% of the credit" in r for r in out.reasons)

    def test_a_non_positive_credit_is_a_structure_rejection(self) -> None:
        out = reprice(book(long_call=q("80", "80.2"), long_put=q("80", "80.2")))
        assert out.state is PlanState.REJECTED_STRUCTURE


class TestF1Marks:
    def test_close_cost_needs_every_leg(self) -> None:
        marks = {LegRole.SHORT_CALL: Decimal(30), LegRole.SHORT_PUT: Decimal(20),
                 LegRole.LONG_CALL: Decimal(5)}  # fmt: skip
        assert f1_close_cost(STRIKES, marks) is None
        marks[LegRole.LONG_PUT] = Decimal(4)
        assert f1_close_cost(STRIKES, marks) == Decimal(41)

    def test_pnl_is_credit_less_the_buy_back(self) -> None:
        assert f1_pnl_inr(Decimal(77), Decimal(41), 65) == Decimal("2340.00")
        assert f1_pnl_inr(Decimal(77), Decimal("192.5"), 65) == Decimal("-7507.50")


class TestF2Reprice:
    def test_the_stop_is_three_atr_under_the_ask_and_the_gtt_the_tighter(self) -> None:
        out = reprice_future(
            bid=Decimal("999.50"), ask=Decimal(1000), atr=Decimal(20), ann_vol=0.30, lot_size=500,
            capital_inr=Decimal(0), mode=Mode.PAPER, config=CFG, ceilings=DEFAULT_FNO_CEILINGS,
        )  # fmt: skip
        assert (out.entry, out.stop) == (Decimal(1000), Decimal("940.00"))
        vol_stop = stop_from_vol(Decimal(1000), 0.30, CFG.stop_vol)
        assert out.trigger == max(Decimal(940), vol_stop)
        assert out.sizing is not None and out.sizing.sizing_mode is SizingMode.PAPER_ONE_LOT
        assert out.sizing.lots == 1 and out.sizing.lots_at_ceiling == 0  # ₹30,000 per lot
        assert out.state is None

    def test_no_two_sided_quote_is_named(self) -> None:
        out = reprice_future(
            bid=None, ask=Decimal(1000), atr=Decimal(20), ann_vol=0.3, lot_size=500,
            capital_inr=Decimal(0), mode=Mode.PAPER, config=CFG, ceilings=DEFAULT_FNO_CEILINGS,
        )  # fmt: skip
        assert out.state is PlanState.REJECTED_LIQUIDITY and out.entry is None

    def test_pnl(self) -> None:
        assert f2_pnl_inr(Decimal(1000), Decimal("1012.5"), 500) == Decimal("6250.00")


class TestTheTrail:
    def test_the_stop_and_the_trigger_are_never_lowered(self) -> None:
        rng = random.Random(7)
        stop, trigger, high = Decimal(940), Decimal(940), Decimal(1000)
        for _ in range(500):
            close = Decimal(900 + rng.randint(0, 250))
            step = f2_trail_step(
                stop=stop, trigger=trigger, highest_close=high, close=close, entry=Decimal(1000),
                atr_at_entry=Decimal(20), ann_vol=0.3, config=CFG,
            )  # fmt: skip
            assert step.stop >= stop and step.trigger >= trigger and step.highest_close >= high
            assert step.moved == (step.trigger > trigger)
            stop, trigger, high = step.stop, step.trigger, step.highest_close

    def test_a_new_high_moves_the_stop_to_three_atr_under_it(self) -> None:
        step = f2_trail_step(
            stop=Decimal(940), trigger=Decimal(940), highest_close=Decimal(1000),
            close=Decimal(1100), entry=Decimal(1000), atr_at_entry=Decimal(20), ann_vol=0.3,
            config=CFG,
        )  # fmt: skip
        assert (step.stop, step.trigger, step.moved) == (
            Decimal("1040.00"),
            Decimal("1040.00"),
            True,
        )


class TestF2Action:
    DAY = dt.date(2026, 11, 10)

    def at(self, hh: int, mm: int = 0, day: dt.date | None = None) -> dt.datetime:
        return dt.datetime.combine(day or self.DAY, dt.time(hh, mm))

    def act(
        self,
        now: dt.datetime,
        price: str | None,
        *,
        naked: bool = False,
        time_exit_date: dt.date = dt.date(2026, 12, 1),
    ) -> ExitReason | None:
        out = f2_action(
            now=now, price=None if price is None else Decimal(price), trigger=Decimal(940),
            time_exit_date=time_exit_date, roll_date=dt.date(2026, 11, 23), naked=naked,
            common=CFG.common,
        )  # fmt: skip
        return None if out is None else out.reason

    def test_naked_first(self) -> None:
        assert self.act(self.at(10), "1000", naked=True) is ExitReason.NAKED_FUTURE

    def test_a_passed_roll_date_is_a_late_exit_never_a_late_roll(self) -> None:
        assert self.act(self.at(9, 15, dt.date(2026, 11, 24)), "1000") is ExitReason.LATE_EXIT

    def test_stop_then_time_then_roll(self) -> None:
        assert self.act(self.at(10), "939.95") is ExitReason.STOP
        assert self.act(self.at(10), "1000") is None
        roll_day = dt.date(2026, 11, 23)
        assert self.act(self.at(15, 0, roll_day), "1000") is ExitReason.ROLL
        timed = self.act(self.at(15, 0, roll_day), "1000", time_exit_date=roll_day)
        assert timed is ExitReason.TIME_EXIT

    def test_without_a_price_the_calendar_still_acts(self) -> None:
        assert self.act(self.at(10), None) is None
        assert self.act(self.at(15, 1, dt.date(2026, 11, 23)), None) is ExitReason.ROLL
