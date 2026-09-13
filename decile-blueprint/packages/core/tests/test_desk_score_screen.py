"""Phase 1.2 — desk SCORE as Sort By: pure re-rank + ScreenDefinition rules."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

import pandas as pd
import pytest

from baskfy_core import ranking
from baskfy_core.factor_registry import FACTORS
from baskfy_core.ranking import DESK_SCORE_KEY, DESK_SCORE_EXPLAIN_COLUMNS
from baskfy_core.screen_definition import ExtraFactor, ScreenDefinition
from baskfy_core.screener import ScreenQueryError, build_screen_query, build_survivors_query
from baskfy_core.universes import UNIVERSE_BY_SLUG


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


def _row(symbol: str, **overrides: float | str | int) -> dict[str, float | str | int]:
    row: dict[str, float | str | int] = {
        "symbol": symbol,
        "instrument_id": abs(hash(symbol)) % 10_000,
        "series": "EQ",
        "date": "2026-08-18",
        "close": 100.0,
        "marketcap_cr": 50_000.0,
        "median_vol_12m": 100_000_000.0,
        "rsi_1m": 60.0,
        "vol_12m": 0.3,
        "beta_12m": 1.0,
        "circuits_3m": 0.0,
        "circuits_12m": 0.0,
        "pos_days_3m": 55.0,
        "pos_days_6m": 54.0,
        "ma_20": 95.0,
        "ma_50": 90.0,
        "ma_100": 85.0,
        "ma_200": 80.0,
        "away_high_1y": -5.0,
        "universe_mask": UNIVERSE_BY_SLUG["nifty-fno"].mask_value,
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
    row.update(overrides)
    return row


def test_desk_score_is_registered_and_computed() -> None:
    assert DESK_SCORE_KEY in FACTORS
    assert ranking.is_desk_score_factor(DESK_SCORE_KEY)
    assert FACTORS[DESK_SCORE_KEY].is_computed is True


def test_rerank_orders_by_score_and_attaches_explain_columns() -> None:
    cfg = _Cfg(REJECT_SERIES=("BE",))
    frame = pd.DataFrame(
        [
            _row("WEAK", ret_12m=10.0, sharpe_12m=0.5),
            _row("STRONG", ret_12m=40.0, sharpe_12m=3.0),
            _row("REJECT", series="BE"),
        ]
    )
    ordered, breakdowns = ranking.rerank_survivors_by_desk_score(frame, cfg=cfg)
    assert list(ordered["symbol"])[:2] == ["STRONG", "WEAK"]
    assert list(ordered["symbol"])[-1] == "REJECT"
    assert ordered.iloc[0]["sorting_factor"] >= ordered.iloc[1]["sorting_factor"]
    for key in DESK_SCORE_EXPLAIN_COLUMNS:
        assert key in ordered.columns
    by_symbol = {row.symbol: row for row in breakdowns}
    assert by_symbol["STRONG"].eligible is True
    assert by_symbol["STRONG"].a_trend is not None
    assert by_symbol["REJECT"].eligible is False


def test_rerank_derives_nifty_fno_from_universe_mask() -> None:
    frame = pd.DataFrame([_row("AAA")])
    assert "is_nifty_fno" not in frame.columns
    ordered, _ = ranking.rerank_survivors_by_desk_score(frame, cfg=_Cfg())
    assert int(ordered.iloc[0]["is_nifty_fno"]) == 1


def test_screen_definition_accepts_desk_score() -> None:
    definition = ScreenDefinition(index="nifty-500", sort_by=DESK_SCORE_KEY)
    assert definition.sort_by == DESK_SCORE_KEY


def test_screen_definition_rejects_desk_score_with_factor_two() -> None:
    with pytest.raises(ValueError, match="desk_score"):
        ScreenDefinition(
            index="nifty-500",
            sort_by=DESK_SCORE_KEY,
            factor_two=ExtraFactor(enabled=True, sort_by="ret_12m"),
        )


def test_build_screen_query_refuses_desk_score() -> None:
    definition = ScreenDefinition(index="nifty-500", sort_by=DESK_SCORE_KEY)
    with pytest.raises(ScreenQueryError, match="desk_score"):
        build_screen_query(definition, __import__("datetime").date(2026, 8, 18))


def test_build_survivors_query_requires_desk_score() -> None:
    definition = ScreenDefinition(index="nifty-500", sort_by="ret_12m")
    with pytest.raises(ScreenQueryError, match="desk_score"):
        build_survivors_query(definition, __import__("datetime").date(2026, 8, 18))


def test_build_survivors_query_emits_input_columns() -> None:
    import datetime as dt

    definition = ScreenDefinition(index="nifty-500", sort_by=DESK_SCORE_KEY)
    query = build_survivors_query(definition, dt.date(2026, 8, 18))
    assert query.sorting_factor.key == DESK_SCORE_KEY
    sql = query.sql()
    assert " AS survivors" in sql or "survivors" in sql
    assert "sorting_factor" not in sql
    for column in ("ret_12m", "universe_mask", "median_vol_12m"):
        assert column in query.row_columns
