"""Can TWT-1's CAGR be improved without touching the signal?  (12 Sep 2026; write-up in IMPROVE.md)

Base = TWT-1 at the live liquidity bar (20-day turnover >= 5 cr): 22.5 % CAGR, -26.5 % DD.
Five levers, each a change to what the book does with capital rather than to which names it buys:
  1. idle cash earns a liquid-fund yield instead of 0            (Rules.idle_cash_yield_pct)
  2. dead money: out if still below entry after N sessions       (Rules.dead_money_sessions)
  3. pyramid once when the line is up X %, adding a fraction     (Rules.pyramid_trigger_pct / pyramid_frac)
  4. size by volatility (calm names bigger, capped)              (Rules.vol_sizing / vol_size_cap_pct)
  5. fewer slots (concentration)                                 (max_positions)
then the combinations, and the stop / trail re-checked on top of the survivors.
Outputs: out/improve.csv (levers), out/improve2.csv (combinations), out/improved_trades.csv,
out/improved_equity.csv, out/improved_yearly.csv for the recommended variant.
"""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
HERE = Path(__file__).absolute().parent; VB = HERE.parent / 'volume-breakout'
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(VB))
from vbt.sim import Rules, run
from tscan import entries
p, ind, sig = pickle.load(open(VB / 'data' / 'panel.pkl', 'rb')); state, parts = pickle.load(open(HERE / 'data' / 'state.pkl', 'rb'))
breadth = pd.read_csv(HERE / 'out' / 'breadth200.csv', index_col=0).iloc[:, 0].to_numpy()
start_col = int(np.searchsorted(p.dates, np.datetime64("2017-10-16"))); split = pd.Timestamp("2023-01-01")
raw = entries(state, 5)
with np.errstate(invalid="ignore"): sig5 = raw & np.nan_to_num(ind.turnover_sma20 >= 5e7, nan=False)   # the live setting
FINAL = dict(entry="next_open", max_positions=10, max_new_per_day=3, max_weight_pct=12.5, rank="turnover", cost_bps_side=25, stop_pct=20, trail_mode="pct", trail_pct=20)

def halves(res):
    e = pd.Series(res.equity, index=pd.to_datetime(res.dates)).dropna(); out = {}
    for tag, s in (("is", e[e.index < split]), ("oos", e[e.index >= split])):
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        out[f"{tag}_cagr"] = round(((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100, 1); out[f"{tag}_dd"] = round((s / s.cummax() - 1).min() * 100, 1)
    return out

def go(label, rows, **kw):
    r = Rules(name=label, **{**FINAL, **kw}); res = run(p, ind, sig5, r, gate_ok=breadth > 0.40, start_col=start_col)
    m = res.metrics(); m.update(halves(res)); m["label"] = label
    t = res.trades_df(); w = t[t.pnl > 0]; m["top10_share_pct"] = round(w.pnl.nlargest(10).sum() / w.pnl.sum() * 100) if len(w) else np.nan
    rows.append(m)
    print(f"{label:44s} CAGR {m['cagr_pct']:6.1f}% (IS {m['is_cagr']:6.1f} / OOS {m['oos_cagr']:6.1f})  DD {m['max_dd_pct']:6.1f}%  Calmar {m['calmar']:.2f}  Sh {m['sharpe']:5.2f}  n {m['trades']:4d}  win {m['win_rate_pct']}%  PF {m['profit_factor']} avg {m['avg_ret_pct']} hold {m['avg_hold']} top10 {m['top10_share_pct']}%  final ₹{m['final_equity']/1e5:.1f}L", flush=True)
    return res

if __name__ == "__main__":
    L = []
    print("== base (turnover >= 5 cr, the live setting)"); go("TWT-1 @5cr", L)
    print("== lever 1: idle cash in a liquid fund")
    for y in (6.0, 7.0): go(f"  idle cash yield {y}%", L, idle_cash_yield_pct=y)
    print("== lever 2: dead-money exit (below entry after N sessions -> out)")
    for n in (40, 60, 90, 120): go(f"  dead money after {n} sessions", L, dead_money_sessions=n)
    print("== lever 3: pyramid once when up X%, adding 50% of the original")
    for t in (10, 15, 25): go(f"  pyramid at +{t}%", L, pyramid_trigger_pct=t)
    go("  pyramid at +15%, add 100%", L, pyramid_trigger_pct=15, pyramid_frac=1.0)
    print("== lever 4: size by volatility (calm names bigger, cap 15%)")
    for c in (15, 20): go(f"  vol sizing cap {c}%", L, vol_sizing=True, vol_size_cap_pct=c)
    print("== lever 5: fewer slots (concentration)"); go("  8 slots", L, max_positions=8, max_weight_pct=15.6)
    pd.DataFrame(L).to_csv(HERE / "out" / "improve.csv", index=False)

    C = []
    print("== combinations")
    go("base @5cr", C)
    go("yield 6%", C, idle_cash_yield_pct=6)
    F = go("yield 6% + pyramid +10%/50%", C, idle_cash_yield_pct=6, pyramid_trigger_pct=10)
    go("yield 6% + pyramid +25%/50%", C, idle_cash_yield_pct=6, pyramid_trigger_pct=25)
    go("yield 6% + dead money 60", C, idle_cash_yield_pct=6, dead_money_sessions=60)
    go("yield 6% + pyramid +10%/50% + 8 slots", C, idle_cash_yield_pct=6, pyramid_trigger_pct=10, max_positions=8, max_weight_pct=15.6)
    go("yield 6% + 8 slots", C, idle_cash_yield_pct=6, max_positions=8, max_weight_pct=15.6)
    for k, v in (("stop_pct", 15), ("stop_pct", 25), ("trail_pct", 18), ("trail_pct", 22), ("trail_pct", 25)):
        go(f"yield 6% + pyramid 10 + {k.split('_')[0]} {v}", C, idle_cash_yield_pct=6, pyramid_trigger_pct=10, **{k: v})
    pd.DataFrame(C).to_csv(HERE / "out" / "improve2.csv", index=False)
    t = F.trades_df(); t.to_csv(HERE / "out" / "improved_trades.csv", index=False)
    pd.DataFrame({"date": F.dates, "equity": F.equity, "n_open": F.n_open}).to_csv(HERE / "out" / "improved_equity.csv", index=False)
    yr = F.yearly(); yr.to_csv(HERE / "out" / "improved_yearly.csv"); print(yr.to_string())
