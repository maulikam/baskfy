"""Load the Baskfy data-plant export into one long panel. The only module here that touches disk."""

from __future__ import annotations

from pathlib import Path

import polars as pl

MIN_NAMES_PER_SESSION = 800  # below this a session is a muhurat / special Saturday print
ETF_NAME_RE = r"(?i)\bETF\b|BEES\b|\bIETF\b|GOLDSHARE|LIQUID"


def load_bars(data: Path) -> pl.DataFrame:
    """ohlcv_daily export → long frame, thin sessions dropped, turnover in rupees on the raw print."""
    p = data / "ohlcv_daily.parquet"
    if p.exists():
        o = pl.read_parquet(p)
    else:
        o = pl.read_csv(
            data / "ohlcv_daily.csv.gz",
            schema_overrides={"upper_circuit": pl.Float64, "lower_circuit": pl.Float64},
        ).with_columns(pl.col("date").str.to_date())
        o.write_parquet(p)
    counts = o.group_by("date").agg(pl.len().alias("n"))
    good = counts.filter(pl.col("n") >= MIN_NAMES_PER_SESSION).select("date")
    o = o.join(good, on="date", how="semi")
    return (
        o.select(
            "instrument_id", "date", "open", "high", "low", "close", "volume",
            "close_raw", "volume_raw", "upper_circuit", "lower_circuit",
        )
        .with_columns((pl.col("close_raw") * pl.col("volume_raw")).alias("turnover"))
        .sort("instrument_id", "date")
    )


def load_instruments(data: Path) -> pl.DataFrame:
    ins = pl.read_csv(data / "instrument.csv.gz")
    members = pl.read_csv(data / "index_member_daily.csv.gz")
    defs = pl.read_csv(data / "index_def.csv.gz")
    etf_id = defs.filter(pl.col("slug") == "etf")["id"].to_list()
    etf_ids = set(members.filter(pl.col("index_id").is_in(etf_id))["instrument_id"].to_list())
    return ins.select(
        pl.col("id").alias("instrument_id"), "symbol", "name", "instrument_type", "series",
        pl.col("listed_on").str.to_date(strict=False), pl.col("delisted_on").str.to_date(strict=False),
    ).with_columns(
        (
            (pl.col("instrument_type") == "EQ")
            & pl.col("series").is_in(["EQ", "BE", "BZ"])
            & ~pl.col("instrument_id").is_in(list(etf_ids))
            & ~pl.col("name").str.contains(ETF_NAME_RE)
        ).alias("eligible")
    )


def load_index_series(data: Path) -> pl.DataFrame:
    """Benchmark closes as a wide frame on date: nifty50, midcap150, smallcap250, mom50."""
    isr = pl.read_csv(data / "index_series.csv").with_columns(pl.col("date").str.to_date())
    names = {
        "NIFTY 50": "nifty50", "NIFTY MIDCAP 150": "midcap150",
        "NIFTY SMLCAP 250": "smallcap250", "NIFTY 500 MOMENTUM 50": "mom50",
    }
    return (
        isr.filter(pl.col("index_name").is_in(list(names)))
        .with_columns(pl.col("index_name").replace_strict(names))
        .pivot(on="index_name", index="date", values="close")
        .sort("date")
    )


def load_index_membership(data: Path) -> pl.DataFrame:
    """Point-in-time membership flags where the export has them (2021-08 →); null before."""
    members = pl.read_csv(data / "index_member_daily.csv.gz").with_columns(pl.col("date").str.to_date())
    defs = pl.read_csv(data / "index_def.csv.gz")
    want = {"nifty-500": "in_n500", "nifty-smallcap-250": "in_sc250", "nifty-midcap-150": "in_mc150", "nifty-fno": "in_fno"}
    ids = defs.filter(pl.col("slug").is_in(list(want))).select("id", pl.col("slug").replace_strict(want).alias("flag"))
    m = members.join(ids, left_on="index_id", right_on="id").with_columns(pl.lit(True).alias("v"))
    return m.pivot(on="flag", index=["instrument_id", "date"], values="v").fill_null(False)
