"""Build compact research panels from the per-day F&O bhavcopy parquet files.

futures.parquet: one row per (date, symbol, expiry) for FUTSTK/FUTIDX.
options.parquet: OPTSTK + OPTIDX(NIFTY, BANKNIFTY) rows for the two nearest monthly expiries,
                 traded or with OI, trimmed columns (memory rule: never load all options at once).
"""
from pathlib import Path
import polars as pl

ROOT = Path(__file__).parent
DAYS = sorted((ROOT / "days").glob("*.parquet"))

fut_parts, opt_parts = [], []
for p in DAYS:
    f = pl.read_parquet(p)
    fut_parts.append(
        f.filter(pl.col("instrument").is_in(["FUTSTK", "FUTIDX"])).select(
            "date", "instrument", "symbol", "expiry", "open", "high", "low", "close", "settle",
            "underlying", "open_interest", "oi_change", "volume", "turnover", "lot_size",
        )
    )
    # Monthly expiries = the expiries that have a future on that symbol that day.
    fexp = f.filter(pl.col("instrument").is_in(["FUTSTK", "FUTIDX"])).select("symbol", "expiry").unique()
    near2 = fexp.sort("expiry").group_by("symbol").head(2)
    o = f.filter(
        (pl.col("instrument") == "OPTSTK")
        | ((pl.col("instrument") == "OPTIDX") & pl.col("symbol").is_in(["NIFTY", "BANKNIFTY"]))
    ).join(near2, on=["symbol", "expiry"], how="inner")
    opt_parts.append(
        o.filter((pl.col("open_interest") > 0) | (pl.col("volume") > 0)).select(
            "date", "instrument", "symbol", "expiry", "strike", "option_type", "close", "settle",
            "open_interest", "volume",
        )
    )

fut = pl.concat(fut_parts)
opt = pl.concat(opt_parts)
fut.write_parquet(ROOT / "futures.parquet")
opt.write_parquet(ROOT / "options.parquet")
print("days", len(DAYS), "futures rows", fut.height, "option rows", opt.height)
print(fut["date"].min(), fut["date"].max())
