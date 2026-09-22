"""``docs/options/04`` §9 — per-sleeve R limits, the per-trade breach, the book's ₹ limits."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from baskfy_core.options.config import DEFAULT_CEILINGS, DEFAULT_OPTIONS_CONFIG, Sleeve
from baskfy_core.options.risk import (
    REJECTED_PAUSED,
    BookLimits,
    Pause,
    PauseReason,
    RealisedTrade,
    book_limits,
    evaluate_book,
    evaluate_sleeve,
    last_trading_day_of_week,
    month_end,
    plan_refusal,
    trade_breached,
)

RISK = DEFAULT_OPTIONS_CONFIG.risk
WED = dt.date(2026, 10, 21)
#: That week's trading days, with Friday 23rd a holiday.
WEEK = (dt.date(2026, 10, 19), dt.date(2026, 10, 20), WED, dt.date(2026, 10, 22))
R = Decimal(1000)


def _trade(day: dt.date, r: str, sleeve: Sleeve = Sleeve.O2) -> RealisedTrade:
    return RealisedTrade(sleeve, day, Decimal(r) * R, Decimal(r))


def _eval(history: list[RealisedTrade], marked: str = "0", today: dt.date = WED) -> Pause | None:
    return evaluate_sleeve(
        sleeve=Sleeve.O2, today=today, history=history, today_marked_inr=Decimal(marked),
        r_today_inr=R, trading_days=WEEK, config=RISK,
    )  # fmt: skip


class TestSleeveLimits:
    def test_minus_two_r_today_closes_and_pauses_the_day(self) -> None:
        got = _eval([_trade(WED, "-1.5")], marked="-500")
        assert got == Pause(WED, (PauseReason.DAILY_R,), close_now=True)

    def test_just_inside_the_daily_limit(self) -> None:
        assert _eval([_trade(WED, "-1.5")], marked="-499.99") is None

    def test_weekly_pauses_to_the_weeks_last_trading_day(self) -> None:
        history = [
            _trade(dt.date(2026, 10, 19), "-2"),
            _trade(dt.date(2026, 10, 20), "-1.5"),
            _trade(WED, "-0.5"),
        ]
        got = _eval(history)
        assert got is not None
        assert PauseReason.WEEKLY_R in got.reasons
        assert got.paused_until == dt.date(2026, 10, 22)  # Friday is a holiday

    def test_last_weeks_losses_do_not_count(self) -> None:
        assert _eval([_trade(dt.date(2026, 10, 16), "-4")]) is None

    def test_monthly_pauses_to_month_end(self) -> None:
        history = [_trade(dt.date(2026, 10, d), "-2") for d in (5, 6, 12, 13)]
        got = _eval(history)
        assert got == Pause(dt.date(2026, 10, 31), (PauseReason.MONTHLY_R,), close_now=False)

    def test_several_limits_report_every_reason_and_the_latest_date_wins(self) -> None:
        history = [_trade(dt.date(2026, 10, d), "-2") for d in (5, 6, 12)] + [_trade(WED, "-2")]
        got = _eval(history)
        assert got is not None
        assert got.reasons == (PauseReason.DAILY_R, PauseReason.MONTHLY_R)
        assert got.paused_until == dt.date(2026, 10, 31)
        assert got.close_now

    def test_the_ledger_never_pools_sleeves(self) -> None:
        with pytest.raises(ValueError, match="never pool"):
            _eval([_trade(WED, "-1", Sleeve.O1W)])

    def test_r_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            evaluate_sleeve(
                sleeve=Sleeve.O2, today=WED, history=[], today_marked_inr=Decimal(0),
                r_today_inr=Decimal(0), trading_days=WEEK, config=RISK,
            )  # fmt: skip

    def test_calendar_helpers(self) -> None:
        assert month_end(dt.date(2026, 2, 3)) == dt.date(2026, 2, 28)
        assert last_trading_day_of_week(WED, ()) == WED


class TestTradeBreach:
    def test_beyond_the_budget_closes(self) -> None:
        assert trade_breached(Decimal("1000.01"), R, RISK)
        assert not trade_breached(Decimal(1000), R, RISK)


class TestBook:
    """``04`` §9.3."""

    def _limits(self, capitals: list[int], daily: int = 0, monthly: int = 0) -> BookLimits | None:
        return book_limits(
            sleeve_capitals_inr=[Decimal(c) for c in capitals],
            daily_loss_limit_inr=Decimal(daily), monthly_pause_inr=Decimal(monthly),
            config=RISK, ceilings=DEFAULT_CEILINGS,
        )  # fmt: skip

    def test_off_until_some_capital(self) -> None:
        assert self._limits([0, 0, 0, 0]) is None
        assert (
            evaluate_book(
                today=WED,
                today_net_inr=Decimal(-(10**9)),
                month_realised_inr=Decimal(0),
                limits=None,
            )
            is None
        )

    def test_derived_from_capital(self) -> None:
        assert self._limits([500_000, 500_000, 0, 0]) == BookLimits(Decimal(15000), Decimal(50000))

    def test_capped_by_the_ceilings(self) -> None:
        assert self._limits([4_000_000]) == BookLimits(Decimal(30000), Decimal(75000))

    def test_an_explicit_setting_is_used(self) -> None:
        assert self._limits([1_000_000], daily=10000, monthly=20000) == BookLimits(
            Decimal(10000), Decimal(20000)
        )

    def test_a_daily_breach_closes_everything_today(self) -> None:
        limits = BookLimits(Decimal(15000), Decimal(50000))
        got = evaluate_book(
            today=WED, today_net_inr=Decimal(-15000), month_realised_inr=Decimal(0), limits=limits
        )
        assert got == Pause(WED, (PauseReason.BOOK_DAILY_INR,), close_now=True)
        assert (
            evaluate_book(
                today=WED,
                today_net_inr=Decimal("-14999.99"),
                month_realised_inr=Decimal(0),
                limits=limits,
            )
            is None
        )

    def test_a_monthly_breach_pauses_to_month_end(self) -> None:
        limits = BookLimits(Decimal(15000), Decimal(50000))
        got = evaluate_book(
            today=WED, today_net_inr=Decimal(0), month_realised_inr=Decimal(-50000), limits=limits
        )
        assert got == Pause(dt.date(2026, 10, 31), (PauseReason.BOOK_MONTHLY_INR,), close_now=False)


class TestAPauseIsARefusal:
    """``04`` §9.4."""

    def test_sleeve_paused_today(self) -> None:
        assert plan_refusal(WED, None, WED) == REJECTED_PAUSED

    def test_pause_over(self) -> None:
        assert plan_refusal(WED - dt.timedelta(days=1), None, WED) is None

    def test_book_paused(self) -> None:
        assert plan_refusal(None, dt.date(2026, 10, 31), WED) == REJECTED_PAUSED
