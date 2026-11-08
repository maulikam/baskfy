"""Does a quality score pick better tight-base entries than 'largest turnover first'?

Score = mean of z-scores (z computed on the in-sample half only, so the out-of-sample test is
honest) of the four features whose sign held in both halves of select_tc.py:
  - lower 20-day ADR            (calm names)
  - shallower 15-session base   (a tight base in price, not just in weekly closes)
  - close near the top of the base
  - recent volume not drying up (5-day / 50-day volume)
Tested three ways: as the ranking key when slots are contested; as a minimum bar (only entries
in the top X % of the in-sample score distribution are eligible); and both.
"""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
HERE = Path(__file__).absolute().parent; VB = HERE.parent / 'volume-breakout'
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(VB))
from vbt.sim import Rules, run
from vbt.scan import _roll
p, ind, sig = pickle.load(open(VB / 'data' / 'panel.pkl', 'rb')); state, parts = pickle.load(open(HERE / 'data' / 'state.pkl', 'rb'))
sigT, sigT2, hi15, lo15 = pickle.load(open(HERE / 'data' / 'sigT.pkl', 'rb'))
f = pd.read_pickle(HERE / "out" / "select_features.pkl")
breadth = pd.read_csv(HERE / 'out' / 'breadth200.csv', index_col=0).iloc[:, 0].to_numpy()
FEATS = {"adr": -1, "base_depth": -1, "close_in_base": +1, "vol_dryup": +1}
isd = f[f["is"]]
mu = {k: isd[k].mean() for k in FEATS}; sd = {k: isd[k].std() for k in FEATS}
f["score"] = sum(s * (f[k] - mu[k]) / sd[k] for k, s in FEATS.items()) / len(FEATS)
# decile spread of the single-trade outcome, both halves
for tag, g in (("IS", f[f["is"]]), ("OOS", f[~f["is"]])):
    g = g.dropna(subset=["score", "ret"]); q = pd.qcut(g.score.rank(method="first"), 10, labels=False)
    dec = g.groupby(q).ret.agg(["mean", "median", "count"]).round(2); print(f"{tag} outcome by score decile (0 = worst score):"); print(dec.T.to_string())
thr = {pct: np.nanpercentile(isd.assign(score=f.loc[isd.index, "score"]).score, pct) for pct in (30, 50, 70, 85)}
print("in-sample score thresholds:", {k: round(v, 3) for k, v in thr.items()})
# score on the panel grid (only at entry cells)
score_grid = np.full_like(p.close, np.nan); score_grid[f.row.to_numpy(), f.col.to_numpy()] = f.score.to_numpy()
start_col = int(np.searchsorted(p.dates, np.datetime64("2017-10-16"))); split = pd.Timestamp("2023-01-01")
def halves(res):
    e = pd.Series(res.equity, index=pd.to_datetime(res.dates)).dropna(); out = {}
    for tag, s in (("is", e[e.index < split]), ("oos", e[e.index >= split])):
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        out[f"{tag}_cagr"] = round(((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100, 1); out[f"{tag}_dd"] = round((s / s.cummax() - 1).min() * 100, 1)
    return out
FINAL = dict(entry="next_open", max_positions=10, max_new_per_day=3, max_weight_pct=12.5, cost_bps_side=25, stop_pct=20, trail_mode="pct", trail_pct=20)
rows = []
def go(label, s, rank="turnover", rank_key=None, **kw):
    r = Rules(name=label, rank=rank, **{**FINAL, **kw}); res = run(p, ind, s, r, gate_ok=breadth > 0.4, start_col=start_col, rank_key=rank_key)
    m = res.metrics(); m.update(halves(res)); m["label"] = label; rows.append(m)
    print(f"{label:52s} CAGR {m['cagr_pct']:6.1f}% (IS {m['is_cagr']:6.1f} / OOS {m['oos_cagr']:6.1f})  DD {m['max_dd_pct']:6.1f}%  Sh {m['sharpe']:5.2f}  n {m['trades']:4d}  win {m.get('win_rate_pct')}%  PF {m.get('profit_factor')} avg {m.get('avg_ret_pct')} exp {m['exposure_pct']}%", flush=True)
    return res
go("TWT-1 (rank by turnover)", sigT)
go("rank by score", sigT, rank="rvol", rank_key=np.nan_to_num(score_grid, nan=-1e9))
for pct in (30, 50, 70, 85):
    m = sigT & np.nan_to_num(score_grid >= thr[pct], nan=False)
    go(f"min score: top {100-pct}% only, rank by turnover (n={m.sum()})", m)
    go(f"min score: top {100-pct}% only, rank by score", m, rank="rvol", rank_key=np.nan_to_num(score_grid, nan=-1e9))
# single-feature bars, for comparison
for k, s_ in FEATS.items():
    v = np.full_like(p.close, np.nan); v[f.row.to_numpy(), f.col.to_numpy()] = f[k].to_numpy() * s_
    cut = np.nanpercentile((isd[k] * s_), 50); m = sigT & np.nan_to_num(v >= cut, nan=False)
    go(f"  only better-than-median {k} (n={m.sum()})", m)
pd.DataFrame(rows).to_csv(HERE / "out" / "select_sim.csv", index=False)
