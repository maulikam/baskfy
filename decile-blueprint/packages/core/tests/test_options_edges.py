"""The boundaries ``docs/options/04`` states and the example tests did not reach.

Written after OP1's first mutation run (``DECISIONS-OP`` OP1.11): each test here asserts a rule of
``04`` at the exact value where an off-by-one, a flipped comparison or a swapped operator would
change the answer — a sell *at* its limit fills, a scratch trade is not a win, a Monday row is in
its week, last month's losses are not this month's, the order of the input never changes the
order of the legs, every result the core hands out is immutable.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import inspect
from decimal import Decimal
from types import ModuleType

import pytest
from options_fixtures import LOT, TICK, quote

from baskfy_core.options import (
    backtest,
    calendar,
    chain,
    costs,
    execution,
    greeks,
    journal,
    risk,
    session,
    sizing,
)
from baskfy_core.options.backtest import DayOutcome, Tier, run
from baskfy_core.options.chain import Level, atm, depth_available, parity_forward, strike_step
from baskfy_core.options.config import (
    DEFAULT_CEILINGS,
    DEFAULT_OPTIONS_CONFIG,
    ExecutionConfig,
    Mode,
    OptionType,
    RiskConfig,
    Side,
    SizingMode,
    Sleeve,
)
from baskfy_core.options.costs import CostFill, CostVerdict, charges, cost_test
from baskfy_core.options.execution import (
    LegRole,
    entry_sequence,
    exit_sequence,
    never_naked,
    next_attempt,
    next_exit_leg,
    simulate_fill,
)
from baskfy_core.options.greeks import black76_price, intrinsic, solve, year_fraction
from baskfy_core.options.journal import JournalRow, r_multiple, summarize_one
from baskfy_core.options.risk import (
    RealisedTrade,
    book_limits,
    evaluate_sleeve,
    last_trading_day_of_week,
    trade_breached,
)
from baskfy_core.options.sizing import (
    MarginCode,
    SizingReject,
    margin_check,
    premium_cap_ok,
    size,
)

CFG = DEFAULT_OPTIONS_CONFIG
CHAIN = CFG.chain
RATE = CHAIN.rate
CE, PE = OptionType.CE, OptionType.PE
LC, LP, SC, SP = LegRole.LONG_CALL, LegRole.LONG_PUT, LegRole.SHORT_CALL, LegRole.SHORT_PUT

MODULES: tuple[ModuleType, ...] = (
    backtest, calendar, chain, costs, execution, greeks, journal, risk, session, sizing,
)  # fmt: skip


def _dataclasses() -> list[type]:
    found: list[type] = []
    for module in MODULES:
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if dataclasses.is_dataclass(cls) and cls.__module__ == module.__name__:
                found.append(cls)
    return found


@pytest.mark.parametrize("cls", _dataclasses(), ids=lambda c: c.__qualname__)
def test_everything_the_core_hands_out_is_frozen(cls: type) -> None:
    """A result that can be mutated after the fact is a result nobody can reconstruct."""
    assert getattr(cls, "__dataclass_params__").frozen  # noqa: B009 - a dunder, read not set


class TestGreeksDomain:
    def test_small_numbers_are_priced_not_treated_as_degenerate(self) -> None:
        """ATM, r = 0: ``F * (2N(sigma * sqrt(T) / 2) - 1)``; sigma 0.2 and 0.5, T = 1."""
        assert black76_price(0.5, 0.5, 1.0, 0.0, 0.2, CE) == pytest.approx(0.0398278, abs=1e-6)
        assert black76_price(0.5, 0.5, 1.0, 0.0, 0.5, CE) == pytest.approx(0.0987064, abs=1e-6)

    @pytest.mark.parametrize(
        ("forward", "strike", "years", "vol", "expected"),
        [
            (25100.0, 25000.0, 0.01, 0.0, 100.0),  # no vol: discounted intrinsic
            (25100.0, 0.0, 0.01, 0.2, 25100.0),  # a zero strike is all intrinsic
            (0.0, 25000.0, 0.01, 0.2, 0.0),  # a zero forward is worth nothing as a call
        ],
    )
    def test_degenerate_inputs_are_the_discounted_intrinsic(
        self, forward: float, strike: float, years: float, vol: float, expected: float
    ) -> None:
        got = black76_price(forward, strike, years, RATE, vol, CE)
        assert got == pytest.approx(expected, rel=1e-3)

    def test_an_otm_call_has_no_intrinsic(self) -> None:
        assert intrinsic(24900, 25000, CE) == 0.0

    def _solve(
        self, mid: float, forward: float, strike: float, kind: OptionType
    ) -> greeks.Greeks | None:
        t = year_fraction(dt.datetime(2026, 10, 20, 10, 0), dt.date(2026, 10, 27), dt.time(15, 30))
        return solve(
            mid, forward, strike, t, RATE, kind,
            min_premium=float(CHAIN.min_premium), tick=float(TICK),
            lower=CHAIN.iv_lower, upper=CHAIN.iv_upper, tolerance=CHAIN.iv_tolerance,
        )  # fmt: skip

    def test_a_mid_exactly_at_min_premium_is_solved(self) -> None:
        """``04`` §2.3 refuses ``mid < min_premium``; the floor itself is admitted."""
        assert self._solve(float(CHAIN.min_premium), 25000.0, 25800.0, CE) is not None

    def test_a_mid_exactly_at_intrinsic_plus_a_tick_is_solved(self) -> None:
        mid = intrinsic(25100.0, 25000.0, CE) + float(TICK)
        assert self._solve(mid, 25100.0, 25000.0, CE) is not None


class TestChainEdges:
    def test_two_strikes_have_a_step(self) -> None:
        assert strike_step([Decimal(25000), Decimal(25050)], Decimal(25000), 15) == 50

    def test_the_window_is_exactly_two_window_plus_one_strikes(self) -> None:
        strikes = [Decimal(k) for k in (0, 10, 110, 210, 310)]
        assert strike_step(strikes, Decimal(5), 1) == 10  # {0, 10, 110}: gaps 10, 100 → finer
        assert strike_step(strikes, Decimal(300), 1) == 100  # {110, 210, 310}

    def test_a_one_point_grid(self) -> None:
        assert atm(Decimal("25000.4"), Decimal(1)) == 25000

    @pytest.mark.parametrize(("bid", "ask"), [("0", "1"), ("1", "0")])
    def test_a_zero_side_has_no_mid(self, bid: str, ask: str) -> None:
        assert quote(25000, CE, bid, ask).mid is None

    def test_a_sub_rupee_quote_has_a_mid(self) -> None:
        assert quote(26500, CE, "0.50", "0.60").mid == Decimal("0.55")

    def test_one_unit_of_depth_counts(self) -> None:
        q = quote(25000, CE, "99", "101", asks=(Level(Decimal(101), 1),))
        assert depth_available(q, Side.BUY, 3) == 1

    def test_the_fallback_takes_the_nearest_pair_first(self) -> None:
        """ATM 25,000 has no put; 25,050 (nearest) and 24,900 disagree, and 25,050 must win."""
        expiry = dt.date(2026, 10, 27)
        quotes = [
            quote(25000, CE, "100", "101", expiry=expiry),
            quote(25050, CE, "80", "81", expiry=expiry),
            quote(25050, PE, "70", "71", expiry=expiry),
            quote(24900, CE, "200", "201", expiry=expiry),
            quote(24900, PE, "50", "51", expiry=expiry),
        ]
        found = parity_forward(quotes, Decimal(25000), Decimal(50), 0.0, RATE)
        assert found == Decimal(25060)


class TestCostEdges:
    BIG = CostFill(Side.BUY, Decimal(100_000), 1_000_000)  # ₹10,000 crore of premium

    def test_the_crore_is_ten_million(self) -> None:
        assert charges([self.BIG], DEFAULT_OPTIONS_CONFIG.costs).sebi == Decimal("100000.00")

    def test_gst_includes_ipft_and_the_total_includes_everything(self) -> None:
        fill = CostFill(Side.BUY, Decimal(1000), 100_000)  # ₹10 crore
        got = charges([fill], DEFAULT_OPTIONS_CONFIG.costs)
        assert got.ipft == Decimal("0.10")
        assert got.gst == Decimal("6417.02")  # 18 % of (20 + 35,530 + 100 + 0.10)
        assert got.total == Decimal("45067.12")  # 20 + 35,530 + 100 + 0.10 + 3,000 + 6,417.02

    def test_a_one_rupee_gain_can_pay_ten_paise(self) -> None:
        got = cost_test(Decimal("0.10"), Decimal(1), Decimal("0.20"))
        assert (got.verdict, got.cost_share) == (CostVerdict.OK, Decimal("0.1"))


def _size(
    capital: str,
    *,
    pct: str = "1.0",
    lot_size: int = LOT,
    risk_per_lot: str = "1000",
) -> sizing.Sizing:
    return size(
        mode=Mode.LIVE, sleeve_capital_inr=Decimal(capital), risk_per_trade_pct=Decimal(pct),
        max_lots=50, risk_per_lot_inr=Decimal(risk_per_lot), lot_size=lot_size,
        real_journal_rows=5, config=CFG.sizing, ceilings=DEFAULT_CEILINGS,
    )  # fmt: skip


class TestSizingEdges:
    def test_a_rejection_carries_no_budget_and_no_half_size(self) -> None:
        got = _size("0")
        assert got.rejection is SizingReject.REJECTED_NO_SLEEVE_CAPITAL
        assert (got.risk_budget_inr, got.r_inr, got.half_size) == (Decimal(0), Decimal(0), False)

    def test_a_lot_size_of_one_and_a_rupee_of_risk_are_sizeable(self) -> None:
        got = _size("100000", lot_size=1, risk_per_lot="1")
        assert got.lots == DEFAULT_CEILINGS.max_lots_max  # 1,000 lots, capped

    def test_one_rupee_of_capital_is_capital(self) -> None:
        assert _size("1").rejection is SizingReject.REJECTED_BUDGET

    def test_the_budget_multiplies_capital_by_the_pct(self) -> None:
        got = _size("2000000", pct="0.5")
        assert got.risk_budget_inr == Decimal(10000)

    def test_the_premium_cap_at_one_rupee_and_at_the_boundary(self) -> None:
        cap = CFG.directional.premium_cap_pct
        assert not premium_cap_ok(1, Decimal(150), LOT, Decimal(1), cap)
        assert premium_cap_ok(1, Decimal(150), LOT, Decimal(97_500), cap)  # 9,750 = 10 % exactly

    def test_a_one_rupee_pool_is_a_pool(self) -> None:
        got = margin_check(
            mode=Mode.PAPER, hedged_estimate_inr=Decimal(50000), transient_estimate_inr=None,
            margin_pool_inr=Decimal(1), margin_in_use_inr=Decimal(0),
        )  # fmt: skip
        assert got.code is MarginCode.REJECTED_MARGIN


WED = dt.date(2026, 10, 21)
WEEK = (dt.date(2026, 10, 19), dt.date(2026, 10, 20), WED, dt.date(2026, 10, 22))


def _trade(day: dt.date, r: str) -> RealisedTrade:
    return RealisedTrade(Sleeve.O2, day, Decimal(r) * 1000, Decimal(r))


def _eval(
    history: list[RealisedTrade], *, r: str = "1000", days: tuple[dt.date, ...] = WEEK
) -> risk.Pause | None:
    return evaluate_sleeve(
        sleeve=Sleeve.O2, today=WED, history=history, today_marked_inr=Decimal(0),
        r_today_inr=Decimal(r), trading_days=days, config=CFG.risk,
    )  # fmt: skip


class TestRiskEdges:
    def test_a_monday_row_is_in_its_week(self) -> None:
        got = _eval([_trade(dt.date(2026, 10, 19), "-4")])
        assert got is not None
        assert got.reasons == (risk.PauseReason.WEEKLY_R,)

    def test_the_week_ends_on_its_sunday(self) -> None:
        assert last_trading_day_of_week(WED, (*WEEK, dt.date(2026, 10, 26))) == dt.date(
            2026, 10, 22
        )

    def test_last_months_losses_are_not_this_months(self) -> None:
        assert _eval([_trade(dt.date(2026, 9, 29), "-8")]) is None

    def test_an_r_of_one_rupee_is_an_r(self) -> None:
        assert _eval([], r="1") is None

    def test_the_breach_fraction_multiplies(self) -> None:
        half = RiskConfig(budget_breach_frac=Decimal("0.5"))
        assert trade_breached(Decimal(600), Decimal(1000), half)

    def test_one_rupee_of_capital_and_one_rupee_settings(self) -> None:
        limits = book_limits(
            sleeve_capitals_inr=[Decimal(1)], daily_loss_limit_inr=Decimal(1),
            monthly_pause_inr=Decimal(1), config=CFG.risk, ceilings=DEFAULT_CEILINGS,
        )  # fmt: skip
        assert limits == risk.BookLimits(Decimal(1), Decimal(1))


class TestJournalEdges:
    def test_an_r_of_one_rupee(self) -> None:
        assert r_multiple(Decimal(5), Decimal(1)) == 5

    def test_a_scratch_is_not_a_win(self) -> None:
        rows = [
            JournalRow(
                Sleeve.O2, WED, True, SizingMode.PAPER_ONE_LOT, True, net_pnl_inr=Decimal(0)
            ),
            JournalRow(
                Sleeve.O2, WED, True, SizingMode.PAPER_ONE_LOT, True, net_pnl_inr=Decimal(1)
            ),
        ]
        assert summarize_one(rows).win_rate == Decimal("0.5")


class TestExecutionEdges:
    def test_the_input_order_never_changes_the_leg_order(self) -> None:
        for roles in ((LC, LP, SC, SP), (SP, SC, LP, LC), (LC, SC, LP, SP)):
            assert entry_sequence(roles) == (LP, LC, SP, SC)
            assert exit_sequence(roles) == (SC, SP, LC, LP)

    def test_never_naked_checks_each_side_and_treats_a_missing_leg_as_zero(self) -> None:
        assert not never_naked({SC: 1, LC: 0, SP: 0, LP: 5})
        assert not never_naked({SP: 1, LP: 0, SC: 0, LC: 5})
        assert not never_naked({SC: 1})
        assert not never_naked({SP: 1})

    def test_a_lone_long_is_closable(self) -> None:
        assert next_exit_leg({LC: 5}) == LC

    def test_the_first_two_attempts_are_not_marketable(self) -> None:
        for number in (1, 2):
            attempt = next_attempt(
                number, Side.BUY, Decimal(100), Decimal("100.5"), TICK,
                closing_reduces_risk=True, config=CFG.execution,
            )  # fmt: skip
            assert attempt is not None
            assert not attempt.marketable

    def test_one_unit_is_a_quantity(self) -> None:
        got = simulate_fill(
            Side.BUY,
            (Level(Decimal(10), 5),),
            1,
            limit_price=Decimal(10),
            tick=TICK,
            config=CFG.execution,
        )
        assert got.filled == 1

    def test_latency_is_ticks_times_latency_ticks(self) -> None:
        ladder = (Level(Decimal(100), 10), Level(Decimal(100), 10))
        got = simulate_fill(
            Side.BUY,
            ladder,
            20,
            limit_price=Decimal(101),
            tick=TICK,
            config=ExecutionConfig(latency_ticks=2),
        )
        assert got.avg_price == Decimal("100.05")  # 10 @ 100.00 + 10 @ 100.10

    def test_zero_price_and_zero_quantity_levels_are_ignored_and_cheap_levels_are_not(self) -> None:
        ladder = (Level(Decimal(0), 65), Level(Decimal("0.50"), 1), Level(Decimal("0.55"), 64))
        got = simulate_fill(
            Side.BUY, ladder, 65, limit_price=Decimal(1), tick=TICK, config=CFG.execution
        )
        assert got.filled == 65
        assert got.avg_price == (Decimal("0.50") + Decimal("0.60") * 64) / 65

    def test_a_sell_at_exactly_its_limit_fills(self) -> None:
        got = simulate_fill(
            Side.SELL,
            (Level(Decimal("99.95"), 65),),
            65,
            limit_price=Decimal("99.95"),
            tick=TICK,
            config=CFG.execution,
        )
        assert got.complete


class TestBacktestEdges:
    def test_tier1_refuses_rupees_without_r(self) -> None:
        day = DayOutcome(WED, traded=True, net_pnl_inr=Decimal(100))
        with pytest.raises(ValueError, match="no P&L"):
            run(Sleeve.O2, Tier.SIGNALS, [day], lambda d: d, min_sessions=60)

    def test_a_scratch_is_not_a_win_and_missing_rupees_count_as_zero(self) -> None:
        days = [
            DayOutcome(WED, traded=True, r_multiple=Decimal(0)),
            DayOutcome(
                WED + dt.timedelta(days=1),
                traded=True,
                r_multiple=Decimal(1),
                net_pnl_inr=Decimal(700),
            ),
        ]
        got = run(Sleeve.O3B, Tier.OBSERVED, days, lambda d: d, min_sessions=20)
        assert got.win_rate == Decimal("0.5")
        assert got.net_pnl_inr == Decimal(700)
