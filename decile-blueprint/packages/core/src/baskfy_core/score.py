"""Momentum Quality Score /100 — the desk's strategy brain, moved to core at M15 (P3.1).

Spec: `kite-momentum-rebalancer/.claude/skills/momentum-rebalance/SKILL.md`. Keep the formulas in
sync with it; this file is a direct port of the validated pipeline and **every formula below is
byte-for-byte what the desk was traded on**.

Two things changed in the move, and only two:

* **Configuration is passed in, not imported.** The desk's `app/config.py` reads the environment
  at import, and core's first law is that it touches nothing (docs/04 §2). So the eleven constants
  this file needs arrive as `cfg` — anything carrying those attributes, which the desk's config
  module does by duck typing. `RET_COLS` and `REQUIRED` were module-level lists derived from
  `MOMENTUM_BLEND`, so they became `ret_cols(cfg)` and `required(cfg)`.
* **`load_scan` did not come.** It reads a CSV from disk, which makes it a boundary, and boundaries
  stay in `services/` or at the desk (docs/04 §2 again). It lives on in `app/scoring.py`, which is
  now that boundary plus a thin binding of the desk's config to these functions.

pandas, not Polars, and deliberately. docs/02 locks "Polars (primary) + NumPy; pandas only at
boundaries", and this is not a boundary — but M15's acceptance is that the moved code produce
**byte-identical** output over the M12 corpus, and a Polars port is a rewrite with its own
rounding. Recorded in docs/DECISIONS-MERGE.md M15.1 as a deviation to revisit deliberately, not a
detail to discover later.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol

import numpy as np
import numpy.typing as npt
import pandas as pd


class ScoringConfig(Protocol):
    """What `score` needs from configuration. The desk's `app.config` satisfies it as-is."""

    EXCLUDED_SYMBOLS: Collection[str]
    REJECT_SERIES: Collection[str]
    MIN_MEDIAN_DAILY_VALUE: float
    MAX_AWAY_FROM_HIGH: float
    MAX_CIRCUITS_3M: float
    PENALTY_CIRCUITS_1Y: float
    MOMENTUM_BLEND: dict[str, float]
    SHARPE_BLEND: dict[str, float]
    STOP_VOL_MULT: float
    STOP_MIN: float
    STOP_MAX: float


SHP_COLS = [
    f"sharpe_return_{w}"
    for w in ("one_month", "three_months", "six_months", "nine_months", "one_year")
]


def ret_cols(cfg: ScoringConfig) -> list[str]:
    """The return columns the blend names, in the blend's own order."""
    return [f"absolute_return_{w}" for w in cfg.MOMENTUM_BLEND]


def required(cfg: ScoringConfig) -> list[str]:
    """Every column a scan must carry. The desk's `load_scan` validates against this."""
    return [
        "symbol",
        "series",
        "date",
        "close",
        "marketcap",
        "median_volume_one_year",
        "rsi_one_month",
        "volatility_one_year",
        "beta",
        "circuits_three_months",
        "circuits_one_year",
        "positive_days_percent_three_months",
        "positive_days_percent_six_months",
        "ma_20",
        "ma_50",
        "ma_100",
        "ma_200",
        "away_from_high_one_year",
        "is_nifty_fno",
        *ret_cols(cfg),
        *SHP_COLS,
    ]


def _pct(s: pd.Series) -> pd.Series:
    return s.rank(pct=True) * 100


def audit(u: pd.DataFrame, cfg: ScoringConfig) -> dict[str, object]:
    return {
        "rows": len(u),
        "scan_date": sorted(u["date"].astype(str).unique()),
        "series_counts": u["series"].value_counts().to_dict(),
        "null_cells": int(u[required(cfg)].isna().sum().sum()),
        "breadth_above_20dma": round(float((u.close > u.ma_20).mean() * 100), 1),
        "breadth_above_50dma": round(float((u.close > u.ma_50).mean() * 100), 1),
        "positive_1m_pct": round(float((u.absolute_return_one_month > 0).mean() * 100), 1),
        "suspicious_sharpe_gt_8": u.loc[u.sharpe_return_one_year > 8, "symbol"].tolist(),
    }


def apply_filters(u: pd.DataFrame, cfg: ScoringConfig) -> pd.DataFrame:
    u = u.copy()
    u["reject"] = ""
    # NULL in a filtered column is a reject, not a pass. pandas comparisons with NaN are
    # False, so without an explicit isna() a missing median_volume / MA / away_from_high
    # used to sail through (AF 3.8; non-negotiable 7 wants NULL = rejected).
    u.loc[
        u.ma_50.isna()
        | u.ma_200.isna()
        | ((u.close < u.ma_50) & (u.close < u.ma_200)),
        "reject",
    ] += "below50&200DMA;"
    u.loc[(u.absolute_return_three_months < 0) & (u.absolute_return_six_months < 0), "reject"] += (
        "neg3M&6M;"
    )
    u.loc[u.circuits_three_months > cfg.MAX_CIRCUITS_3M, "reject"] += "circuits;"
    u.loc[
        u.median_volume_one_year.isna() | (u.median_volume_one_year < cfg.MIN_MEDIAN_DAILY_VALUE),
        "reject",
    ] += "illiquid;"
    u.loc[
        u.away_from_high_one_year.isna() | (u.away_from_high_one_year < cfg.MAX_AWAY_FROM_HIGH),
        "reject",
    ] += "far_from_high;"
    u.loc[u.series.isin(cfg.REJECT_SERIES), "reject"] += "T2T_series;"
    u.loc[u.symbol.isin(cfg.EXCLUDED_SYMBOLS), "reject"] += "excluded_instrument;"
    return u


def score(u: pd.DataFrame, cfg: ScoringConfig) -> pd.DataFrame:
    """Returns full universe with `reject` + eligible rows scored/ranked."""
    u = apply_filters(u, cfg)
    e = u[u.reject == ""].copy()
    if e.empty:
        u["SCORE"], u["rank"] = np.nan, np.nan
        return u

    for c in ret_cols(cfg) + SHP_COLS:
        lo, hi = e[c].quantile([0.01, 0.99])
        e[c] = e[c].clip(lo, hi)

    a = (
        4 * (e.close > e.ma_20)
        + 4 * (e.close > e.ma_50)
        + 4 * (e.close > e.ma_100)
        + 4 * (e.close > e.ma_200)
        + 5
        * ((e.close > e.ma_20) & (e.ma_20 > e.ma_50) & (e.ma_50 > e.ma_100) & (e.ma_100 > e.ma_200))
    )
    ext = (e.close / e.ma_20 - 1) * 100
    a = a + np.where(ext.between(0, 12), 4, np.where(ext.between(12, 20), 2, 0))
    e["A_trend"] = np.clip(a, 0, 25)

    # Annotated because the running total starts as a pandas Series and is then combined with
    # numpy arrays from np.where; the arithmetic is unchanged.
    b: pd.Series[float] | npt.NDArray[np.float64] | float = (
        sum(w * _pct(e[f"absolute_return_{k}"]) for k, w in cfg.MOMENTUM_BLEND.items()) / 100 * 25
    )
    b = b - np.where((e.absolute_return_one_month > 25) & (e.absolute_return_six_months < 40), 4, 0)
    b = b - np.where(e.absolute_return_one_month > 35, 3, 0)
    e["B_momentum"] = np.clip(b, 0, 25)

    cscore = sum(w * _pct(e[f"sharpe_return_{k}"]) for k, w in cfg.SHARPE_BLEND.items()) / 100 * 20
    e["C_sharpe"] = np.clip(cscore, 0, 20)

    d = (
        (
            0.4 * _pct(e.positive_days_percent_three_months)
            + 0.3 * _pct(e.positive_days_percent_six_months)
        )
        / 100
        * 6
    )
    d += np.where(
        e.away_from_high_one_year >= -8, 4, np.where(e.away_from_high_one_year >= -15, 2.5, 0)
    )
    e["D_consistency"] = np.clip(d, 0, 10)

    liq = _pct(e.median_volume_one_year) / 100 * 6
    liq += np.where(e.is_nifty_fno == 1, 2, 0)
    liq += np.where(e.marketcap > 20000, 2, np.where(e.marketcap > 10000, 1, 0))
    e["E_liquidity"] = np.clip(liq, 0, 10)

    f = np.zeros(len(e))
    f -= np.where(e.rsi_one_month > 82, 4, np.where(e.rsi_one_month > 78, 2, 0))
    f -= np.where(e.volatility_one_year > 0.55, 3, np.where(e.volatility_one_year > 0.45, 1.5, 0))
    f -= np.where(e.beta > 1.6, 2, 0)
    f -= np.where(e.circuits_one_year > cfg.PENALTY_CIRCUITS_1Y, 2, 0)
    f -= np.where(ext > 25, 3, np.where(ext > 18, 1.5, 0))
    e["F_penalty"] = np.maximum(f, -10)

    e["SCORE"] = (
        e.A_trend + e.B_momentum + e.C_sharpe + e.D_consistency + e.E_liquidity + e.F_penalty
    ).round(1)
    e["ext_over_20dma"] = ext.round(1)
    e = e.sort_values("SCORE", ascending=False).reset_index(drop=True)
    e["rank"] = e.index + 1
    out = u.merge(
        e[
            [
                "symbol",
                "SCORE",
                "rank",
                "ext_over_20dma",
                "A_trend",
                "B_momentum",
                "C_sharpe",
                "D_consistency",
                "E_liquidity",
                "F_penalty",
            ]
        ],
        on="symbol",
        how="left",
    )
    return out


def stop_from_vol(price: float, ann_vol: float, cfg: ScoringConfig) -> float:
    pct = float(np.clip(ann_vol / np.sqrt(52) * cfg.STOP_VOL_MULT, cfg.STOP_MIN, cfg.STOP_MAX))
    return round(price * (1 - pct), 1)
