"""``docs/options/04`` §1 — expiries, day roles, lot sizes, from the master and never a weekday.

The load-bearing test is the holiday shift: a master whose October monthly moved from Tuesday
27th to Monday 26th must move the answer, and a weekday-rule implementation (the last Tuesday of
the month) must get it wrong — otherwise the fixture proves nothing.
"""

from __future__ import annotations

import calendar as _calendar
import dataclasses
import datetime as dt
import re
from decimal import Decimal
from pathlib import Path

import pytest
from options_fixtures import (
    EXPIRIES,
    LOT,
    SHIFTED_MONTHLY,
    contract,
    master,
    shifted_master,
)

from baskfy_core.options.calendar import (
    RoleReason,
    expiries,
    expiry_for_o1_o3,
    expiry_for_o2,
    kind,
    lot_size_for,
    monthly_expiry,
    next_session,
    role,
    tick_size_for,
)
from baskfy_core.options.config import ExpiryKind, OptionType, Sleeve

MON_19 = dt.date(2026, 10, 19)
TUE_20 = dt.date(2026, 10, 20)
WED_21 = dt.date(2026, 10, 21)
TUE_27 = dt.date(2026, 10, 27)


def weekday_rule_monthly(year: int, month: int) -> dt.date:
    """What a weekday implementation would answer: the last Tuesday of the month."""
    day = dt.date(year, month, _calendar.monthrange(year, month)[1])
    while day.isoweekday() != 2:
        day -= dt.timedelta(days=1)
    return day


class TestExpiriesComeFromTheMaster:
    def test_expiries_are_the_distinct_nifty_expiries_ascending(self) -> None:
        assert expiries(master()) == EXPIRIES

    def test_another_underlying_never_leaks_in(self) -> None:
        assert dt.date(2026, 10, 29) not in expiries(master())
        assert expiries(master(), "BANKNIFTY") == (dt.date(2026, 10, 29),)

    def test_monthly_is_the_last_expiry_of_the_month(self) -> None:
        assert monthly_expiry(master(), 2026, 10) == TUE_27
        assert monthly_expiry(master(), 2026, 11) == dt.date(2026, 11, 24)
        assert monthly_expiry(master(), 2026, 12) is None

    def test_the_holiday_shift_moves_the_monthly(self) -> None:
        assert monthly_expiry(shifted_master(), 2026, 10) == SHIFTED_MONTHLY
        assert kind(shifted_master(), SHIFTED_MONTHLY) is ExpiryKind.MONTHLY

    def test_the_holiday_fixture_defeats_a_weekday_rule(self) -> None:
        """If a weekday rule agreed with the master here, the test above would prove nothing."""
        assert weekday_rule_monthly(2026, 10) != monthly_expiry(shifted_master(), 2026, 10)
        assert weekday_rule_monthly(2026, 10) == monthly_expiry(master(), 2026, 10)

    def test_kind_distinguishes_weekly_and_monthly(self) -> None:
        assert kind(master(), TUE_20) is ExpiryKind.WEEKLY
        assert kind(master(), TUE_27) is ExpiryKind.MONTHLY

    def test_kind_refuses_a_date_that_is_not_an_expiry(self) -> None:
        with pytest.raises(ValueError, match="not a NIFTY expiry"):
            kind(master(), WED_21)

    def test_the_module_does_no_weekday_arithmetic(self) -> None:
        """``04`` §1.1: "No weekday arithmetic anywhere" — asserted over the source."""
        source = (
            Path(__file__).resolve().parents[1] / "src" / "baskfy_core" / "options" / "calendar.py"
        ).read_text(encoding="utf-8")
        assert not re.search(r"\.(iso)?weekday\(|TUESDAY|THURSDAY", source)


def _role(
    day: dt.date, sleeve: Sleeve, *, events: tuple[dt.date, ...] = (), trading: bool = True
) -> RoleReason:
    return role(day, sleeve, rows=master(), event_days=events, trading_day=trading).reason


class TestDayRoles:
    """``04`` §1.2's table, row by row."""

    @pytest.mark.parametrize(
        ("day", "sleeve", "expected"),
        [
            (TUE_27, Sleeve.O1M, RoleReason.TRADES),
            (TUE_20, Sleeve.O1M, RoleReason.NOT_MONTHLY),
            (TUE_20, Sleeve.O1W, RoleReason.TRADES),
            (TUE_27, Sleeve.O1W, RoleReason.MONTHLY_EXPIRY),
            (TUE_20, Sleeve.O3A, RoleReason.TRADES),
            (TUE_27, Sleeve.O3B, RoleReason.TRADES),
            (WED_21, Sleeve.O3A, RoleReason.NOT_EXPIRY),
            (WED_21, Sleeve.O1W, RoleReason.NOT_EXPIRY),
            (WED_21, Sleeve.O2, RoleReason.TRADES),
            (TUE_27, Sleeve.O2, RoleReason.TRADES),
        ],
    )
    def test_role_table(self, day: dt.date, sleeve: Sleeve, expected: RoleReason) -> None:
        assert _role(day, sleeve) is expected

    def test_o1w_refuses_the_monthly_tuesday(self) -> None:
        verdict = role(TUE_27, Sleeve.O1W, rows=master(), event_days=(), trading_day=True)
        assert not verdict.trades
        assert verdict.reason is RoleReason.MONTHLY_EXPIRY

    @pytest.mark.parametrize("sleeve", list(Sleeve))
    def test_an_event_day_stops_every_sleeve(self, sleeve: Sleeve) -> None:
        assert _role(TUE_27, sleeve, events=(TUE_27,)) is RoleReason.EVENT_DAY

    @pytest.mark.parametrize("sleeve", list(Sleeve))
    def test_a_non_trading_day_stops_every_sleeve(self, sleeve: Sleeve) -> None:
        assert _role(TUE_27, sleeve, trading=False) is RoleReason.NOT_TRADING_DAY

    def test_the_shifted_monthly_is_o1ms_day_and_the_tuesday_is_nobodys(self) -> None:
        rows = shifted_master()
        assert role(SHIFTED_MONTHLY, Sleeve.O1M, rows=rows, event_days=(), trading_day=True).trades
        on_tuesday = role(TUE_27, Sleeve.O1M, rows=rows, event_days=(), trading_day=True)
        assert on_tuesday.reason is RoleReason.NOT_EXPIRY


class TestNextSession:
    def test_next_monthly(self) -> None:
        assert (
            next_session(dt.date(2026, 10, 1), Sleeve.O1M, rows=master(), event_days=()) == TUE_27
        )

    def test_an_event_expiry_is_skipped_not_shifted(self) -> None:
        """``04`` §1.3 — the next session is the next *expiry*, never the day after the event."""
        found = next_session(dt.date(2026, 10, 1), Sleeve.O1M, rows=master(), event_days=(TUE_27,))
        assert found == dt.date(2026, 11, 24)

    def test_next_weekly_skips_the_monthly(self) -> None:
        assert next_session(TUE_20, Sleeve.O1W, rows=master(), event_days=()) == dt.date(
            2026, 11, 3
        )

    def test_strictly_after(self) -> None:
        assert next_session(TUE_20, Sleeve.O3A, rows=master(), event_days=()) == TUE_27

    def test_none_past_the_master(self) -> None:
        assert next_session(dt.date(2026, 11, 24), Sleeve.O3A, rows=master(), event_days=()) is None

    def test_o2_is_not_answered_from_the_master(self) -> None:
        with pytest.raises(ValueError, match="trading calendar"):
            next_session(TUE_20, Sleeve.O2, rows=master(), event_days=())


class TestContractChoice:
    """``04`` §1.5."""

    def test_monday_uses_tuesdays_contract(self) -> None:
        assert expiry_for_o2(MON_19, master()) == TUE_20

    def test_tuesday_uses_next_weeks_contract(self) -> None:
        assert expiry_for_o2(TUE_20, master()) == TUE_27

    def test_midweek_uses_the_next_expiry(self) -> None:
        assert expiry_for_o2(WED_21, master()) == TUE_27

    def test_no_expiry_left(self) -> None:
        assert expiry_for_o2(dt.date(2026, 11, 24), master()) is None

    def test_o1_and_o3_trade_todays_contract(self) -> None:
        assert expiry_for_o1_o3(TUE_20) == TUE_20


class TestLotAndTickSize:
    """``04`` §1.4 — read per expiry; missing, zero or ambiguous is ``None`` (sizing refuses)."""

    def test_read_from_the_master(self) -> None:
        assert lot_size_for(master(), TUE_20) == LOT
        assert tick_size_for(master(), TUE_20) == Decimal("0.05")

    def test_banknifty_lot_size_is_its_own(self) -> None:
        assert lot_size_for(master(), dt.date(2026, 10, 29), "BANKNIFTY") == 30
        assert lot_size_for(master(), dt.date(2026, 10, 29)) is None

    def test_missing_expiry(self) -> None:
        assert lot_size_for(master(), WED_21) is None
        assert tick_size_for(master(), WED_21) is None

    def test_zero_is_refused(self) -> None:
        rows = (contract(TUE_20, Decimal(25000), OptionType.CE, lot_size=0, tick=Decimal(0)),)
        assert lot_size_for(rows, TUE_20) is None
        assert tick_size_for(rows, TUE_20) is None

    def test_disagreeing_rows_are_refused(self) -> None:
        rows = (
            contract(TUE_20, Decimal(25000), OptionType.CE, lot_size=65),
            contract(TUE_20, Decimal(25000), OptionType.PE, lot_size=75),
        )
        assert lot_size_for(rows, TUE_20) is None


class TestVerdictShape:
    """The verdict's ``trades`` flag agrees with its reason on every row, and results are frozen."""

    @pytest.mark.parametrize("sleeve", list(Sleeve))
    @pytest.mark.parametrize("day", [MON_19, TUE_20, WED_21, TUE_27])
    @pytest.mark.parametrize("trading", [True, False])
    @pytest.mark.parametrize("events", [(), (TUE_27,)])
    def test_trades_iff_the_reason_is_trades(
        self, sleeve: Sleeve, day: dt.date, trading: bool, events: tuple[dt.date, ...]
    ) -> None:
        verdict = role(day, sleeve, rows=master(), event_days=events, trading_day=trading)
        assert verdict.trades is (verdict.reason is RoleReason.TRADES)

    def test_a_lot_size_of_one_is_a_lot_size(self) -> None:
        assert (
            lot_size_for((contract(TUE_20, Decimal(25000), OptionType.CE, lot_size=1),), TUE_20)
            == 1
        )

    def test_rows_and_verdicts_are_frozen(self) -> None:
        verdict = role(TUE_27, Sleeve.O1M, rows=master(), event_days=(), trading_day=True)
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(verdict, dataclasses.fields(verdict)[0].name, False)
        row = master()[0]
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(row, dataclasses.fields(row)[0].name, 1)
