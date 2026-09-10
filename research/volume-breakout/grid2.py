"""Rule grid on the trend-filtered scan, reported in-sample (→2022) and out-of-sample (2023→).

.. warning::

   **This script's ``trend`` mask is the stale half. STRATEGY.md §3 is the strategy.**

   ``trend`` below applies **eight** filters: STRATEGY §3's six (A-F) plus ``close > sma50``
   and ``adr20_pct <= 8``, which §3 says were "tested and dropped as redundant". The two
   readings are not the same, and it is the **six-filter** one that produced every number in
   STRATEGY §3-§4 (measured 10 Sep 2026 against ``aws/``):

   =========================== ======= ====== ======= ====== ====
   reading                     signals CAGR   max DD  trades PF
   =========================== ======= ====== ======= ====== ====
   six filters (STRATEGY §3)   6,293   18.23% -27.94% 761    1.55
   eight filters (this script) 6,254   17.47% -27.94% 765    1.51
   =========================== ======= ====== ======= ====== ====

   So ``data/sig2.pkl`` as consumed by ``final.py`` was **not** produced by this file as it now
   stands. Nothing here is changed: this is a research script that produced ``out/grid2.csv``
   once, and rewriting its arithmetic would invalidate that output too. The production rules live
   in ``baskfy_core.vbt`` and are contracted by ``docs/vbt/04-business-rules.md``; the reasoning
   is ``docs/vbt/DECISIONS-VB.md`` **VB0.2**.
"""
import pickle, sys, time, math
import numpy as np, pandas as pd
sys.path.insert(0, '.')
from vbt.scan import _roll
from vbt.sim import Rules, run, index_gate
from vbt.data import load_index

p, ind, sig = pickle.load(open('data/panel.pkl', 'rb'))
sma200 = _roll(p.close, 200, "mean")
hi20_prev = np.roll(_roll(p.high, 20, "max"), 1, axis=1)
ret20 = (p.close / np.roll(p.close, 20, axis=1) - 1) * 100
with np.errstate(invalid="ignore"):
    trend = (p.close > ind.sma50) & (p.close > sma200) & (p.close > hi20_prev) & (ret20 < 25) \
            & (ind.close_pos >= 0.6) & (ind.turnover_sma20 >= 2e7) & (ind.change_pct <= 15) & (ind.adr20_pct <= 8)
sig2 = sig & np.nan_to_num(trend, nan=False)
print("filtered signals:", sig2.sum(), "of", sig.sum())
pickle.dump(sig2, open('data/sig2.pkl', 'wb'))
del sma200, hi20_prev, ret20, trend

idx_d, idx_c = load_index('data/index_series.csv', "NIFTY MIDCAP 150")
gates = {m: index_gate(p, idx_d, idx_c, m) for m in ["none", "idx_10_20", "idx_above_50"]}
start_col = int(np.searchsorted(p.dates, np.datetime64("2017-10-16")))
split = pd.Timestamp("2023-01-01")

def halves(res):
    e = pd.Series(res.equity, index=pd.to_datetime(res.dates)).dropna()
    out = {}
    for tag, s in (("is", e[e.index < split]), ("oos", e[e.index >= split])):
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        cagr = (s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1
        dd = (s / s.cummax() - 1).min()
        out[f"{tag}_cagr"] = round(cagr * 100, 1); out[f"{tag}_dd"] = round(dd * 100, 1)
    return out

base = dict(max_positions=10, max_new_per_day=3, sizing="equal", max_weight_pct=12.5, cost_bps_side=25.0, rank="turnover")
grid = [
    Rules(name="hold10_close", max_hold=10, stop_pct=100, **base),
    Rules(name="hold20_close", max_hold=20, stop_pct=100, **base),
    Rules(name="stop8_hold20", stop_pct=8, max_hold=20, **base),
    Rules(name="stop10_hold20", stop_pct=10, max_hold=20, **base),
    Rules(name="siglow_hold20", stop_mode="min_low_pct", stop_pct=10, max_hold=20, **base),
    Rules(name="stop10_ema10", stop_pct=10, exit_close_below_ema=10, **base),
    Rules(name="stop10_ema21", stop_pct=10, exit_close_below_ema=21, **base),
    Rules(name="stop10_ema21_be1", stop_pct=10, exit_close_below_ema=21, breakeven_r=1, **base),
    Rules(name="stop10_chand3", stop_pct=10, trail_mode="chandelier", trail_atr_mult=3, **base),
    Rules(name="stop10_chand3_be1", stop_pct=10, trail_mode="chandelier", trail_atr_mult=3, breakeven_r=1, **base),
    Rules(name="stop10_trail12", stop_pct=10, trail_mode="pct", trail_pct=12, **base),
    Rules(name="stop10_trail15", stop_pct=10, trail_mode="pct", trail_pct=15, **base),
    Rules(name="stop10_nlow10", stop_pct=10, trail_mode="nlow", trail_n=10, **base),
    Rules(name="stop10_t2R", stop_pct=10, target_r=2, max_hold=30, **base),
    Rules(name="stop10_t3R_be1", stop_pct=10, target_r=3, breakeven_r=1, max_hold=40, **base),
    Rules(name="stop10_p2R_ema21", stop_pct=10, partial_r=2, partial_frac=0.5, breakeven_r=2, exit_close_below_ema=21, **base),
    Rules(name="atr2_ema21", stop_mode="atr", atr_mult=2, stop_pct=12, exit_close_below_ema=21, **base),
    Rules(name="stop10_ema21_gap3", stop_pct=10, exit_close_below_ema=21, max_gap_pct=3, **base),
]
rows = []
for r in grid:
    for g in ["none", "idx_10_20"]:
        t = time.time()
        res = run(p, ind, sig2, r, gate_ok=gates[g], start_col=start_col)
        m = res.metrics(); m.update(halves(res)); m.update(rule=r.name, gate=g, **{f"skip_{k}": v for k, v in res.skipped.items()})
        rows.append(m)
        print(f"{r.name:22s} {g:10s} CAGR {m['cagr_pct']:6.1f}% (IS {m['is_cagr']:6.1f} / OOS {m['oos_cagr']:6.1f})  DD {m['max_dd_pct']:6.1f}%  Sh {m['sharpe']:5.2f}  n {m['trades']:5d}  win {m.get('win_rate_pct')}%  PF {m.get('profit_factor')}  avgR {m.get('avg_r')} hold {m.get('avg_hold')} exp {m['exposure_pct']}%  [{time.time()-t:.0f}s]", flush=True)
        res.trades_df().to_csv(f"out/g2_trades_{r.name}_{g}.csv", index=False)
        pd.DataFrame({"date": res.dates, "equity": res.equity, "n_open": res.n_open}).to_csv(f"out/g2_equity_{r.name}_{g}.csv", index=False)
pd.DataFrame(rows).to_csv("out/grid2.csv", index=False)
