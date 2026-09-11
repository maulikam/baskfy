"""The sleeve drawdown lock-out test referenced in STRATEGY.md §3 — tested, not adopted."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
HERE = Path(__file__).absolute().parent; VB = HERE.parent / 'volume-breakout'
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(VB))
from vbt.sim import Rules, run
p, ind, sig = pickle.load(open(VB / 'data' / 'panel.pkl', 'rb')); sigM = pickle.load(open(HERE / 'data' / 'sigM.pkl', 'rb'))
breadth = pd.read_csv(HERE / 'out' / 'breadth200.csv', index_col=0).iloc[:, 0].to_numpy()
start_col = int(np.searchsorted(p.dates, np.datetime64("2017-10-16")))
FINAL = dict(entry="next_open", max_positions=10, max_new_per_day=3, max_weight_pct=12.5, rank="turnover", cost_bps_side=25, stop_pct=15, exit_close_below_sma=50)
def go(label, gate=None, **kw):
    res = run(p, ind, sigM, Rules(name=label, **{**FINAL, **kw}), gate_ok=gate, start_col=start_col); m = res.metrics()
    print(f"{label:40s} CAGR {m['cagr_pct']:6.1f}%  DD {m['max_dd_pct']:6.1f}%  Calmar {m['calmar']}  Sh {m['sharpe']}  n {m['trades']}  exp {m['exposure_pct']}%  lock-skips {res.skipped['dd_lock']}")
go("MOM-1 no lock")
for lock, cd in [(10, 20), (15, 20), (15, 40), (20, 20), (20, 40), (10, 40)]:
    go(f"lock={lock}% cooldown={cd}", dd_lock_pct=lock, dd_cooldown=cd)
go("lock=15/20 + breadth>40", gate=breadth > 0.40, dd_lock_pct=15, dd_cooldown=20)
go("lock=15/40 + breadth>35", gate=breadth > 0.35, dd_lock_pct=15, dd_cooldown=40)
