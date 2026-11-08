"""SIP reminder calendar — docs/smallcase SC7 / leaf 1.6.1.

Asserts the spec: holiday roll-forward, (plan, year-month) idempotency, REMINDER-only
write path (AUTO refused), pause/resume, SIP_DUE pending-action shape. No OrderGateway.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from baskfy_core.curated_sip import (
    FORBIDDEN_WRITE_MODES,
    SIP_DUE,
    SIP_MODE_REMINDER,
    SIP_STATUS_ACTIVE,
    SIP_STATUS_PAUSED,
    advance_next_fire_date,
    assert_reminder_mode,
    evaluate_plan_for_fire,
    fire_idempotency_key,
    next_fire_date,
    pause_sip,
    raise_sip_due,
    resume_sip,
    should_fire,
)


def _d(value: str) -> Decimal:
    return Decimal(value)


def _trading_weekdays(
    start: dt.date,
    end: dt.date,
    *,
    closed: set[dt.date] | None = None,
) -> set[dt.date]:
    """Mon-Fri set minus explicit closed dates (holidays)."""
    closed = closed or set()
    out: set[dt.date] = set()
    day = start
    while day <= end:
        if day.weekday() < 5 and day not in closed:
            out.add(day)
        day += dt.timedelta(days=1)
    return out


class TestReminderModeWritePath:
    def test_reminder_accepted(self) -> None:
        assert_reminder_mode(SIP_MODE_REMINDER)

    def test_auto_refused(self) -> None:
        assert "AUTO" in FORBIDDEN_WRITE_MODES
        with pytest.raises(ValueError, match="AUTO"):
            assert_reminder_mode("AUTO")

    def test_unknown_mode_refused(self) -> None:
        with pytest.raises(ValueError, match="REMINDER"):
            assert_reminder_mode("WEEKLY")

    def test_raise_sip_due_refuses_auto(self) -> None:
        with pytest.raises(ValueError, match="AUTO"):
            raise_sip_due(
                plan_id=1,
                investment_id=2,
                user_id=3,
                amount=_d("5000"),
                fire_date=dt.date(2026, 1, 15),
                mode="AUTO",
            )


class TestNextFireDateHolidayAware:
    def test_nominal_weekday_is_unchanged(self) -> None:
        # 2026-01-15 is a Thursday.
        trading = _trading_weekdays(dt.date(2026, 1, 1), dt.date(2026, 3, 31))
        assert next_fire_date(
            day_of_month=15,
            as_of=dt.date(2026, 1, 1),
            trading_dates=trading,
        ) == dt.date(2026, 1, 15)

    def test_weekend_rolls_forward_to_monday(self) -> None:
        # 2026-01-24 is Saturday → Monday 26.
        trading = _trading_weekdays(dt.date(2026, 1, 1), dt.date(2026, 2, 28))
        assert next_fire_date(
            day_of_month=24,
            as_of=dt.date(2026, 1, 1),
            trading_dates=trading,
        ) == dt.date(2026, 1, 26)

    def test_holiday_weekend_rolls_forward(self) -> None:
        # Nominal Friday 23 Jan 2026 closed (holiday) → Monday 26.
        closed = {dt.date(2026, 1, 23)}
        trading = _trading_weekdays(dt.date(2026, 1, 1), dt.date(2026, 2, 28), closed=closed)
        assert dt.date(2026, 1, 23) not in trading
        assert next_fire_date(
            day_of_month=23,
            as_of=dt.date(2026, 1, 1),
            trading_dates=trading,
        ) == dt.date(2026, 1, 26)

    def test_past_nominal_moves_to_next_month(self) -> None:
        trading = _trading_weekdays(dt.date(2026, 1, 1), dt.date(2026, 3, 31))
        assert next_fire_date(
            day_of_month=10,
            as_of=dt.date(2026, 1, 15),
            trading_dates=trading,
        ) == dt.date(2026, 2, 10)

    def test_day_of_month_bounds(self) -> None:
        trading = {dt.date(2026, 1, 15)}
        with pytest.raises(ValueError, match="day_of_month"):
            next_fire_date(day_of_month=29, as_of=dt.date(2026, 1, 1), trading_dates=trading)
        with pytest.raises(ValueError, match="day_of_month"):
            next_fire_date(day_of_month=0, as_of=dt.date(2026, 1, 1), trading_dates=trading)


class TestFireIdempotency:
    def test_key_is_plan_and_year_month(self) -> None:
        assert fire_idempotency_key(42, 2026, 3) == "42:2026-03"

    def test_second_raise_same_month_is_none(self) -> None:
        fire = dt.date(2026, 3, 10)
        first = raise_sip_due(
            plan_id=7,
            investment_id=1,
            user_id=9,
            amount=_d("1000.00"),
            fire_date=fire,
        )
        assert first is not None
        assert first["type"] == SIP_DUE
        assert first["payload"]["fire_key"] == "7:2026-03"
        assert first["payload"]["mode"] == SIP_MODE_REMINDER

        again = raise_sip_due(
            plan_id=7,
            investment_id=1,
            user_id=9,
            amount=_d("1000.00"),
            fire_date=fire,
            already_fired_keys={first["payload"]["fire_key"]},
        )
        assert again is None

    def test_different_month_is_independent(self) -> None:
        keys = {"7:2026-03"}
        action = raise_sip_due(
            plan_id=7,
            investment_id=1,
            user_id=9,
            amount=_d("1000"),
            fire_date=dt.date(2026, 4, 10),
            already_fired_keys=keys,
        )
        assert action is not None
        assert action["payload"]["fire_key"] == "7:2026-04"


class TestPauseResume:
    def test_pause_active(self) -> None:
        assert pause_sip(SIP_STATUS_ACTIVE) == SIP_STATUS_PAUSED

    def test_pause_already_paused_is_idempotent(self) -> None:
        assert pause_sip(SIP_STATUS_PAUSED) == SIP_STATUS_PAUSED

    def test_resume_recomputes_next_fire(self) -> None:
        trading = _trading_weekdays(dt.date(2026, 2, 1), dt.date(2026, 4, 30))
        status, nxt = resume_sip(
            SIP_STATUS_PAUSED,
            day_of_month=5,
            as_of=dt.date(2026, 2, 20),
            trading_dates=trading,
        )
        assert status == SIP_STATUS_ACTIVE
        assert nxt == dt.date(2026, 3, 5)

    def test_paused_plan_should_not_fire(self) -> None:
        assert not should_fire(
            status=SIP_STATUS_PAUSED,
            next_fire=dt.date(2026, 1, 10),
            as_of=dt.date(2026, 1, 10),
        )


class TestEvaluatePlan:
    def test_due_plan_raises_action_and_advances(self) -> None:
        trading = _trading_weekdays(dt.date(2026, 1, 1), dt.date(2026, 4, 30))
        plan = {
            "id": 11,
            "investment_id": 22,
            "user_id": 33,
            "amount": _d("2500.50"),
            "day_of_month": 10,
            "mode": SIP_MODE_REMINDER,
            "status": SIP_STATUS_ACTIVE,
            "next_fire_date": dt.date(2026, 2, 10),
        }
        action, advanced = evaluate_plan_for_fire(
            plan,
            as_of=dt.date(2026, 2, 10),
            trading_dates=trading,
        )
        assert action is not None
        assert action["type"] == SIP_DUE
        assert action["user_id"] == 33
        assert action["payload"]["plan_id"] == 11
        assert action["payload"]["amount"] == "2500.50"
        assert advanced == dt.date(2026, 3, 10)

    def test_idempotent_evaluate_skips_second_fire(self) -> None:
        trading = _trading_weekdays(dt.date(2026, 1, 1), dt.date(2026, 4, 30))
        plan = {
            "id": 11,
            "investment_id": 22,
            "user_id": 33,
            "amount": _d("1000"),
            "day_of_month": 10,
            "mode": SIP_MODE_REMINDER,
            "status": SIP_STATUS_ACTIVE,
            "next_fire_date": dt.date(2026, 2, 10),
        }
        key = fire_idempotency_key(11, 2026, 2)
        action, advanced = evaluate_plan_for_fire(
            plan,
            as_of=dt.date(2026, 2, 10),
            trading_dates=trading,
            already_fired_keys={key},
        )
        assert action is None
        assert advanced is None

    def test_advance_after_fire(self) -> None:
        trading = _trading_weekdays(dt.date(2026, 1, 1), dt.date(2026, 5, 31))
        nxt = advance_next_fire_date(
            day_of_month=15,
            fired_on=dt.date(2026, 1, 15),
            trading_dates=trading,
        )
        assert nxt == dt.date(2026, 2, 16)  # 15 Feb 2026 is Sunday → Monday 16
