from __future__ import annotations

import datetime as dt
from dataclasses import replace

import polars as pl
import pytest

from baskfy_core.opportunities.config import (
    DEFAULT_SWING_PARAMS,
    SetupType,
    SwingFilters,
    TriggerStatus,
)
from baskfy_core.opportunities.indicators import with_indicators
from baskfy_core.opportunities.swing import InstrumentContext, missing_sessions, rank, scan

START = dt.date(2025, 1, 1)
FILTERS = SwingFilters(
    min_turnover_cr=0.0,
    min_price=1.0,
    atr_pct_min=0.0,
    atr_pct_max=50.0,
    min_rr=0.0,
    horizon_min=1,
    horizon_max=60,
)


def _days(n: int) -> list[dt.date]:
    out: list[dt.date] = []
    day = START
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def _frame(instrument_id: int, closes: list[float], volumes: list[float]) -> pl.DataFrame:
    days = _days(len(closes))
    return pl.DataFrame(
        {
            "instrument_id": [instrument_id] * len(closes),
            "symbol": [f"S{instrument_id}"] * len(closes),
            "date": days,
            "open": closes,
            "high": [c + 1.5 for c in closes],
            "low": [c - 1.5 for c in closes],
            "close": closes,
            "volume": volumes,
            "turnover": [None] * len(closes),
            "adj_factor": [1.0] * len(closes),
        },
        schema_overrides={"turnover": pl.Float64},
    )


def _base_closes() -> list[float]:
    trend = [100.0 + i * 0.3 for i in range(60)]
    base = [118.0 + (1.0 if i % 2 else -1.0) for i in range(20)]
    return trend + base


def _bench(n: int, level: float = 1000.0) -> pl.DataFrame:
    return pl.DataFrame({"date": _days(n), "level": [level] * n})


def test_breakout_with_volume_is_triggered_and_risk_reward_uses_the_entry() -> None:
    closes = [*_base_closes(), 121.0]
    volumes = [1000.0] * 80 + [3000.0]
    bars = _frame(1, closes, volumes)
    indicated = with_indicators(bars, _bench(len(closes)))
    as_of = _days(len(closes))[-1]
    result = scan(indicated, as_of, {}, FILTERS)
    found = [o for o in result.opportunities if o.evaluation.setup is SetupType.BREAKOUT]
    assert len(found) == 1
    opp = found[0]
    assert opp.evaluation.status is TriggerStatus.TRIGGERED
    assert float(opp.entry) == pytest.approx(121.0)
    atr = float(opp.atr)
    assert float(opp.invalidation) == pytest.approx(121.0 - 1.5 * atr, abs=0.02)
    t1 = float(opp.targets[0][1])
    expected = (t1 - float(opp.entry)) / (float(opp.entry) - float(opp.invalidation))
    assert float(opp.risk_reward) == pytest.approx(expected, abs=0.02)
    assert result.counts.triggered >= 1


def test_close_above_pivot_without_volume_is_not_a_breakout() -> None:
    closes = [*_base_closes(), 121.0]
    bars = _frame(1, closes, [1000.0] * 81)
    indicated = with_indicators(bars, _bench(len(closes)))
    result = scan(indicated, _days(81)[-1], {}, FILTERS)
    assert all(o.evaluation.setup is not SetupType.BREAKOUT for o in result.opportunities)


def test_close_just_below_pivot_is_pending_with_pivot_as_entry() -> None:
    closes = [*_base_closes(), 118.5]
    bars = _frame(1, closes, [1000.0] * 81)
    indicated = with_indicators(bars, _bench(len(closes)))
    result = scan(indicated, _days(81)[-1], {}, FILTERS)
    found = [o for o in result.opportunities if o.evaluation.setup is SetupType.BREAKOUT]
    assert len(found) == 1
    assert found[0].evaluation.status is TriggerStatus.PENDING
    assert found[0].entry == found[0].trigger_level


def test_insufficient_history_is_counted_not_scanned() -> None:
    closes = [100.0 + i for i in range(30)]
    bars = _frame(1, closes, [1000.0] * 30)
    result = scan(with_indicators(bars, None), _days(30)[-1], {}, FILTERS)
    assert result.counts.insufficient_history == 1
    assert result.opportunities == ()


def test_scan_is_point_in_time() -> None:
    closes = [*_base_closes(), 121.0, 90.0, 80.0]
    volumes = [1000.0] * 80 + [3000.0, 1000.0, 1000.0]
    as_of = _days(len(closes))[80]
    full = with_indicators(_frame(1, closes, volumes), _bench(len(closes)))
    cut = with_indicators(_frame(1, closes[:81], volumes[:81]), _bench(81))
    a = scan(full, as_of, {}, FILTERS)
    b = scan(cut, as_of, {}, FILTERS)
    assert [(o.id, o.entry, o.score.total) for o in a.opportunities] == [
        (o.id, o.entry, o.score.total) for o in b.opportunities
    ]


def test_relative_strength_needs_excess_return_over_benchmark() -> None:
    closes = [100.0 + i * 0.5 for i in range(81)]
    bars = _frame(1, closes, [1000.0] * 81)
    rs = replace(FILTERS, setups=frozenset({SetupType.RELATIVE_STRENGTH}))
    flat = scan(with_indicators(bars, _bench(81)), _days(81)[-1], {}, rs)
    assert any(o.evaluation.setup is SetupType.RELATIVE_STRENGTH for o in flat.opportunities)
    rising = pl.DataFrame({"date": _days(81), "level": [1000.0 + i * 10 for i in range(81)]})
    strong_bench = scan(with_indicators(bars, rising), _days(81)[-1], {}, rs)
    assert all(
        o.evaluation.setup is not SetupType.RELATIVE_STRENGTH for o in strong_bench.opportunities
    )


def test_too_many_missing_sessions_makes_the_opportunity_not_actionable() -> None:
    closes = [*_base_closes(), 121.0]
    volumes = [1000.0] * 80 + [3000.0]
    indicated = with_indicators(_frame(1, closes, volumes), _bench(len(closes)))
    context = InstrumentContext(missing_sessions=DEFAULT_SWING_PARAMS.data.max_missing_sessions + 1)
    result = scan(indicated, _days(81)[-1], {1: context}, FILTERS)
    assert result.opportunities
    assert all(not o.actionable for o in result.opportunities)
    assert all(any("missing" in note for note in o.limitations) for o in result.opportunities)


def test_missing_sessions_ignores_days_before_listing() -> None:
    calendar = _days(10)
    assert missing_sessions(calendar[5:], calendar, 10) == 0
    assert missing_sessions([calendar[0], calendar[2]], calendar[:3], 3) == 1
    assert missing_sessions(calendar[:7], calendar, 10) == 3


def test_sector_filter_excludes_other_sectors() -> None:
    closes = [*_base_closes(), 121.0]
    volumes = [1000.0] * 80 + [3000.0]
    indicated = with_indicators(_frame(1, closes, volumes), _bench(len(closes)))
    only_it = replace(FILTERS, sectors=frozenset({"nifty-it"}))
    result = scan(indicated, _days(81)[-1], {1: InstrumentContext(sector="nifty-bank")}, only_it)
    assert result.opportunities == ()


def test_ranking_is_by_total_and_total_is_sum_of_contributions() -> None:
    frames = [
        _frame(1, [*_base_closes(), 121.0], [1000.0] * 80 + [3000.0]),
        _frame(2, [*_base_closes(), 120.5], [1000.0] * 80 + [1600.0]),
    ]
    indicated = with_indicators(pl.concat(frames), _bench(81))
    result = scan(indicated, _days(81)[-1], {}, FILTERS)
    totals = [o.score.total for o in result.opportunities]
    assert totals == sorted(totals, reverse=True)
    for opp in result.opportunities:
        assert opp.score.total == pytest.approx(
            sum(c.contribution for c in opp.score.components), abs=0.1
        )
    assert rank(reversed(result.opportunities)) == list(result.opportunities)
