import pickle, sys, time
from pathlib import Path
import numpy as np, pandas as pd
HERE = Path(__file__).absolute().parent; VB = HERE.parent / 'volume-breakout'
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(VB))
from vbt.sim import Rules, run
from vbt.scan import _roll
from tscan import entries
p, ind, sig = pickle.load(open(VB / 'data' / 'panel.pkl', 'rb')); state, parts = pickle.load(open(HERE / 'data' / 'state.pkl', 'rb'))
breadth = pd.read_csv(HERE / 'out' / 'breadth200.csv', index_col=0).iloc[:, 0].to_numpy()
hi15 = _roll(p.high, 15, "max"); lo15 = _roll(p.low, 15, "min"); sma200 = _roll(p.close, 200, "mean")
ev = entries(state, 5)
with np.errstate(invalid="ignore"): liq = np.nan_to_num(ind.turnover_sma20 >= 2e7, nan=False); ab = np.nan_to_num((p.close > sma200) & (p.close > ind.sma50), nan=False)
sigT = ev & liq; sigT2 = sigT & ab
print("entries", ev.sum(), "liquid", sigT.sum(), "liquid+trend", sigT2.sum())
pickle.dump((sigT, sigT2, hi15, lo15), open(HERE / 'data' / 'sigT.pkl', 'wb'))
start_col = int(np.searchsorted(p.dates, np.datetime64("2017-10-16"))); split = pd.Timestamp("2023-01-01")
def halves(res):
    e = pd.Series(res.equity, index=pd.to_datetime(res.dates)).dropna(); out = {}
    for tag, s in (("is", e[e.index < split]), ("oos", e[e.index >= split])):
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        out[f"{tag}_cagr"] = round(((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100, 1); out[f"{tag}_dd"] = round((s / s.cummax() - 1).min() * 100, 1)
    return out
rows = []
tightness = -np.nan_to_num(parts["range_pct"], nan=1e9)   # tighter first
def go(label, s, gate=None, rank_key=None, **kw):
    base = dict(entry_valid=10, max_positions=10, max_new_per_day=3, max_weight_pct=12.5, rank="turnover", cost_bps_side=25, stop_pct=10)
    r = Rules(name=label, **{**base, **kw}); res = run(p, ind, s, r, gate_ok=gate, start_col=start_col, entry_level=hi15, stop_level=lo15, rank_key=rank_key, hold_state=None)
    m = res.metrics(); m.update(halves(res)); m["label"] = label; rows.append(m)
    print(f"{label:52s} CAGR {m['cagr_pct']:6.1f}% (IS {m['is_cagr']:6.1f} / OOS {m['oos_cagr']:6.1f})  DD {m['max_dd_pct']:6.1f}%  Sh {m['sharpe']:5.2f}  n {m['trades']:5d}  win {m.get('win_rate_pct')}%  PF {m.get('profit_factor')} hold {m.get('avg_hold')} exp {m['exposure_pct']}%", flush=True)
    return res
exits = {"hold20": dict(max_hold=20), "hold40": dict(max_hold=40), "ema21": dict(exit_close_below_ema=21), "sma50": dict(exit_close_below_sma=50),
         "trail15": dict(trail_mode="pct", trail_pct=15), "trail20": dict(trail_mode="pct", trail_pct=20), "chand3": dict(trail_mode="chandelier", trail_atr_mult=3)}
for sname, s in [("liquid", sigT), ("liquid+trend", sigT2)]:
    for entry, stopm in [("next_open", "pct"), ("stop_level", "level"), ("stop_level", "pct")]:
        for gname, gate in [("none", None), ("br>40", breadth > 0.4)]:
            for ex, kw in exits.items():
                go(f"{sname}|{entry}/{stopm}|{gname}|{ex}", s, gate, entry=entry, stop_mode=stopm, **kw)
pd.DataFrame(rows).to_csv(HERE / "out" / "grid_tc.csv", index=False)
