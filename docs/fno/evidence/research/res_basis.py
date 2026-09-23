"""Research E — cash-futures basis (UDiFF days only: the file carries the underlying's price).

Annualised front-month basis = (F/S - 1) * 365 / calendar days to expiry, on sessions >= 5 days
before expiry. A cash-and-carry (buy delivery, sell the future, hold to expiry, where the future
converges to the underlying) earns the basis minus: delivery STT 0.1 % buy + 0.1 % sell, futures
STT 0.05 % on the sale, exchange + stamp + GST ~0.02 %, DP charge, and slippage ~0.05 % on each of
four fills. ~0.45 % per cycle all-in, before the cost of the capital itself.
"""
from pathlib import Path
import polars as pl

ROOT = Path(__file__).parent
f = pl.read_parquet(ROOT / "futures.parquet").filter(
    (pl.col("instrument") == "FUTSTK") & pl.col("underlying").is_not_null() & (pl.col("settle") > 0)
)
front = f.sort("expiry").group_by("symbol", "date").first().with_columns(
    (pl.col("expiry") - pl.col("date")).dt.total_days().alias("dte")
).filter(pl.col("dte") >= 5)
front = front.with_columns(
    (pl.col("settle") / pl.col("underlying") - 1).alias("basis"),
).with_columns((pl.col("basis") * 365 / pl.col("dte")).alias("ann"))
front = front.filter(pl.col("ann").abs() < 1.0)
CYCLE_COST = 0.0045
front = front.with_columns((pl.col("basis") - CYCLE_COST).alias("net_to_expiry"),
                           ((pl.col("basis") - CYCLE_COST) * 365 / pl.col("dte")).alias("net_ann"))
print("sessions", front["date"].n_unique(), "from", front["date"].min(), "to", front["date"].max())
print(front.select(pl.col("ann").quantile(q).alias(f"ann_q{q}") for q in (0.1, 0.25, 0.5, 0.75, 0.9)))
print("share of stock-days where net-of-cost carry to expiry > 7 % annualised:",
      round(front.filter(pl.col("net_ann") > 0.07).height / front.height, 3))
# by month: median annualised basis across liquid names
liquid = front.with_columns(pl.col("turnover").rank(descending=True).over("date").alias("r")).filter(pl.col("r") <= 50)
print(liquid.group_by(pl.col("date").dt.strftime("%Y-%m").alias("m")).agg(
    pl.col("ann").median().round(4).alias("median_ann_top50"),
    (pl.col("net_ann") > 0.07).mean().round(3).alias("share_net>7%")).sort("m"))
