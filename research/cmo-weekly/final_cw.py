"""The best cell of the CMO grid, run once for the record — with the reasons it is not adopted in STRATEGY.md."""
import pickle, sys, json
from pathlib import Path
import numpy as np, pandas as pd
HERE = Path(__file__).absolute().parent; VB = HERE.parent / 'volume-breakout'
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(VB))
from vbt.sim import Rules, run
from vbt.data import load_index
p, ind, sig0 = pickle.load(open(VB / 'data' / 'panel.pkl', 'rb')); sigC, state = pickle.load(open(HERE / 'data' / 'sigC.pkl', 'rb'))
breadth = pd.read_csv(HERE / 'out' / 'breadth200.csv', index_col=0).iloc[:, 0].to_numpy()
ret20 = (p.close / np.roll(p.close, 20, axis=1) - 1) * 100
with np.errstate(invalid="ignore"): s = sigC & np.nan_to_num(ret20 < 15, nan=False)
start_col = int(np.searchsorted(p.dates, np.datetime64("2018-01-01"))); split = pd.Timestamp("2023-01-01")
R = Rules(name="cmo-best-cell", entry="next_open", max_positions=10, max_new_per_day=5, max_weight_pct=12.5, rank="turnover", cost_bps_side=25, stop_pct=20)
res = run(p, ind, s, R, gate_ok=breadth > 0.4, start_col=start_col, hold_state=state)
m = res.metrics(); e = pd.Series(res.equity, index=pd.to_datetime(res.dates)).dropna()
for tag, ss in (("is", e[e.index < split]), ("oos", e[e.index >= split])):
    yrs = (ss.index[-1] - ss.index[0]).days / 365.25; m[f"{tag}_cagr"] = round(((ss.iloc[-1] / ss.iloc[0]) ** (1 / yrs) - 1) * 100, 1)
json.dump({k: (float(v) if isinstance(v, (np.floating, float, int)) else v) for k, v in m.items()}, open(HERE / "out" / "bestcell_metrics.json", "w"), indent=1); print(json.dumps(m, indent=1, default=str))
t = res.trades_df(); t.to_csv(HERE / "out" / "bestcell_trades.csv", index=False); print(res.yearly().to_string()); res.yearly().to_csv(HERE / "out" / "bestcell_yearly.csv")
pd.DataFrame({"date": res.dates, "equity": res.equity, "n_open": res.n_open}).to_csv(HERE / "out" / "bestcell_equity.csv", index=False)
w = t[t.pnl > 0]; print("top10 share %", round(w.pnl.nlargest(10).sum() / w.pnl.sum() * 100, 1)); print(t.reason.value_counts().to_dict())
for nm in ["NIFTY MIDCAP 150", "NIFTY 50"]:
    d, c = load_index(HERE / 'data' / 'index_series.csv', nm); ss = pd.Series(c, index=pd.to_datetime(d)).reindex(e.index, method="ffill"); yrs = (ss.index[-1] - ss.index[0]).days / 365.25
    print(f"benchmark {nm}: CAGR {((ss.iloc[-1]/ss.iloc[0])**(1/yrs)-1)*100:.1f}%  maxDD {(ss/ss.cummax()-1).min()*100:.1f}%  ({ss.index[0].date()}→{ss.index[-1].date()})")
