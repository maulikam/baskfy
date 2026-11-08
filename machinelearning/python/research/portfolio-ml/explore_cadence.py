"""PM0.2/PM0.3: skip-held before/after, and entry cadence, on the walk-forward scores through
``tranches.simulate``. Writes out/book_rules/."""

from __future__ import annotations

import glob
from pathlib import Path

import polars as pl

from portfolioml import panel, tranches

G = "mb_u100_h250"
OUT = Path("out/book_rules")
CFG = tranches.TrancheConfig


def row(name: str, picks: pl.DataFrame, eq: pl.DataFrame, bench: pl.DataFrame) -> dict:
    s = tranches.summary(eq, picks, bench)
    return {"sim": name, "cagr": s["cagr"], "max_dd": s["max_dd"], "sharpe": s["sharpe"], "picks": picks.height,
            "distinct_names": picks["instrument_id"].n_unique(), "hit_rate": picks["label"].mean(),
            "mean_pick": picks["fwd_ret_h"].mean(), "median_pick": picks["fwd_ret_h"].median()}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ds = pl.read_parquet(f"data/dataset_{G}.parquet").filter(pl.col("universe") & (pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7))
    scores = pl.concat([pl.read_parquet(p) for p in sorted(glob.glob(f"out/mb_scores_{G}_touch_*.parquet"))])
    bench = panel.load_index_series(Path("data")).select("date", "nifty50", "midcap150", "smallcap250")
    pl.Config.set_tbl_rows(40); pl.Config.set_tbl_width_chars(200); pl.Config.set_float_precision(3)

    held, years = [], []
    for n in (10, 20):
        for skip in (False, True):
            name = f"top{n}_{'skip' if skip else 'rebuy'}"
            picks, eq = tranches.simulate(ds, scores, "score", CFG(top_n=n, skip_held=skip))
            held.append(row(name, picks, eq, bench))
            years.append(tranches.yearly(eq, bench).select("year", pl.col("equity").alias(name)))
            print(name, flush=True)
    t = pl.DataFrame(held); t.write_csv(OUT / "skip_held.csv"); print(t)
    y = years[0]
    for other in years[1:]:
        y = y.join(other, on="year")
    y.write_csv(OUT / "skip_held_yearly.csv"); print(y)

    cadence = {"monthly_top10": CFG(top_n=10, skip_held=True), "monthly_top20": CFG(top_n=20, skip_held=True),
               "fortnightly_top5": CFG(top_n=5, cadence_sessions=10, skip_held=True),
               "weekly_top2": CFG(top_n=2, cadence_sessions=5, skip_held=True),
               "every2_top1": CFG(top_n=1, cadence_sessions=2, skip_held=True),
               "daily_top1": CFG(top_n=1, cadence_sessions=1, skip_held=True)}
    rows = []
    for name, cfg in cadence.items():
        picks, eq = tranches.simulate(ds, scores, "score", cfg)
        rows.append(row(name, picks, eq, bench))
        print(name, flush=True)
    c = pl.DataFrame(rows); c.write_csv(OUT / "cadence.csv"); print(c)


if __name__ == "__main__":
    main()
