"""PM0.13: whole-book exits on NIFTY Smallcap 250 regime signals, on the corrected 2014→ book. Reports every row, picks none.

usage: PM_DATA=<export> python explore_overlay.py       (run from the folder holding that export's out/from2014; writes out/overlay/)"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl

from portfolioml import overlay as O, tranches
from run_longrun import CFG, load

OUT = Path("out/overlay")
TERMINAL = "b"
MIN_DD_CUT, MAX_CAGR_COST = 0.25, 0.03
pl.Config.set_tbl_rows(60); pl.Config.set_tbl_width_chars(200); pl.Config.set_float_precision(3); pl.Config.set_tbl_cols(30)


def signals(idx: pl.DataFrame, ds: pl.DataFrame) -> pl.DataFrame:
    px = pl.col("smallcap250")
    f = idx.select("date", "smallcap250").drop_nulls().sort("date")
    breadth = ds.group_by("date").agg(pl.col("breadth200").first()).sort("date")
    return f.with_columns(*[px.rolling_mean(k, min_samples=k).alias(f"s{k}") for k in (10, 20, 50, 100, 200)]).join(breadth, on="date", how="left").with_columns(
        pl.col("breadth200").forward_fill()).with_columns(
        (px > pl.col("s200")).fill_null(True).alias("close_gt_200dma"), (px > pl.col("s100")).fill_null(True).alias("close_gt_100dma"), (px > pl.col("s50")).fill_null(True).alias("close_gt_50dma"),
        (pl.col("s10") > pl.col("s20")).fill_null(True).alias("dma10_gt_dma20"), (pl.col("breadth200") >= 0.4).fill_null(True).alias("breadth200_ge_40"))


NAMES = ["close_gt_200dma", "close_gt_100dma", "close_gt_50dma", "dma10_gt_dma20", "breadth200_ge_40"]


def one_book(book: str, detail: dict, sig: pl.DataFrame) -> tuple[list[dict], list[dict]]:
    days, slices, periods = detail["days"], detail["slices"], detail["periods"]
    grid = pl.DataFrame({"date": days}).join(sig, on="date", how="left")
    grid = grid.with_columns([pl.col(c).forward_fill().fill_null(True) for c in NAMES])
    arms = {"none": np.ones(len(days), dtype=bool)}
    for name in NAMES:
        raw = grid[name].to_numpy().astype(bool)
        arms[name] = O.positions(raw)
        arms[f"{name}_5s"] = O.positions(O.debounce(raw))
    rows, yearly = [], []
    for arm, held in arms.items():
        v, taxed = O.run(slices, periods, held, days), O.run(slices, periods, held, days, tax=True)
        rows.append({"book": book, "signal": arm, **O.stats(v, days, held), "cagr_after_tax": O.stats(taxed, days, held)["cagr"]})
        eq = pl.DataFrame({"date": days, "v": v})
        for (y,), g in eq.group_by(pl.col("date").dt.year(), maintain_order=True):
            yearly.append({"book": book, "signal": arm, "year": y, "ret": g["v"][-1] / g["v"][0] - 1})
    return rows, yearly


def verdicts(t: pl.DataFrame) -> pl.DataFrame:
    base = t.filter(pl.col("signal") == "none").select("book", pl.col("cagr").alias("c0"), pl.col("max_dd").alias("d0"))
    j = t.filter(pl.col("signal") != "none").join(base, on="book").with_columns(((pl.col("max_dd") - pl.col("d0")) * 100).alias("dd_cut_pts"), ((pl.col("c0") - pl.col("cagr")) * 100).alias("cagr_cost_pts"))
    j = j.with_columns(((pl.col("dd_cut_pts") >= MIN_DD_CUT * 100) & (pl.col("cagr_cost_pts") <= MAX_CAGR_COST * 100)).alias("pass"), pl.col("signal").str.replace("_5s", "").alias("rule"), pl.col("signal").str.ends_with("_5s").alias("slow"))
    return j


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ds, scores, marks, idx = load(TERMINAL, True)
    sig = signals(idx, ds)
    flat = scores.select("instrument_id", "date", pl.lit(0.0).alias("score"))
    books = {"top20_skip": (scores, CFG(top_n=20, skip_held=True, terminal=TERMINAL, cost_per_side=O.COST)), "investable_ew": (flat, CFG(top_n=10_000, terminal=TERMINAL, cost_per_side=O.COST))}
    rows, yearly = [], []
    for name, (s, cfg) in books.items():
        _, _, detail = tranches.simulate(ds, s, "score", cfg, marks=marks, detail=True)
        r, y = one_book(name, detail, sig)
        rows += r
        yearly += y
        print(name, flush=True)
    t = pl.DataFrame(rows)
    t.write_csv(OUT / "summary.csv")
    print(t)
    y = pl.DataFrame(yearly)
    y.write_csv(OUT / "yearly.csv")
    for b in books:
        print(b); print(y.filter(pl.col("book") == b).pivot(on="signal", index="year", values="ret").sort("year"))
    j = verdicts(t)
    j.write_csv(OUT / "verdicts.csv")
    print(j.select("book", "signal", "cagr", "max_dd", "dd_cut_pts", "cagr_cost_pts", "pass"))
    both = j.group_by("book", "rule").agg(pl.col("pass").all().alias("pass_plain_and_5s"))
    print(both.sort("book", "rule"))
    print("ANY RULE PASSES BOTH VERSIONS:", bool(both["pass_plain_and_5s"].any()))


if __name__ == "__main__":
    main()
