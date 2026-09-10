import pickle, sys, itertools
import numpy as np, pandas as pd
sys.path.insert(0, '.')
from vbt.sim import Rules, run, index_gate
from vbt.data import load_index
from vbt.scan import _roll
p, ind, sig = pickle.load(open('data/panel.pkl', 'rb')); sig2 = pickle.load(open('data/sig2.pkl', 'rb'))
idx_d, idx_c = load_index('data/index_series.csv', "NIFTY MIDCAP 150")
gates = {m: index_gate(p, idx_d, idx_c, m) for m in ["none", "idx_above_50", "idx_10_20", "idx_above_200"]}
start_col = int(np.searchsorted(p.dates, np.datetime64("2017-10-16"))); split = pd.Timestamp("2023-01-01")
def halves(res):
    e = pd.Series(res.equity, index=pd.to_datetime(res.dates)).dropna(); out = {}
    for tag, s in (("is", e[e.index < split]), ("oos", e[e.index >= split])):
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        out[f"{tag}_cagr"] = round(((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100, 1); out[f"{tag}_dd"] = round((s / s.cummax() - 1).min() * 100, 1)
    return out
BASE = dict(entry="limit_close", entry_valid=3, max_positions=10, max_new_per_day=3, max_weight_pct=12.5, rank="turnover", cost_bps_side=25, stop_pct=10, exit_close_below_ema=21)
def go(label, s=sig2, gate="none", **kw):
    r = Rules(name=label, **{**BASE, **kw}); res = run(p, ind, s, r, gate_ok=gates[gate], start_col=start_col)
    m = res.metrics(); m.update(halves(res))
    print(f"{label:34s} CAGR {m['cagr_pct']:6.1f}% (IS {m['is_cagr']:6.1f} / OOS {m['oos_cagr']:6.1f})  DD {m['max_dd_pct']:6.1f}%  Sh {m['sharpe']:5.2f}  n {m['trades']:5d}  win {m.get('win_rate_pct')}%  PF {m.get('profit_factor')} avgR {m.get('avg_r')} hold {m.get('avg_hold')} exp {m['exposure_pct']}%", flush=True)
    return res
print("== sensitivity around A (limit_close | 10 | ema21x)")
res_A = go("A base")
for k, v in [("entry_valid", 2), ("entry_valid", 5), ("stop_pct", 8), ("stop_pct", 12), ("stop_pct", 15), ("exit_close_below_ema", 10),
             ("max_new_per_day", 2), ("max_new_per_day", 5), ("rank", "rvol"), ("rank", "change"), ("rank", "close_pos"), ("rank", "none"),
             ("cost_bps_side", 40), ("cost_bps_side", 60), ("max_positions", 8), ("max_positions", 15), ("breakeven_r", 1.0), ("max_pct_of_turnover", 0.5), ("max_pct_of_turnover", 2.0)]:
    go(f"A {k}={v}", **{k: v})
for g in ["idx_above_50", "idx_10_20", "idx_above_200"]:
    go(f"A gate={g}", gate=g)
print("== filter ablation (drop one trend filter at a time)")
sma200 = _roll(p.close, 200, "mean"); hi20_prev = np.roll(_roll(p.high, 20, "max"), 1, axis=1); ret20 = (p.close / np.roll(p.close, 20, axis=1) - 1) * 100
with np.errstate(invalid="ignore"):
    F = {"above50": p.close > ind.sma50, "above200": p.close > sma200, "nh20": p.close > hi20_prev, "ret20<25": ret20 < 25,
         "cpos>=.6": ind.close_pos >= 0.6, "tsma>=2cr": ind.turnover_sma20 >= 2e7, "chg<=15": ind.change_pct <= 15, "adr<=8": ind.adr20_pct <= 8}
F = {k: np.nan_to_num(v, nan=False) for k, v in F.items()}
for drop in list(F) + [None]:
    m = sig.copy()
    for k, v in F.items():
        if k != drop: m &= v
    go(f"drop {drop}  (n={m.sum()})", s=m)
print("== raw Chartink scan, same execution")
go("raw scan | limit_close | ema21x", s=sig)
go("raw scan | next_open | ema21x", s=sig, entry="next_open")
print("== yearly, A base"); print(res_A.yearly().to_string())
res_A.trades_df().to_csv("out/A_trades.csv", index=False)
pd.DataFrame({"date": res_A.dates, "equity": res_A.equity, "n_open": res_A.n_open}).to_csv("out/A_equity.csv", index=False)
t = res_A.trades_df(); print(t.reason.value_counts()); print(t.ret_pct.describe(percentiles=[.05,.25,.5,.75,.95]).round(1))
print("top10 winners share of gross profit %:", round(t[t.pnl>0].pnl.nlargest(10).sum()/t[t.pnl>0].pnl.sum()*100,1))
