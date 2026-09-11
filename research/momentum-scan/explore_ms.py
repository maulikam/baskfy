"""Event study on scan *entries*: forward returns IS/OOS by trigger rule and by context."""
import pickle, sys
import numpy as np, pandas as pd
from pathlib import Path
HERE = Path(__file__).absolute().parent; VB = HERE.parent / 'volume-breakout'
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(VB))
from vbt.scan import _roll
from mscan import entries, shift
p, ind, sig = pickle.load(open(VB / 'data' / 'panel.pkl', 'rb'))
state, parts = pickle.load(open(HERE / 'data' / 'state.pkl', 'rb'))
N, D = p.n, p.d
sma200 = _roll(p.close, 200, "mean"); hi20_prev = np.roll(_roll(p.high, 20, "max"), 1, axis=1)
hi250_prev = np.roll(_roll(p.high, 250, "max"), 1, axis=1)
ret20 = (p.close / np.roll(p.close, 20, axis=1) - 1) * 100
ret5 = (p.close / np.roll(p.close, 5, axis=1) - 1) * 100
ret90 = (p.close / shift(p.close, 90) - 1) * 100
breadth = pd.read_csv(HERE / 'out' / 'breadth200.csv', index_col=0).iloc[:, 0].to_numpy()
LB = 10
ev = entries(state, LB)
rows, cols = np.nonzero(ev); n = len(rows); c, j = rows, cols
f = pd.DataFrame({
    "symbol": p.symbols[c], "date": p.dates[j], "year": pd.to_datetime(p.dates[j]).year,
    "trig_r5": parts["r5"][c, j], "trig_r30": parts["r30"][c, j], "trig_r90": parts["r90"][c, j],
    "chg": ind.change_pct[c, j], "rvol": ind.rvol[c, j], "close_pos": ind.close_pos[c, j],
    "tsma20_cr": ind.turnover_sma20[c, j] / 1e7, "adr": ind.adr20_pct[c, j],
    "above200": p.close[c, j] > sma200[c, j], "above50": p.close[c, j] > ind.sma50[c, j],
    "nh20": p.close[c, j] > hi20_prev[c, j], "nh250": p.close[c, j] > hi250_prev[c, j],
    "off_hi250": (p.close[c, j] / hi250_prev[c, j] - 1) * 100,
    "ret5": ret5[c, j], "ret20": ret20[c, j], "ret90": ret90[c, j], "breadth": breadth[j],
})
f["is"] = f.date < np.datetime64("2023-01-01")
f["trig"] = np.select([f.trig_r5 & ~(f.trig_r30 | f.trig_r90), (f.trig_r30 | f.trig_r90) & ~f.trig_r5], ["r5 only", "r30/r90 only"], "both")
e_a = np.minimum(cols + 1, D - 1); px_a = p.open[rows, e_a]
# limit at signal close within 3 sessions
px_c = np.full(n, np.nan); e_c = np.full(n, -1)
for i in range(n):
    r, c0 = rows[i], cols[i]; sc = p.close[r, c0]
    for k in range(c0 + 1, min(c0 + 4, D)):
        o, h, l = p.open[r, k], p.high[r, k], p.low[r, k]
        if np.isfinite(o) and l <= sc and not (o == h == l):
            px_c[i] = min(o, sc); e_c[i] = k; break
def mark(k):
    out = np.full(n, np.nan); ok = k < D; out[ok] = p.close[rows[ok], k[ok]]; return out
for tag, px, e in [("a", px_a, e_a), ("c", px_c, e_c)]:
    for k in (5, 10, 20, 40, 60):
        ek = np.where(e >= 0, e + k, D); f[f"{tag}_r{k}"] = (mark(ek) / px - 1) * 100
    f[f"{tag}_filled"] = e >= 0
# hold-while-in-scan outcome: from entry (a), exit at the open after the first close with state False, max 120
res = np.full(n, np.nan); hold = np.full(n, np.nan)
for i in range(n):
    r, e = rows[i], e_a[i]
    if e >= D - 1 or not np.isfinite(px_a[i]): continue
    out = np.nan
    for k in range(e, min(e + 120, D - 1)):
        if not state[r, k]:
            out = p.open[r, k + 1] if np.isfinite(p.open[r, k + 1]) else p.close[r, k]; hold[i] = k + 1 - e; break
    if np.isnan(out): out = p.close[r, min(e + 120, D - 1)]; hold[i] = 120
    res[i] = (out / px_a[i] - 1) * 100
f["a_state_exit"] = res; f["a_state_hold"] = hold
f.to_pickle(str(HERE / "out") + "/entry_features.pkl")

def table(mask, name):
    g = f[mask]; out = {}
    for s in ("is", "oos"):
        gg = g[g["is"]] if s == "is" else g[~g["is"]]
        out[f"{s}_n"] = len(gg)
        for col in ("a_r10", "a_r20", "a_r60", "c_r20", "a_state_exit"):
            out[f"{s}_{col}"] = round(gg[col].mean(), 2)
        out[f"{s}_hold"] = round(gg.a_state_hold.mean(), 1)
    return pd.Series(out, name=name)
slices = {"ALL": np.ones(n, bool)}
for t in ("r5 only", "r30/r90 only", "both"): slices[f"trig={t}"] = (f.trig == t).to_numpy()
slices.update({"above200": f.above200, "below200": ~f.above200, "nh20": f.nh20, "nh250": f.nh250, "not nh20": ~f.nh20,
               "tsma>=2cr": f.tsma20_cr >= 2, "tsma<1cr": f.tsma20_cr < 1, "tsma>=5cr": f.tsma20_cr >= 5,
               "ret5<15": f.ret5 < 15, "ret5>=25": f.ret5 >= 25, "ret20<30": f.ret20 < 30, "ret20>=50": f.ret20 >= 50,
               "chg<=5": f.chg <= 5, "chg>10": f.chg > 10, "adr<=6": f.adr <= 6, "adr>8": f.adr > 8,
               "breadth>40": f.breadth > 0.4, "breadth<=40": f.breadth <= 0.4,
               "off_hi250>-10": f.off_hi250 > -10, "off_hi250<-30": f.off_hi250 < -30,
               "COMBO: above200, nh20, tsma>=2cr, ret20<30, chg<=10": f.above200 & f.nh20 & (f.tsma20_cr >= 2) & (f.ret20 < 30) & (f.chg <= 10),
               "COMBO2: above200, nh250, tsma>=2cr": f.above200 & f.nh250 & (f.tsma20_cr >= 2),
               "COMBO3: r30/r90 trig, above200, tsma>=2cr, adr<=6": f.trig.isin(["r30/r90 only", "both"]) & f.above200 & (f.tsma20_cr >= 2) & (f.adr <= 6)})
T = pd.DataFrame([table(np.asarray(m), k) for k, m in slices.items()])
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
print(f"entries (lookback {LB}): {n}; limit fill rate {f.c_filled.mean()*100:.0f}%"); print(f.trig.value_counts().to_dict())
print(T.to_string()); T.to_csv(str(HERE / "out") + "/entry_slices.csv")
print(f.groupby("year")[["a_r20", "a_r60", "a_state_exit"]].mean().round(2).T.to_string())
