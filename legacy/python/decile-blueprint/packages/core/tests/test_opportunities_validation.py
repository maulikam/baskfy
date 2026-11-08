from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from baskfy_core.opportunities.config import SetupType, SwingFilters
from baskfy_core.opportunities.indicators import with_indicators
from baskfy_core.opportunities.validation import (
    ExitReason,
    members_as_of,
    round_trip_cost_pct,
    simulate,
    validate_setup,
)

FILTERS = SwingFilters(
    min_turnover_cr=0.0,
    min_price=1.0,
    atr_pct_min=0.0,
    atr_pct_max=50.0,
    min_rr=0.0,
    horizon_min=1,
    horizon_max=60,
)


def _bar(open_: float, high: float, low: float, close: float) -> dict[str, object]:
    return {"open": open_, "high": high, "low": low, "close": close}


def test_entry_is_next_open_and_a_bar_touching_both_levels_is_a_stop() -> None:
    rows = [_bar(100, 101, 99, 100), _bar(101, 112, 94, 100)]
    outcome = simulate(rows, 0, stop=95.0, target=110.0, horizon=5)
    assert outcome is not None
    index, fill, reason, worst = outcome
    assert (index, fill, reason) == (1, 95.0, ExitReason.STOP)
    assert worst < -1.0


def test_gap_beyond_the_stop_fills_at_the_open() -> None:
    rows = [_bar(100, 101, 99, 100), _bar(101, 103, 100, 102), _bar(90, 91, 88, 89)]
    outcome = simulate(rows, 0, stop=95.0, target=110.0, horizon=5)
    assert outcome is not None
    assert outcome[1:3] == (90.0, ExitReason.STOP)


def test_entry_gap_through_target_or_stop_is_skipped() -> None:
    assert simulate([_bar(100, 101, 99, 100), _bar(111, 112, 110, 111)], 0, 95.0, 110.0, 5) is None
    assert simulate([_bar(100, 101, 99, 100), _bar(94, 95, 93, 94)], 0, 95.0, 110.0, 5) is None


def test_time_exit_at_the_horizon_close() -> None:
    rows = [_bar(100, 101, 99, 100)] + [_bar(101, 103, 100, 102)] * 4
    outcome = simulate(rows, 0, stop=95.0, target=120.0, horizon=3)
    assert outcome is not None
    assert outcome[:3] == (3, 102.0, ExitReason.TIME)
    assert outcome[3] == pytest.approx(-1 / 6)


def test_costs_include_fees_and_both_sides_of_slippage() -> None:
    assert round_trip_cost_pct(10) == pytest.approx(round_trip_cost_pct(0) + 0.2)
    assert round_trip_cost_pct(0) > 0.2


def _days(n: int) -> list[dt.date]:
    out: list[dt.date] = []
    day = dt.date(2024, 1, 1)
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def test_signals_never_use_bars_after_the_signal_day() -> None:
    closes = [100.0 + i * 0.3 for i in range(60)] + [
        118.0 + (1 if i % 2 else -1) for i in range(20)
    ]
    closes += [121.0] + [125.0] * 10
    volumes = [1000.0] * 80 + [3000.0] + [1000.0] * 10
    days = _days(len(closes))

    def frame(tail_close: float) -> pl.DataFrame:
        cs = [*closes[:-1], tail_close]
        return pl.DataFrame(
            {
                "instrument_id": [1] * len(cs),
                "symbol": ["A"] * len(cs),
                "date": days,
                "open": cs,
                "high": [c + 1.5 for c in cs],
                "low": [c - 1.5 for c in cs],
                "close": cs,
                "volume": volumes,
            }
        )

    a = validate_setup(
        with_indicators(frame(125.0), None),
        SetupType.BREAKOUT,
        days[80],
        days[80],
        membership=None,
        filters=FILTERS,
        slippage_bps=5,
    )
    b = validate_setup(
        with_indicators(frame(10.0), None),
        SetupType.BREAKOUT,
        days[80],
        days[80],
        membership=None,
        filters=FILTERS,
        slippage_bps=5,
    )
    assert len(a.trades) == len(b.trades) == 1
    assert a.trades[0].signal_date == days[80]
    assert a.trades[0].entry_date == days[81]
    assert a.trades[0].entry == pytest.approx(125.0)


def test_membership_is_point_in_time() -> None:
    closes = [100.0 + i * 0.3 for i in range(60)] + [
        118.0 + (1 if i % 2 else -1) for i in range(20)
    ]
    closes += [121.0, 122.0]
    days = _days(len(closes))
    bars = pl.DataFrame(
        {
            "instrument_id": [1] * len(closes),
            "symbol": ["A"] * len(closes),
            "date": days,
            "open": closes,
            "high": [c + 1.5 for c in closes],
            "low": [c - 1.5 for c in closes],
            "close": closes,
            "volume": [1000.0] * 80 + [3000.0, 1000.0],
        }
    )
    result = validate_setup(
        with_indicators(bars, None),
        SetupType.BREAKOUT,
        days[0],
        days[-1],
        membership={days[80]: frozenset({2})},
        filters=FILTERS,
        slippage_bps=0,
    )
    assert result.trades == ()


def test_members_resolve_to_the_latest_snapshot_on_or_before_the_day() -> None:
    snapshots = {dt.date(2024, 1, 1): frozenset({1}), dt.date(2024, 3, 1): frozenset({2})}
    assert members_as_of(snapshots, dt.date(2023, 12, 31)) == frozenset()
    assert members_as_of(snapshots, dt.date(2024, 2, 15)) == frozenset({1})
    assert members_as_of(snapshots, dt.date(2024, 3, 1)) == frozenset({2})
