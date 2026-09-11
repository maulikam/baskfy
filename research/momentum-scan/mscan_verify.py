"""Build the scan state on the panel and verify it against Chartink's backtest export."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
HERE = Path(__file__).absolute().parent; VB = HERE.parent / "volume-breakout"
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(VB))
from mscan import momentum_state, entries
p, ind, sig = pickle.load(open(VB / "data" / "panel.pkl", "rb"))
state, parts = momentum_state(p, ind)
print("state cells", state.sum(), "avg names/day", round(state.sum(axis=0).mean()))
ck = pd.read_csv(HERE / "chartink_backtest.csv", encoding="utf-8-sig"); ck["date"] = pd.to_datetime(ck.Date, format="%d-%m-%Y").dt.date; ck = ck[ck.Sector != "Indices"]
lo, hi = ck.date.min(), min(ck.date.max(), pd.Timestamp(p.dates[-1]).date())
rows, cols = np.nonzero(state); mine = pd.DataFrame({"date": pd.to_datetime(p.dates[cols]).date, "symbol": p.symbols[rows]})
ckw = ck[(ck.date >= lo) & (ck.date <= hi)]; mw = mine[(mine.date >= lo) & (mine.date <= hi)]
a = set(zip(ckw.date, ckw.Symbol)); b = set(zip(mw.date, mw.symbol)); both = a & b
print(f"window {lo}->{hi}: chartink {len(a)} mine {len(b)} both {len(both)} recall {len(both)/len(a)*100:.1f}% precision {len(both)/len(b)*100:.1f}%")
rowof = {s: i for i, s in enumerate(p.symbols)}; why = {"no_bar": 0, "volsma_nan": 0, "fails": 0, "unknown_sym": 0}
for d, s in (a - b):
    r = rowof.get(s)
    if r is None: why["unknown_sym"] += 1; continue
    j = p.col(np.datetime64(d))
    if j >= p.d or p.dates[j] != np.datetime64(d) or not np.isfinite(p.close[r, j]): why["no_bar"] += 1
    elif not np.isfinite(ind.vol_sma[r, j]): why["volsma_nan"] += 1
    else: why["fails"] += 1
print("chartink-only reasons", why)
y = pd.to_datetime(p.dates).year
print("avg names in scan by year", pd.Series(state.sum(axis=0), index=y).groupby(level=0).mean().round(0).to_dict())
for lb in (5, 10, 20):
    e = entries(state, lb); print("entries lookback", lb, ":", e.sum())
(HERE / "data").mkdir(exist_ok=True); pickle.dump((state, parts), open(HERE / "data" / "state.pkl", "wb"))
