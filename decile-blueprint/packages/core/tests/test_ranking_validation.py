"""Spec tests for ``baskfy_core.ranking_validation`` (docs/ranking/PLAN.md C8).

Every expected number below is worked by hand in the comments from the tiny panel, never read back
from the code. Three instruments, five sessions, two monthly rebalances:

    session      A open/close   B open/close   C open/close
    2024-01-31   10 / 10        20 / 20        50 / 50      signal 1: A=1, B=2, C=3
    2024-02-01   10 / 11        20 / 20        50 / 50      fill 1 (next open)
    2024-02-29   11 / 12        20 / 22        50 / 50      signal 2: A=1, C=2, B=3
    2024-03-01   12 / 12        22 / 22        50 / 55      fill 2 (next open)
    2024-03-04   12 / 13        22 / 22        55 / 55

max_names = 2, cost 25 bps a side (rate c = 0.0025).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from pathlib import Path

import pandas as pd
import pytest

from baskfy_core.ranking_validation import (
    TradeReason,
    TradeSide,
    ValidationConfig,
    ValidationResult,
    build_price_panel,
    monthly_signal_dates,
    simulate_monthly_rebalance,
    summary_row,
)

A, B, C = 1, 2, 3
D1, D2, D3, D4, D5 = (
    dt.date(2024, 1, 31),
    dt.date(2024, 2, 1),
    dt.date(2024, 2, 29),
    dt.date(2024, 3, 1),
    dt.date(2024, 3, 4),
)
RATE = 0.0025
TOL = 1e-9

PRICES: dict[int, list[tuple[dt.date, float, float]]] = {
    A: [(D1, 10, 10), (D2, 10, 11), (D3, 11, 12), (D4, 12, 12), (D5, 12, 13)],
    B: [(D1, 20, 20), (D2, 20, 20), (D3, 20, 22), (D4, 22, 22), (D5, 22, 22)],
    C: [(D1, 50, 50), (D2, 50, 50), (D3, 50, 50), (D4, 50, 55), (D5, 55, 55)],
}
SECTORS = {A: "IT", B: "IT", C: "BANK"}


def bars(
    prices: dict[int, list[tuple[dt.date, float, float]]] = PRICES,
    drop: frozenset[tuple[int, dt.date]] = frozenset(),
) -> pd.DataFrame:
    rows = [
        {"date": day, "instrument_id": instrument, "open": o, "close": c}
        for instrument, series in prices.items()
        for day, o, c in series
        if (instrument, day) not in drop
    ]
    return pd.DataFrame(rows)


def signals(ranks: dict[dt.date, dict[int, int]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"date": day, "instrument_id": i, "quality_rank": r, "sector": SECTORS[i]}
            for day, by_id in ranks.items()
            for i, r in by_id.items()
        ]
    )


BASE_SIGNALS = {D1: {A: 1, B: 2, C: 3}, D3: {A: 1, C: 2, B: 3}}


def run(
    config: ValidationConfig,
    frame: pd.DataFrame | None = None,
    ranks: dict[dt.date, dict[int, int]] | None = None,
) -> ValidationResult:
    panel = build_price_panel(bars() if frame is None else frame)
    return simulate_monthly_rebalance(panel, signals(ranks or BASE_SIGNALS), config)


def no_buffer() -> ValidationConfig:
    return ValidationConfig(
        max_names=2, entry_rank=2, retention_rank=2, missing_bar_tolerance_sessions=0
    )


def nav_on(result: ValidationResult, day: dt.date) -> float:
    return next(point.nav for point in result.nav if point.date == day)


# --- known fills, known costs --------------------------------------------------------------------


def test_known_fills_and_costs_without_a_buffer() -> None:
    result = run(no_buffer())
    # Fill 1 at D2 open. E = 1.0 (all cash). Targets A, B at slot 1/2 -> 0.5 each.
    #   notional A = 0.5, B = 0.5; cost = 0.0025 * 1.0 = 0.0025; E' = 0.9975.
    #   shares A = 0.49875 / 10 = 0.049875; shares B = 0.49875 / 20 = 0.0249375.
    fill1 = [t for t in result.trades if t.date == D2]
    assert [(t.instrument_id, t.side, t.price) for t in fill1] == [
        (A, TradeSide.BUY, 10.0),
        (B, TradeSide.BUY, 20.0),
    ]
    assert [t.notional for t in fill1] == pytest.approx([0.5, 0.5], abs=TOL)
    assert [t.cost for t in fill1] == pytest.approx([0.00125, 0.00125], abs=TOL)
    # D2 close: A 0.049875*11 = 0.548625; B 0.0249375*20 = 0.49875; NAV 1.047375.
    assert nav_on(result, D2) == pytest.approx(1.047375, abs=TOL)
    # D3 close: A 0.049875*12 = 0.5985; B 0.0249375*22 = 0.548625; NAV 1.147125.
    assert nav_on(result, D3) == pytest.approx(1.147125, abs=TOL)

    # Fill 2 at D4 open. Values at open: A 0.049875*12 = 0.5985; B 0.0249375*22 = 0.548625.
    #   E = 1.147125. Signal 2: A rank 1 holds; B rank 3 > retention 2 exits; C rank 2 enters.
    #   Target slot = 1.147125 / 2 = 0.5735625.
    #   notional A (trim) = 0.5985 - 0.5735625 = 0.0249375 (sell)
    #   notional B (exit) = 0.548625 (sell); notional C (enter) = 0.5735625 (buy)
    #   traded = 0.0249375 + 0.548625 + 0.5735625 = 1.147125 (the whole of E: a full swap of B for
    #   C plus a trim of A is exactly one book's worth of two-way notional here);
    #   cost = 0.0025 * 1.147125 = 0.0028678125; E' = 1.1442571875.
    fill2 = [t for t in result.trades if t.date == D4]
    assert [(t.instrument_id, t.side, t.price) for t in fill2] == [
        (A, TradeSide.SELL, 12.0),
        (B, TradeSide.SELL, 22.0),
        (C, TradeSide.BUY, 50.0),
    ]
    assert [t.notional for t in fill2] == pytest.approx([0.0249375, 0.548625, 0.5735625], abs=TOL)
    assert sum(t.cost for t in fill2) == pytest.approx(0.0028678125, abs=TOL)
    record = result.rebalances[1]
    assert (record.signal_date, record.fill_date) == (D3, D4)
    assert (record.holds, record.exits, record.entries) == ((A,), (B,), (C,))
    assert record.equity_before == pytest.approx(1.147125, abs=TOL)
    # D4 close: each slot holds 1.1442571875 / 2 = 0.57212859375.
    #   A at 12 (bought at 12) = 0.57212859375; C at 55 (bought at 50) = 0.629341453125.
    assert nav_on(result, D4) == pytest.approx(1.201470046875, abs=TOL)
    # D5 close: A at 13 = 0.57212859375 * 13/12 = 0.619805976562...; C unchanged.
    d5 = 0.57212859375 * 13 / 12 + 0.629341453125
    assert nav_on(result, D5) == pytest.approx(d5, abs=TOL)


def test_known_turnover_cagr_and_drawdown() -> None:
    result = run(no_buffer())
    # Batch turnover = traded / 2 / equity before.
    #   fill 1: 1.0 / 2 / 1.0 = 0.5
    #   fill 2: 1.147125 / 2 / 1.147125 = 0.5
    turn2 = 0.5
    assert [r.turnover for r in result.rebalances] == pytest.approx([0.5, turn2], abs=TOL)
    # Window D1 -> D5 is 33 calendar days; years = 33 / 365.25.
    years = 33 / 365.25
    assert result.full.years == pytest.approx(years, abs=TOL)
    assert result.full.annual_turnover == pytest.approx((0.5 + turn2) / years, abs=TOL)
    d5 = 0.57212859375 * 13 / 12 + 0.629341453125
    assert result.full.total_return == pytest.approx(d5 - 1, abs=TOL)
    assert result.full.cagr_net == pytest.approx(d5 ** (1 / years) - 1, abs=TOL)
    # NAV path 1.0, 1.047375, 1.147125, 1.2014..., 1.2491... never falls: drawdown 0.
    assert result.full.max_drawdown == 0.0
    assert result.full.rebalances == 2


def test_max_drawdown_is_the_worst_fall_from_a_running_peak() -> None:
    prices = {**PRICES, A: [*PRICES[A][:4], (D5, 12, 6)]}
    result = run(no_buffer(), frame=bars(prices))
    # D4 NAV 1.201470046875 is the peak; at D5 A halves: 0.57212859375 * 6/12 = 0.286064296875,
    # NAV = 0.286064296875 + 0.629341453125 = 0.91540575. DD = 0.91540575 / 1.201470046875 - 1.
    assert result.full.max_drawdown == pytest.approx(0.91540575 / 1.201470046875 - 1, abs=TOL)


def test_zero_cost_charges_nothing() -> None:
    result = run(dataclasses.replace(no_buffer(), cost_bps_per_side=0.0))
    assert all(t.cost == 0 for t in result.trades)
    # E' = E = 1.0: A 0.05 sh, B 0.025 sh; D2 close = 0.55 + 0.5.
    assert nav_on(result, D2) == pytest.approx(1.05, abs=TOL)


# --- entry / retention buffer ------------------------------------------------------------------


def test_retention_buffer_keeps_a_name_and_cuts_turnover() -> None:
    config = dataclasses.replace(no_buffer(), retention_rank=3)
    result = run(config)
    # Signal 2: B rank 3 <= retention 3 -> holds; C rank 2 would enter but the book is FULL.
    record = result.rebalances[1]
    assert (record.holds, record.exits, record.entries) == ((A, B), (), ())
    # Both resized to the slot 0.5735625: A sells 0.0249375;
    # B buys 0.5735625 - 0.548625 = 0.0249375.
    #   traded = 0.049875; turnover = 0.049875 / 2 / 1.147125.
    assert record.turnover == pytest.approx(0.049875 / 2 / 1.147125, abs=TOL)
    assert record.cost == pytest.approx(0.049875 * RATE, abs=TOL)
    assert record.turnover < run(no_buffer()).rebalances[1].turnover


def test_entry_rank_below_max_names_leaves_the_slot_in_cash() -> None:
    config = dataclasses.replace(no_buffer(), entry_rank=1, retention_rank=1)
    result = run(config)
    # Signal 1: only A (rank 1) may enter. Slot 0.5; cost 0.00125; E' = 0.99875;
    # A holds 0.499375, cash 0.499375 -> cash weight 0.5.
    first = result.rebalances[0]
    assert first.entries == (A,)
    assert first.names_after == 1
    assert first.cash_weight == pytest.approx(0.5, abs=TOL)
    # D2 close: A 0.499375 * 11/10 = 0.5493125 + cash 0.499375.
    assert nav_on(result, D2) == pytest.approx(1.0486875, abs=TOL)


# --- sector weight ------------------------------------------------------------------------------


def test_mean_max_sector_weight() -> None:
    result = run(no_buffer())
    # After fill 1 the book is A, B — both IT at half of E' each -> IT weight 1.0.
    # After fill 2 the book is A (IT) and C (BANK) at half each -> max 0.5. Mean 0.75.
    assert [r.max_sector_weight for r in result.rebalances] == pytest.approx([1.0, 0.5], abs=TOL)
    assert result.full.mean_max_sector_weight == pytest.approx(0.75, abs=TOL)


# --- delisting ----------------------------------------------------------------------------------


def test_a_delisted_holding_is_liquidated_at_its_last_close() -> None:
    frame = bars(drop=frozenset({(B, D3), (B, D4), (B, D5)}))
    ranks = {D1: {A: 1, B: 2, C: 3}, D3: {A: 1, C: 2}}
    result = run(no_buffer(), frame=frame, ranks=ranks)
    # Tolerance 0: B has no close on D3 -> sold at its last close 20.
    #   notional 0.0249375 * 20 = 0.49875; cost 0.001246875; cash 0.497503125.
    delisted = [t for t in result.trades if t.reason is TradeReason.DELISTED]
    assert [(t.date, t.instrument_id, t.side, t.price) for t in delisted] == [
        (D3, B, TradeSide.SELL, 20.0)
    ]
    assert delisted[0].notional == pytest.approx(0.49875, abs=TOL)
    assert delisted[0].cost == pytest.approx(0.001246875, abs=TOL)
    # D3 NAV = A 0.5985 + cash 0.497503125.
    assert nav_on(result, D3) == pytest.approx(1.096003125, abs=TOL)
    # Fill 2: B is no longer held, so it is neither held nor exited; A holds, C enters.
    record = result.rebalances[1]
    assert (record.holds, record.exits, record.entries) == ((A,), (), (C,))
    # Liquidation batch turnover = 0.49875 / 2 / 1.09725, the equity before (A 0.5985 + B 0.49875).
    fill2 = record.turnover
    years = 33 / 365.25
    expected = (0.5 + 0.49875 / 2 / 1.09725 + fill2) / years
    assert result.full.annual_turnover == pytest.approx(expected, abs=1e-8)


def test_a_gap_inside_the_tolerance_is_carried_not_sold() -> None:
    frame = bars(drop=frozenset({(B, D3)}))
    config = dataclasses.replace(no_buffer(), missing_bar_tolerance_sessions=1)
    result = run(config, frame=frame)
    assert not [t for t in result.trades if t.reason is TradeReason.DELISTED]
    # D3: B carried at its last close 20 -> 0.49875; A 0.5985.
    assert nav_on(result, D3) == pytest.approx(1.09725, abs=TOL)


def test_a_held_name_with_no_open_at_the_fill_is_frozen_and_keeps_its_slot() -> None:
    frame = bars(drop=frozenset({(B, D4)}))
    config = dataclasses.replace(no_buffer(), missing_bar_tolerance_sessions=1)
    result = run(config, frame=frame)
    # Fill 2 at D4: B has no open -> frozen at its last close 22: 0.0249375 * 22 = 0.548625.
    #   E = A 0.5985 + B 0.548625 = 1.147125. One slot is left for selection: A (rank 1) holds,
    #   C (rank 2) is refused FULL. A is trimmed to 1.147125 / 2 = 0.5735625: sells 0.0249375.
    #   cost = 0.0025 * 0.0249375 = 0.00006234375; E' = 1.14706265625;
    #   A holds E'/2 = 0.573531328125; cash = E' - 0.573531328125 - 0.548625 = 0.024906328125.
    record = result.rebalances[1]
    assert record.frozen == (B,)
    assert (record.holds, record.exits, record.entries) == ((A,), (), ())
    assert record.cost == pytest.approx(0.00006234375, abs=TOL)
    assert record.cash_weight == pytest.approx(0.024906328125 / 1.14706265625, abs=TOL)
    assert not [t for t in result.trades if t.instrument_id == B and t.date == D4]
    # D4 close: A 0.573531328125 (close 12 = fill open); B carried at 22; cash.
    assert nav_on(result, D4) == pytest.approx(1.14706265625, abs=TOL)


# --- look-ahead ---------------------------------------------------------------------------------


def test_a_future_price_cannot_alter_an_earlier_decision() -> None:
    base = run(no_buffer())
    # Rewrite every price after the first fill's open, wildly, and the second signal's list.
    future = {
        instrument: [
            (day, o, c) if day < D2 else (day, o if day == D2 else o * 7, c * 13)
            for day, o, c in series
        ]
        for instrument, series in PRICES.items()
    }
    changed = run(no_buffer(), frame=bars(future), ranks={D1: BASE_SIGNALS[D1], D3: {C: 1, B: 2}})
    assert changed.rebalances[0] == base.rebalances[0]
    assert [t for t in changed.trades if t.date <= D2] == [t for t in base.trades if t.date <= D2]
    assert [p for p in changed.nav if p.date < D2] == [p for p in base.nav if p.date < D2]


def test_prices_after_a_fill_cannot_alter_that_fill() -> None:
    base = run(no_buffer())
    future = {**PRICES, C: [*PRICES[C][:3], (D4, 50, 500), (D5, 1, 1)]}
    changed = run(no_buffer(), frame=bars(future))
    # D4's open is unchanged; its close and everything later are not. Both fills are identical.
    assert changed.rebalances == base.rebalances
    assert changed.trades == base.trades
    assert [p for p in changed.nav if p.date < D4] == [p for p in base.nav if p.date < D4]


def test_a_signal_off_the_calendar_is_refused() -> None:
    panel = build_price_panel(bars())
    with pytest.raises(ValueError, match="not a session"):
        simulate_monthly_rebalance(panel, signals({dt.date(2024, 2, 2): {A: 1}}), no_buffer())


# --- calendar, per-year, IS / OOS -----------------------------------------------------------------


def test_monthly_signal_dates_are_confirmed_month_ends() -> None:
    sessions = [dt.date(2024, 1, 30), D1, D2, D3, D4, D5]
    # March has no later session, so its end is not known yet.
    assert monthly_signal_dates(sessions) == (D1, D3)


def test_per_year_returns_and_the_in_out_of_sample_split() -> None:
    y1, y2, y3, y4 = (
        dt.date(2019, 12, 30),
        dt.date(2019, 12, 31),
        dt.date(2020, 1, 2),
        dt.date(2020, 1, 31),
    )
    frame = pd.DataFrame(
        [
            {"date": y1, "instrument_id": A, "open": 100, "close": 100},
            {"date": y2, "instrument_id": A, "open": 100, "close": 110},
            {"date": y3, "instrument_id": A, "open": 110, "close": 121},
            {"date": y4, "instrument_id": A, "open": 121, "close": 133.1},
        ]
    )
    ranks = pd.DataFrame(
        [
            {"date": y1, "instrument_id": A, "quality_rank": 1},
            {"date": y4, "instrument_id": A, "quality_rank": 1},
        ]
    )
    config = ValidationConfig(max_names=1, entry_rank=1, retention_rank=1)
    result = simulate_monthly_rebalance(build_price_panel(frame), ranks, config)
    # Signal on the panel's last session has nothing to fill at.
    assert result.dropped_signal_dates == (y4,)
    # Fill at y2 open 100: E=1, notional 1, cost 0.0025, E'=0.9975, shares 0.009975.
    # NAV: y1 1.0; y2 1.09725; y3 1.206975; y4 1.3276725.
    assert [p.nav for p in result.nav] == pytest.approx(
        [1.0, 1.09725, 1.206975, 1.3276725], abs=TOL
    )
    # 2019: 1.09725 / 1 - 1; 2020: 1.3276725 / 1.09725 - 1 = 133.1/110 - 1 = 0.21.
    assert [y.year for y in result.per_year] == [2019, 2020]
    assert [y.return_net for y in result.per_year] == pytest.approx([0.09725, 0.21], abs=TOL)
    # IS: y1 -> y2 (1 day), one rebalance with turnover 0.5.
    assert result.in_sample is not None
    assert (result.in_sample.start, result.in_sample.end) == (y1, y2)
    assert result.in_sample.total_return == pytest.approx(0.09725, abs=TOL)
    assert result.in_sample.annual_turnover == pytest.approx(0.5 / (1 / 365.25), abs=1e-6)
    # OOS: base is y2's NAV, runs to y4 (31 days), no rebalance, no turnover.
    assert result.out_of_sample is not None
    assert (result.out_of_sample.start, result.out_of_sample.end) == (y2, y4)
    assert result.out_of_sample.total_return == pytest.approx(0.21, abs=TOL)
    assert result.out_of_sample.cagr_net == pytest.approx(1.21 ** (365.25 / 31) - 1, abs=1e-6)
    assert result.out_of_sample.annual_turnover == 0.0
    assert result.out_of_sample.rebalances == 0
    row = summary_row("base", result)
    assert row["model"] == "base"
    assert row["oos_cagr_net"] == result.out_of_sample.cagr_net
    assert row["return_2020"] == result.per_year[1].return_net


def test_outputs_are_rounded_to_ratio_precision() -> None:
    result = run(no_buffer())
    values = [p.nav for p in result.nav] + [t.notional for t in result.trades]
    assert all(round(value, 10) == value for value in values)


# --- law 1 --------------------------------------------------------------------------------------

MODULE = Path(__file__).resolve().parents[1] / "src" / "baskfy_core" / "ranking_validation.py"

FORBIDDEN = (
    re.compile(r"^\s*(from|import)\s+(sqlalchemy|httpx|requests|asyncpg|psycopg|redis|celery)"),
    re.compile(r"^\s*(from|import)\s+(os|pathlib|socket|subprocess|asyncio|io)\b"),
    re.compile(r"\.(now|today|utcnow)\(\)"),
    re.compile(r"\bopen\("),
    re.compile(r"\b(read_csv|read_parquet|write_csv|to_csv)\b"),
    re.compile(r"baskfy_execution|kite_client|place_order"),
)


def test_law1_validation_touches_nothing() -> None:
    offenders = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(MODULE.read_text(encoding="utf-8").splitlines(), start=1)
        for pattern in FORBIDDEN
        if pattern.search(line)
    ]
    assert offenders == []
