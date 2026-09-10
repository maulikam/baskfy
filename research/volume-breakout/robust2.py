import pickle, sys
import numpy as np, pandas as pd
sys.path.insert(0, '.')
from vbt.sim import Rules, run, index_gate
from vbt.data import load_index
from vbt.scan import _roll
p, ind, sig = pickle.load(open('data/panel.pkl', 'rb')); sig2 = pickle.load(open('data/sig2.pkl', 'rb'))
idx_d, idx_c = load_index('data/index_series.csv', "NIFTY MIDCAP 150")
gates = {m: index_gate(p, idx_d, idx_c, m) for m in ["none", "idx_above_50", "idx_above_200"]}
sma200 = _roll(p.close, 200, "mean")
with np.errstate(invalid="ignore"):
    above = (p.close > sma200); valid = np.isfinite(sma200) & np.isfinite(p.close)
breadth = above.sum(axis=0) / np.maximum(valid.sum(axis=0), 1)
pd.Series(breadth, index=p.dates).to_csv("out/breadth200.csv")
gates["breadth>40"] = breadth > 0.40; gates["breadth>50"] = breadth > 0.50; gates["breadth>30"] = breadth > 0.30
gates["breadth_rising"] = pd.Series(breadth).diff(10).fillna(0).to_numpy() > 0
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
    print(f"{label:40s} CAGR {m['cagr_pct']:6.1f}% (IS {m['is_cagr']:6.1f} / OOS {m['oos_cagr']:6.1f})  DD {m['max_dd_pct']:6.1f}%  Sh {m['sharpe']:5.2f}  n {m['trades']:5d}  win {m.get('win_rate_pct')}%  PF {m.get('profit_factor')} avgR {m.get('avg_r')} hold {m.get('avg_hold')} exp {m['exposure_pct']}%", flush=True)
    return res
print("breadth>200dma by year:", pd.Series(breadth, index=p.dates).groupby(pd.to_datetime(p.dates).year).mean().round(2).to_dict())
res = go("A base (10, ema21x)")
for g in ["breadth>30", "breadth>40", "breadth>50", "breadth_rising", "idx_above_50"]:
    go(f"A gate={g}", gate=g)
go("A stop15", stop_pct=15)
go("A 20 slots", max_positions=20, max_new_per_day=5, max_weight_pct=6.5)
go("A 20 slots stop15", max_positions=20, max_new_per_day=5, max_weight_pct=6.5, stop_pct=15)
go("A 20 slots trail25", max_positions=20, max_new_per_day=5, max_weight_pct=6.5, stop_pct=12, exit_close_below_ema=None, trail_mode="pct", trail_pct=25)
go("A 20 slots trail25 breadth>40", gate="breadth>40", max_positions=20, max_new_per_day=5, max_weight_pct=6.5, stop_pct=12, exit_close_below_ema=None, trail_mode="pct", trail_pct=25)
# quality universe: turnover >= 5cr and price >= 50
with np.errstate(invalid="ignore"):
    q = np.nan_to_num((ind.turnover_sma20 >= 5e7) & (p.close_raw >= 50), nan=False)
sig3 = sig2 & q; print("sig3 (tsma>=5cr, px>=50):", sig3.sum())
go("Q base (10, ema21x)", s=sig3)
go("Q 20 slots", s=sig3, max_positions=20, max_new_per_day=5, max_weight_pct=6.5)
go("Q 20 slots stop15", s=sig3, max_positions=20, max_new_per_day=5, max_weight_pct=6.5, stop_pct=15)
go("Q 20 slots trail25", s=sig3, max_positions=20, max_new_per_day=5, max_weight_pct=6.5, stop_pct=12, exit_close_below_ema=None, trail_mode="pct", trail_pct=25)
go("Q 20 slots sma50x", s=sig3, max_positions=20, max_new_per_day=5, max_weight_pct=6.5, stop_pct=12, exit_close_below_ema=None, exit_close_below_sma=50)
go("Q 10 breadth>40", s=sig3, gate="breadth>40")
go("Q 20 breadth>40", s=sig3, gate="breadth>40", max_positions=20, max_new_per_day=5, max_weight_pct=6.5)
go("Q risk1% 20 slots", s=sig3, sizing="risk", risk_pct=1.0, max_positions=20, max_new_per_day=5, max_weight_pct=10)
print(res.yearly().to_string())
