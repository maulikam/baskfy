"""Build the tight-close state on the panel and verify the weekly/monthly readings against Chartink."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
HERE = Path(__file__).absolute().parent; VB = HERE.parent / "volume-breakout"
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(VB))
from tscan import tight_state, entries
p, ind, sig = pickle.load(open(VB / "data" / "panel.pkl", "rb"))
ck = pd.read_csv(HERE / "chartink_backtest.csv", encoding="utf-8-sig"); ck["date"] = pd.to_datetime(ck.Date, format="%d-%m-%Y").dt.date; ck = ck[ck.Sector != "Indices"]
lo, hi = ck.date.min(), min(ck.date.max(), pd.Timestamp(p.dates[-1]).date())
ckw = ck[(ck.date >= lo) & (ck.date <= hi)]; a = set(zip(ckw.date, ckw.Symbol))
rowof = {s: i for i, s in enumerate(p.symbols)}
def score(state, label):
    rows, cols = np.nonzero(state); mine = pd.DataFrame({"date": pd.to_datetime(p.dates[cols]).date, "symbol": p.symbols[rows]})
    mw = mine[(mine.date >= lo) & (mine.date <= hi)]; b = set(zip(mw.date, mw.symbol)); both = a & b
    print(f"{label:40s} chartink {len(a)} mine {len(b)} both {len(both)} recall {len(both)/len(a)*100:.1f}% precision {len(both)/max(1,len(b))*100:.1f}%")
    return b
best = None
for cur in (True, False):
    for ma in (2, 3, 4):
        st, parts = tight_state(p, ind, include_current_week=cur, months_ago=ma)
        b = score(st, f"current_week={cur} months_ago={ma}")
        if cur and ma == 3: best = (st, parts, b)
state, parts, b = best
why = {"no_bar": 0, "volsma_nan": 0, "not_tight": 0, "not_up": 0, "other": 0}
for d, s in (a - b):
    r = rowof.get(s)
    if r is None: why["other"] += 1; continue
    j = p.col(np.datetime64(d))
    if j >= p.d or p.dates[j] != np.datetime64(d) or not np.isfinite(p.close[r, j]): why["no_bar"] += 1
    elif not np.isfinite(ind.vol_sma[r, j]): why["volsma_nan"] += 1
    elif not parts["tight"][r, j]: why["not_tight"] += 1
    elif not parts["up"][r, j]: why["not_up"] += 1
    else: why["other"] += 1
print("chartink-only reasons", why)
# a few near-misses on tightness
ex = [(d, s) for d, s in sorted(a - b) if s in rowof][:400]
near = []
for d, s in ex:
    r = rowof[s]; j = p.col(np.datetime64(d))
    if j < p.d and p.dates[j] == np.datetime64(d) and np.isfinite(p.close[r, j]) and np.isfinite(ind.vol_sma[r, j]) and not parts["tight"][r, j]:
        near.append(round(float(parts["range_pct"][r, j]), 2))
print("range_pct of chartink-only not-tight cases (sample):", sorted(near)[:20], "... median", np.nanmedian(near) if near else None)
y = pd.to_datetime(p.dates).year
print("avg names in scan by year", pd.Series(state.sum(axis=0), index=y).groupby(level=0).mean().round(0).to_dict())
for lb in (5, 10, 20): print("entries lookback", lb, ":", entries(state, lb).sum())
(HERE / "data").mkdir(exist_ok=True); pickle.dump((state, parts), open(HERE / "data" / "state.pkl", "wb"))
