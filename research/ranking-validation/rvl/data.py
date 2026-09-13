"""Load the AWS export (research/volume-breakout/aws/) the way the volume-breakout study does.

Conventions carried over from ``research/volume-breakout/vbt/data.py`` (imported, not copied, where
they are constants):

* Universe of instruments: ``instrument_type == EQ``, current ``series`` in EQ/BE/BZ (a NULL
  series is a delisted name and is kept, so the history is survivorship-free); SME series out.
* ETFs out: members of the ``etf`` index plus vbt's narrow name/symbol regexes.
* Special sessions (muhurat / test sessions where a few hundred names printed) are dropped from the
  bars *and* the calendar: fewer than 25% of the bars of the surrounding 41-session median.

Memory: the CSV is read once, projected to the columns C8 needs, and written to
``cache/bars.parquet``; every later read is a projected, predicate-pushed parquet scan. Prices and
volumes stay Float64 — float32 cannot hold a 6-digit price to 4 dp or a volume above 16.7M exactly,
and the factor engine would then read different numbers than the nightly does. Ids and circuit
bands are downcast.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import polars as pl

HERE = Path(__file__).resolve().parent.parent
RESEARCH = HERE.parent
AWS = RESEARCH / "volume-breakout" / "aws"
#: desk.index_series (NIFTY 50 / Midcap 150 / Smallcap 250 / NIFTY 500 Momentum 50), from the
#: 22 Aug dump — the same file volume-breakout's gate study reads.
INDEX_SERIES = RESEARCH / "volume-breakout" / "data" / "index_series.csv"
CACHE = HERE / "cache"
OUT = HERE / "out"

sys.path.insert(0, str(RESEARCH / "volume-breakout"))
from vbt.data import ETF_NAME_RE, ETF_SYMBOL_RE  # noqa: E402

#: vbt.data.load_panel: a session with fewer than this share of its neighbourhood's bars is special.
THIN_SESSION_SHARE = 0.25
THIN_SESSION_WINDOW = 41

#: C8: "trailing 63-session median traded value >= Rs 5 cr".
UNIVERSE_MEDIAN_SESSIONS = 63
UNIVERSE_MIN_TRADED_VALUE_INR = 5e7

F64 = pl.Float64
BAR_SCHEMA = {
    "instrument_id": pl.Int32,
    "open": F64,
    "high": F64,
    "low": F64,
    "close": F64,
    "close_raw": F64,
    "volume_raw": F64,
    "upper_circuit": pl.Float32,
    "lower_circuit": pl.Float32,
}


def _csv(data_dir: Path, name: str) -> Path:
    path = data_dir / f"{name}.csv.gz"
    return path if path.exists() else data_dir / f"{name}.csv"


def instruments(data_dir: Path = AWS) -> pl.DataFrame:
    """``(instrument_id, symbol, series)`` of every EQ, non-SME, non-ETF instrument."""
    inst = pl.read_csv(_csv(data_dir, "instrument"), infer_schema_length=10000)
    eq = inst.filter(pl.col("instrument_type") == "EQ").with_columns(
        pl.col("series").fill_null("EQ").alias("series_filled")
    )
    eq = eq.filter(pl.col("series_filled").is_in(["EQ", "BE", "BZ"]))
    idef = pl.read_csv(_csv(data_dir, "index_def"))
    etf_index = idef.filter(pl.col("slug") == "etf")["id"].to_list()
    members = pl.scan_csv(_csv(data_dir, "index_member_daily"))
    etf_ids = set(
        members.filter(pl.col("index_id").is_in(etf_index))
        .select("instrument_id")
        .unique()
        .collect()["instrument_id"]
        .to_list()
    )
    keep = [
        not (
            i in etf_ids or ETF_NAME_RE.search(name or "") or ETF_SYMBOL_RE.search(symbol or "")
        )
        for i, name, symbol in zip(eq["id"], eq["name"], eq["symbol"], strict=True)
    ]
    return eq.filter(pl.Series(keep)).select(
        pl.col("id").cast(pl.Int32).alias("instrument_id"), "symbol", "series"
    )


def thin_sessions(data_dir: Path = AWS) -> list[dt.date]:
    counts = (
        pl.scan_csv(_csv(data_dir, "ohlcv_daily"))
        .select(pl.col("date").str.to_date())
        .group_by("date")
        .len()
        .sort("date")
        .collect()
    )
    median = counts["len"].cast(F64).rolling_median(THIN_SESSION_WINDOW, center=True, min_samples=5)
    return counts.filter(counts["len"] < THIN_SESSION_SHARE * median)["date"].to_list()


def bars_parquet(data_dir: Path = AWS, cache: Path = CACHE) -> Path:
    """Build ``cache/bars.parquet`` once: eligible instruments, no special sessions, + ``mtv_63``.

    ``mtv_63`` is the trailing 63-bar median of traded value, NULL until 63 bars exist. The export
    has no exchange ``turnover`` column, so traded value is ``close_raw x volume_raw`` — the same
    fallback ``baskfy_core.factors`` takes for ``vol_day_val`` when turnover is absent.
    """
    path = cache / "bars.parquet"
    if path.exists():
        return path
    cache.mkdir(parents=True, exist_ok=True)
    ids = instruments(data_dir)["instrument_id"]
    thin = thin_sessions(data_dir)
    bars = (
        pl.scan_csv(_csv(data_dir, "ohlcv_daily"), schema_overrides=BAR_SCHEMA)
        .select(*BAR_SCHEMA, "date")
        .with_columns(pl.col("date").str.to_date())
        .filter(pl.col("instrument_id").is_in(ids.implode()) & ~pl.col("date").is_in(thin))
        .sort("instrument_id", "date")
        .with_columns(
            (pl.col("close_raw") * pl.col("volume_raw"))
            .rolling_median(UNIVERSE_MEDIAN_SESSIONS, min_samples=UNIVERSE_MEDIAN_SESSIONS)
            .over("instrument_id")
            .alias("mtv_63")
        )
        .collect()
    )
    bars.write_parquet(path)
    del bars
    return path


def load_bars(
    columns: list[str],
    *,
    ids: list[int] | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    cache: Path = CACHE,
) -> pl.DataFrame:
    scan = pl.scan_parquet(cache / "bars.parquet").select(columns)
    if ids is not None:
        scan = scan.filter(pl.col("instrument_id").is_in(ids))
    if start is not None:
        scan = scan.filter(pl.col("date") >= start)
    if end is not None:
        scan = scan.filter(pl.col("date") <= end)
    return scan.collect()


def calendar(data_dir: Path = AWS) -> tuple[dt.date, ...]:
    """``trading_day`` rows marked trading, without special sessions, up to the last bar."""
    days = pl.read_csv(_csv(data_dir, "trading_day")).filter(pl.col("is_trading_day") == "t")
    last = pl.scan_csv(_csv(data_dir, "ohlcv_daily")).select(pl.col("date").max()).collect().item()
    thin = set(thin_sessions(data_dir))
    # The export is not in date order; resolve_window bisects, so the calendar must be sorted.
    return tuple(
        sorted(
            d
            for d in days.select(pl.col("date").str.to_date())["date"].to_list()
            if d <= dt.date.fromisoformat(last) and d not in thin
        )
    )


def index_levels(name: str, path: Path = INDEX_SERIES) -> pl.DataFrame | None:
    """``(date, level)`` of one index from desk.index_series; None when the dump lacks it."""
    frame = pl.read_csv(path).filter(pl.col("index_name") == name)
    if frame.height == 0:
        return None
    return frame.select(pl.col("date").str.to_date(), pl.col("close").cast(F64).alias("level")).sort(
        "date"
    )


def membership(slug: str, data_dir: Path = AWS) -> dict[dt.date, frozenset[int]]:
    """Snapshot date -> instrument ids for one index (point-in-time membership as exported)."""
    idef = pl.read_csv(_csv(data_dir, "index_def"))
    index_ids = idef.filter(pl.col("slug") == slug)["id"].to_list()
    rows = (
        pl.scan_csv(_csv(data_dir, "index_member_daily"))
        .filter(pl.col("index_id").is_in(index_ids))
        .select(pl.col("date").str.to_date(), "instrument_id")
        .collect()
    )
    return {
        day: frozenset(group["instrument_id"].to_list())
        for (day,), group in rows.group_by(["date"])
    }


#: A snapshot older than this does not describe the date (the export's snapshots are ~weekly).
MEMBERSHIP_MAX_AGE_DAYS = 31


def member_on(snapshots: dict[dt.date, frozenset[int]], day: dt.date) -> frozenset[int]:
    """Members per the latest snapshot on or before ``day`` — never a later one (no look-ahead)."""
    earlier = [d for d in snapshots if d <= day]
    if not earlier:
        return frozenset()
    latest = max(earlier)
    if (day - latest).days > MEMBERSHIP_MAX_AGE_DAYS:
        return frozenset()
    return snapshots[latest]


def to_pandas(frame: pl.DataFrame) -> pd.DataFrame:
    """Column-by-column conversion: the uv environment has no pyarrow for ``DataFrame.to_pandas``."""
    return pd.DataFrame({name: frame[name].to_numpy() for name in frame.columns})
