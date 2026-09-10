"""Conditional event study: which slices of the scan carry an edge, and which entry tactic.

In-sample = signals up to 2022-12-31; out-of-sample = 2023-01-01 onward. Every slice is
reported for both so a slice that only works in one half is visible as such.
"""
import pickle, sys
import numpy as np, pandas as pd
sys.path.insert(0, '.')
from vbt.scan import _roll, signals_frame

p, ind, sig = pickle.load(open('data/panel.pkl', 'rb'))
N, D = p.n, p.d
rows, cols = np.nonzero(sig)
n = len(rows)

# ---- features at the signal close (all point-in-time: computed from bars <= signal day)
sma200 = _roll(p.close, 200, "mean"); sma50 = ind.sma50; sma20 = ind.sma20
hi60 = _roll(p.high, 60, "max"); hi250 = _roll(p.high, 250, "max"); hi20 = _roll(p.high, 20, "max")
prev_hi20 = np.roll(hi20, 1, axis=1); prev_hi60 = np.roll(hi60, 1, axis=1); prev_hi250 = np.roll(hi250, 1, axis=1)
ret20 = (p.close / np.roll(p.close, 20, axis=1) - 1) * 100
ret60 = (p.close / np.roll(p.close, 60, axis=1) - 1) * 100
c, j = rows, cols
f = pd.DataFrame({
    "symbol": p.symbols[c], "date": p.dates[j], "year": pd.to_datetime(p.dates[j]).year,
    "close": p.close[c, j], "chg": ind.change_pct[c, j], "rvol": ind.rvol[c, j], "close_pos": ind.close_pos[c, j],
    "turnover_cr": ind.turnover[c, j] / 1e7, "tsma20_cr": ind.turnover_sma20[c, j] / 1e7, "adr": ind.adr20_pct[c, j],
    "above50": p.close[c, j] > sma50[c, j], "above200": p.close[c, j] > sma200[c, j],
    "sma50_up": sma50[c, j] > np.roll(sma50, 10, axis=1)[c, j],
    "nh20": p.close[c, j] > prev_hi20[c, j], "nh60": p.close[c, j] > prev_hi60[c, j], "nh250": p.close[c, j] > prev_hi250[c, j],
    "off_hi250": (p.close[c, j] / prev_hi250[c, j] - 1) * 100,
    "ret20": ret20[c, j], "ret60": ret60[c, j],
    "range_pct": (p.high[c, j] / p.low[c, j] - 1) * 100,
    "sig_low": p.low[c, j], "sig_high": p.high[c, j],
})
f["is"] = f.date < np.datetime64("2023-01-01")

# ---- outcomes for three entry tactics
def mark(r, k):  # close k sessions after entry col array
    out = np.full(n, np.nan); ok = k < D
    out[ok] = p.close[rows[ok], k[ok]]; return out

# (a) next open
e_a = np.minimum(cols + 1, D - 1); px_a = p.open[rows, e_a]
# (b) buy-stop at signal high, triggered within 5 sessions; fill at max(open, sig_high)
px_b = np.full(n, np.nan); e_b = np.full(n, -1)
# (c) limit at signal close within 5 sessions (a pullback); fill at min(open, sig_close)
px_c = np.full(n, np.nan); e_c = np.full(n, -1)
for i in range(n):
    r, c0 = rows[i], cols[i]
    sh, sc = p.high[r, c0], p.close[r, c0]
    for k in range(c0 + 1, min(c0 + 6, D)):
        o, h, l = p.open[r, k], p.high[r, k], p.low[r, k]
        if not np.isfinite(o): continue
        if e_b[i] < 0 and h >= sh and not (o == h == l):
            px_b[i] = max(o, sh); e_b[i] = k
        if e_c[i] < 0 and l <= sc and not (o == h == l):
            px_c[i] = min(o, sc); e_c[i] = k
        if e_b[i] >= 0 and e_c[i] >= 0: break
for tag, px, e in [("a", px_a, e_a), ("b", px_b, e_b), ("c", px_c, e_c)]:
    for k in (5, 10, 20, 60):
        ek = np.where(e >= 0, e + k, D)
        f[f"{tag}_r{k}"] = (mark(rows, ek) / px - 1) * 100
    # stop-based outcome: stop at signal low, exit at close of +20 or stop, whichever first
    res = np.full(n, np.nan)
    for i in range(n):
        if e[i] < 0 or not np.isfinite(px[i]): continue
        r, st = rows[i], p.low[rows[i], cols[i]]
        out = np.nan
        for k in range(e[i] + 1, min(e[i] + 21, D)):
            o, l, cl = p.open[r, k], p.low[r, k], p.close[r, k]
            if not np.isfinite(o): continue
            if o <= st: out = o; break
            if l <= st: out = st; break
            out = cl
        if e[i] == D - 1: out = px[i]
        res[i] = (out / px[i] - 1) * 100 if np.isfinite(out) else np.nan
    f[f"{tag}_stop20"] = res
    f[f"{tag}_filled"] = e >= 0
f.to_pickle("out/signal_features.pkl")

def table(mask, name):
    g = f[mask]
    out = {}
    for s in ("is", "oos"):
        gg = g[g["is"]] if s == "is" else g[~g["is"]]
        out[f"{s}_n"] = len(gg)
        for col in ("a_r10", "a_r20", "b_r20", "c_r20", "a_stop20", "b_stop20", "c_stop20"):
            out[f"{s}_{col}"] = round(gg[col].mean(), 2)
        out[f"{s}_b_fill"] = round(gg["b_filled"].mean() * 100, 0)
    return pd.Series(out, name=name)

slices = {
    "ALL": np.ones(n, bool),
    "above50": f.above50, "below50": ~f.above50,
    "above200": f.above200, "below200": ~f.above200,
    "above50&200": f.above50 & f.above200,
    "nh20": f.nh20, "nh60": f.nh60, "nh250": f.nh250,
    "not_nh20": ~f.nh20,
    "chg6.5-12": f.chg.between(6.5, 12), "chg>15": f.chg > 15,
    "close_pos>=0.7": f.close_pos >= 0.7, "close_pos<0.5": f.close_pos < 0.5,
    "turn>=5cr": f.turnover_cr >= 5, "tsma20>=2cr": f.tsma20_cr >= 2, "tsma20<1cr": f.tsma20_cr < 1,
    "rvol3-6": f.rvol.between(3, 6), "rvol>10": f.rvol > 10,
    "ret20<10": f.ret20 < 10, "ret20>30": f.ret20 > 30,
    "off_hi250>-10": f.off_hi250 > -10, "off_hi250<-40": f.off_hi250 < -40,
    "adr<4": f.adr < 4, "adr4-8": f.adr.between(4, 8), "adr>8": f.adr > 8,
    "range<12": f.range_pct < 12,
    "COMBO1: above50&200, nh60, chg6.5-15, cpos>=.6, tsma>=2cr": f.above50 & f.above200 & f.nh60 & f.chg.between(6.5, 15) & (f.close_pos >= .6) & (f.tsma20_cr >= 2),
    "COMBO2: above200, nh250, chg<=15, cpos>=.6, tsma>=2cr": f.above200 & f.nh250 & (f.chg <= 15) & (f.close_pos >= .6) & (f.tsma20_cr >= 2),
    "COMBO3: above50&200, nh20, ret20<25, cpos>=.6, tsma>=2cr": f.above50 & f.above200 & f.nh20 & (f.ret20 < 25) & (f.close_pos >= .6) & (f.tsma20_cr >= 2),
    "COMBO4: below200, off_hi250<-40 (bounce)": (~f.above200) & (f.off_hi250 < -40),
}
T = pd.DataFrame([table(m, k) for k, m in slices.items()])
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
print(T.to_string())
T.to_csv("out/slices.csv")
