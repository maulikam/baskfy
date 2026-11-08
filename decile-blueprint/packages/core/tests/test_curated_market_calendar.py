from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from baskfy_core.curated_rebalance import (
    NSE_ZONE,
    CuratedRebalanceError,
    market_session_at,
)

IST = ZoneInfo("Asia/Kolkata")
UTC = dt.UTC
FRIDAY = dt.date(2026, 8, 21)
MONDAY = dt.date(2026, 8, 24)
TUESDAY = dt.date(2026, 8, 25)
SESSIONS = (FRIDAY, MONDAY, TUESDAY)


def _ist(
    day: dt.date, hour: int, minute: int, second: int = 0, microsecond: int = 0
) -> dt.datetime:
    return dt.datetime.combine(
        day,
        dt.time(hour, minute, second, microsecond),
        tzinfo=IST,
    )


def test_exact_open_boundary_is_inclusive() -> None:
    decision = market_session_at(_ist(MONDAY, 9, 15), SESSIONS)
    assert decision.is_open
    assert decision.next_open_at is None
    assert decision.observed_at_ist == _ist(MONDAY, 9, 15)


def test_last_microsecond_before_close_is_open() -> None:
    decision = market_session_at(_ist(MONDAY, 15, 29, 59, 999_999), SESSIONS)
    assert decision.is_open


def test_exact_close_boundary_is_closed_until_next_stored_session() -> None:
    decision = market_session_at(_ist(MONDAY, 15, 30), SESSIONS)
    assert not decision.is_open
    assert decision.next_open_at == _ist(TUESDAY, 9, 15)


def test_preopen_on_trading_day_names_same_day_opening() -> None:
    decision = market_session_at(_ist(MONDAY, 9, 14, 59, 999_999), SESSIONS)
    assert not decision.is_open
    assert decision.next_open_at == _ist(MONDAY, 9, 15)


def test_weekend_returns_next_stored_monday_not_a_guessed_weekday() -> None:
    saturday = dt.date(2026, 8, 22)
    decision = market_session_at(_ist(saturday, 12, 0), SESSIONS)
    assert not decision.is_open
    assert decision.next_open_at == _ist(MONDAY, 9, 15)


def test_missing_weekday_is_treated_as_holiday_and_skipped() -> None:
    holiday = dt.date(2026, 8, 26)
    later_session = dt.date(2026, 8, 27)
    decision = market_session_at(
        _ist(holiday, 10, 0),
        (MONDAY, TUESDAY, later_session),
    )
    assert not decision.is_open
    assert decision.next_open_at == _ist(later_session, 9, 15)


def test_aware_utc_instant_is_converted_to_ist_before_date_decision() -> None:
    # Sunday UTC is already Monday in India, so the opening is Monday, not Tuesday.
    instant = dt.datetime(2026, 8, 23, 20, 0, tzinfo=UTC)
    decision = market_session_at(instant, SESSIONS)
    assert decision.observed_at_ist == _ist(MONDAY, 1, 30)
    assert decision.next_open_at == _ist(MONDAY, 9, 15)
    assert decision.observed_at_ist.tzinfo == NSE_ZONE


def test_utc_open_boundary_converts_exactly() -> None:
    decision = market_session_at(dt.datetime(2026, 8, 24, 3, 45, tzinfo=UTC), SESSIONS)
    assert decision.is_open
    assert decision.observed_at_ist == _ist(MONDAY, 9, 15)


def test_naive_datetime_fails_closed() -> None:
    with pytest.raises(CuratedRebalanceError, match="timezone-aware"):
        market_session_at(dt.datetime(2026, 8, 24, 10, 0), SESSIONS)


@pytest.mark.parametrize(
    "sessions",
    [
        (),
        (MONDAY, MONDAY),
        (TUESDAY, MONDAY),
        (dt.datetime(2026, 8, 24, tzinfo=UTC),),
    ],
)
def test_invalid_stored_session_sequences_fail_closed(sessions: tuple[dt.date, ...]) -> None:
    with pytest.raises(CuratedRebalanceError):
        market_session_at(_ist(MONDAY, 10, 0), sessions)


def test_calendar_without_a_future_session_fails_closed() -> None:
    with pytest.raises(CuratedRebalanceError, match="no future NSE session"):
        market_session_at(_ist(TUESDAY, 15, 30), SESSIONS)
