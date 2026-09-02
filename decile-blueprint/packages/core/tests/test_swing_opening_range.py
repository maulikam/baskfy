"""docs/swing/04 §7 — the opening range and the live trigger."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from baskfy_core.swing.config import OpeningRangeConfig
from baskfy_core.swing.opening_range import (
    Candle,
    OpeningRange,
    TriggerState,
    TriggerVerdict,
    evaluate_trigger,
    live_gap,
    opening_range,
)

CONFIG = OpeningRangeConfig()
DAY = dt.date(2026, 9, 2)
D = Decimal


def candle(hh: int, mm: int, ohlc: tuple[str, str, str, str], v: int = 1000) -> Candle:
    o, h, l_, c = ohlc
    return Candle(dt.datetime.combine(DAY, dt.time(hh, mm)), D(o), D(h), D(l_), D(c), v)


MORNING = [
    candle(9, 15, ("100", "101.5", "99.5", "101")),
    candle(9, 16, ("101", "102", "100.5", "101.5")),
    candle(9, 17, ("101.5", "102.4", "101", "102")),
    candle(9, 18, ("102", "102.2", "100.8", "101")),
    candle(9, 19, ("101", "101.6", "100.9", "101.2")),
    candle(9, 20, ("101.2", "103", "101", "102.9")),
]


def test_five_minute_range_is_the_first_five_candles_and_complete_once_a_sixth_exists() -> None:
    r = opening_range(MORNING, day=DAY, window_minutes=5, config=CONFIG)
    assert (r.high, r.low, r.candles, r.complete) == (D("102.4"), D("99.5"), 5, True)


def test_one_minute_range_is_the_first_candle() -> None:
    r = opening_range(MORNING, day=DAY, window_minutes=1, config=CONFIG)
    assert (r.high, r.low, r.candles) == (D("101.5"), D("99.5"), 1)


def test_an_unfinished_window_is_not_a_range() -> None:
    r = opening_range(MORNING[:3], day=DAY, window_minutes=5, config=CONFIG)
    assert r.complete is False


def test_only_the_documented_windows_exist() -> None:
    with pytest.raises(ValueError, match="not one of"):
        opening_range(MORNING, day=DAY, window_minutes=15, config=CONFIG)


RANGE = OpeningRange(D("102.4"), D("99.5"), 5, complete=True, candles=5)
AT = dt.datetime.combine(DAY, dt.time(9, 31))


def verdict(
    price: str,
    *,
    pivot: str | None = "101",
    uc: str | None = None,
    at: dt.datetime = AT,
    opening: OpeningRange = RANGE,
) -> TriggerVerdict:
    return evaluate_trigger(
        last_price=D(price),
        opening=opening,
        pivot_high=D(pivot) if pivot else None,
        low_of_day=D("99.5"),
        upper_circuit=D(uc) if uc else None,
        at=at,
        config=CONFIG,
    )


def test_break_of_the_range_high_above_the_pivot_triggers_with_orl_stop() -> None:
    v = verdict("102.6")
    assert v.state is TriggerState.TRIGGERED
    assert v.entry == D("102.6")
    assert v.stop == D("99.5")


def test_a_tick_over_the_high_is_not_a_break() -> None:
    assert verdict("102.45").state is TriggerState.WAITING


def test_orh_break_below_the_daily_pivot_is_not_a_breakout() -> None:
    assert verdict("102.6", pivot="105").state is TriggerState.BELOW_PIVOT


def test_ep_has_no_pivot_requirement() -> None:
    assert verdict("102.6", pivot=None).state is TriggerState.TRIGGERED


def test_locked_at_the_upper_band_is_not_a_fill() -> None:
    assert verdict("105", uc="105").state is TriggerState.LOCKED_UPPER_CIRCUIT


def test_incomplete_range_never_triggers() -> None:
    partial = OpeningRange(D("102.4"), D("99.5"), 5, complete=False, candles=3)
    assert verdict("110", opening=partial).state is TriggerState.RANGE_INCOMPLETE


def test_after_the_monitor_window_the_session_is_over() -> None:
    late = dt.datetime.combine(DAY, dt.time(10, 46))
    assert verdict("110", at=late).state is TriggerState.SESSION_OVER


def test_stop_is_the_lower_of_range_low_and_day_low() -> None:
    v = evaluate_trigger(
        last_price=D("103"),
        opening=RANGE,
        pivot_high=None,
        low_of_day=D("99.0"),
        upper_circuit=None,
        at=AT,
        config=CONFIG,
    )
    assert v.stop == D("99.0")


def test_live_gap_needs_size_and_volume_pace() -> None:
    ok = live_gap(
        prev_close=D("100"),
        last_price=D("113"),
        volume_so_far=200_000,
        avg_daily_volume=D("1000000"),
        minutes_elapsed=15,
        config=CONFIG,
    )
    assert ok.is_candidate is True
    assert ok.gap_pct == D("13.00")
    assert ok.volume_pace == D("5.00")
    thin = live_gap(
        prev_close=D("100"),
        last_price=D("113"),
        volume_so_far=20_000,
        avg_daily_volume=D("1000000"),
        minutes_elapsed=15,
        config=CONFIG,
    )
    assert thin.is_candidate is False
    small = live_gap(
        prev_close=D("100"),
        last_price=D("105"),
        volume_so_far=200_000,
        avg_daily_volume=D("1000000"),
        minutes_elapsed=15,
        config=CONFIG,
    )
    assert small.is_candidate is False
