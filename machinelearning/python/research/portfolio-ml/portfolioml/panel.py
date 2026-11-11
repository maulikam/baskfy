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
    return bars_frame(o)


def bars_frame(o: pl.DataFrame) -> pl.DataFrame:
    """ohlcv_daily rows → the panel: thin sessions dropped, turnover on the raw print."""
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
    ins = ins.with_columns(pl.col("listed_on").str.to_date(strict=False), pl.col("delisted_on").str.to_date(strict=False))
    return instruments_frame(ins, members, defs)


def instruments_frame(ins: pl.DataFrame, members: pl.DataFrame, defs: pl.DataFrame) -> pl.DataFrame:
    """instrument rows (dates parsed) + ETF membership → instrument_id, symbol, eligible, ..."""
    etf_id = defs.filter(pl.col("slug") == "etf")["id"].to_list()
    etf_ids = set(members.filter(pl.col("index_id").is_in(etf_id))["instrument_id"].to_list())
    return ins.select(
        pl.col("id").alias("instrument_id"), "symbol", "name", "instrument_type", "series",
        "listed_on", "delisted_on",
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
    return index_frame(pl.read_csv(data / "index_series.csv").with_columns(pl.col("date").str.to_date()))


def index_frame(isr: pl.DataFrame) -> pl.DataFrame:
    """index_name, date, close rows → wide on date."""
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


def load_fundamentals(data: Path) -> pl.DataFrame:
    """fundamental_pit export; ``available_on`` is the only date a feature may join on."""
    numeric = ["revenue", "ebitda", "depreciation", "pat", "total_debt", "total_equity", "cfo"]
    return pl.read_csv(
        data / "fundamental_pit.csv.gz", schema_overrides=dict.fromkeys(numeric, pl.Float64)
    ).with_columns(
        pl.col("period_end").str.to_date(), pl.col("available_on").str.to_date(),
        pl.col("filed_at").str.to_datetime(),
    )


def load_shareholding(data: Path) -> pl.DataFrame:
    """shareholding_pit export; ``available_on`` is the only date a feature may join on."""
    pct = ["promoter_pct", "promoter_pledged_pct", "fii_pct", "dii_pct", "mf_pct", "public_pct"]
    return pl.read_csv(
        data / "shareholding_pit.csv.gz", schema_overrides=dict.fromkeys(pct, pl.Float64)
    ).with_columns(
        pl.col("period_end").str.to_date(), pl.col("available_on").str.to_date(),
        pl.col("filed_at").str.to_datetime(),
    )


def load_sector(data: Path) -> pl.DataFrame:
    """instrument_sector export. Static across history: a known small leak (PM3.1)."""
    return pl.read_csv(data / "instrument_sector.csv.gz").select("instrument_id", "sector")


def load_fundamental_daily(data: Path) -> pl.DataFrame:
    """fundamental_daily export; ``marketcap_cr`` is the check on ours, never a feature."""
    return pl.read_csv(data / "fundamental_daily.csv.gz").with_columns(pl.col("date").str.to_date())


def load_terminal(data: Path) -> pl.DataFrame:
    """instrument_terminal export: how a name stopped. A name with no row is alive."""
    return pl.read_csv(data / "instrument_terminal.csv.gz").select("instrument_id", pl.col("status").alias("terminal"))


def load_index_membership(data: Path) -> pl.DataFrame:
    """Point-in-time membership flags where the export has them (2021-08 →); null before."""
    members = pl.read_csv(data / "index_member_daily.csv.gz").with_columns(pl.col("date").str.to_date())
    defs = pl.read_csv(data / "index_def.csv.gz")
    want = {"nifty-500": "in_n500", "nifty-smallcap-250": "in_sc250", "nifty-midcap-150": "in_mc150", "nifty-fno": "in_fno"}
    ids = defs.filter(pl.col("slug").is_in(list(want))).select("id", pl.col("slug").replace_strict(want).alias("flag"))
    m = members.join(ids, left_on="index_id", right_on="id").with_columns(pl.lit(True).alias("v"))
    return m.pivot(on="flag", index=["instrument_id", "date"], values="v").fill_null(False)
