"""PM0.4/PM0.8: the §4 book from 2014 on the twelve-year walk-forward scores. Writes out/from2014/.

usage: python run_longrun.py [--terminal=a|b] [--marks]
--terminal reads the dataset with dead names; --marks alone re-marks the survivors book on every bar."""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

import numpy as np
import polars as pl

from portfolioml import panel, tranches

G = "mb_u100_h250"
DATA = Path(os.environ.get("PM_DATA", "data"))
OUT = Path("out/from2014")
CFG = tranches.TrancheConfig


def per_year(eq: pl.DataFrame, name: str) -> pl.DataFrame:
    rows = []
    for (y,), g in eq.sort("date").group_by(pl.col("date").dt.year(), maintain_order=True):
        e = g["equity"].to_numpy()
        r = np.diff(np.log(e))
        rows.append({"year": y, "sim": name, "ret": e[-1] / e[0] - 1, "max_dd": (e / np.maximum.accumulate(e) - 1).min(),
                     "sharpe": r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else None})
    return pl.DataFrame(rows)


def options(argv: list[str]) -> tuple[str | None, bool, str]:
    terminal = next((a.split("=")[1] for a in argv if a.startswith("--terminal=")), None)
    use_marks = terminal is not None or "--marks" in argv
    return terminal, use_marks, f"_t{terminal}" if terminal else "_marks" if use_marks else ""


def load(terminal: str | None, use_marks: bool) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame | None, pl.DataFrame]:
    """Investable rows, scores, every bar for marking, benchmarks."""
    g = G + (f"_t{terminal}" if terminal else "")
    lf = pl.scan_parquet(DATA / f"dataset_{g}.parquet")
    ds = lf.filter(pl.col("universe") & (pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7)).collect()
    scores = pl.concat([pl.read_parquet(p) for p in sorted(glob.glob(f"{OUT}/mb_scores_{g}_touch_*.parquet"))])
    marks = lf.select("instrument_id", "date", "close", *(["terminal"] if terminal else [])).collect() if use_marks else None
    idx = panel.load_index_series(DATA).select("date", "nifty50", "midcap150", "smallcap250").sort("date")
    return ds, scores, marks, idx


def main(argv: list[str]) -> None:
    terminal, use_marks, suffix = options(argv)
    ds, scores, marks, idx = load(terminal, use_marks)
    flat = scores.select("instrument_id", "date", pl.lit(0.0).alias("score"))
    sims = {"top10_skip": (scores, CFG(top_n=10, skip_held=True, terminal=terminal)),
            "top20_skip": (scores, CFG(top_n=20, skip_held=True, terminal=terminal)),
            "top10_rebuy": (scores, CFG(top_n=10, terminal=terminal)), "top20_rebuy": (scores, CFG(top_n=20, terminal=terminal)),
            "investable_ew": (flat, CFG(top_n=10_000, terminal=terminal))}
    pl.Config.set_tbl_rows(60); pl.Config.set_tbl_width_chars(200); pl.Config.set_float_precision(3)
    totals, yearly = [], []
    for name, (s, cfg) in sims.items():
        picks, eq = tranches.simulate(ds, s, "score", cfg, marks=marks)
        t = tranches.summary(eq, picks, idx)
        totals.append({"sim": name, **{k: t[k] for k in ("start", "end", "cagr", "max_dd", "sharpe", "picks", "pct_doubled_touch", "mean_12m", "median_12m", "smallcap250_cagr", "smallcap250_max_dd")}})
        yearly.append(per_year(eq, name))
        print(name, flush=True)
    sc = idx.filter(pl.col("date") >= pl.date(2014, 1, 1)).select("date", pl.col("smallcap250").alias("equity")).drop_nulls()
    yearly.append(per_year(sc, "smallcap250"))
    pl.DataFrame(totals).write_csv(OUT / f"book_totals{suffix}.csv"); print(pl.DataFrame(totals))
    y = pl.concat(yearly); y.write_csv(OUT / f"book_yearly{suffix}.csv")
    print(y.pivot(on="sim", index="year", values="ret").sort("year"))
    print(y.pivot(on="sim", index="year", values="max_dd").sort("year"))
    print(y.pivot(on="sim", index="year", values="sharpe").sort("year"))


if __name__ == "__main__":
    main(sys.argv[1:])
