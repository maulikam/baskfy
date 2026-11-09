"""PM0.8: regime gates on the book from 2014, each with and without. Reports all; picks none.

usage: python explore_gates.py [--terminal=a|b] [--marks]     (writes out/from2014/gates*.csv)"""

from __future__ import annotations

import sys

import polars as pl

from portfolioml import tranches
from run_longrun import CFG, OUT, load, options, per_year

GATES = {"none": (None, 0.0), "smallcap250_above_50dma": ("smallcap250_vs_sma50", 0.0),
         "breadth200_ge_40": ("breadth200", 0.40), "breadth200_ge_50": ("breadth200", 0.50)}
BOOKS = {"top10_skip": {"top_n": 10, "skip_held": True}, "top20_skip": {"top_n": 20, "skip_held": True}, "top20_rebuy": {"top_n": 20}}


def main(argv: list[str]) -> None:
    terminal, use_marks, suffix = options(argv)
    ds, scores, marks, idx = load(terminal, use_marks)
    totals, yearly = [], []
    for book, kw in BOOKS.items():
        for gate, (col, floor) in GATES.items():
            picks, eq = tranches.simulate(ds, scores, "score", CFG(gate_col=col, gate_min=floor, terminal=terminal, **kw), marks=marks)
            t = tranches.summary(eq, picks, idx)
            totals.append({"book": book, "gate": gate, **{k: t[k] for k in ("cagr", "max_dd", "sharpe", "picks", "mean_12m", "median_12m")}})
            yearly.append(per_year(eq, f"{book}|{gate}"))
            print(book, gate, flush=True)
    pl.Config.set_tbl_rows(60); pl.Config.set_tbl_width_chars(200); pl.Config.set_float_precision(3)
    out = pl.DataFrame(totals)
    out.write_csv(OUT / f"gates{suffix}.csv"); print(out)
    pl.concat(yearly).write_csv(OUT / f"gates_yearly{suffix}.csv")


if __name__ == "__main__":
    main(sys.argv[1:])
