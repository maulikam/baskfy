"""Book-level levers on the existing scores: concentration, rank weighting, ML×momentum ensemble,
're-rank, don't sell'. Nothing here retrains; it is how the same ranking is *used*."""

from __future__ import annotations

import glob
import json
from pathlib import Path

import polars as pl

from portfolioml import panel, tranches

ds = pl.read_parquet("data/dataset_mb_u100_h250.parquet").filter(pl.col("universe"))
scores = pl.concat([pl.read_parquet(p) for p in sorted(glob.glob("out/mb_scores_mb_u100_h250_touch_*.parquet"))])
sc = scores.join(ds.select("instrument_id", "date", "mom_12_1_cs", "r252_cs"), on=["instrument_id", "date"]).with_columns(
    (pl.col("score").rank("average").over("date") / pl.len().over("date")).alias("ml_pct"))
sc = sc.with_columns(
    (0.5 * pl.col("ml_pct") + 0.5 * pl.col("mom_12_1_cs")).alias("ens50"),
    ((2 * pl.col("ml_pct") + pl.col("mom_12_1_cs")) / 3).alias("ens67"),
    (pl.min_horizontal("ml_pct", "mom_12_1_cs")).alias("ens_min"),  # must be high on both
)
bench = panel.load_index_series(Path("data")).select("date", "nifty50", "midcap150", "smallcap250")
inv = ds.filter((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7))
T = tranches.TrancheConfig
sims = {
    "ml_top20": ("score", T()),
    "ml_top10": ("score", T(top_n=10)),
    "ml_top20_rankw": ("score", T(weighting="rank")),
    "ml_top10_rankw": ("score", T(top_n=10, weighting="rank")),
    "ens50_top20": ("ens50", T()),
    "ens67_top20": ("ens67", T()),
    "ensmin_top20": ("ens_min", T()),
    "ens50_top10": ("ens50", T(top_n=10)),
    "ml_top20_keep25": ("score", T(keep_if_top_frac=0.25)),
    "ml_top20_keep10": ("score", T(keep_if_top_frac=0.10)),
    "ens50_top20_keep25": ("ens50", T(keep_if_top_frac=0.25)),
    "ens50_top10_keep25": ("ens50", T(top_n=10, keep_if_top_frac=0.25)),
    "ens50_top10_keep25_rankw": ("ens50", T(top_n=10, keep_if_top_frac=0.25, weighting="rank")),
}
rows = []
Path("out/book_levers").mkdir(exist_ok=True)
for name, (col, cfg) in sims.items():
    picks, eq = tranches.simulate(inv, sc, col, cfg)
    s = {"sim": name, **tranches.summary(eq, picks, bench), "kept_share": picks["kept"].mean()}
    rows.append(s)
    eq.write_csv(f"out/book_levers/equity_{name}.csv")
    tranches.yearly(eq).write_csv(f"out/book_levers/yearly_{name}.csv")
    print(f"{name:28s} cagr {s['cagr']:+.3f} dd {s['max_dd']:.3f} sharpe {s['sharpe']:.2f} picks {s['picks']} touch2x {s['pct_doubled_touch']:.3f} pos {s['pct_pos_12m']:.3f} med {s['median_12m']:.3f} kept {s['kept_share']:.2f}", flush=True)
pl.DataFrame(rows).write_csv("out/book_levers/summary.csv")
