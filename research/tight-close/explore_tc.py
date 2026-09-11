"""Event study on tight-base entries: next-open vs breakout-of-base-high, IS/OOS, by context."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
HERE = Path(__file__).absolute().parent; VB = HERE.parent / 'volume-breakout'
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(VB))
from vbt.scan import _roll
from tscan import entries
p, ind, sig = pickle.load(open(VB / 'data' / 'panel.pkl', 'rb')); state, parts = pickle.load(open(HERE / 'data' / 'state.pkl', 'rb'))
N, D = p.n, p.d
sma200 = _roll(p.close, 200, "mean"); hi15 = _roll(p.high, 15, "max"); lo15 = _roll(p.low, 15, "min")
hi250_prev = np.roll(_roll(p.high, 250, "max"), 1, axis=1); ret60 = (p.close / np.roll(p.close, 60, axis=1) - 1) * 100
breadth = pd.read_csv(HERE / 'out' / 'breadth200.csv', index_col=0).iloc[:, 0].to_numpy()
LB = 5
ev = entries(state, LB); rows, cols = np.nonzero(ev); n = len(rows); c, j = rows, cols
f = pd.DataFrame({"symbol": p.symbols[c], "date": p.dates[j], "year": pd.to_datetime(p.dates[j]).year,
    "range_pct": parts["range_pct"][c, j], "base_hi": hi15[c, j], "base_lo": lo15[c, j],
    "base_depth": (hi15[c, j] / lo15[c, j] - 1) * 100, "close_vs_hi": (p.close[c, j] / hi15[c, j] - 1) * 100,
    "tsma20_cr": ind.turnover_sma20[c, j] / 1e7, "adr": ind.adr20_pct[c, j], "rvol": ind.rvol[c, j],
    "above200": p.close[c, j] > sma200[c, j], "above50": p.close[c, j] > ind.sma50[c, j],
    "off_hi250": (p.close[c, j] / hi250_prev[c, j] - 1) * 100, "ret60": ret60[c, j], "breadth": breadth[j]})
f["is"] = f.date < np.datetime64("2023-01-01")
e_a = np.minimum(cols + 1, D - 1); px_a = p.open[rows, e_a]
px_b = np.full(n, np.nan); e_b = np.full(n, -1)          # buy-stop at base high within 10 sessions
for i in range(n):
    r, c0 = rows[i], cols[i]; lvl = hi15[r, c0]
    if not np.isfinite(lvl): continue
    for k in range(c0 + 1, min(c0 + 11, D)):
        o, h, l = p.open[r, k], p.high[r, k], p.low[r, k]
        if np.isfinite(o) and h >= lvl and not (o == h == l):
            px_b[i] = max(o, lvl); e_b[i] = k; break
def mark(k):
    out = np.full(n, np.nan); ok = k < D; out[ok] = p.close[rows[ok], k[ok]]; return out
for tag, px, e in [("a", px_a, e_a), ("b", px_b, e_b)]:
    for k in (5, 10, 20, 40, 60):
        ek = np.where(e >= 0, e + k, D); f[f"{tag}_r{k}"] = (mark(ek) / px - 1) * 100
    f[f"{tag}_filled"] = e >= 0
# stop-at-base-low outcome for the breakout entry, exit at close of +40 or stop
res = np.full(n, np.nan)
for i in range(n):
    e = e_b[i]
    if e < 0: continue
    r, st = rows[i], lo15[rows[i], cols[i]]; out = np.nan
    for k in range(e + 1, min(e + 41, D)):
        o, l, cl = p.open[r, k], p.low[r, k], p.close[r, k]
        if not np.isfinite(o): continue
        if o <= st: out = o; break
        if l <= st: out = st; break
        out = cl
    res[i] = (out / px_b[i] - 1) * 100 if np.isfinite(out) else np.nan
f["b_stop40"] = res
f.to_pickle(HERE / "out" / "entry_features.pkl")
def table(mask, name):
    g = f[mask]; out = {}
    for s in ("is", "oos"):
        gg = g[g["is"]] if s == "is" else g[~g["is"]]
        out[f"{s}_n"] = len(gg)
        for col in ("a_r20", "a_r60", "b_r20", "b_r60", "b_stop40"): out[f"{s}_{col}"] = round(gg[col].mean(), 2)
        out[f"{s}_b_fill"] = round(gg.b_filled.mean() * 100, 0)
    return pd.Series(out, name=name)
slices = {"ALL": np.ones(n, bool), "above200": f.above200, "below200": ~f.above200, "above50&200": f.above50 & f.above200,
          "tsma>=2cr": f.tsma20_cr >= 2, "tsma<1cr": f.tsma20_cr < 1, "adr<=4": f.adr <= 4, "adr>6": f.adr > 6,
          "base_depth<=8": f.base_depth <= 8, "base_depth>12": f.base_depth > 12, "close within 3% of base hi": f.close_vs_hi >= -3,
          "off_hi250>-10": f.off_hi250 > -10, "off_hi250<-30": f.off_hi250 < -30, "ret60>=30": f.ret60 >= 30, "ret60<15": f.ret60 < 15,
          "breadth>40": f.breadth > .4, "breadth<=40": f.breadth <= .4,
          "COMBO: above50&200, tsma>=2cr, adr<=6, base_depth<=10, off_hi250>-15": f.above50 & f.above200 & (f.tsma20_cr >= 2) & (f.adr <= 6) & (f.base_depth <= 10) & (f.off_hi250 > -15)}
T = pd.DataFrame([table(np.asarray(m), k) for k, m in slices.items()]); pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
print(f"entries (lookback {LB}): {n}; breakout fill rate {f.b_filled.mean()*100:.0f}%"); print(T.to_string()); T.to_csv(HERE / "out" / "entry_slices.csv")
print(f.groupby("year")[["a_r20", "b_r20", "b_r60", "b_stop40"]].mean().round(2).T.to_string())
