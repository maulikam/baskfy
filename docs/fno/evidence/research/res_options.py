"""Research B/C — stock (and NIFTY) options, overnight, defined risk only, EOD bhavcopy prices.

B: monthly iron condor (short ~1 sigma, long wings further out) entered N trading days before the
   monthly expiry, exited at the close of the session BEFORE expiry (stock options settle
   physically — the book never holds one into expiry day), or earlier at 50 % of credit captured
   (EOD check). Also bucketed by IV / RV to test whether a variance premium predicts the result.
C: directional debit spreads on the futures breakout signal vs the future itself.

Prices are the bhavcopy close; a leg with no trade on a day is priced by Black-76 at the entry
IV and counted as MODELLED (reported). Costs per leg per side: slippage max(0.05, 3 % of price),
brokerage Rs 20 per order spread over the lot, STT 0.15 % of premium on sells (current regime,
applied to every year — conservative), exchange 0.0355 % of premium, GST 18 % on those.
"""
from pathlib import Path
import math
import sys
import numpy as np
import polars as pl

ROOT = Path(__file__).parent
import os
R_RATE = 0.065
SLIP_PCT, SLIP_MIN = float(os.environ.get("SLIP_PCT", "0.03")), 0.05
TOPN = int(os.environ.get("TOPN", "0"))
MODE = os.environ.get("MODE", "condor")
DEFEND = os.environ.get("DEFEND", "0") == "1"
LOSS_MULT = float(os.environ.get("LOSS_MULT", "0"))  # close when loss >= LOSS_MULT x credit (0 = off)  # close at the next close after a short strike is breached at a close  # condor | trend_credit

opt = pl.scan_parquet(ROOT / "options.parquet").filter(
    pl.col("date") >= pl.col("expiry") - pl.duration(days=32))
cont = pl.read_parquet(ROOT / "cont.parquet")
fut = pl.read_parquet(ROOT / "futures.parquet").filter(pl.col("settle") > 0)


def ncdf(x):
    return 0.5 * (1 + np.vectorize(math.erf)(x / math.sqrt(2)))


def b76(F, K, T, vol, cp):
    vol = np.maximum(vol, 1e-4); T = np.maximum(T, 1e-6)
    d1 = (np.log(F / K) + 0.5 * vol * vol * T) / (vol * np.sqrt(T)); d2 = d1 - vol * np.sqrt(T)
    df = math.exp(-R_RATE * 0) * np.exp(-R_RATE * T)
    call = df * (F * ncdf(d1) - K * ncdf(d2)); put = df * (K * ncdf(-d2) - F * ncdf(-d1))
    return np.where(cp == "CE", call, put)


def implied(F, K, T, price, cp):
    lo, hi = np.full_like(price, 0.01), np.full_like(price, 3.0)
    for _ in range(50):
        mid = 0.5 * (lo + hi); p = b76(F, K, T, mid, cp)
        hi = np.where(p > price, mid, hi); lo = np.where(p > price, lo, mid)
    return 0.5 * (lo + hi)


# monthly futures price per (symbol, date, expiry)
fp = fut.select("symbol", "date", "expiry", pl.col("settle").alias("F"), "lot_size", "instrument")
lots = fut.filter(pl.col("lot_size").is_not_null()).group_by("symbol").agg(pl.col("lot_size").median())
dates = sorted(fut["date"].unique().to_list())
didx = {d: i for i, d in enumerate(dates)}
rv = cont.sort("symbol", "di").with_columns(
    (pl.col("r").rolling_std(20).over("symbol") * math.sqrt(252)).alias("rv20")
).select("symbol", "date", "rv20")


def trading_days_before(expiry, n):
    """The session n trading days before expiry (expiry itself is 0)."""
    if expiry not in didx:
        return None
    i = didx[expiry] - n
    return dates[i] if i >= 0 else None


GROUPS = None
FGROUPS = None
CA_SKIPPED = []


def load_groups(symbols_filter=None):
    global GROUPS, FGROUPS
    if symbols_filter is None:
        # memory rule: only underlyings that were ever in the top TOPN by futures turnover
        liq = cont.filter(pl.col("instrument") == "FUTSTK").sort("symbol", "di").with_columns(
            pl.col("fut_turnover").rolling_median(20).over("symbol").alias("t20")
        ).with_columns(pl.col("t20").rank(descending=True).over("date").alias("lr"))
        symbols_filter = set(liq.filter(pl.col("lr") <= max(TOPN, 60))["symbol"].unique().to_list())
    o = opt.filter(pl.col("symbol").is_in(list(symbols_filter))).collect()
    GROUPS = {}
    for (sym, e), g in o.partition_by(["symbol", "expiry"], as_dict=True).items():
        by_day = {}
        for (d,), gd in g.partition_by("date", as_dict=True).items():
            by_day[d] = {(r[0], r[1]): (r[2], r[3]) for r in gd.select("strike", "option_type", "close", "volume").iter_rows()}
        GROUPS[(sym, e)] = by_day
    FGROUPS = {(s, e, d): F for s, e, d, F in fp.select("symbol", "expiry", "date", "F").iter_rows()}


def leg_costs(price, sell, lot):
    slip = max(SLIP_MIN, SLIP_PCT * price)
    brokerage = 20.0 / lot
    stt = 0.0015 * price if sell else 0.0
    exch = 0.000355 * price
    return slip + stt + 1.18 * (brokerage + exch)


def condor_trades(n_before: int, k_short: float, k_wing: float, symbols_filter=None, pt=0.5, exit_n=1):
    rows = []
    lotd = dict(lots.iter_rows())
    rvd = {(s, d): v for s, d, v in rv.iter_rows()}
    liq = cont.filter(pl.col("instrument") == "FUTSTK").sort("symbol", "di").with_columns(
        pl.col("fut_turnover").rolling_median(20).over("symbol").alias("t20")
    ).with_columns(pl.col("t20").rank(descending=True).over("date").alias("lr"))
    liqd = {(s, d): r for s, d, r in liq.select("symbol", "date", "lr").iter_rows()}
    nif = cont.filter(pl.col("symbol") == "NIFTY").sort("di").with_columns(
        (pl.col("c") > pl.col("c").rolling_mean(50)).alias("up")).select("date", "up")
    nifd = dict(nif.iter_rows())
    tr = cont.sort("symbol", "di").with_columns(pl.col("c").rolling_mean(50).over("symbol").alias("ma50"))
    trend = {}
    for s_, d_, c_, m_ in tr.select("symbol", "date", "c", "ma50").iter_rows():
        if m_ is None or nifd.get(d_) is None:
            continue
        up = nifd[d_] if s_ not in ("NIFTY", "BANKNIFTY") else (c_ > m_)
        trend[(s_, d_)] = 1 if (c_ > m_ and up) else (-1 if (c_ < m_ and not up) else 0)
    for (sym, e), by_day in GROUPS.items():
        if symbols_filter and sym not in symbols_filter:
            continue
        d0 = trading_days_before(e, n_before); dx = trading_days_before(e, exit_n)
        if d0 is None or dx is None or d0 >= dx or d0 not in by_day:
            continue
        F = FGROUPS.get((sym, e, d0))
        if F is None:
            continue
        if TOPN and sym not in ("NIFTY", "BANKNIFTY") and (liqd.get((sym, d0)) is None or liqd[(sym, d0)] > TOPN):
            continue
        lot = float(lotd.get(sym) or 500.0)
        ch = {k: v for k, v in by_day[d0].items() if v[0] and v[0] > 0}
        if len(ch) < 8:
            continue
        T = (e - d0).days / 365.0
        strikes = np.array(sorted({k[0] for k in ch}))
        atm = strikes[np.argmin(np.abs(strikes - F))]
        if (atm, "CE") not in ch or (atm, "PE") not in ch:
            continue
        ivs = implied(np.full(2, F), np.full(2, atm), np.full(2, T), np.array([ch[(atm, "CE")][0], ch[(atm, "PE")][0]]), np.array(["CE", "PE"]))
        iv = float(np.mean(ivs))
        if not (0.05 < iv < 1.5):
            continue
        sd = F * iv * math.sqrt(T)
        def pick(target, side):
            cands = strikes[strikes >= target] if side > 0 else strikes[strikes <= target]
            if len(cands) == 0:
                return None
            return cands.min() if side > 0 else cands.max()
        sc, sp = pick(F + k_short * sd, 1), pick(F - k_short * sd, -1)
        wc, wp = pick(F + (k_short + k_wing) * sd, 1), pick(F - (k_short + k_wing) * sd, -1)
        if None in (sc, sp, wc, wp) or wc <= sc or wp >= sp:
            continue
        legs = [(sc, "CE", -1), (wc, "CE", 1), (sp, "PE", -1), (wp, "PE", 1)]
        if MODE == "trend_credit":
            tdir = trend.get((sym, d0), 0)
            if tdir == 0:
                continue
            legs = [(sp, "PE", -1), (wp, "PE", 1)] if tdir > 0 else [(sc, "CE", -1), (wc, "CE", 1)]
        px0 = {}
        ok = True
        for K, cp, q in legs:
            r = ch.get((K, cp))
            if r is None or (q < 0 and not r[1]):
                ok = False; break
            px0[(K, cp)] = r[0]
        if not ok:
            continue
        credit = sum(-q * px0[(K, cp)] for K, cp, q in legs)
        width = max((wc - sc) if any(l[1] == "CE" for l in legs) else 0, (sp - wp) if any(l[1] == "PE" for l in legs) else 0)
        maxloss = width - credit
        if credit <= 0 or maxloss <= 0:
            continue
        cost = sum(leg_costs(px0[(K, cp)], q < 0, lot) for K, cp, q in legs)
        # walk forward day by day to dx: EOD profit-take at pt of credit
        i0, ix = didx[d0], didx[dx]
        exit_val, exit_d, modelled = None, dx, 0
        F_prev, broken, breached = F, False, False
        for i in range(i0 + 1, ix + 1):
            d = dates[i]
            chd = by_day.get(d, {})
            Fdv = FGROUPS.get((sym, e, d))
            if Fdv is None:
                continue
            if not (0.7 < Fdv / F_prev < 1.4):  # split/bonus re-cuts strikes: not a tradeable path
                broken = True; break
            F_prev = Fdv
            Td = max((e - d).days, 0.5) / 365.0
            side_val = {"CE": 0.0, "PE": 0.0}; mod = 0; legpx = []
            for K, cp, q in legs:
                r = chd.get((K, cp))
                if r is None or not r[1] or not r[0]:
                    p = float(b76(np.array([Fdv]), np.array([K]), np.array([Td]), np.array([iv]), np.array([cp]))[0]); mod += 1
                else:
                    p = r[0]
                side_val[cp] += -q * p
                legpx.append((p, q > 0))
            # no-arbitrage bound: a vertical is worth between 0 and its width (closes are not
            # simultaneous, so raw marks can breach it)
            val = min(max(side_val["CE"], 0.0), wc - sc) + min(max(side_val["PE"], 0.0), sp - wp)
            stopped = LOSS_MULT > 0 and val >= (1 + LOSS_MULT) * credit
            if stopped or breached or val <= (1 - pt) * credit or i == ix:
                exit_val, exit_d, modelled, exit_legs = val, d, mod, legpx
                if stopped or breached or val <= (1 - pt) * credit:
                    break
            if DEFEND and (Fdv >= sc or Fdv <= sp):
                breached = True
        if broken:
            CA_SKIPPED.append((sym, e))
            continue
        if exit_val is None:
            continue
        # closing a long is a sale (STT); closing a short is a purchase
        exit_cost = sum(leg_costs(p, sells, lot) for p, sells in exit_legs)
        pnl = credit - exit_val - cost - exit_cost
        rows.append((sym, d0, e, exit_d, iv, rvd.get((sym, d0)),
                     credit, maxloss, pnl, pnl / maxloss, modelled, credit / F, (cost + exit_cost) / maxloss))
    return pl.DataFrame(rows, schema=["symbol", "entry", "expiry", "exit", "iv", "rv20", "credit",
                                      "maxloss", "pnl", "R", "modelled_legs", "credit_pct", "cost_R"], orient="row")


def summarise(name, t):
    print(f"(corporate-action cycles excluded so far: {len(CA_SKIPPED)})")
    if t.is_empty():
        print(name, "none"); return
    tstat = t["R"].mean() / (t["R"].std() / math.sqrt(t.height))
    print(f"\n== {name}: n={t.height} expR={t['R'].mean():.3f} t={tstat:.2f} win={(t['R']>0).mean():.1%} "
          f"worst={t['R'].min():.2f} modelled_exit_legs={t['modelled_legs'].mean():.2f} "
          f"gross_expR={(t['R'] + t['cost_R']).mean():.3f} cost_R={t['cost_R'].mean():.3f}")
    t = t.with_columns((pl.col("iv") / pl.col("rv20")).alias("vrp"), pl.col("entry").dt.year().alias("yr"))
    print(t.group_by("yr").agg(pl.len().alias("n"), pl.col("R").mean().round(3).alias("expR"), (pl.col("R") > 0).mean().round(3).alias("win")).sort("yr"))
    tv = t.filter(pl.col("vrp").is_not_null()).with_columns(
        pl.when(pl.col("vrp") < 1.0).then(pl.lit("a IV<RV")).when(pl.col("vrp") < 1.3).then(pl.lit("b 1-1.3"))
        .otherwise(pl.lit("c >1.3")).alias("bucket"))
    print(tv.group_by("bucket").agg(pl.len().alias("n"), pl.col("R").mean().round(3).alias("expR"), (pl.col("R") > 0).mean().round(3).alias("win")).sort("bucket"))


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "stock"
    load_groups({"NIFTY", "BANKNIFTY"} if which == "index" else None)
    if which == "index":
        for n in (20, 15, 10):
            t = condor_trades(n, 1.0, 0.5, symbols_filter={"NIFTY", "BANKNIFTY"})
            t.write_parquet(ROOT / f"condor_index_{n}_{SLIP_PCT}_{int(DEFEND)}.parquet"); summarise(f"INDEX condor N={n} defend={DEFEND} loss_mult={LOSS_MULT}", t)
    else:
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        k = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
        w = float(sys.argv[4]) if len(sys.argv) > 4 else 0.5
        x = int(sys.argv[5]) if len(sys.argv) > 5 else 1
        pt = float(sys.argv[6]) if len(sys.argv) > 6 else 0.5
        t = condor_trades(n, k, w, exit_n=x, pt=pt)
        t.write_parquet(ROOT / f"{MODE}_stock_{n}_{k}_{w}_{x}_{pt}_{TOPN}_{SLIP_PCT}.parquet"); summarise(f"STOCK {MODE} N={n} k={k} wing={w} exit=E-{x} pt={pt} top={TOPN or 'all'} slip={SLIP_PCT}", t)
