import pickle, sys, json
import numpy as np, pandas as pd
from pathlib import Path
HERE = Path(__file__).absolute().parent; VB = HERE.parent / 'volume-breakout'
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(VB))
from vbt.sim import Rules, run
from vbt.scan import _roll
from vbt.data import load_index
from mscan import entries, shift
p, ind, sig = pickle.load(open(VB / 'data' / 'panel.pkl', 'rb')); state, parts = pickle.load(open(HERE / 'data' / 'state.pkl', 'rb'))
breadth = pd.read_csv(HERE / 'out' / 'breadth200.csv', index_col=0).iloc[:, 0].to_numpy()
sma200 = _roll(p.close, 200, "mean"); ret20 = (p.close / np.roll(p.close, 20, axis=1) - 1) * 100; ret5 = (p.close / np.roll(p.close, 5, axis=1) - 1) * 100
with np.errstate(invalid="ignore"):
    F = {"A trig r30/r90": parts["r30"] | parts["r90"], "B above200": p.close > sma200, "C tsma>=2cr": ind.turnover_sma20 >= 2e7,
         "D adr<=6": ind.adr20_pct <= 6, "E ret5<15": ret5 < 15, "F ret20<30": ret20 < 30, "G above50": p.close > ind.sma50}
F = {k: np.nan_to_num(v, nan=False) for k, v in F.items()}
LB = 5
ev = entries(state, LB); sigM = ev.copy()
for v in F.values(): sigM &= v
print("entries", ev.sum(), "MOM-1 signals", sigM.sum()); pickle.dump(sigM, open(HERE / 'data' / 'sigM.pkl', 'wb'))
start_col = int(np.searchsorted(p.dates, np.datetime64("2017-10-16"))); split = pd.Timestamp("2023-01-01")
def halves(res):
    e = pd.Series(res.equity, index=pd.to_datetime(res.dates)).dropna(); out = {}
    for tag, s in (("is", e[e.index < split]), ("oos", e[e.index >= split])):
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        out[f"{tag}_cagr"] = round(((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100, 1); out[f"{tag}_dd"] = round((s / s.cummax() - 1).min() * 100, 1)
    return out
FINAL = dict(entry="next_open", max_positions=10, max_new_per_day=3, max_weight_pct=12.5, rank="turnover", cost_bps_side=25, stop_pct=15, exit_close_below_sma=50)
rows = []
def go(label, s=sigM, gate=None, **kw):
    r = Rules(name=label, **{**FINAL, **kw}); res = run(p, ind, s, r, gate_ok=gate, start_col=start_col)
    m = res.metrics(); m.update(halves(res)); m["label"] = label; rows.append(m)
    print(f"{label:38s} CAGR {m['cagr_pct']:6.1f}% (IS {m['is_cagr']:6.1f} / OOS {m['oos_cagr']:6.1f})  DD {m['max_dd_pct']:6.1f}%  Sh {m['sharpe']:5.2f}  n {m['trades']:5d}  win {m.get('win_rate_pct')}%  PF {m.get('profit_factor')} avgR {m.get('avg_r')} hold {m.get('avg_hold')} exp {m['exposure_pct']}%", flush=True)
    return res
Fres = go("MOM-1")
for k, v in [("stop_pct", 10), ("stop_pct", 12), ("stop_pct", 20), ("max_positions", 8), ("max_positions", 15), ("max_new_per_day", 5), ("cost_bps_side", 40), ("cost_bps_side", 60),
             ("rank", "none"), ("rank", "rvol"), ("exit_close_below_sma", 20), ("breakeven_r", 1.0), ("max_weight_pct", 10)]:
    kw = {k: v}
    if k == "max_positions": kw.update(max_new_per_day=5 if v > 10 else 3, max_weight_pct=round(125 / v, 1))
    go(f"  {k}={v}", **kw)
go("  exit ema21 instead", exit_close_below_sma=None, exit_close_below_ema=21)
go("  exit trail 20% instead", exit_close_below_sma=None, trail_mode="pct", trail_pct=20)
for thr in (0.35, 0.4, 0.45): go(f"  gate breadth>{int(thr*100)}", gate=breadth > thr)
G35 = go("  MOM-1g: gate breadth>35 (the gated variant)", gate=breadth > 0.35)
gt = G35.trades_df(); gt.to_csv(str(HERE / "out") + "/final_trades_gated.csv", index=False)
pd.DataFrame({"date": G35.dates, "equity": G35.equity, "n_open": G35.n_open}).to_csv(str(HERE / "out") + "/final_equity_gated.csv", index=False)
G35.yearly().to_csv(str(HERE / "out") + "/final_yearly_gated.csv"); print(G35.yearly().to_string())
go("  entry=limit_close", entry="limit_close", entry_valid=3)
for lb in (3, 10): 
    m = entries(state, lb)
    for v in F.values(): m &= v
    go(f"  entry lookback={lb} (n={m.sum()})", s=m)
print("== ablation")
abl = []
for drop in [None] + list(F) + ["ALL (raw entries)"]:
    m = ev.copy()
    if drop != "ALL (raw entries)":
        for k, v in F.items():
            if k != drop: m &= v
    r = go(f"  drop {drop} (n={m.sum()})", s=m); mm = r.metrics(); mm["dropped"] = drop or "none (MOM-1)"; mm["signals"] = int(m.sum()); abl.append(mm)
pd.DataFrame(abl).to_csv(str(HERE / "out") + "/final_ablation.csv", index=False)
pd.DataFrame(rows).to_csv(str(HERE / "out") + "/final_sensitivity.csv", index=False)
t = Fres.trades_df(); t.to_csv(str(HERE / "out") + "/final_trades.csv", index=False)
pd.DataFrame({"date": Fres.dates, "equity": Fres.equity, "n_open": Fres.n_open}).to_csv(str(HERE / "out") + "/final_equity.csv", index=False)
yr = Fres.yearly(); yr.to_csv(str(HERE / "out") + "/final_yearly.csv"); print(yr.to_string())
m = Fres.metrics(); m.update(halves(Fres)); json.dump({k: (float(v) if isinstance(v, (np.floating, float, int)) else v) for k, v in m.items()}, open(str(HERE / "out") + "/final_metrics.json", "w"), indent=1); print(json.dumps(m, indent=1, default=str))
print(t.reason.value_counts().to_dict()); print(t.ret_pct.describe(percentiles=[.05, .25, .5, .75, .95]).round(1).to_dict())
w = t[t.pnl > 0]; print("top10 winners share of gross profit %:", round(w.pnl.nlargest(10).sum() / w.pnl.sum() * 100, 1), "| top1 %:", round(w.pnl.max() / w.pnl.sum() * 100, 1)); print(t.nlargest(5, "pnl")[["symbol", "entry_date", "exit_date", "ret_pct", "hold"]].to_string(index=False))
es = pd.Series(Fres.equity, index=pd.to_datetime(Fres.dates)).dropna()
for nm in ["NIFTY MIDCAP 150", "NIFTY 50", "NIFTY SMLCAP 250"]:
    d, c = load_index(HERE / 'data' / 'index_series.csv', nm); s = pd.Series(c, index=pd.to_datetime(d)).reindex(es.index, method="ffill"); yrs = (s.index[-1] - s.index[0]).days / 365.25
    print(f"benchmark {nm}: CAGR {((s.iloc[-1]/s.iloc[0])**(1/yrs)-1)*100:.1f}%  maxDD {(s/s.cummax()-1).min()*100:.1f}%")
mo = es.resample("ME").last().pct_change().dropna() * 100
tbl = mo.groupby([mo.index.year, mo.index.month]).sum().unstack().round(1); tbl.to_csv(str(HERE / "out") + "/final_monthly.csv"); print(tbl.to_string())
dd = es / es.cummax() - 1; print("worst drawdown:", round(dd.min()*100, 1), "at", dd.idxmin().date(), "; peak", es[:dd.idxmin()].idxmax().date())
print("breadth by year:", pd.Series(breadth, index=p.dates).groupby(pd.to_datetime(p.dates).year).mean().round(2).to_dict())
