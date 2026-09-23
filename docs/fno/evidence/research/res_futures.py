"""Research A — stock/index futures, overnight, EOD signals, entry next open.

Every trade: signal at close t, entry at open t+1 of the held contract, exit at close after H
days or a stop at k*ATR touched intraday (filled at the stop, or at the open if it gapped
through). R = P&L / initial risk. Costs: 0.12 % of notional round trip (futures STT 0.05 % sell
side from 1 Apr 2026, exchange, stamp, GST, and 0.03 % slippage each side) — the current regime
applied to every year, which is conservative for 2022-2025.
"""
from pathlib import Path
import sys
import numpy as np
import polars as pl

ROOT = Path(__file__).parent
COST = 0.0012
c = pl.read_parquet(ROOT / "cont.parquet")

c = c.sort("symbol", "di").with_columns(
    pl.max_horizontal(pl.col("h") - pl.col("l"), (pl.col("h") - pl.col("c").shift(1).over("symbol")).abs(),
                      (pl.col("l") - pl.col("c").shift(1).over("symbol")).abs()).alias("tr"),
)
c = c.with_columns(
    pl.col("tr").rolling_mean(14).over("symbol").alias("atr"),
    pl.col("c").shift(1).rolling_max(20).over("symbol").alias("hi20"),
    pl.col("c").shift(1).rolling_min(20).over("symbol").alias("lo20"),
    pl.col("c").rolling_mean(50).over("symbol").alias("ma50"),
    pl.col("c").rolling_mean(200).over("symbol").alias("ma200"),
    (pl.col("oi_total") / pl.col("oi_total").shift(5).over("symbol") - 1).alias("oi5"),
    (pl.col("c") / pl.col("c").shift(1).over("symbol") - 1).alias("ret1"),
    (pl.col("oi_total") / pl.col("oi_total").shift(1).over("symbol") - 1).alias("oi1"),
    (pl.col("c").shift(5) / pl.col("c").shift(68) - 1).over("symbol").alias("mom63"),
    pl.col("fut_turnover").rolling_median(20).over("symbol").alias("turn20"),
)

# NIFTY regime from the index future
nifty = pl.read_parquet(ROOT / "cont.parquet").filter(pl.col("symbol") == "NIFTY").sort("di").with_columns(
    (pl.col("c") > pl.col("c").rolling_mean(50)).alias("nifty_up")
).select("di", "nifty_up")
c = c.join(nifty, on="di", how="left")

arr = {s: g for s, g in c.partition_by("symbol", as_dict=True, maintain_order=True).items()}


def simulate(signals: pl.DataFrame, H: int, k: float, trail: bool = False) -> pl.DataFrame:
    """signals: symbol, di, side (+1/-1). Returns one row per trade."""
    out = []
    for (sym,), g in signals.partition_by("symbol", as_dict=True).items():
        s = arr[(sym,)]
        di = s["di"].to_numpy(); o = s["o"].to_numpy(); h = s["h"].to_numpy(); l = s["l"].to_numpy()
        cl = s["c"].to_numpy(); atr = s["atr"].to_numpy(); dates = s["date"].to_list()
        pos = {d: i for i, d in enumerate(di)}
        busy_until = -1
        for row in g.sort("di").iter_rows(named=True):
            i = pos.get(row["di"])
            if i is None or i + 1 >= len(di) or i <= busy_until or not np.isfinite(atr[i]):
                continue
            side = row["side"]; e = o[i + 1]; risk = k * atr[i]
            if risk <= 0:
                continue
            stop = e - side * risk
            exit_px, j = None, i + 1
            last = min(i + H, len(di) - 1)
            best = e
            while j <= last:
                if trail and j > i + 1:
                    # chandelier: the stop follows the best close since entry, never loosens
                    best = max(best, cl[j - 1]) if side > 0 else min(best, cl[j - 1])
                    stop = max(stop, best - risk) if side > 0 else min(stop, best + risk)
                if side > 0 and l[j] <= stop:
                    exit_px = min(stop, o[j]) if j > i + 1 else stop; break
                if side < 0 and h[j] >= stop:
                    exit_px = max(stop, o[j]) if j > i + 1 else stop; break
                j += 1
            if exit_px is None:
                j = last; exit_px = cl[j]
            pnl = side * (exit_px - e) - COST * e
            out.append((sym, dates[i + 1], side, pnl / risk, pnl / e, j - i))
            busy_until = j
    return pl.DataFrame(out, schema=["symbol", "entry", "side", "R", "ret", "days"], orient="row")


def report(name: str, t: pl.DataFrame) -> None:
    if t.is_empty():
        print(name, "no trades"); return
    t = t.with_columns(pl.col("entry").dt.year().alias("yr"))
    s = t.group_by("side").agg(pl.len().alias("n"), pl.col("R").mean().alias("expR"),
                               (pl.col("R") > 0).mean().alias("win"), pl.col("ret").mean().alias("ret"))
    y = t.group_by("yr", "side").agg(pl.len().alias("n"), pl.col("R").mean().round(3).alias("expR")).sort("side", "yr")
    tstat = t["R"].mean() / (t["R"].std() / np.sqrt(t.height))
    print(f"\n== {name}: n={t.height} expR={t['R'].mean():.3f} t={tstat:.2f} win={(t['R']>0).mean():.2%} avg_days={t['days'].mean():.1f}")
    print(s.sort("side")); print(y.pivot(on="yr", index="side", values="expR"))


liquid = pl.col("turn20") > pl.col("turn20").quantile(0.0)  # placeholder, filter below
idx = c.filter(pl.col("symbol").is_in(["NIFTY", "BANKNIFTY", "MIDCPNIFTY"]) & pl.col("atr").is_not_null() & pl.col("ma50").is_not_null())
base = c.filter((pl.col("instrument") == "FUTSTK") & pl.col("atr").is_not_null() & pl.col("ma50").is_not_null())
# liquidity: top 60 % by 20-day futures turnover each day
base = base.with_columns(pl.col("turn20").rank(descending=True).over("di").alias("trank"),
                         pl.col("turn20").count().over("di").alias("tn"))
base = base.filter(pl.col("trank") <= 0.6 * pl.col("tn"))

# A1: 20-day breakout with trend, OI confirming
a1 = pl.concat([
    base.filter((pl.col("c") > pl.col("hi20")) & (pl.col("c") > pl.col("ma50")) & (pl.col("oi5") > 0)).select("symbol", "di", pl.lit(1).alias("side")),
    base.filter((pl.col("c") < pl.col("lo20")) & (pl.col("c") < pl.col("ma50")) & (pl.col("oi5") > 0)).select("symbol", "di", pl.lit(-1).alias("side")),
])
report("A1 breakout+OI, H=10, 2ATR", simulate(a1, 10, 2.0))
# A1 with NIFTY regime alignment
a1r = pl.concat([
    base.filter((pl.col("c") > pl.col("hi20")) & (pl.col("c") > pl.col("ma50")) & (pl.col("oi5") > 0) & pl.col("nifty_up")).select("symbol", "di", pl.lit(1).alias("side")),
    base.filter((pl.col("c") < pl.col("lo20")) & (pl.col("c") < pl.col("ma50")) & (pl.col("oi5") > 0) & ~pl.col("nifty_up")).select("symbol", "di", pl.lit(-1).alias("side")),
])
report("A1r breakout+OI aligned with NIFTY regime, H=10, 2ATR", simulate(a1r, 10, 2.0))
report("A1r H=20, 3ATR", simulate(a1r, 20, 3.0))
# A0: breakout without OI condition (control)
a0 = pl.concat([
    base.filter((pl.col("c") > pl.col("hi20")) & (pl.col("c") > pl.col("ma50")) & pl.col("nifty_up")).select("symbol", "di", pl.lit(1).alias("side")),
    base.filter((pl.col("c") < pl.col("lo20")) & (pl.col("c") < pl.col("ma50")) & ~pl.col("nifty_up")).select("symbol", "di", pl.lit(-1).alias("side")),
])
report("A0 control: breakout aligned, no OI filter, H=10, 2ATR", simulate(a0, 10, 2.0))
# A3: OI buildup day classification, 5-day hold
a3 = pl.concat([
    base.filter((pl.col("ret1") > 0.02) & (pl.col("oi1") > 0.05)).select("symbol", "di", pl.lit(1).alias("side")),
    base.filter((pl.col("ret1") < -0.02) & (pl.col("oi1") > 0.05)).select("symbol", "di", pl.lit(-1).alias("side")),
])
report("A3 one-day long/short buildup, H=5, 2ATR", simulate(a3, 5, 2.0))

# A2: cross-sectional momentum, weekly, long top-10 short bottom-10 (market-neutral book)
wk = base.filter(pl.col("mom63").is_not_null() & (pl.col("di") % 5 == 0))
wk = wk.with_columns(pl.col("mom63").rank().over("di").alias("mr"), pl.len().over("di").alias("mn"))
a2 = pl.concat([
    wk.filter(pl.col("mr") > pl.col("mn") - 10).select("symbol", "di", pl.lit(1).alias("side")),
    wk.filter(pl.col("mr") <= 10).select("symbol", "di", pl.lit(-1).alias("side")),
])
report("A2 XS momentum top/bottom 10, weekly, H=5, 3ATR", simulate(a2, 5, 3.0))

# A4: index futures trend — 20-day breakout in the direction of the 50-day average
a4 = pl.concat([
    idx.filter((pl.col("c") > pl.col("hi20")) & (pl.col("c") > pl.col("ma50"))).select("symbol", "di", pl.lit(1).alias("side")),
    idx.filter((pl.col("c") < pl.col("lo20")) & (pl.col("c") < pl.col("ma50"))).select("symbol", "di", pl.lit(-1).alias("side")),
])
t4 = simulate(a4, 20, 2.0)
report("A4 index futures 20d breakout with 50DMA, H=20, 2ATR", t4)
print(t4.group_by("symbol").agg(pl.len(), pl.col("R").mean().round(3)))

report("A0 trail: aligned breakout, 3ATR chandelier, max 40d", simulate(a0, 40, 3.0, trail=True))
report("A0 H=20 3ATR fixed", simulate(a0, 20, 3.0))
t_long = simulate(a0.filter(pl.col("side") == 1), 40, 3.0, trail=True)
report("A0 trail LONG only", t_long)
t_idx = simulate(a4, 60, 3.0, trail=True)
report("A4 index trail 3ATR max 60d", t_idx)
print(t_idx.group_by("symbol").agg(pl.len(), pl.col("R").mean().round(3)))
