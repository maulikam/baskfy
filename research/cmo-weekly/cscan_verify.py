import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
HERE = Path(__file__).absolute().parent; VB = HERE.parent / "volume-breakout"
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(VB))
from cscan import cmo_signal, week_ends
p, ind, sig0 = pickle.load(open(VB / "data" / "panel.pkl", "rb"))
ck = pd.read_csv(HERE / "chartink_backtest.csv", encoding="utf-8-sig"); ck["date"] = pd.to_datetime(ck.Date, format="%d-%m-%Y")
iso = ck.date.dt.isocalendar(); ck["wk"] = iso.year * 100 + iso.week
ends, wk = week_ends(p)
lo, hi = ck.wk.min(), wk[ends[-1]]
ckw = ck[(ck.wk >= lo) & (ck.wk <= hi)]; a = set(zip(ckw.wk, ckw.Symbol))
def score(sig, label):
    rows, cols = np.nonzero(sig); mine = pd.DataFrame({"wk": wk[cols], "symbol": p.symbols[rows]})
    mw = mine[(mine.wk >= lo) & (mine.wk <= hi)]; b = set(zip(mw.wk, mw.symbol)); both = a & b
    print(f"{label:48s} chartink {len(a)} mine {len(b)} both {len(both)} recall {len(both)/len(a)*100:.1f}% precision {len(both)/max(1,len(b))*100:.1f}%")
    return b
sig, parts = cmo_signal(p, ind)
b = score(sig, "CMO rules, no market-cap filter")
# market-cap proxies
tsma = ind.turnover_sma20
for thr in (5e6, 1e7, 2e7, 5e7):
    with np.errstate(invalid="ignore"): m = np.nan_to_num(tsma >= thr, nan=False)
    score(sig & m, f"  + 20d turnover >= {thr/1e7:.1f} cr")
with np.errstate(invalid="ignore"): m = np.nan_to_num(p.close_raw > 30, nan=False)
score(sig & m, "  + close_raw > 30")
# why chartink-only
rowof = {s: i for i, s in enumerate(p.symbols)}; why = {"no_bar_week": 0, "no_cross": 0, "no_mrule": 0, "no_volrule": 0, "unknown": 0}
wk_to_i = {w: i for i, w in enumerate(wk[ends])}
for w, s in (a - b):
    r = rowof.get(s); i = wk_to_i.get(w)
    if r is None or i is None: why["unknown"] += 1; continue
    if not np.isfinite(parts["wcmo"][r, i]): why["no_bar_week"] += 1
    elif not parts["cross"][r, i]: why["no_cross"] += 1
    elif not parts["mrule"][r, i]: why["no_mrule"] += 1
    elif not parts["volrule"][r, i]: why["no_volrule"] += 1
    else: why["unknown"] += 1
print("chartink-only reasons", why)
y = pd.to_datetime(p.dates[ends]).year
print("signals per year (no cap filter):", pd.Series(sig[:, ends].sum(axis=0), index=y).groupby(level=0).sum().to_dict())
pickle.dump((sig, parts), open(HERE / "data" / "sig.pkl", "wb"))
