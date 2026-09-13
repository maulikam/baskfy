"""Desk SCORE adapter — must call baskfy_core.score.score, not a second formula."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

import pandas as pd

from baskfy_core import ranking
from baskfy_core.score import score


@dataclass
class _Cfg:
    EXCLUDED_SYMBOLS: Collection[str] = ()
    REJECT_SERIES: Collection[str] = ()
    MIN_MEDIAN_DAILY_VALUE: float = 0.0
    MAX_AWAY_FROM_HIGH: float = -100.0
    MAX_CIRCUITS_3M: float = 99.0
    PENALTY_CIRCUITS_1Y: float = 99.0
    MOMENTUM_BLEND: dict[str, float] = field(
        default_factory=lambda: {
            "one_month": 0.2,
            "three_months": 0.2,
            "six_months": 0.2,
            "nine_months": 0.2,
            "one_year": 0.2,
        }
    )
    SHARPE_BLEND: dict[str, float] = field(
        default_factory=lambda: {
            "one_month": 0.2,
            "three_months": 0.2,
            "six_months": 0.2,
            "nine_months": 0.2,
            "one_year": 0.2,
        }
    )
    STOP_VOL_MULT: float = 2.0
    STOP_MIN: float = 0.08
    STOP_MAX: float = 0.25


CFG = _Cfg()


def _desk_row(symbol: str, **overrides: float | str | int) -> dict[str, float | str | int]:
    row: dict[str, float | str | int] = {
        "symbol": symbol,
        "series": "EQ",
        "date": "2026-08-18",
        "close": 100.0,
        "marketcap": 50_000.0,
        "median_volume_one_year": 100_000_000.0,
        "rsi_one_month": 60.0,
        "volatility_one_year": 0.3,
        "beta": 1.0,
        "circuits_three_months": 0.0,
        "circuits_one_year": 0.0,
        "positive_days_percent_three_months": 55.0,
        "positive_days_percent_six_months": 54.0,
        "ma_20": 95.0,
        "ma_50": 90.0,
        "ma_100": 85.0,
        "ma_200": 80.0,
        "away_from_high_one_year": -5.0,
        "is_nifty_fno": 1,
        "absolute_return_one_month": 5.0,
        "absolute_return_three_months": 12.0,
        "absolute_return_six_months": 20.0,
        "absolute_return_nine_months": 25.0,
        "absolute_return_one_year": 30.0,
        "sharpe_return_one_month": 0.5,
        "sharpe_return_three_months": 1.0,
        "sharpe_return_six_months": 1.5,
        "sharpe_return_nine_months": 1.8,
        "sharpe_return_one_year": 2.0,
    }
    row.update(overrides)
    return row


def test_adapter_matches_direct_score_call() -> None:
    frame = pd.DataFrame([_desk_row("AAA"), _desk_row("BBB", absolute_return_one_year=10.0)])
    direct = score(frame.copy(), CFG)
    via = ranking.score_universe(frame.copy(), CFG)
    assert list(via["SCORE"]) == list(direct["SCORE"])
    assert list(via["A_trend"]) == list(direct["A_trend"])


def test_adapter_renames_factor_daily_columns() -> None:
    """Screener rows speak factor_daily; the book speaks export names."""
    frame = pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "series": "EQ",
                "date": "2026-08-18",
                "close": 100.0,
                "marketcap_cr": 50_000,
                "median_vol_12m": 100_000_000,
                "rsi_1m": 60.0,
                "vol_12m": 0.3,
                "beta_12m": 1.0,
                "circuits_3m": 0,
                "circuits_12m": 0,
                "pos_days_3m": 55.0,
                "pos_days_6m": 54.0,
                "ma_20": 95.0,
                "ma_50": 90.0,
                "ma_100": 85.0,
                "ma_200": 80.0,
                "away_high_1y": -5.0,
                "is_nifty_fno": 1,
                "ret_1m": 5.0,
                "ret_3m": 12.0,
                "ret_6m": 20.0,
                "ret_9m": 25.0,
                "ret_12m": 30.0,
                "sharpe_1m": 0.5,
                "sharpe_3m": 1.0,
                "sharpe_6m": 1.5,
                "sharpe_9m": 1.8,
                "sharpe_12m": 2.0,
            }
        ]
    )
    scored = ranking.score_universe(frame, CFG)
    assert "SCORE" in scored.columns
    assert scored.loc[0, "SCORE"] == scored.loc[0, "SCORE"]  # not NaN path for eligible


def test_breakdown_exposes_a_to_f() -> None:
    cfg = _Cfg(REJECT_SERIES=("BE",))
    frame = pd.DataFrame([_desk_row("AAA"), _desk_row("ZZZ", series="BE")])
    scored = ranking.score_universe(frame, cfg)
    rows = ranking.breakdowns(scored)
    by_symbol = {row.symbol: row for row in rows}
    assert by_symbol["AAA"].eligible is True
    assert by_symbol["AAA"].a_trend is not None
    assert by_symbol["AAA"].b_momentum is not None
    assert by_symbol["AAA"].c_sharpe is not None
    assert by_symbol["AAA"].d_consistency is not None
    assert by_symbol["AAA"].e_liquidity is not None
    assert by_symbol["AAA"].f_penalty is not None
    assert by_symbol["ZZZ"].eligible is False
    assert "T2T" in by_symbol["ZZZ"].reject
