import pickle, sys, time
import numpy as np, pandas as pd
sys.path.insert(0, '.')
from vbt.sim import Rules, run, index_gate
from vbt.data import load_index
p, ind, sig = pickle.load(open('data/panel.pkl', 'rb')); sig2 = pickle.load(open('data/sig2.pkl', 'rb'))
idx_d, idx_c = load_index('data/index_series.csv', "NIFTY MIDCAP 150")
gates = {m: index_gate(p, idx_d, idx_c, m) for m in ["none", "idx_above_50", "idx_above_200"]}
start_col = int(np.searchsorted(p.dates, np.datetime64("2017-10-16"))); split = pd.Timestamp("2023-01-01")
def halves(res):
    e = pd.Series(res.equity, index=pd.to_datetime(res.dates)).dropna(); out = {}
    for tag, s in (("is", e[e.index < split]), ("oos", e[e.index >= split])):
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        out[f"{tag}_cagr"] = round(((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100, 1); out[f"{tag}_dd"] = round((s / s.cummax() - 1).min() * 100, 1)
    return out
exits = {
    "hold20": dict(max_hold=20, stop_pct=10),
    "hold40": dict(max_hold=40, stop_pct=10),
    "trail15": dict(stop_pct=10, trail_mode="pct", trail_pct=15),
    "trail20": dict(stop_pct=10, trail_mode="pct", trail_pct=20),
    "trail25": dict(stop_pct=12, trail_mode="pct", trail_pct=25),
    "sma50x": dict(stop_pct=12, exit_close_below_sma=50),
    "sma20x": dict(stop_pct=10, exit_close_below_sma=20),
    "ema21x": dict(stop_pct=10, exit_close_below_ema=21),
    "ema21x_h60": dict(stop_pct=10, exit_close_below_ema=21, max_hold=60),
}
rows = []
for entry in ["next_open", "limit_close"]:
    for slots, mx, mw in [(10, 3, 12.5), (20, 5, 6.5)]:
        for gname in ["none", "idx_above_50"]:
            for ex, kw in exits.items():
                r = Rules(name=f"{entry}|{slots}|{ex}", entry=entry, entry_valid=3, max_positions=slots, max_new_per_day=mx, max_weight_pct=mw, rank="turnover", cost_bps_side=25, **kw)
                res = run(p, ind, sig2, r, gate_ok=gates[gname], start_col=start_col)
                m = res.metrics(); m.update(halves(res)); m.update(entry=entry, slots=slots, exit=ex, gate=gname)
                rows.append(m)
                print(f"{entry:11s} {slots:2d} {gname:12s} {ex:10s} CAGR {m['cagr_pct']:6.1f}% (IS {m['is_cagr']:6.1f} / OOS {m['oos_cagr']:6.1f})  DD {m['max_dd_pct']:6.1f}%  Sh {m['sharpe']:5.2f}  n {m['trades']:5d}  win {m.get('win_rate_pct')}%  PF {m.get('profit_factor')} hold {m.get('avg_hold')} exp {m['exposure_pct']}%", flush=True)
pd.DataFrame(rows).to_csv("out/grid3.csv", index=False)
