import pickle, sys, json
from pathlib import Path
import numpy as np, pandas as pd
HERE = Path(__file__).absolute().parent; VB = HERE.parent / 'volume-breakout'
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(VB))
from vbt.sim import Rules, run
from vbt.scan import _roll
from vbt.data import load_index
from tscan import entries
p, ind, sig = pickle.load(open(VB / 'data' / 'panel.pkl', 'rb')); state, parts = pickle.load(open(HERE / 'data' / 'state.pkl', 'rb'))
sigT, sigT2, hi15, lo15 = pickle.load(open(HERE / 'data' / 'sigT.pkl', 'rb'))
breadth = pd.read_csv(HERE / 'out' / 'breadth200.csv', index_col=0).iloc[:, 0].to_numpy()
sma200 = _roll(p.close, 200, "mean")
start_col = int(np.searchsorted(p.dates, np.datetime64("2017-10-16"))); split = pd.Timestamp("2023-01-01")
def halves(res):
    e = pd.Series(res.equity, index=pd.to_datetime(res.dates)).dropna(); out = {}
    for tag, s in (("is", e[e.index < split]), ("oos", e[e.index >= split])):
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        out[f"{tag}_cagr"] = round(((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100, 1); out[f"{tag}_dd"] = round((s / s.cummax() - 1).min() * 100, 1)
    return out
FINAL = dict(entry="next_open", max_positions=10, max_new_per_day=3, max_weight_pct=12.5, rank="turnover", cost_bps_side=25, stop_pct=20, trail_mode="pct", trail_pct=20)
rows = []
def go(label, s=sigT, thr=0.40, **kw):
    r = Rules(name=label, **{**FINAL, **kw}); res = run(p, ind, s, r, gate_ok=(breadth > thr) if thr is not None else None, start_col=start_col, entry_level=hi15, stop_level=lo15)
    m = res.metrics(); m.update(halves(res)); m["label"] = label; rows.append(m)
    print(f"{label:40s} CAGR {m['cagr_pct']:6.1f}% (IS {m['is_cagr']:6.1f} / OOS {m['oos_cagr']:6.1f})  DD {m['max_dd_pct']:6.1f}%  Sh {m['sharpe']:5.2f}  n {m['trades']:4d}  win {m.get('win_rate_pct')}%  PF {m.get('profit_factor')} avg {m.get('avg_ret_pct')} hold {m.get('avg_hold')} exp {m['exposure_pct']}%", flush=True)
    return res
F = go("TWT-1: next_open stop20 trail20 br>40")
for k, v in [("stop_pct", 15), ("stop_pct", 30), ("trail_pct", 15), ("trail_pct", 25), ("trail_pct", 30), ("max_positions", 8), ("max_positions", 15), ("max_new_per_day", 5),
             ("cost_bps_side", 40), ("cost_bps_side", 60), ("rank", "none"), ("rank", "rvol"), ("rank", "change"), ("max_weight_pct", 10), ("breakeven_r", 1.0)]:
    kw = {k: v}
    if k == "max_positions": kw.update(max_new_per_day=5 if v > 10 else 3, max_weight_pct=round(125 / v, 1))
    go(f"  {k}={v}", **kw)
go("  exit sma50 instead", trail_mode="none", exit_close_below_sma=50)
go("  exit ema21 instead", trail_mode="none", exit_close_below_ema=21)
go("  exit hold60 instead", trail_mode="none", max_hold=60)
go("  trail20 + sma50", exit_close_below_sma=50)
for thr in (0.30, 0.35, 0.45, 0.50): go(f"  gate breadth>{int(thr*100)}", thr=thr)
go("  no gate", thr=None)
go("  entry=stop_level (base high)", entry="stop_level", entry_valid=10)
go("  entry=limit_close", entry="limit_close", entry_valid=3)
go("  + trend filter (close>50&200 SMA)", s=sigT2)
ev = entries(state, 10); liq = np.nan_to_num(ind.turnover_sma20 >= 2e7, nan=False); go("  entry lookback=10", s=ev & liq)
ev20 = entries(state, 20); go("  entry lookback=20", s=ev20 & liq)
raw = entries(state, 5); go("  no liquidity filter (raw entries)", s=raw)
liq5 = np.nan_to_num(ind.turnover_sma20 >= 5e7, nan=False); go("  liquidity >= 5cr", s=raw & liq5)
pd.DataFrame(rows).to_csv(HERE / "out" / "final_sensitivity.csv", index=False)
t = F.trades_df(); t.to_csv(HERE / "out" / "final_trades.csv", index=False)
pd.DataFrame({"date": F.dates, "equity": F.equity, "n_open": F.n_open}).to_csv(HERE / "out" / "final_equity.csv", index=False)
yr = F.yearly(); yr.to_csv(HERE / "out" / "final_yearly.csv"); print(yr.to_string())
m = F.metrics(); m.update(halves(F)); json.dump({k: (float(v) if isinstance(v, (np.floating, float, int)) else v) for k, v in m.items()}, open(HERE / "out" / "final_metrics.json", "w"), indent=1); print(json.dumps(m, indent=1, default=str))
print(t.reason.value_counts().to_dict()); print(t.ret_pct.describe(percentiles=[.05, .25, .5, .75, .95]).round(1).to_dict())
w = t[t.pnl > 0]; print("top10 winners share of gross profit %:", round(w.pnl.nlargest(10).sum() / w.pnl.sum() * 100, 1), "| top1 %:", round(w.pnl.max() / w.pnl.sum() * 100, 1)); print(t.nlargest(5, "pnl")[["symbol", "entry_date", "exit_date", "ret_pct", "hold"]].to_string(index=False))
es = pd.Series(F.equity, index=pd.to_datetime(F.dates)).dropna()
for nm in ["NIFTY MIDCAP 150", "NIFTY 50"]:
    d, c = load_index(HERE / 'data' / 'index_series.csv', nm); s = pd.Series(c, index=pd.to_datetime(d)).reindex(es.index, method="ffill"); yrs = (s.index[-1] - s.index[0]).days / 365.25
    print(f"benchmark {nm}: CAGR {((s.iloc[-1]/s.iloc[0])**(1/yrs)-1)*100:.1f}%  maxDD {(s/s.cummax()-1).min()*100:.1f}%")
mo = es.resample("ME").last().pct_change().dropna() * 100
tbl = mo.groupby([mo.index.year, mo.index.month]).sum().unstack().round(1); tbl.to_csv(HERE / "out" / "final_monthly.csv"); print(tbl.to_string())
dd = es / es.cummax() - 1; print("worst drawdown:", round(dd.min()*100, 1), "at", dd.idxmin().date(), "; peak", es[:dd.idxmin()].idxmax().date())
