"""Features at the close of session t, from data ≤ t only. Frames in, frames out.

Per-name time-series features, market features from the benchmark closes, breadth computed from
the panel itself, and cross-sectional percentile ranks per date across the tradable universe
(the model is a ranker: *which names today*, not *how much*)."""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

G = pl.col("instrument_id")


@dataclass(frozen=True)
class UniverseConfig:
    min_turnover_20d: float = 2e7  # ₹2 cr average daily traded value
    min_close_raw: float = 20.0
    min_history: int = 260  # sessions of bars before a name may be scored


def _ret(n: int) -> pl.Expr:
    return (pl.col("close") / pl.col("close").shift(n).over(G) - 1).alias(f"r{n}")


def name_features(bars: pl.DataFrame, ucfg: UniverseConfig = UniverseConfig()) -> pl.DataFrame:
    c, h, lo, v, tv = pl.col("close"), pl.col("high"), pl.col("low"), pl.col("volume"), pl.col("turnover")
    r1 = (c / c.shift(1).over(G) - 1)
    prev_c = c.shift(1).over(G)
    tr = pl.max_horizontal(h - lo, (h - prev_c).abs(), (lo - prev_c).abs())
    sma = lambda n: c.rolling_mean(n).over(G)  # noqa: E731
    out = bars.with_columns(
        r1.alias("r1"), _ret(5), _ret(21), _ret(63), _ret(126), _ret(252),
        (c.shift(21).over(G) / c.shift(252).over(G) - 1).alias("mom_12_1"),
        pl.len().over(G).alias("_n_total"),
        pl.int_range(pl.len()).over(G).alias("n_hist"),
    ).with_columns(
        pl.col("r1").rolling_std(20).over(G).alias("vol20"),
        pl.col("r1").rolling_std(60).over(G).alias("vol60"),
        (tr.rolling_mean(14).over(G) / c).alias("atr14_pct"),
        (v / v.rolling_mean(20).over(G)).alias("dvol1"),
        (v.rolling_mean(5).over(G) / v.rolling_mean(20).over(G)).alias("dvol5"),
        tv.rolling_mean(20).over(G).alias("turnover20"),
        (c / h.rolling_max(252).over(G) - 1).alias("dist_hi252"),
        (c / lo.rolling_min(252).over(G) - 1).alias("dist_lo252"),
        (c / h.rolling_max(63).over(G) - 1).alias("dist_hi63"),
        (c / sma(20) - 1).alias("px_sma20"),
        (c / sma(50) - 1).alias("px_sma50"),
        (c / sma(200) - 1).alias("px_sma200"),
        (sma(50) / sma(200) - 1).alias("sma50_200"),
        (pl.col("open") / prev_c - 1).alias("gap"),
        pl.when(h > lo).then((c - lo) / (h - lo)).otherwise(0.5).alias("range_pos"),
        (pl.col("r1") > 0).cast(pl.Float64).rolling_mean(20).over(G).alias("up_frac20"),
        pl.col("r1").rolling_max(20).over(G).alias("max_r1_20"),
        pl.col("r1").rolling_min(20).over(G).alias("min_r1_20"),
        (pl.col("r1").clip(lower_bound=0).rolling_mean(14).over(G)
         / (pl.col("r1").abs().rolling_mean(14).over(G) + 1e-12)).alias("rsi14"),
        ((h - lo) / c).rolling_mean(5).over(G).alias("range5_pct"),
        (c / h.shift(1).over(G).rolling_max(20).over(G) - 1).alias("dist_hi20"),
        (c / lo.shift(1).over(G).rolling_min(20).over(G) - 1).alias("dist_lo20"),
        (v.rolling_mean(5).over(G) / v.rolling_mean(50).over(G)).alias("vol5_50"),
        (tv.rolling_mean(20).over(G) / tv.rolling_mean(60).over(G)).alias("tv20_60"),
        (pl.col("r1").abs() / (tv + 1.0)).rolling_mean(20).over(G).log1p().alias("amihud20"),
        (pl.col("r1") > 0).cast(pl.Float64).rolling_mean(5).over(G).alias("up_frac5"),
        pl.col("close_raw").log().alias("log_px"),
        (c / h.cum_max().over(G) - 1).alias("dist_ath"),
        (c / c.shift(504).over(G) - 1).alias("r504"),
        (c / c.shift(756).over(G) - 1).alias("r756"),
        (h.rolling_max(252).over(G) / h.cum_max().over(G) - 1).alias("hi252_vs_ath"),
    ).with_columns(
        (pl.col("vol20") / pl.col("vol60")).alias("vol_ratio"),
        pl.col("turnover20").log1p().alias("log_turnover20"),
        (pl.col("r63") / (pl.col("vol60") * 63 ** 0.5 + 1e-9)).alias("vam63"),
        (pl.col("r126") - pl.col("r21")).alias("mom_6_1"),
    )
    return out.with_columns(
        (
            (pl.col("turnover20") >= ucfg.min_turnover_20d)
            & (pl.col("close_raw") >= ucfg.min_close_raw)
            & (pl.col("n_hist") >= ucfg.min_history)
        ).alias("liquid")
    ).drop("_n_total")


def market_features(index: pl.DataFrame) -> pl.DataFrame:
    """From the wide benchmark-close frame (date, nifty50, midcap150, smallcap250, mom50)."""
    cols = []
    for k in ("nifty50", "midcap150", "smallcap250"):
        c = pl.col(k)
        cols += [
            (c / c.shift(21) - 1).alias(f"{k}_r21"),
            (c / c.shift(63) - 1).alias(f"{k}_r63"),
            (c / c.rolling_mean(20) - 1).alias(f"{k}_vs_sma20"),
            (c / c.rolling_mean(50) - 1).alias(f"{k}_vs_sma50"),
            (c / c.shift(1) - 1).rolling_std(20).alias(f"{k}_vol20"),
            (c / c.rolling_max(252) - 1).alias(f"{k}_dd252"),
        ]
    return index.sort("date").with_columns(cols).drop("nifty50", "midcap150", "smallcap250", "mom50", strict=False)


def breadth(feat: pl.DataFrame) -> pl.DataFrame:
    """Share of the liquid universe above its 200/50-DMA, and the 20-day change in it."""
    b = (
        feat.filter(pl.col("liquid") & pl.col("eligible"))
        .group_by("date")
        .agg(
            (pl.col("px_sma200") > 0).mean().alias("breadth200"),
            (pl.col("px_sma50") > 0).mean().alias("breadth50"),
            (pl.col("r21") > 0).mean().alias("breadth_r21_pos"),
            (pl.col("dist_hi20") >= 0).mean().alias("breadth_new_hi20"),
            (pl.col("r1") > 0).mean().alias("breadth_up_day"),
            pl.len().alias("universe_n"),
        )
        .sort("date")
    )
    return b.with_columns((pl.col("breadth200") - pl.col("breadth200").shift(20)).alias("breadth200_chg20"))


RANKED = ["r5", "r21", "r63", "r126", "mom_12_1", "vol20", "atr14_pct", "dvol1", "dvol5", "dist_hi252",
          "px_sma20", "px_sma50", "px_sma200", "log_turnover20", "rs21", "rs63", "up_frac20", "gap",
          "dist_hi20", "vam63", "mom_6_1", "tv20_60", "vol5_50", "amihud20", "dist_ath", "r504", "r252"]


def cross_sectional(feat: pl.DataFrame) -> pl.DataFrame:
    """Percentile ranks per date within the tradable universe; relative strength vs NIFTY 50."""
    f = feat.with_columns(
        (pl.col("r21") - pl.col("nifty50_r21")).alias("rs21"),
        (pl.col("r63") - pl.col("nifty50_r63")).alias("rs63"),
    )
    univ = pl.col("liquid") & pl.col("eligible")
    # in place, no self-join: a masked column ranks only within the universe (nulls take no rank)
    masked = {k: pl.when(univ).then(pl.col(k)) for k in RANKED}
    ranks = [
        pl.when(univ).then(masked[k].rank("average").over("date") / masked[k].count().over("date")).alias(f"{k}_cs")
        for k in RANKED
    ]
    return f.with_columns(ranks)


NAME_FEATURES = [
    "r1", "r5", "r21", "r63", "r126", "r252", "mom_12_1", "vol20", "vol60", "vol_ratio", "atr14_pct",
    "dvol1", "dvol5", "log_turnover20", "dist_hi252", "dist_lo252", "dist_hi63", "px_sma20", "px_sma50",
    "px_sma200", "sma50_200", "gap", "range_pos", "up_frac20", "max_r1_20", "min_r1_20", "rsi14",
    "range5_pct", "rs21", "rs63", "dist_hi20", "dist_lo20", "vol5_50", "tv20_60", "amihud20", "up_frac5",
    "log_px", "vam63", "mom_6_1", "dist_ath", "r504", "r756", "hi252_vs_ath", "n_hist",
]
MARKET_FEATURES = [
    f"{k}_{s}" for k in ("nifty50", "midcap150", "smallcap250")
    for s in ("r21", "r63", "vs_sma20", "vs_sma50", "vol20", "dd252")
] + ["breadth200", "breadth50", "breadth_r21_pos", "breadth200_chg20", "breadth_new_hi20", "breadth_up_day"]
CS_FEATURES = [f"{k}_cs" for k in RANKED]
ALL_FEATURES = NAME_FEATURES + MARKET_FEATURES + CS_FEATURES
