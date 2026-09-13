"""Combining the scans: one book fed by several sleeves, versus split capital, versus confluence.

Three different things "combine" can mean, all tested on the same bars, capital and costs:
  A. split capital  — separate books, each sleeve on its own slice (what most people do)
  B. shared book    — one pool of capital and slots, every sleeve's signals compete for them,
                      each position managed by its own sleeve's exit (vbt.sim.run_book)
  C. confluence     — only trade a name when two scans agree (intersection), or trade the union
                      of the scans under one exit
Sleeves: VBT-1 (volume breakout, limit-at-close entry, 12 % stop, exit close < 21-EMA),
TWT-1 (three-weeks-tight, next-open, 20 % stop, 20 % trail; turnover >= 5 cr as it goes live),
MOM-1g (momentum, next-open, 15 % stop, exit close < 50-SMA, breadth > 35 %).
Outputs: out/combo.csv, out/combo.log, out/combo_book_trades.csv, out/combo_book_equity.csv
"""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
HERE = Path(__file__).absolute().parent; VB = HERE.parent / 'volume-breakout'; TC = HERE.parent / 'tight-close'; MS = HERE.parent / 'momentum-scan'
sys.path.insert(0, str(VB)); sys.path.insert(0, str(TC)); sys.path.insert(0, str(MS))
from vbt.sim import Rules, Sleeve, run, run_book
from vbt.data import load_index
from tscan import entries as t_entries
p, ind, sig = pickle.load(open(VB / 'data' / 'panel.pkl', 'rb'))
sigV = pickle.load(open(VB / 'data' / 'sig2.pkl', 'rb'))                                   # VBT-1 signals (filters incl. turnover >= 2 cr)
tstate, tparts = pickle.load(open(TC / 'data' / 'state.pkl', 'rb'))                        # three-weeks-tight state
sigM = pickle.load(open(MS / 'data' / 'sigM.pkl', 'rb'))                                   # MOM-1 signals
mstate, mparts = pickle.load(open(MS / 'data' / 'state.pkl', 'rb'))                        # momentum state
breadth = pd.read_csv(HERE / 'out' / 'breadth200.csv', index_col=0).iloc[:, 0].to_numpy()
with np.errstate(invalid="ignore"):
    liq5 = np.nan_to_num(ind.turnover_sma20 >= 5e7, nan=False); liq2 = np.nan_to_num(ind.turnover_sma20 >= 2e7, nan=False)
tent = t_entries(tstate, 5); sigT = tent & liq5; sigT2 = tent & liq2
start_col = int(np.searchsorted(p.dates, np.datetime64("2017-10-16"))); split = pd.Timestamp("2023-01-01")
G40 = breadth > 0.40; G35 = breadth > 0.35
RV = dict(entry="limit_close", entry_valid=3, rank="turnover", stop_pct=12, exit_close_below_ema=21)
RT = dict(entry="next_open", rank="turnover", stop_pct=20, trail_mode="pct", trail_pct=20)
RM = dict(entry="next_open", rank="turnover", stop_pct=15, exit_close_below_sma=50)
BOOK = dict(max_positions=10, max_new_per_day=3, max_weight_pct=12.5, cost_bps_side=25, max_pct_of_turnover=1.0)

def halves(e):
    e = e.dropna(); out = {}
    for tag, s in (("is", e[e.index < split]), ("oos", e[e.index >= split])):
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        out[f"{tag}_cagr"] = round(((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100, 1); out[f"{tag}_dd"] = round((s / s.cummax() - 1).min() * 100, 1)
    return out

def curve_metrics(e):
    e = e.dropna(); yrs = (e.index[-1] - e.index[0]).days / 365.25; dd = e / e.cummax() - 1; r = e.pct_change().dropna()
    cagr = ((e.iloc[-1] / e.iloc[0]) ** (1 / yrs) - 1) * 100
    return dict(cagr_pct=round(cagr, 2), max_dd_pct=round(dd.min() * 100, 2), calmar=round(cagr / 100 / abs(dd.min()), 2), sharpe=round(r.mean() / r.std() * 252 ** 0.5, 2), final_equity=round(e.iloc[-1]))

ROWS = []
def report(label, m, extra=""):
    m["label"] = label; ROWS.append(m)
    print(f"{label:58s} CAGR {m['cagr_pct']:6.1f}% (IS {m['is_cagr']:6.1f} / OOS {m['oos_cagr']:6.1f})  DD {m['max_dd_pct']:6.1f}%  Calmar {m['calmar']:.2f}  Sh {m['sharpe']:5.2f}  n {m.get('trades', 0):4d}  PF {m.get('profit_factor', '')} exp {m.get('exposure_pct', '')}%  final ₹{m['final_equity']/1e5:.1f}L {extra}", flush=True)

def single(label, s, rules, gate, **bk):
    r = Rules(name=label, **{**BOOK, **bk, **rules}); res = run(p, ind, s, r, gate_ok=gate, start_col=start_col)
    m = res.metrics(); m.update(halves(pd.Series(res.equity, index=pd.to_datetime(res.dates)))); report(label, m); return res

def book(label, sleeves, **bk):
    B = Rules(name=label, **{**BOOK, **bk}); res = run_book(p, ind, sleeves, book=B, start_col=start_col)
    m = res.metrics(); m.update(halves(pd.Series(res.equity, index=pd.to_datetime(res.dates))))
    t = res.trades_df(); by = t.groupby("sleeve").agg(n=("pnl", "size"), pnl=("pnl", "sum")) if len(t) else pd.DataFrame()
    extra = " | " + ", ".join(f"{k}: {v.n} trades, ₹{v.pnl/1e5:.1f}L" for k, v in by.iterrows())
    for k, v in by.iterrows(): m[f"n_{k}"] = int(v.n); m[f"pnl_{k}"] = round(v.pnl)
    report(label, m, extra); return res

def blend(label, results, weights=None, rebalance=None):
    """Split capital: separate books. rebalance=None: buy-and-hold slices; 'D': daily rebalanced (return blend)."""
    curves = [pd.Series(r.equity, index=pd.to_datetime(r.dates)) for r in results]; curves = [c / c.dropna().iloc[0] for c in curves]
    w = weights or [1 / len(curves)] * len(curves)
    if rebalance == "D":
        rets = sum(wi * c.pct_change().fillna(0) for wi, c in zip(w, curves)); e = (1 + rets).cumprod() * 1e6
    else:
        e = sum(wi * c for wi, c in zip(w, curves)) * 1e6
    m = curve_metrics(e); m.update(halves(e)); m["trades"] = sum(len(r.trades) for r in results); report(label, m); return e

def recent(state, k):
    """True if the state was True at any of the last k sessions (inclusive)."""
    cs = np.cumsum(state, axis=1, dtype=np.int32); prev = np.zeros_like(cs); prev[:, k:] = cs[:, :-k]; return (cs - prev) > 0

if __name__ == "__main__":
    print("signals: VBT-1", sigV.sum(), " TWT-1@5cr", sigT.sum(), " TWT-1@2cr", sigT2.sum(), " MOM-1", sigM.sum())
    print("\n== reference: each sleeve alone, ₹10 L, 10 slots")
    V = single("VBT-1 alone", sigV, RV, G40)
    T = single("TWT-1 alone (@5cr)", sigT, RT, G40)
    T2 = single("TWT-1 alone (@2cr)", sigT2, RT, G40)
    M = single("MOM-1g alone", sigM, RM, G35)
    print("\n== A. split capital (separate books)")
    blend("50/50 VBT-1 + TWT-1, buy-and-hold slices", [V, T])
    blend("50/50 VBT-1 + TWT-1, daily rebalanced", [V, T], rebalance="D")
    blend("1/3 each VBT-1 + TWT-1 + MOM-1g, buy-and-hold slices", [V, T, M])
    blend("1/3 each, daily rebalanced", [V, T, M], rebalance="D")
    print("\n== B. one shared book")
    sv = lambda **kw: Sleeve("VBT", sigV, Rules(name="VBT", **{**BOOK, **RV, **kw}), G40)
    st = lambda **kw: Sleeve("TWT", sigT, Rules(name="TWT", **{**BOOK, **RT, **kw}), G40)
    sm = lambda **kw: Sleeve("MOM", sigM, Rules(name="MOM", **{**BOOK, **RM, **kw}), G35)
    BK = book("book: TWT first, then VBT — 10 slots, 3/day each", [st(), sv()])
    book("book: VBT first, then TWT — 10 slots", [sv(), st()])
    book("book: TWT + VBT — 12 slots", [st(), sv()], max_positions=12, max_weight_pct=10.4)
    book("book: TWT + VBT — 15 slots, 5/day", [st(max_new_per_day=5), sv(max_new_per_day=5)], max_positions=15, max_weight_pct=8.3)
    book("book: TWT + VBT — 20 slots, 5/day", [st(max_new_per_day=5), sv(max_new_per_day=5)], max_positions=20, max_weight_pct=6.25)
    book("book: TWT (cap 6) + VBT (cap 6) — 10 slots", [Sleeve("TWT", sigT, Rules(name="TWT", **{**BOOK, **RT}), G40, max_positions=6), Sleeve("VBT", sigV, Rules(name="VBT", **{**BOOK, **RV}), G40, max_positions=6)])
    book("book: TWT (cap 5) + VBT (cap 5) — 10 slots", [Sleeve("TWT", sigT, Rules(name="TWT", **{**BOOK, **RT}), G40, max_positions=5), Sleeve("VBT", sigV, Rules(name="VBT", **{**BOOK, **RV}), G40, max_positions=5)])
    book("book: TWT + VBT + MOM — 10 slots", [st(), sv(), sm()])
    book("book: TWT + VBT + MOM — 15 slots, 5/day", [st(max_new_per_day=5), sv(max_new_per_day=5), sm(max_new_per_day=5)], max_positions=15, max_weight_pct=8.3)
    book("book: TWT + VBT — 10 slots, +yield 6%", [st(), sv()], idle_cash_yield_pct=6)
    BKF = book("book: TWT(+pyramid 10) + VBT — 10 slots, +yield 6%", [st(pyramid_trigger_pct=10), sv()], idle_cash_yield_pct=6)
    book("book: TWT(+pyramid 10) + VBT — 12 slots, +yield 6%", [st(pyramid_trigger_pct=10), sv()], max_positions=12, max_weight_pct=10.4, idle_cash_yield_pct=6)
    book("book: TWT@2cr + VBT — 10 slots", [Sleeve("TWT", sigT2, Rules(name="TWT", **{**BOOK, **RT}), G40), sv()])
    print("\n== C. confluence and union")
    single("union VBT-1 | TWT-1, TWT exit (20/20)", sigV | sigT, RT, G40)
    single("union VBT-1 | TWT-1, VBT exit (limit, 12 % stop, 21-EMA)", sigV | sigT, RV, G40)
    mom = np.nan_to_num(mparts["r30"] | mparts["r90"], nan=False)
    c1 = sigT & mom; print("  TWT entry & momentum state (r30|r90):", c1.sum()); single("TWT-1 & in momentum state, TWT exit", c1, RT, G40)
    c2 = sigT & recent(sigV, 20); print("  TWT entry & VBT-1 signal in last 20:", c2.sum()); single("TWT-1 & VBT-1 fired in last 20 sessions, TWT exit", c2, RT, G40)
    c3 = sigV & recent(tstate, 10); print("  VBT signal & tight in last 10:", c3.sum()); single("VBT-1 & was 3-weeks-tight in last 10, VBT exit", c3, RV, G40); single("  same, TWT exit (20/20)", c3, RT, G40)
    c4 = sigV & recent(tstate, 25); print("  VBT signal & tight in last 25:", c4.sum()); single("VBT-1 & was 3-weeks-tight in last 25, VBT exit", c4, RV, G40); single("  same, TWT exit (20/20)", c4, RT, G40)
    c5 = sigV & ~recent(tstate, 25); single("VBT-1 & NOT tight in last 25 (the complement)", c5, RV, G40)
    c6 = sigT & mom & liq5; single("TWT-1 & momentum & VBT-style close>200-DMA", c6 & np.nan_to_num(p.close > ind.sma50, nan=False), RT, G40)
    pd.DataFrame(ROWS).to_csv(HERE / "out" / "combo.csv", index=False)
    t = BKF.trades_df(); t.to_csv(HERE / "out" / "combo_book_trades.csv", index=False)
    pd.DataFrame({"date": BKF.dates, "equity": BKF.equity, "n_open": BKF.n_open}).to_csv(HERE / "out" / "combo_book_equity.csv", index=False)
    print(BKF.yearly().to_string()); print(BK.yearly().to_string())
    es = pd.Series(BKF.equity, index=pd.to_datetime(BKF.dates)).dropna()
    for nm in ["NIFTY MIDCAP 150", "NIFTY 50"]:
        d, c = load_index(HERE / 'data' / 'index_series.csv', nm); s = pd.Series(c, index=pd.to_datetime(d)).reindex(es.index, method="ffill"); yrs = (s.index[-1] - s.index[0]).days / 365.25
        print(f"benchmark {nm}: CAGR {((s.iloc[-1]/s.iloc[0])**(1/yrs)-1)*100:.1f}%  maxDD {(s/s.cummax()-1).min()*100:.1f}%")
