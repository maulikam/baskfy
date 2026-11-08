import pickle, sys, json
import numpy as np, pandas as pd
sys.path.insert(0, '.')
from vbt.sim import Rules, run
from vbt.data import load_index
p, ind, sig = pickle.load(open('data/panel.pkl', 'rb')); sig2 = pickle.load(open('data/sig2.pkl', 'rb'))
breadth = pd.read_csv("out/breadth200.csv", index_col=0).iloc[:, 0].to_numpy()
start_col = int(np.searchsorted(p.dates, np.datetime64("2017-10-16"))); split = pd.Timestamp("2023-01-01")
def halves(res):
    e = pd.Series(res.equity, index=pd.to_datetime(res.dates)).dropna(); out = {}
    for tag, s in (("is", e[e.index < split]), ("oos", e[e.index >= split])):
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        out[f"{tag}_cagr"] = round(((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100, 1); out[f"{tag}_dd"] = round((s / s.cummax() - 1).min() * 100, 1)
    return out
FINAL = dict(entry="limit_close", entry_valid=3, max_positions=10, max_new_per_day=3, max_weight_pct=12.5, rank="turnover",
             cost_bps_side=25, stop_pct=12, exit_close_below_ema=21, max_pct_of_turnover=1.0)
print("VBT-1 signals:", sig2.sum())
rows = []
def go(label, s=sig2, thr=0.40, **kw):
    r = Rules(name=label, **{**FINAL, **kw}); res = run(p, ind, s, r, gate_ok=breadth > thr, start_col=start_col)
    m = res.metrics(); m.update(halves(res)); m["label"] = label; rows.append(m)
    print(f"{label:34s} CAGR {m['cagr_pct']:6.1f}% (IS {m['is_cagr']:6.1f} / OOS {m['oos_cagr']:6.1f})  DD {m['max_dd_pct']:6.1f}%  Sh {m['sharpe']:5.2f}  n {m['trades']:5d}  win {m.get('win_rate_pct')}%  PF {m.get('profit_factor')} avgR {m.get('avg_r')} hold {m.get('avg_hold')} exp {m['exposure_pct']}%", flush=True)
    return res
print("== FINAL and its neighbourhood")
F = go("FINAL: br>40 stop12 ema21 v3 10slots")
for k, v in [("stop_pct", 10), ("stop_pct", 15), ("entry_valid", 2), ("entry_valid", 5), ("max_positions", 8), ("max_positions", 15), ("max_new_per_day", 5),
             ("cost_bps_side", 40), ("cost_bps_side", 60), ("rank", "none"), ("rank", "change"), ("exit_close_below_ema", 10), ("max_weight_pct", 10)]:
    go(f"  {k}={v}", **{k: v})
for thr in (0.30, 0.35, 0.45, 0.50):
    go(f"  breadth>{int(thr*100)}", thr=thr)
go("  no gate", thr=-1)
go("  entry=next_open", entry="next_open")
go("  raw Chartink scan + gate + same exec", s=sig)
pd.DataFrame(rows).to_csv("out/final_sensitivity.csv", index=False)
# ---- outputs for the final
t = F.trades_df(); t.to_csv("out/final_trades.csv", index=False)
e = pd.DataFrame({"date": F.dates, "equity": F.equity, "n_open": F.n_open}); e.to_csv("out/final_equity.csv", index=False)
yr = F.yearly(); print(yr.to_string()); yr.to_csv("out/final_yearly.csv")
m = F.metrics(); m.update(halves(F)); json.dump({k: (float(v) if isinstance(v, (np.floating, float, int)) else v) for k, v in m.items()}, open("out/final_metrics.json", "w"), indent=1)
print(json.dumps(m, indent=1, default=str))
print(t.reason.value_counts()); print(t.ret_pct.describe(percentiles=[.05, .25, .5, .75, .95]).round(1))
print("top10 winners share of gross profit %:", round(t[t.pnl > 0].pnl.nlargest(10).sum() / t[t.pnl > 0].pnl.sum() * 100, 1))
# benchmark on same dates
es = pd.Series(F.equity, index=pd.to_datetime(F.dates)).dropna()
for nm in ["NIFTY MIDCAP 150", "NIFTY 50", "NIFTY SMLCAP 250"]:
    d, c = load_index('data/index_series.csv', nm); s = pd.Series(c, index=pd.to_datetime(d)).reindex(es.index, method="ffill")
    yrs = (s.index[-1] - s.index[0]).days / 365.25
    print(f"benchmark {nm}: CAGR {((s.iloc[-1]/s.iloc[0])**(1/yrs)-1)*100:.1f}%  maxDD {(s/s.cummax()-1).min()*100:.1f}%  (same window {s.index[0].date()}→{s.index[-1].date()})")
    if nm == "NIFTY MIDCAP 150":
        by = s.resample("YE").last(); by = pd.concat([pd.Series([s.iloc[0]], index=[s.index[0]]), by]); print("  yearly:", (by.pct_change().dropna()*100).round(1).to_dict())
# monthly returns table
mo = es.resample("ME").last().pct_change().dropna() * 100
tbl = mo.groupby([mo.index.year, mo.index.month]).sum().unstack().round(1); tbl.to_csv("out/final_monthly.csv"); print(tbl.to_string())
# drawdown episodes
dd = es / es.cummax() - 1
print("worst drawdown:", round(dd.min()*100, 1), "at", dd.idxmin().date(), "; peak", es[:dd.idxmin()].idxmax().date())
# last 12 months trades
t["exit_date"] = pd.to_datetime(t.exit_date); print(t[t.exit_date >= "2025-09-01"][["symbol", "entry_date", "exit_date", "entry", "exit", "ret_pct", "hold", "reason"]].round(2).to_string(index=False))
