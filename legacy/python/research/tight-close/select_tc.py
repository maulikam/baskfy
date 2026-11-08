"""Selection study: among the ~9 liquid tight-base entries a day, which ones are worth a slot?

For every entry event (first tight day after >= 5 out, turnover >= 2 cr) we compute the features a
trader could see at that close, and the outcome of the actual TWT-1 trade rule applied to that
single event in isolation (next-open entry, 20 % stop, 20 % trail off the highest high, 250-session
cap). Then: which features rank the outcome (Spearman, in-sample to 2022 and out-of-sample
2023 ->), a composite score from the features that agree in both halves, and the score's
decile spread. `final_tc.py`-style sims with the score as the ranking key and as a minimum
threshold come in `select_sim.py`.
"""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
HERE = Path(__file__).absolute().parent; VB = HERE.parent / 'volume-breakout'
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(VB))
from vbt.scan import _roll
from vbt.data import load_index
from tscan import entries
p, ind, sig = pickle.load(open(VB / 'data' / 'panel.pkl', 'rb')); state, parts = pickle.load(open(HERE / 'data' / 'state.pkl', 'rb'))
N, D = p.n, p.d
ev = entries(state, 5)
with np.errstate(invalid="ignore"): liq = np.nan_to_num(ind.turnover_sma20 >= 2e7, nan=False)
ev &= liq
rows, cols = np.nonzero(ev); n = len(rows); c, j = rows, cols
sma200 = _roll(p.close, 200, "mean"); hi15 = _roll(p.high, 15, "max"); lo15 = _roll(p.low, 15, "min")
hi250 = np.roll(_roll(p.high, 250, "max"), 1, axis=1); vol5 = _roll(p.volume, 5, "mean"); vol20 = _roll(p.volume, 20, "mean")
ret60 = (p.close / np.roll(p.close, 60, axis=1) - 1) * 100; ret20 = (p.close / np.roll(p.close, 20, axis=1) - 1) * 100
sma50_slope = (ind.sma50 / np.roll(ind.sma50, 10, axis=1) - 1) * 100
breadth = pd.read_csv(HERE / 'out' / 'breadth200.csv', index_col=0).iloc[:, 0].to_numpy()
d, cidx = load_index(HERE / 'data' / 'index_series.csv', "NIFTY MIDCAP 150"); idx = pd.Series(cidx, index=pd.to_datetime(d)).reindex(pd.to_datetime(p.dates), method="ffill").to_numpy()
idx60 = (idx / np.roll(idx, 60) - 1) * 100
# days since the 250-session high
dsh = np.full_like(p.close, np.nan)
for r in range(N):
    h = p.high[r]; best = -np.inf; last = -1
    for k in range(D):
        if np.isfinite(h[k]) and h[k] >= best: best, last = h[k], k
        if last >= 0: dsh[r, k] = k - last
f = pd.DataFrame({
    "symbol": p.symbols[c], "date": p.dates[j], "row": c, "col": j,
    "log_turnover": np.log10(ind.turnover_sma20[c, j]), "tight_pct": parts["range_pct"][c, j],
    "base_depth": (hi15[c, j] / lo15[c, j] - 1) * 100, "close_in_base": (p.close[c, j] - lo15[c, j]) / (hi15[c, j] - lo15[c, j]),
    "vol_dryup": vol5[c, j] / ind.vol_sma[c, j], "vol20_vs50": vol20[c, j] / ind.vol_sma[c, j],
    "off_hi250": (p.close[c, j] / hi250[c, j] - 1) * 100, "days_since_high": dsh[c, j],
    "ret60": ret60[c, j], "ret20": ret20[c, j], "rs60": ret60[c, j] - idx60[j], "mlow_mult": p.close[c, j] / parts["mlow"][c, j],
    "adr": ind.adr20_pct[c, j], "sma50_slope": sma50_slope[c, j], "above200": (p.close[c, j] > sma200[c, j]).astype(float),
    "above50": (p.close[c, j] > ind.sma50[c, j]).astype(float), "dist_sma50": (p.close[c, j] / ind.sma50[c, j] - 1) * 100,
    "breadth": breadth[j], "close_raw": p.close_raw[c, j], "day_chg": ind.change_pct[c, j],
})
# outcome: the TWT-1 trade rule on this single event
ret = np.full(n, np.nan); hold = np.full(n, np.nan); reason = np.empty(n, dtype=object)
for i in range(n):
    r, c0 = rows[i], cols[i]; e = c0 + 1
    if e >= D or not np.isfinite(p.open[r, e]): continue
    entry = p.open[r, e] * 1.0025; stop = entry / 1.0025 * 0.80; hi = p.open[r, e]; out = np.nan; why = "cap"
    for k in range(e, min(e + 250, D)):
        o, h, l, cl = p.open[r, k], p.high[r, k], p.low[r, k], p.close[r, k]
        if not np.isfinite(o): continue
        if k > e and o <= stop: out, why = o, "gap"; break
        if l <= stop and not (k == e and o > stop and l > stop):
            if k == e and l <= stop: out, why = stop, "stop0"; break
            out, why = stop, "stop"; break
        hi = max(hi, h); stop = max(stop, hi * 0.80)
        out = cl; last = k
    ret[i] = (out * (1 - 0.0025) / entry - 1) * 100 if np.isfinite(out) else np.nan; reason[i] = why
f["ret"] = ret; f["reason"] = reason
f["is"] = f.date < np.datetime64("2023-01-01")
f.to_pickle(HERE / "out" / "select_features.pkl")
feat = ["log_turnover", "tight_pct", "base_depth", "close_in_base", "vol_dryup", "vol20_vs50", "off_hi250", "days_since_high", "ret60", "ret20", "rs60",
        "mlow_mult", "adr", "sma50_slope", "above200", "above50", "dist_sma50", "breadth", "close_raw", "day_chg"]
from scipy.stats import spearmanr
rows_ = []
for k in feat:
    a = f[f["is"]].dropna(subset=[k, "ret"]); b = f[~f["is"]].dropna(subset=[k, "ret"])
    ra = spearmanr(a[k], a.ret).correlation; rb = spearmanr(b[k], b.ret).correlation
    # top-quintile minus bottom-quintile mean return, both halves
    def spread(g):
        q = pd.qcut(g[k].rank(method="first"), 5, labels=False); return g.ret[q == 4].mean() - g.ret[q == 0].mean()
    rows_.append(dict(feature=k, rho_is=round(ra, 3), rho_oos=round(rb, 3), q5_minus_q1_is=round(spread(a), 2), q5_minus_q1_oos=round(spread(b), 2), n_is=len(a), n_oos=len(b)))
T = pd.DataFrame(rows_).sort_values("rho_is"); pd.set_option("display.width", 200); print(T.to_string(index=False)); T.to_csv(HERE / "out" / "select_features_rank.csv", index=False)
print("\noutcome overall: mean %.2f median %.2f, by half IS %.2f / OOS %.2f; reasons %s" % (f.ret.mean(), f.ret.median(), f[f["is"]].ret.mean(), f[~f["is"]].ret.mean(), f.reason.value_counts().to_dict()))
