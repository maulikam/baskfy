"""Run the whole study: reproduce the scan, verify against Chartink, event study, rule grid."""
from __future__ import annotations

import dataclasses as dc
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from vbt.data import load_panel, load_index
from vbt.scan import ScanParams, compute_indicators, scan, signals_frame, forward_returns
from vbt.sim import Rules, run, index_gate

HERE = Path(__file__).parent
DATA = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "aws"
OUT = HERE / "out"; OUT.mkdir(exist_ok=True)
CHARTINK = HERE / "chartink_backtest.csv"          # Chartink's own backtest export of the scan
IDX = HERE / "data" / "index_series.csv"           # desk.index_series (NIFTY 50 / Midcap 150 / Smallcap 250), from the 22 Aug dump

t0 = time.time()
import pickle
PKL = HERE / "data" / "panel.pkl"; PKL.parent.mkdir(exist_ok=True)
if PKL.exists():
    p, ind, sig = pickle.load(open(PKL, "rb"))
else:
    p = load_panel(DATA)
    ind = compute_indicators(p)
    sig = scan(p, ind)
print(f"panel: {p.n} instruments x {p.d} sessions, {p.dates[0]} → {p.dates[-1]}, etf-flagged {p.is_etf.sum()}  [{time.time()-t0:.0f}s]")
print(f"scan (literal Chartink rules): {sig.sum()} signals, {sig.any(axis=1).sum()} names  [{time.time()-t0:.0f}s]")

# ---------------------------------------------------------------- 1. verify vs Chartink
ck = pd.read_csv(CHARTINK, encoding="utf-8-sig")
ck["date"] = pd.to_datetime(ck["Date"], format="%d-%m-%Y").dt.date
ck = ck[ck.Sector != "Indices"]
mine = signals_frame(p, sig, ind)
mine["date"] = pd.to_datetime(mine["date"]).dt.date
lo, hi = ck.date.min(), min(ck.date.max(), pd.Timestamp(p.dates[-1]).date())
ck_w = ck[(ck.date >= lo) & (ck.date <= hi)]
mine_w = mine[(mine.date >= lo) & (mine.date <= hi)]
ck_set = set(zip(ck_w.date, ck_w.Symbol)); my_set = set(zip(mine_w.date, mine_w.symbol))
both = ck_set & my_set
print(f"overlap window {lo}→{hi}: chartink {len(ck_set)}, mine {len(my_set)}, both {len(both)} "
      f"(recall {len(both)/max(1,len(ck_set))*100:.1f}%, precision {len(both)/max(1,len(my_set))*100:.1f}%)")
only_ck = sorted(ck_set - my_set); only_me = sorted(my_set - ck_set)
pd.DataFrame(only_ck, columns=["date", "symbol"]).to_csv(OUT / "only_chartink.csv", index=False)
pd.DataFrame(only_me, columns=["date", "symbol"]).to_csv(OUT / "only_mine.csv", index=False)
# how many chartink-only names are simply not in the panel?
known = set(p.symbols.tolist())
missing = [s for _, s in only_ck if s not in known]
print(f"  chartink-only: {len(only_ck)}, of which symbol absent from panel: {len(missing)} (e.g. {missing[:8]})")
rowof = {s: i for i, s in enumerate(p.symbols)}
why = {"no_bar_that_day": 0, "vol_sma_nan(<50 bars or gaps)": 0, "fails_a_rule": 0, "etf": 0}
for d, s in only_ck:
    r = rowof.get(s)
    if r is None: continue
    j = p.col(np.datetime64(d))
    if j >= p.d or p.dates[j] != np.datetime64(d) or not np.isfinite(p.close[r, j]): why["no_bar_that_day"] += 1
    elif p.is_etf[r]: why["etf"] += 1
    elif not np.isfinite(ind.vol_sma[r, j]): why["vol_sma_nan(<50 bars or gaps)"] += 1
    else: why["fails_a_rule"] += 1
print("  why chartink-only:", why)

# ---------------------------------------------------------------- 2. event study
fr = forward_returns(p, sig)
fr["year"] = pd.to_datetime(fr.date).dt.year
cols = ["gap_open_pct", "r1", "r2", "r3", "r5", "r10", "r20", "r40", "r60", "mae20", "mfe20"]
ev = fr[cols].describe(percentiles=[.1, .25, .5, .75, .9]).round(2).T
ev["win_rate"] = [(fr[c] > 0).mean() * 100 for c in cols]
ev.to_csv(OUT / "event_study.csv")
print("\nEVENT STUDY (buy next open, mark at close of +k sessions), % :"); print(ev[["count", "mean", "50%", "10%", "90%", "win_rate"]].round(2))
by_year = fr.groupby("year")[["r5", "r10", "r20"]].agg(["mean", "median", "count"]).round(2)
by_year.to_csv(OUT / "event_study_by_year.csv"); print("\nby year:"); print(by_year)
# by signal characteristics
fr["date"] = pd.to_datetime(fr["date"]).dt.date
fr2 = fr.merge(mine[["date", "symbol", "rvol", "change_pct", "close_pos", "turnover_cr", "adr20_pct"]], on=["date", "symbol"], how="left")
for k, bins in [("rvol", [3, 4, 6, 10, 1e9]), ("change_pct", [6.5, 8, 10, 15, 20, 1e9]), ("close_pos", [0, .5, .8, .95, 1.01]),
                ("turnover_cr", [0, 1, 5, 20, 1e9]), ("gap_open_pct", [-1e9, -2, 0, 2, 5, 1e9])]:
    g = fr2.groupby(pd.cut(fr2[k], bins), observed=True)[["r5", "r10", "r20"]].agg(["mean", "count"]).round(2)
    g.to_csv(OUT / f"event_by_{k}.csv"); print(f"\nby {k}:"); print(g)

# ---------------------------------------------------------------- 3. rule grid
idx_d, idx_c = load_index(IDX, "NIFTY MIDCAP 150")
gates = {m: index_gate(p, idx_d, idx_c, m) for m in ["none", "idx_10_20", "idx_above_50", "idx_above_200"]}
start_col = int(np.searchsorted(p.dates, np.datetime64("2017-04-01")))  # 50-session warm-up

base = dict(max_positions=10, max_new_per_day=3, sizing="equal", max_weight_pct=12.5, cost_bps_side=25.0)
grid = [
    Rules(name="A_hold5_close", max_hold=5, stop_pct=100, **base),
    Rules(name="A_hold10_close", max_hold=10, stop_pct=100, **base),
    Rules(name="A_hold20_close", max_hold=20, stop_pct=100, **base),
    Rules(name="B_stop8_hold10", stop_pct=8, max_hold=10, **base),
    Rules(name="B_stop8_hold20", stop_pct=8, max_hold=20, **base),
    Rules(name="B_siglow_hold20", stop_mode="min_low_pct", stop_pct=10, max_hold=20, **base),
    Rules(name="C_siglow_chand3_be1", stop_mode="min_low_pct", stop_pct=10, trail_mode="chandelier", trail_atr_mult=3, breakeven_r=1, **base),
    Rules(name="C_siglow_ema10", stop_mode="min_low_pct", stop_pct=10, exit_close_below_ema=10, **base),
    Rules(name="C_siglow_ema21", stop_mode="min_low_pct", stop_pct=10, exit_close_below_ema=21, **base),
    Rules(name="C_siglow_ema10_partial2R", stop_mode="min_low_pct", stop_pct=10, exit_close_below_ema=10, partial_r=2, partial_frac=0.5, breakeven_r=2, **base),
    Rules(name="C_stop8_trail10pct", stop_pct=8, trail_mode="pct", trail_pct=10, **base),
    Rules(name="D_target2R_stop", stop_mode="min_low_pct", stop_pct=10, target_r=2, max_hold=20, **base),
    Rules(name="D_target3R_be1", stop_mode="min_low_pct", stop_pct=10, target_r=3, breakeven_r=1, max_hold=30, **base),
]
rows = []
for r in grid:
    for gname in ["none", "idx_10_20"]:
        t = time.time()
        res = run(p, ind, sig, r, gate_ok=gates[gname], start_col=start_col)
        m = res.metrics(); m.update(rule=r.name, gate=gname, **{f"skip_{k}": v for k, v in res.skipped.items()})
        rows.append(m)
        print(f"{r.name:28s} gate={gname:10s} CAGR {m.get('cagr_pct'):6.1f}%  DD {m.get('max_dd_pct'):6.1f}%  Sharpe {m.get('sharpe')}  trades {m.get('trades')}  win {m.get('win_rate_pct')}%  PF {m.get('profit_factor')}  [{time.time()-t:.0f}s]")
        res.trades_df().to_csv(OUT / f"trades_{r.name}_{gname}.csv", index=False)
        pd.DataFrame({"date": res.dates, "equity": res.equity, "n_open": res.n_open}).to_csv(OUT / f"equity_{r.name}_{gname}.csv", index=False)
        res.yearly().to_csv(OUT / f"yearly_{r.name}_{gname}.csv")
pd.DataFrame(rows).to_csv(OUT / "grid.csv", index=False)
print(f"\ndone [{time.time()-t0:.0f}s]")
