"""Named presets + desk_quality validation vs baskfy_core.score."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

import pandas as pd
import pytest

from baskfy_core.ranking import DESK_SCORE_KEY, RANKING_PRESETS, rerank_survivors_by_desk_score
from baskfy_core.ranking_presets import PRESET_SPECS, apply_preset, list_presets
from baskfy_core.score import score
from baskfy_core.screen_definition import ScreenDefinition


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
        "instrument_id": hash(symbol) % 10_000,
        "universe_mask": 1,
    }
    row.update(overrides)
    return row


def test_preset_specs_cover_ranking_presets() -> None:
    assert set(PRESET_SPECS) == set(RANKING_PRESETS)


def test_desk_quality_patch_is_desk_score() -> None:
    patch = apply_preset("desk_quality")
    assert patch["sort_by"] == DESK_SCORE_KEY
    definition = ScreenDefinition.model_validate(
        {"index": "nifty-500", "sort_by": "ret_12m", **patch}
    )
    assert definition.sort_by == DESK_SCORE_KEY
    assert definition.ranking_mode == "single"


def test_nse_momentum_refused() -> None:
    with pytest.raises(KeyError, match="nse_momentum"):
        apply_preset("nse_momentum")


def test_list_presets_exposes_sort_by() -> None:
    rows = list_presets()
    assert {r["key"] for r in rows} == set(RANKING_PRESETS)
    desk = next(r for r in rows if r["key"] == "desk_quality")
    assert desk["sort_by"] == DESK_SCORE_KEY
    assert desk["status"] == "ready"


def test_desk_quality_ordering_matches_score() -> None:
    """Validation harness: preset path order == direct score() order."""
    cfg = _Cfg()
    frame = pd.DataFrame(
        [
            _desk_row("AAA", absolute_return_one_year=40.0),
            _desk_row("BBB", absolute_return_one_year=10.0),
            _desk_row("CCC", absolute_return_one_year=25.0),
        ]
    )
    direct = score(frame.copy(), cfg).sort_values("SCORE", ascending=False, kind="mergesort")
    ordered, _ = rerank_survivors_by_desk_score(frame.copy(), cfg=cfg, direction="desc")
    eligible = ordered.loc[ordered["desk_eligible"]]
    assert list(eligible["symbol"]) == list(direct.loc[direct["reject"].fillna("") == ""]["symbol"])
