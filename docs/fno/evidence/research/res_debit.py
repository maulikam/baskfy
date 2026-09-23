"""Research C — directional overnight options vs futures on the same breakout signal.

Signal: A0 (20-day breakout/breakdown aligned with the 50-day average and the NIFTY regime) computed at close t.
Futures leg: entry close t+1, exit close t+1+H (no stop, to match the spread's hold).
Debit spread: buy the strike nearest F, sell the strike nearest F*(1 +/- 0.5*IV*sqrt(T)) on the
first monthly expiry with >= 12 sessions left at entry; entry close t+1; exit close t+1+H or the
session before expiry, whichever first. R = P&L / debit (the spread's max loss).
"""
import math, sys
import numpy as np
import polars as pl
import res_options as ro

H = int(sys.argv[1]) if len(sys.argv) > 1 else 10
STRUCT = sys.argv[2] if len(sys.argv) > 2 else "spread"  # spread | long
ro.load_groups(None)
c = pl.read_parquet(ro.ROOT / "cont.parquet").filter(pl.col("instrument") == "FUTSTK").sort("symbol", "di")
c = c.with_columns(
    pl.col("c").shift(1).rolling_max(20).over("symbol").alias("hi20"),
    pl.col("c").shift(1).rolling_min(20).over("symbol").alias("lo20"),
    pl.col("c").rolling_mean(50).over("symbol").alias("ma50"),
    (pl.col("oi_total") / pl.col("oi_total").shift(5).over("symbol") - 1).alias("oi5"),
    pl.col("fut_turnover").rolling_median(20).over("symbol").alias("turn20"),
)
nifty = pl.read_parquet(ro.ROOT / "cont.parquet").filter(pl.col("symbol") == "NIFTY").sort("di").with_columns(
    (pl.col("c") > pl.col("c").rolling_mean(50)).alias("nifty_up")).select("di", "nifty_up")
c = c.join(nifty, on="di", how="left").with_columns(
    pl.col("turn20").rank(descending=True).over("di").alias("tr"), pl.len().over("di").alias("tn"))
c = c.filter(pl.col("tr") <= 0.6 * pl.col("tn"))
sig = pl.concat([
    c.filter((pl.col("c") > pl.col("hi20")) & (pl.col("c") > pl.col("ma50")) & pl.col("nifty_up")).select("symbol", "date", pl.lit(1).alias("side")),
    c.filter((pl.col("c") < pl.col("lo20")) & (pl.col("c") < pl.col("ma50")) & ~pl.col("nifty_up")).select("symbol", "date", pl.lit(-1).alias("side")),
]).sort("symbol", "date")
lotd = dict(ro.lots.iter_rows())
expiries = {}
for (sym, e) in ro.GROUPS:
    expiries.setdefault(sym, []).append(e)
rows = []
busy = {}
for sym, d, side in sig.iter_rows():
    if d not in ro.didx or ro.didx[d] + 1 >= len(ro.dates):
        continue
    if busy.get(sym) and d <= busy[sym]:
        continue
    d1 = ro.dates[ro.didx[d] + 1]
    es = sorted(e for e in expiries.get(sym, []) if e in ro.didx and ro.didx[e] - ro.didx[d1] >= 12)
    if not es:
        continue
    e = es[0]
    by_day = ro.GROUPS[(sym, e)]
    F = ro.FGROUPS.get((sym, e, d1))
    if F is None or d1 not in by_day:
        continue
    ch = {k: v for k, v in by_day[d1].items() if v[0] and v[0] > 0 and v[1]}
    cp = "CE" if side > 0 else "PE"
    strikes = np.array(sorted({k[0] for k in ch if k[1] == cp}))
    if len(strikes) < 4:
        continue
    T = (e - d1).days / 365.0
    kb = strikes[np.argmin(np.abs(strikes - F))]
    iv = float(ro.implied(np.array([F]), np.array([kb]), np.array([T]), np.array([ch[(kb, cp)][0]]), np.array([cp]))[0])
    target = F * (1 + side * 0.5 * iv * math.sqrt(T))
    ks = strikes[np.argmin(np.abs(strikes - target))]
    if STRUCT == "long":
        ks = None
    elif (ks - kb) * side <= 0:
        continue
    debit = ch[(kb, cp)][0] - (ch[(ks, cp)][0] if ks is not None else 0.0)
    width = abs(ks - kb) if ks is not None else 1e12
    if debit <= 0 or debit >= width:
        continue
    lot = float(lotd.get(sym) or 500.0)
    ix = min(ro.didx[d1] + H, ro.didx[e] - 1)
    dx = ro.dates[ix]
    Fx = ro.FGROUPS.get((sym, e, dx))
    chx = by_day.get(dx, {})
    if Fx is None or not (0.7 < Fx / F < 1.4):
        continue
    Tx = max((e - dx).days, 0.5) / 365.0
    def px(K):
        r = chx.get((K, cp))
        if r and r[0] and r[1]:
            return r[0], 0
        return float(ro.b76(np.array([Fx]), np.array([K]), np.array([Tx]), np.array([iv]), np.array([cp]))[0]), 1
    pb, m1 = px(kb)
    ps, m2 = px(ks) if ks is not None else (0.0, 0)
    val = min(max(pb - ps, 0.0), width)
    cost = ro.leg_costs(ch[(kb, cp)][0], False, lot) + ro.leg_costs(pb, True, lot)
    if ks is not None:
        cost += ro.leg_costs(ch[(ks, cp)][0], True, lot) + ro.leg_costs(ps, False, lot)
    pnl = val - debit - cost
    fut_ret = side * (Fx / F - 1) - 0.0012
    rows.append((sym, d1, side, iv, debit / F, pnl / debit, fut_ret, m1 + m2))
    busy[sym] = dx
t = pl.DataFrame(rows, schema=["symbol", "entry", "side", "iv", "debit_pct", "R_spread", "fut_ret", "modelled"], orient="row")
t.write_parquet(ro.ROOT / f"debit_{H}_{STRUCT}.parquet")
t = t.with_columns(pl.col("entry").dt.year().alias("yr"))
print(f"== C {STRUCT} vs future on A0 (aligned 20d breakout), H={H}: n={t.height}  spread expR={t['R_spread'].mean():.3f} "
      f"(t={t['R_spread'].mean()/(t['R_spread'].std()/math.sqrt(t.height)):.2f}) win={(t['R_spread']>0).mean():.1%}  "
      f"future mean ret={t['fut_ret'].mean():.4f} (t={t['fut_ret'].mean()/(t['fut_ret'].std()/math.sqrt(t.height)):.2f})  modelled={t['modelled'].mean():.2f}")
print(t.group_by("yr", "side").agg(pl.len().alias("n"), pl.col("R_spread").mean().round(3).alias("spreadR"),
      pl.col("fut_ret").mean().round(4).alias("fut_ret")).sort("side", "yr"))
