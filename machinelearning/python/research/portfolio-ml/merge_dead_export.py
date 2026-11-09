"""PM0.7: an export with the dead, without touching the running stack: data/ plus the throwaway
database's instrument register, terminal table and dead-name bars.

usage: python merge_dead_export.py <dead_dir> <out_dir>
<dead_dir> holds instrument.csv.gz, instrument_terminal.csv.gz, dead_bars.csv.gz."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import polars as pl

HERE = Path(__file__).parent
REPLACED = {"instrument.csv.gz", "ohlcv_daily.csv.gz", "ohlcv_daily.parquet", "features.parquet"}


def merge(live: pl.DataFrame, dead: pl.DataFrame) -> pl.DataFrame:
    """Bars of both; on a clash the dead-name backfill's row stands."""
    aligned = dead.select([pl.col(c).cast(live.schema[c]) if c in dead.columns else pl.lit(None, dtype=live.schema[c]).alias(c) for c in live.columns])
    return pl.concat([live, aligned]).unique(["instrument_id", "date"], keep="last", maintain_order=True).sort("instrument_id", "date")


def main(dead_dir: str, out_dir: str) -> None:
    source, dead, out = HERE / "data", Path(dead_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for f in source.iterdir():
        if f.name in REPLACED or f.name.startswith(("dataset", "portfolio-ml-pack")):
            continue
        shutil.copy2(f, out / f.name)
    shutil.copy2(dead / "instrument.csv.gz", out / "instrument.csv.gz")
    shutil.copy2(dead / "instrument_terminal.csv.gz", out / "instrument_terminal.csv.gz")
    live = pl.read_parquet(source / "ohlcv_daily.parquet")
    bars = pl.read_csv(dead / "dead_bars.csv.gz", infer_schema_length=50000).with_columns(pl.col("date").str.to_date())
    merged = merge(live, bars)
    merged.write_parquet(out / "ohlcv_daily.parquet")  # load_bars prefers it over the csv
    print(f"live {live.height}, dead bars {bars.height}, merged {merged.height}, names {merged['instrument_id'].n_unique()}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
