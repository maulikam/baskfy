import pickle, sys
import numpy as np, pandas as pd
from pathlib import Path
HERE = Path(__file__).absolute().parent; VB = HERE.parent / 'volume-breakout'
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(VB))
from vbt.sim import Rules, run
from vbt.scan import _roll
from mscan import entries, shift
p, ind, sig = pickle.load(open(VB / 'data' / 'panel.pkl', 'rb')); state, parts = pickle.load(open(HERE / 'data' / 'state.pkl', 'rb'))
sigA, F = pickle.load(open(HERE / 'data' / 'sigA.pkl', 'rb'))
breadth = pd.read_csv(HERE / 'out' / 'breadth200.csv', index_col=0).iloc[:, 0].to_numpy()
ret90 = (p.close / shift(p.close, 90) - 1) * 100; ret20 = (p.close / np.roll(p.close, 20, axis=1) - 1) * 100
hi250_prev = np.roll(_roll(p.high, 250, "max"), 1, axis=1)
with np.errstate(invalid="ignore"): off_hi = p.close / hi250_prev
start_col = int(np.searchsorted(p.dates, np.datetime64("2017-10-16"))); split = pd.Timestamp("2023-01-01")
def halves(res):
    e = pd.Series(res.equity, index=pd.to_datetime(res.dates)).dropna(); out = {}
    for tag, s in (("is", e[e.index < split]), ("oos", e[e.index >= split])):
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        out[f"{tag}_cagr"] = round(((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100, 1); out[f"{tag}_dd"] = round((s / s.cummax() - 1).min() * 100, 1)
    return out
BASE = dict(entry="next_open", max_positions=10, max_new_per_day=3, max_weight_pct=12.5, rank="turnover", cost_bps_side=25, stop_pct=12, exit_close_below_sma=50)
rows = []
def go(label, s=sigA, gate=None, rank_key=None, **kw):
    r = Rules(name=label, **{**BASE, **kw}); res = run(p, ind, s, r, gate_ok=gate, start_col=start_col, rank_key=rank_key)
    m = res.metrics(); m.update(halves(res)); m["label"] = label; rows.append(m)
    print(f"{label:40s} CAGR {m['cagr_pct']:6.1f}% (IS {m['is_cagr']:6.1f} / OOS {m['oos_cagr']:6.1f})  DD {m['max_dd_pct']:6.1f}%  Sh {m['sharpe']:5.2f}  n {m['trades']:5d}  win {m.get('win_rate_pct')}%  PF {m.get('profit_factor')} avgR {m.get('avg_r')} hold {m.get('avg_hold')} exp {m['exposure_pct']}%", flush=True)
    return res
print("== base and neighbourhood")
B = go("BASE next_open sma50x 10 slots")
for k, v in [("stop_pct", 8), ("stop_pct", 10), ("stop_pct", 15), ("stop_pct", 20), ("max_positions", 15), ("max_positions", 20), ("max_new_per_day", 5),
             ("cost_bps_side", 40), ("cost_bps_side", 60), ("rank", "none"), ("rank", "rvol"), ("rank", "change"), ("exit_close_below_sma", 20), ("max_weight_pct", 10)]:
    kw = {k: v}
    if k == "max_positions": kw.update(max_new_per_day=5, max_weight_pct=round(125 / v, 1))
    go(f"  {k}={v}", **kw)
go("  rank=ret90 (strongest first)", rank_key=np.nan_to_num(ret90, nan=-1e9))
go("  rank=ret90 weakest first", rank_key=-np.nan_to_num(ret90, nan=1e9))
go("  rank=closest to 52w high", rank_key=np.nan_to_num(off_hi, nan=-1e9))
go("  sma50x + trail25", trail_mode="pct", trail_pct=25)
go("  sma50x + trail20", trail_mode="pct", trail_pct=20)
go("  sma50x + breakeven 1R", breakeven_r=1.0)
go("  ema21x instead", exit_close_below_sma=None, exit_close_below_ema=21)
go("  sma50x + max_hold 120", max_hold=120)
for thr in (0.3, 0.35, 0.4, 0.5):
    go(f"  gate breadth>{int(thr*100)}", gate=breadth > thr)
go("  entry=limit_close", entry="limit_close", entry_valid=3)
print("== filter ablation")
ev = entries(state, 10)
for drop in [None] + list(F):
    m = ev.copy()
    for k, v in F.items():
        if k != drop: m &= v
    go(f"  drop {drop} (n={m.sum()})", s=m)
for lb in (5, 20):
    m = entries(state, lb)
    for v in F.values(): m &= v
    go(f"  entry lookback={lb} (n={m.sum()})", s=m)
pd.DataFrame(rows).to_csv(str(HERE / "out") + "/robust_ms.csv", index=False)
print(B.yearly().to_string()); t = B.trades_df(); print(t.reason.value_counts().to_dict()); print(t.ret_pct.describe(percentiles=[.05, .25, .5, .75, .95]).round(1).to_dict())
