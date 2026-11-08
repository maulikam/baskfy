"""PM3: market cap and sector features. Frames in, frames out.

STATIC_SECTOR_FEATURES carry a known small leak (DECISIONS-PM PM3.1): today's classification is
applied to history."""

from __future__ import annotations

import polars as pl

MCAP_FEATURES = ["log_mcap", "mcap_decile"]
STATIC_SECTOR_FEATURES = ["sector_r21", "sector_r63", "rel_sector_r21", "rel_sector_r63"]
FEATURES = [*MCAP_FEATURES, *STATIC_SECTOR_FEATURES]


def join_mcap(panel: pl.DataFrame, filings: pl.DataFrame) -> pl.DataFrame:
    """mcap_cr = close_raw × shares from the newest filing with ``available_on <= date``."""
    shares = (
        filings.filter(pl.col("shares_outstanding").is_not_null())
        .sort("available_on", "filed_at")
        .unique(["instrument_id", "available_on"], keep="last")
        .select("instrument_id", "available_on", "shares_outstanding")
        .sort("available_on")
    )
    return (
        panel.sort("date")
        .join_asof(shares, left_on="date", right_on="available_on", by="instrument_id", strategy="backward")
        .with_columns((pl.col("close_raw") * pl.col("shares_outstanding") / 1e7).alias("mcap_cr"))
        .drop("available_on", "shares_outstanding")
    )


def mcap_features(panel: pl.DataFrame) -> pl.DataFrame:
    """log_mcap, and the day's decile within the frame it is given (1 smallest, 10 largest)."""
    return panel.with_columns(
        pl.col("mcap_cr").log().alias("log_mcap"),
        ((pl.col("mcap_cr").rank("ordinal").over("date") - 1) * 10 / pl.col("mcap_cr").count().over("date"))
        .floor().cast(pl.Float32).add(1).alias("mcap_decile"),
    )


def sector_features(panel: pl.DataFrame) -> pl.DataFrame:
    """Equal-weight sector momentum and each name's return over it; null without a sector."""
    known = pl.col("sector").is_not_null()
    return panel.with_columns(
        pl.when(known).then(pl.col("r21").mean().over("date", "sector")).alias("sector_r21"),
        pl.when(known).then(pl.col("r63").mean().over("date", "sector")).alias("sector_r63"),
    ).with_columns(
        (pl.col("r21") - pl.col("sector_r21")).alias("rel_sector_r21"),
        (pl.col("r63") - pl.col("sector_r63")).alias("rel_sector_r63"),
    )


def mcap_check(panel: pl.DataFrame, daily: pl.DataFrame) -> dict:
    """Median |ours / fundamental_daily − 1| where both exist; ``marketcap_cr`` is the check only."""
    both = panel.join(daily, on=["instrument_id", "date"]).filter(
        pl.col("mcap_cr").is_not_null() & pl.col("marketcap_cr").is_not_null() & (pl.col("marketcap_cr") > 0)
    )
    return {
        "rows": both.height,
        "median_rel_error": None if both.is_empty() else both.select(
            (pl.col("mcap_cr") / pl.col("marketcap_cr") - 1).abs().median()
        ).item(),
    }


def sector_coverage(universe: pl.DataFrame) -> float:
    """Share of universe names with a sector (PM3 AC)."""
    names = universe.select("instrument_id", "sector").unique("instrument_id")
    return names["sector"].is_not_null().mean()
