"""AF lane E — core desk-math regressions."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from baskfy_core.basket import build_plan
from baskfy_core.factors import _cutler_rsi
from baskfy_core.score import apply_filters
from baskfy_core.windows import FactorWindow, bars_required, subtract_months


def _cfg(**kw: object) -> SimpleNamespace:
    base = dict(
        EXCLUDED_SYMBOLS=frozenset(),
        REJECT_SERIES=frozenset({"BE"}),
        MIN_MEDIAN_DAILY_VALUE=5e7,
        MAX_AWAY_FROM_HIGH=-30.0,
        MAX_CIRCUITS_3M=5,
        PENALTY_CIRCUITS_1Y=8,
        MOMENTUM_BLEND={"one_month": 0.1},
        SHARPE_BLEND={"three_months": 1.0},
        STOP_VOL_MULT=2.2,
        STOP_MIN=0.08,
        STOP_MAX=0.12,
        CASH_BANDS=[(0.0, 20.0)],
        CLUSTER_CAP=25.0,
        FULLY_INVESTED=True,
        HALF_SIZE_WEIGHT=3.0,
        MAX_POS_VS_DAY_VALUE=0.01,
        MAX_SINGLE_WEIGHT=15.0,
        MAX_TRADE_COST_PCT=1.0,
        MIN_POSITION_WEIGHT=6.0,
        MIN_TRADE_PCT=0.5,
        MIN_TRADE_VALUE=5000.0,
        PARABOLIC_RSI=82.0,
        REPLACEMENT_EDGE=5.0,
        RETENTION_BUFFER=5,
        RUNNER_MAX_VALUE=50_000.0,
        TARGET_POSITIONS=(2, 5),
        TRIM_TO_WEIGHT=3.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _row(**kw: object) -> dict[str, object]:
    row = dict(
        symbol="AAA",
        series="EQ",
        date="2026-08-18",
        close=100.0,
        ma_50=90.0,
        ma_200=80.0,
        absolute_return_three_months=5.0,
        absolute_return_six_months=10.0,
        circuits_three_months=0,
        median_volume_one_year=1e8,
        away_from_high_one_year=-5.0,
    )
    row.update(kw)
    return row


# --- 3.8 -------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "column",
    ["median_volume_one_year", "ma_200", "away_from_high_one_year"],
)
def test_null_in_a_filtered_column_is_rejected(column: str) -> None:
    frame = pd.DataFrame([_row(**{column: np.nan})])
    out = apply_filters(frame, _cfg())
    assert out.loc[0, "reject"] != ""


# --- 3.9 -------------------------------------------------------------------------------
def test_bars_required_is_n_not_n_plus_one() -> None:
    window = FactorWindow(
        months=1,
        as_of=dt.date(2026, 8, 18),
        calendar_start=dt.date(2026, 7, 18),
        start=dt.date(2026, 7, 20),
        trading_days=tuple(dt.date(2026, 7, 20) + dt.timedelta(days=i) for i in range(22)),
        calendar_first=dt.date(2011, 1, 1),
    )
    # Force length via object — FactorWindow.length is len(trading_days)
    assert bars_required(window) == window.length
    assert bars_required(window) == 22


def test_rsi_skips_a_window_that_contains_a_gap() -> None:
    gains = np.array([[1.0], [np.nan], [1.0], [1.0]], dtype=float)
    losses = np.array([[0.0], [np.nan], [0.0], [0.0]], dtype=float)
    rsi = _cutler_rsi(gains, losses, period=2)
    # The window ending at index 2 includes the gap at index 1 → NaN
    assert np.isnan(rsi[2, 0])


def test_half_size_caps_a_young_listing() -> None:
    as_of = dt.date(2026, 8, 18)
    young = as_of - dt.timedelta(days=30)
    scored = pd.DataFrame(
        [
            {
                **_row(symbol="YOUNG"),
                "SCORE": 80.0,
                "rank": 1,
                "reject": "",
                "rsi_one_month": 50.0,
                "volatility_one_year": 0.3,
                "ma_20": 95.0,
                "ma_100": 85.0,
                "listed_on": young,
            },
            {
                **_row(symbol="OLD"),
                "SCORE": 79.0,
                "rank": 2,
                "reject": "",
                "rsi_one_month": 50.0,
                "volatility_one_year": 0.3,
                "ma_20": 95.0,
                "ma_100": 85.0,
                "listed_on": subtract_months(as_of, 24),
            },
        ]
    )
    plan = build_plan(
        scored,
        holdings=[],
        cash=1_000_000.0,
        cfg=_cfg(TARGET_POSITIONS=(2, 2), MIN_POSITION_WEIGHT=1.0),
        tradeable=lambda _s: True,
        live_prices={"YOUNG": 100.0, "OLD": 100.0},
    )
    weights = {o["symbol"]: float(o["weight"]) for o in plan["orders"] if o.get("delta", 0) > 0}
    assert weights.get("YOUNG", 99) <= 3.0 + 1e-9
