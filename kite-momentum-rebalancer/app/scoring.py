"""Momentum Quality Score /100 — direct port of the validated pipeline.
Spec: .claude/skills/momentum-rebalance/SKILL.md. Keep formulas in sync with it."""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from . import config as C

RET_COLS = [f"absolute_return_{w}" for w in C.MOMENTUM_BLEND]
SHP_COLS = [f"sharpe_return_{w}" for w in ("one_month", "three_months", "six_months", "nine_months", "one_year")]
REQUIRED = ["symbol", "series", "date", "close", "marketcap", "median_volume_one_year",
            "rsi_one_month", "volatility_one_year", "beta", "circuits_three_months",
            "circuits_one_year", "positive_days_percent_three_months",
            "positive_days_percent_six_months", "ma_20", "ma_50", "ma_100", "ma_200",
            "away_from_high_one_year", "is_nifty_fno"] + RET_COLS + SHP_COLS


def _pct(s: pd.Series) -> pd.Series:
    return s.rank(pct=True) * 100


def load_scan(path: str) -> pd.DataFrame:
    u = pd.read_csv(path, encoding="utf-8-sig")
    missing = [c for c in REQUIRED if c not in u.columns]
    if missing:
        raise ValueError(f"Scan CSV missing columns: {missing}")
    u = u.drop_duplicates(subset="symbol").reset_index(drop=True)
    return u


def audit(u: pd.DataFrame) -> dict:
    return {
        "rows": int(len(u)),
        "scan_date": sorted(u["date"].astype(str).unique()),
        "series_counts": u["series"].value_counts().to_dict(),
        "null_cells": int(u[REQUIRED].isna().sum().sum()),
        "breadth_above_20dma": round(float((u.close > u.ma_20).mean() * 100), 1),
        "breadth_above_50dma": round(float((u.close > u.ma_50).mean() * 100), 1),
        "positive_1m_pct": round(float((u.absolute_return_one_month > 0).mean() * 100), 1),
        "suspicious_sharpe_gt_8": u.loc[u.sharpe_return_one_year > 8, "symbol"].tolist(),
    }


def apply_filters(u: pd.DataFrame) -> pd.DataFrame:
    u = u.copy()
    u["reject"] = ""
    u.loc[(u.close < u.ma_50) & (u.close < u.ma_200), "reject"] += "below50&200DMA;"
    u.loc[(u.absolute_return_three_months < 0) & (u.absolute_return_six_months < 0), "reject"] += "neg3M&6M;"
    u.loc[u.circuits_three_months > C.MAX_CIRCUITS_3M, "reject"] += "circuits;"
    u.loc[u.median_volume_one_year < C.MIN_MEDIAN_DAILY_VALUE, "reject"] += "illiquid;"
    u.loc[u.away_from_high_one_year < C.MAX_AWAY_FROM_HIGH, "reject"] += "far_from_high;"
    u.loc[u.series.isin(C.REJECT_SERIES), "reject"] += "T2T_series;"
    u.loc[u.symbol.isin(C.EXCLUDED_SYMBOLS), "reject"] += "excluded_instrument;"
    return u


def score(u: pd.DataFrame) -> pd.DataFrame:
    """Returns full universe with `reject` + eligible rows scored/ranked."""
    u = apply_filters(u)
    e = u[u.reject == ""].copy()
    if e.empty:
        u["SCORE"], u["rank"] = np.nan, np.nan
        return u

    for c in RET_COLS + SHP_COLS:
        lo, hi = e[c].quantile([.01, .99])
        e[c] = e[c].clip(lo, hi)

    a = (4 * (e.close > e.ma_20) + 4 * (e.close > e.ma_50)
         + 4 * (e.close > e.ma_100) + 4 * (e.close > e.ma_200)
         + 5 * ((e.close > e.ma_20) & (e.ma_20 > e.ma_50)
                & (e.ma_50 > e.ma_100) & (e.ma_100 > e.ma_200)))
    ext = (e.close / e.ma_20 - 1) * 100
    a = a + np.where(ext.between(0, 12), 4, np.where(ext.between(12, 20), 2, 0))
    e["A_trend"] = np.clip(a, 0, 25)

    b = sum(w * _pct(e[f"absolute_return_{k}"]) for k, w in C.MOMENTUM_BLEND.items()) / 100 * 25
    b -= np.where((e.absolute_return_one_month > 25) & (e.absolute_return_six_months < 40), 4, 0)
    b -= np.where(e.absolute_return_one_month > 35, 3, 0)
    e["B_momentum"] = np.clip(b, 0, 25)

    cscore = sum(w * _pct(e[f"sharpe_return_{k}"]) for k, w in C.SHARPE_BLEND.items()) / 100 * 20
    e["C_sharpe"] = np.clip(cscore, 0, 20)

    d = (.4 * _pct(e.positive_days_percent_three_months)
         + .3 * _pct(e.positive_days_percent_six_months)) / 100 * 6
    d += np.where(e.away_from_high_one_year >= -8, 4,
                  np.where(e.away_from_high_one_year >= -15, 2.5, 0))
    e["D_consistency"] = np.clip(d, 0, 10)

    liq = _pct(e.median_volume_one_year) / 100 * 6
    liq += np.where(e.is_nifty_fno == 1, 2, 0)
    liq += np.where(e.marketcap > 20000, 2, np.where(e.marketcap > 10000, 1, 0))
    e["E_liquidity"] = np.clip(liq, 0, 10)

    f = np.zeros(len(e))
    f -= np.where(e.rsi_one_month > 82, 4, np.where(e.rsi_one_month > 78, 2, 0))
    f -= np.where(e.volatility_one_year > .55, 3, np.where(e.volatility_one_year > .45, 1.5, 0))
    f -= np.where(e.beta > 1.6, 2, 0)
    f -= np.where(e.circuits_one_year > C.PENALTY_CIRCUITS_1Y, 2, 0)
    f -= np.where(ext > 25, 3, np.where(ext > 18, 1.5, 0))
    e["F_penalty"] = np.maximum(f, -10)

    e["SCORE"] = (e.A_trend + e.B_momentum + e.C_sharpe + e.D_consistency
                  + e.E_liquidity + e.F_penalty).round(1)
    e["ext_over_20dma"] = ext.round(1)
    e = e.sort_values("SCORE", ascending=False).reset_index(drop=True)
    e["rank"] = e.index + 1
    out = u.merge(e[["symbol", "SCORE", "rank", "ext_over_20dma",
                     "A_trend", "B_momentum", "C_sharpe", "D_consistency",
                     "E_liquidity", "F_penalty"]], on="symbol", how="left")
    return out


def stop_from_vol(price: float, ann_vol: float) -> float:
    pct = float(np.clip(ann_vol / np.sqrt(52) * C.STOP_VOL_MULT, C.STOP_MIN, C.STOP_MAX))
    return round(price * (1 - pct), 1)


if __name__ == "__main__":
    df = score(load_scan(sys.argv[1]))
    cols = ["rank", "symbol", "SCORE", "close", "rsi_one_month",
            "away_from_high_one_year", "ext_over_20dma", "reject"]
    print(df.sort_values("rank").head(30)[cols].to_string(index=False))
