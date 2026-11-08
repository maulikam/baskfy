"""Continuous, tradeable front-month futures series per symbol (no look-ahead).

Held contract for the move from t-1 to t is the nearest expiry strictly after t-1, so a position
never sits in a contract past its own expiry. The day's OHLC is that contract's OHLC, scaled into
a continuous level L so that close_adj(t) = L(t) and a roll never shows up as a price jump.
"""
from pathlib import Path
import polars as pl

ROOT = Path(__file__).parent


def continuous() -> pl.DataFrame:
    fut = pl.read_parquet(ROOT / "futures.parquet").filter(pl.col("settle") > 0)
    dates = fut.select("date").unique().sort("date").with_row_index("di")
    fut = fut.join(dates, on="date")
    # total futures OI per symbol-day (all expiries)
    oi = fut.group_by("symbol", "date").agg(
        pl.col("open_interest").sum().alias("oi_total"),
        pl.col("turnover").sum().alias("fut_turnover"),
        pl.col("lot_size").max().alias("lot_size"),
        pl.col("underlying").max().alias("spot"),
        pl.col("instrument").first(),
    )
    # held(t-1): nearest expiry > date(t-1)
    front = (
        fut.filter(pl.col("expiry") > pl.col("date"))
        .sort("expiry")
        .group_by("symbol", "di")
        .agg(pl.col("expiry").first().alias("held_expiry"), pl.col("settle").first().alias("held_settle"))
    )
    # next-day bar of the held contract
    nxt = front.with_columns((pl.col("di") + 1).alias("di_next")).join(
        fut.select("symbol", pl.col("di").alias("di_next"), pl.col("expiry").alias("held_expiry"),
                   "open", "high", "low", "settle", "date"),
        on=["symbol", "di_next", "held_expiry"],
        how="inner",
    )
    nxt = nxt.with_columns(
        (pl.col("settle") / pl.col("held_settle")).alias("gc"),
        (pl.col("open") / pl.col("held_settle")).alias("go"),
        (pl.col("high") / pl.col("held_settle")).alias("gh"),
        (pl.col("low") / pl.col("held_settle")).alias("gl"),
    ).select("symbol", "date", pl.col("di_next").alias("di"), "gc", "go", "gh", "gl",
             pl.col("held_expiry").alias("expiry"))
    nxt = nxt.filter((pl.col("gc") > 0.7) & (pl.col("gc") < 1.4))  # split/bonus + corrupt-row guard
    nxt = nxt.with_columns(
        pl.when((pl.col("go") <= 0) | pl.col("go").is_null()).then(1.0).otherwise(pl.col("go")).alias("go"),
        pl.when((pl.col("gh") <= 0) | pl.col("gh").is_null()).then(pl.max_horizontal("gc", 1.0)).otherwise(pl.col("gh")).alias("gh"),
        pl.when((pl.col("gl") <= 0) | pl.col("gl").is_null()).then(pl.min_horizontal("gc", 1.0)).otherwise(pl.col("gl")).alias("gl"),
    )
    c = nxt.sort("symbol", "di").with_columns(
        pl.col("gc").log().cum_sum().over("symbol").exp().alias("L")
    ).with_columns(
        (pl.col("L") / pl.col("gc")).alias("Lprev"),
    ).with_columns(
        (pl.col("Lprev") * pl.col("go")).alias("o"),
        (pl.col("Lprev") * pl.col("gh")).alias("h"),
        (pl.col("Lprev") * pl.col("gl")).alias("l"),
        pl.col("L").alias("c"),
        pl.col("gc").log().alias("r"),
    )
    c = c.join(oi, on=["symbol", "date"], how="left")
    return c.select("symbol", "instrument", "date", "di", "expiry", "o", "h", "l", "c", "r",
                    "oi_total", "fut_turnover", "lot_size", "spot").sort("symbol", "di")


if __name__ == "__main__":
    c = continuous()
    c.write_parquet(ROOT / "cont.parquet")
    print(c.height, c["symbol"].n_unique())
    print(c.filter(pl.col("symbol") == "RELIANCE").tail(3))
