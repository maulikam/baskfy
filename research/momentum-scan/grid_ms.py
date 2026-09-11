import pickle, sys, time
import numpy as np, pandas as pd
from pathlib import Path
HERE = Path(__file__).absolute().parent; VB = HERE.parent / 'volume-breakout'
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(VB))
from vbt.sim import Rules, run
from vbt.scan import _roll
from mscan import entries, shift
p, ind, sig = pickle.load(open(VB / 'data' / 'panel.pkl', 'rb')); state, parts = pickle.load(open(HERE / 'data' / 'state.pkl', 'rb'))
breadth = pd.read_csv(HERE / 'out' / 'breadth200.csv', index_col=0).iloc[:, 0].to_numpy()
sma200 = _roll(p.close, 200, "mean"); ret20 = (p.close / np.roll(p.close, 20, axis=1) - 1) * 100; ret5 = (p.close / np.roll(p.close, 5, axis=1) - 1) * 100
ev = entries(state, 10)
with np.errstate(invalid="ignore"):
    F = {"trig30_90": parts["r30"] | parts["r90"], "above200": p.close > sma200, "tsma>=2cr": ind.turnover_sma20 >= 2e7,
         "adr<=6": ind.adr20_pct <= 6, "chg<=10": ind.change_pct <= 10, "ret5<15": ret5 < 15, "ret20<30": ret20 < 30}
F = {k: np.nan_to_num(v, nan=False) for k, v in F.items()}
sigA = ev.copy()
for v in F.values(): sigA &= v
print("raw entries", ev.sum(), "filtered", sigA.sum())
pickle.dump((sigA, F), open(HERE / 'data' / 'sigA.pkl', 'wb'))
start_col = int(np.searchsorted(p.dates, np.datetime64("2017-10-16"))); split = pd.Timestamp("2023-01-01")
def halves(res):
    e = pd.Series(res.equity, index=pd.to_datetime(res.dates)).dropna(); out = {}
    for tag, s in (("is", e[e.index < split]), ("oos", e[e.index >= split])):
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        out[f"{tag}_cagr"] = round(((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100, 1); out[f"{tag}_dd"] = round((s / s.cummax() - 1).min() * 100, 1)
    return out
rows = []
def go(label, s, gate, hold_state=None, **kw):
    base = dict(entry_valid=3, max_positions=10, max_new_per_day=3, max_weight_pct=12.5, rank="turnover", cost_bps_side=25, stop_pct=12)
    r = Rules(name=label, **{**base, **kw}); res = run(p, ind, s, r, gate_ok=(breadth > 0.4) if gate else None, start_col=start_col, hold_state=hold_state)
    m = res.metrics(); m.update(halves(res)); m["label"] = label; rows.append(m)
    print(f"{label:46s} CAGR {m['cagr_pct']:6.1f}% (IS {m['is_cagr']:6.1f} / OOS {m['oos_cagr']:6.1f})  DD {m['max_dd_pct']:6.1f}%  Sh {m['sharpe']:5.2f}  n {m['trades']:5d}  win {m.get('win_rate_pct')}%  PF {m.get('profit_factor')} hold {m.get('avg_hold')} exp {m['exposure_pct']}%", flush=True)
    return res
exits = {"state": dict(hold_state=state), "state+ema21": dict(hold_state=state, exit_close_below_ema=21), "state+sma50": dict(hold_state=state, exit_close_below_sma=50),
         "ema21": dict(exit_close_below_ema=21), "sma50": dict(exit_close_below_sma=50), "sma20": dict(exit_close_below_sma=20),
         "trail15": dict(trail_mode="pct", trail_pct=15), "trail20": dict(trail_mode="pct", trail_pct=20), "hold20": dict(max_hold=20), "hold60": dict(max_hold=60)}
for sname, s in [("raw", ev), ("filtered", sigA)]:
    for entry in ["next_open", "limit_close"]:
        for gate in [False, True]:
            for ex, kw in exits.items():
                go(f"{sname}|{entry}|gate={int(gate)}|{ex}", s, gate, entry=entry, **kw)
pd.DataFrame(rows).to_csv(str(HERE / "out") + "/grid_ms.csv", index=False)
