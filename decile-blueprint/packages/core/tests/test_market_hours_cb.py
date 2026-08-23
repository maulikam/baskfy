"""Market-hours guard for curated-basket plan generation (docs/smallcase/04 §5, §9)."""

from __future__ import annotations

import datetime as dt

import pytest

from baskfy_core.market_hours_cb import (
    IST,
    SESSION_CLOSE,
    SESSION_OPEN,
    closed_market_payload,
    is_nse_session_open,
    next_session_open,
)


def _ist(year: int, month: int, day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=IST)


#: Monday 2026-08-24 and Tuesday 2026-08-25 — known weekdays.
TRADING = {dt.date(2026, 8, 24), dt.date(2026, 8, 25), dt.date(2026, 8, 26)}


class TestIsNseSessionOpen:
    def test_open_mid_session_on_trading_day(self) -> None:
        assert is_nse_session_open(_ist(2026, 8, 24, 10, 0), TRADING) is True

    def test_open_at_bell_and_at_close(self) -> None:
        assert is_nse_session_open(_ist(2026, 8, 24, 9, 15), TRADING) is True
        assert (
            is_nse_session_open(
                dt.datetime.combine(dt.date(2026, 8, 24), SESSION_CLOSE, tzinfo=IST),
                TRADING,
            )
            is True
        )

    def test_closed_before_open(self) -> None:
        assert is_nse_session_open(_ist(2026, 8, 24, 9, 14), TRADING) is False

    def test_closed_after_close(self) -> None:
        assert is_nse_session_open(_ist(2026, 8, 24, 15, 31), TRADING) is False

    def test_closed_on_non_trading_day_even_at_noon(self) -> None:
        saturday = dt.date(2026, 8, 22)
        assert saturday not in TRADING
        assert is_nse_session_open(_ist(2026, 8, 22, 12, 0), TRADING) is False

    def test_naive_datetime_treated_as_ist(self) -> None:
        naive = dt.datetime(2026, 8, 24, 11, 0)
        assert is_nse_session_open(naive, TRADING) is True

    def test_utc_input_converted_to_ist(self) -> None:
        # 10:00 IST = 04:30 UTC
        utc = dt.datetime(2026, 8, 24, 4, 30, tzinfo=dt.timezone.utc)
        assert is_nse_session_open(utc, TRADING) is True
        # 08:00 IST = 02:30 UTC — still closed
        early = dt.datetime(2026, 8, 24, 2, 30, tzinfo=dt.timezone.utc)
        assert is_nse_session_open(early, TRADING) is False


class TestNextSessionOpen:
    def test_before_open_same_day(self) -> None:
        got = next_session_open(_ist(2026, 8, 24, 8, 0), TRADING)
        assert got == dt.datetime.combine(dt.date(2026, 8, 24), SESSION_OPEN, tzinfo=IST)

    def test_after_close_rolls_to_next_trading_day(self) -> None:
        got = next_session_open(_ist(2026, 8, 24, 16, 0), TRADING)
        assert got == dt.datetime.combine(dt.date(2026, 8, 25), SESSION_OPEN, tzinfo=IST)

    def test_weekend_rolls_forward(self) -> None:
        got = next_session_open(_ist(2026, 8, 22, 12, 0), TRADING)  # Saturday
        assert got == dt.datetime.combine(dt.date(2026, 8, 24), SESSION_OPEN, tzinfo=IST)

    def test_empty_calendar_refused(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            next_session_open(_ist(2026, 8, 24, 16, 0), set())

    def test_no_future_day_refused(self) -> None:
        only_past = {dt.date(2026, 8, 20)}
        with pytest.raises(ValueError, match="no trading day after"):
            next_session_open(_ist(2026, 8, 24, 16, 0), only_past)


class TestClosedMarketPayload:
    def test_shape_and_iso(self) -> None:
        nxt = dt.datetime.combine(dt.date(2026, 8, 25), SESSION_OPEN, tzinfo=IST)
        payload = closed_market_payload(nxt)
        assert payload == {
            "market_open": False,
            "next_open_ist": nxt.isoformat(),
        }
        assert "T09:15:00+05:30" in str(payload["next_open_ist"])

    def test_never_invents_a_plan_key(self) -> None:
        payload = closed_market_payload(_ist(2026, 8, 25, 9, 15))
        assert "desk_plan_id" not in payload
        assert "legs" not in payload
        assert "kind" not in payload
