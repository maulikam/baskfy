"""Event study + rule grid for the weekly CMO scan (signal at Friday's close, trade Monday's open)."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
HERE = Path(__file__).absolute().parent; VB = HERE.parent / 'volume-breakout'
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(VB))
from vbt.sim import Rules, run
from vbt.scan import _roll, forward_returns
p, ind, sig0 = pickle.load(open(VB / 'data' / 'panel.pkl', 'rb')); sig, parts = pickle.load(open(HERE / 'data' / 'sig.pkl', 'rb'))
breadth = pd.read_csv(HERE / 'out' / 'breadth200.csv', index_col=0).iloc[:, 0].to_numpy()
ends = parts["ends"]
with np.errstate(invalid="ignore"):
    cap = np.nan_to_num(ind.turnover_sma20 >= 5e7, nan=False)     # market-cap > 1000 cr proxy
sigC = sig & cap
print("signals:", sig.sum(), "with cap proxy:", sigC.sum(), "per year:", pd.Series(sigC[:, ends].sum(axis=0), index=pd.to_datetime(p.dates[ends]).year).groupby(level=0).sum().to_dict())
# hold state: weekly CMO > 21, applied to the sessions of the following week
wc = parts["wcmo"]; state = np.zeros_like(p.close, dtype=bool)
for i, e in enumerate(ends):
    nxt = ends[i + 1] if i + 1 < len(ends) else p.d - 1
    state[:, e:nxt + 1] = np.nan_to_num(wc[:, i] > 21, nan=False)[:, None]
pickle.dump((sigC, state), open(HERE / 'data' / 'sigC.pkl', 'wb'))
# ---- event study
sma200 = _roll(p.close, 200, "mean"); hi250 = np.roll(_roll(p.high, 250, "max"), 1, axis=1); ret20 = (p.close / np.roll(p.close, 20, axis=1) - 1) * 100
fr = forward_returns(p, sigC, horizons=(5, 10, 20, 40, 60)); rows, cols = np.nonzero(sigC)
fr["is"] = fr.date < np.datetime64("2023-01-01"); fr["above200"] = p.close[rows, cols] > sma200[rows, cols]; fr["off_hi"] = (p.close[rows, cols] / hi250[rows, cols] - 1) * 100
fr["ret20"] = ret20[rows, cols]; fr["wcmo"] = wc[rows, np.searchsorted(ends, cols)]; fr["mcmo"] = parts["mcmo"][rows, np.searchsorted(ends, cols)]; fr["chg"] = ind.change_pct[rows, cols]; fr["breadth"] = breadth[cols]
fr["tsma"] = ind.turnover_sma20[rows, cols] / 1e7
def tbl(mask, name):
    g = fr[mask]; out = {}
    for s in ("is", "oos"):
        gg = g[g["is"]] if s == "is" else g[~g["is"]]; out[f"{s}_n"] = len(gg)
        for c in ("r5", "r20", "r60"): out[f"{s}_{c}"] = round(gg[c].mean(), 2)
        out[f"{s}_win20"] = round((gg.r20 > 0).mean() * 100, 0)
    return pd.Series(out, name=name)
slices = {"ALL": np.ones(len(fr), bool), "above200": fr.above200, "below200": ~fr.above200, "off_hi>-10": fr.off_hi > -10, "off_hi<-30": fr.off_hi < -30,
          "ret20<15": fr.ret20 < 15, "ret20>=30": fr.ret20 >= 30, "wcmo 21-40": fr.wcmo.between(21, 40), "wcmo>60": fr.wcmo > 60, "mcmo>50": fr.mcmo > 50,
          "day chg>5": fr.chg > 5, "day chg<=2": fr.chg <= 2, "breadth>40": fr.breadth > .4, "breadth<=40": fr.breadth <= .4, "tsma>=20cr": fr.tsma >= 20}
T = pd.DataFrame([tbl(np.asarray(m), k) for k, m in slices.items()]); pd.set_option("display.width", 250); print(T.to_string()); T.to_csv(HERE / "out" / "entry_slices.csv")
print(fr.groupby(pd.to_datetime(fr.date).dt.year)[["r5", "r20", "r60"]].mean().round(2).T.to_string())
# ---- grid
start_col = int(np.searchsorted(p.dates, np.datetime64("2018-01-01"))); split = pd.Timestamp("2023-01-01")
def halves(res):
    e = pd.Series(res.equity, index=pd.to_datetime(res.dates)).dropna(); out = {}
    for tag, s in (("is", e[e.index < split]), ("oos", e[e.index >= split])):
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        out[f"{tag}_cagr"] = round(((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100, 1); out[f"{tag}_dd"] = round((s / s.cummax() - 1).min() * 100, 1)
    return out
rows_ = []
def go(label, s=sigC, gate=None, hold_state=None, **kw):
    base = dict(entry_valid=3, max_positions=10, max_new_per_day=5, max_weight_pct=12.5, rank="turnover", cost_bps_side=25, stop_pct=15)
    r = Rules(name=label, **{**base, **kw}); res = run(p, ind, s, r, gate_ok=gate, start_col=start_col, hold_state=hold_state)
    m = res.metrics(); m.update(halves(res)); m["label"] = label; rows_.append(m)
    print(f"{label:44s} CAGR {m['cagr_pct']:6.1f}% (IS {m['is_cagr']:6.1f} / OOS {m['oos_cagr']:6.1f})  DD {m['max_dd_pct']:6.1f}%  Sh {m['sharpe']:5.2f}  n {m['trades']:4d}  win {m.get('win_rate_pct')}%  PF {m.get('profit_factor')} avg {m.get('avg_ret_pct')} hold {m.get('avg_hold')} exp {m['exposure_pct']}%", flush=True)
    return res
exits = {"state(wcmo>21)": dict(hold_state=state), "hold20": dict(max_hold=20), "hold40": dict(max_hold=40), "ema21": dict(exit_close_below_ema=21), "sma50": dict(exit_close_below_sma=50),
         "trail15": dict(trail_mode="pct", trail_pct=15), "trail20": dict(trail_mode="pct", trail_pct=20)}
for entry in ("next_open", "limit_close"):
    for gname, gate in (("none", None), ("br>40", breadth > 0.4)):
        for ex, kw in exits.items():
            go(f"{entry}|{gname}|{ex}", gate=gate, entry=entry, **kw)
for stop in (10, 20, 30):
    go(f"next_open|none|sma50|stop{stop}", stop_pct=stop, exit_close_below_sma=50); go(f"next_open|br>40|trail20|stop{stop}", gate=breadth > 0.4, stop_pct=stop, trail_mode="pct", trail_pct=20)
pd.DataFrame(rows_).to_csv(HERE / "out" / "grid_cw.csv", index=False)
