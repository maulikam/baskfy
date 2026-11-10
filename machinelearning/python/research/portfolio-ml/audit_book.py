"""PM0.12: the 2014→ book (top-20 skip-held, variant b, full marks) and its pick outcomes, for one export.

usage: PM_DATA=<export> python audit_book.py <label>       (run from the folder holding that export's out/from2014)"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

from portfolioml import tranches
from run_longrun import CFG, load


def main(label: str) -> None:
    ds, scores, marks, idx = load("b", True)
    picks, eq = tranches.simulate(ds, scores, "score", CFG(top_n=20, skip_held=True, terminal="b"), marks=marks)
    t = tranches.summary(eq, picks, idx)
    r = picks["fwd_ret_h"]
    row = {"label": label, "picks": picks.height, "halved": (r <= -0.5).mean(), "minus_75": (r <= -0.75).mean(), "cagr": t["cagr"], "max_dd": t["max_dd"], "sharpe": t["sharpe"], "hit": picks["label"].mean(), "median_12m": r.median(), "universe_rows": ds.height}
    Path("out/audit").mkdir(parents=True, exist_ok=True)
    pl.DataFrame([row]).write_csv(f"out/audit/book_{label}.csv")
    print(row)


if __name__ == "__main__":
    main(sys.argv[1])
