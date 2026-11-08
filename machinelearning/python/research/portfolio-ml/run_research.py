"""Step 2: data/dataset.parquet → out/*.csv (walk-forward scores, rank quality, book sims)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import polars as pl

from portfolioml import book, metrics, model, panel

HERE = Path(__file__).parent
DATA, OUT = HERE / "data", HERE / "out"


def main(argv: list[str]) -> None:
    t0 = time.time()
    geometry = next((a for a in argv if a.startswith("u")), "u10_d5_h20")
    ds = pl.read_parquet(DATA / f"dataset_{geometry}.parquet")
    out = OUT / geometry
    out.mkdir(parents=True, exist_ok=True)
    cfg = model.ModelConfig()
    if "--quick" in argv:
        cfg = model.ModelConfig(rounds=150, early_stop=30)

    if (out / "scores.parquet").exists() and "--rescore" not in argv:
        scores = pl.read_parquet(out / "scores.parquet")
        imps = pl.read_csv(out / "importance.csv")
    else:
        scores, imps = model.walk_forward(ds, cfg)
        scores.write_parquet(out / "scores.parquet")
        imps.write_csv(out / "importance.csv")
    print(f"scored {scores.shape[0]:,} rows  {time.time()-t0:.0f}s", flush=True)

    imp = imps.group_by("feature").agg(pl.col("gain").mean()).with_columns(
        (pl.col("gain") / pl.col("gain").sum()).alias("share")).sort("gain", descending=True)
    imp.write_csv(out / "importance_mean.csv")
    print(imp.head(20))

    bl = model.baselines(ds).join(scores.select("instrument_id", "date"), on=["instrument_id", "date"], how="semi")
    allscores = scores.join(bl, on=["instrument_id", "date"], how="left")
    quality = {}
    for col in ("score", "bl_mom12_1", "bl_r63", "bl_near_high"):
        q = metrics.rank_quality(ds, allscores, col)
        q.write_csv(out / f"rank_quality_{col}.csv")
        quality[col] = q
        print(col); print(q)
    metrics.decile_table(ds, allscores, "score").write_csv(out / "deciles_score.csv")
    print(pl.read_csv(out / "deciles_score.csv"))

    idx = panel.load_index_series(DATA)
    bench = idx.select("date", "nifty50", "midcap150", "smallcap250")
    sims = {
        "ml_top": ("score", book.BookConfig()),
        "ml_top_gated": ("score", book.BookConfig(gate_col="breadth200", gate_min=0.40)),
        "ml_top_gated50": ("score", book.BookConfig(gate_col="breadth200", gate_min=0.50)),
        "ml_top_gated_sc50": ("score", book.BookConfig(gate_col="smallcap250_vs_sma50", gate_min=0.0)),
        "bl_mom12_1_gated": ("bl_mom12_1", book.BookConfig(gate_col="breadth200", gate_min=0.40)),
        "bl_mom12_1": ("bl_mom12_1", book.BookConfig()),
        "bl_r63": ("bl_r63", book.BookConfig()),
        "bl_near_high": ("bl_near_high", book.BookConfig()),
    }
    rows = []
    for name, (col, bc) in sims.items():
        tr, eq = book.simulate(ds, allscores, col, bc)
        if tr.is_empty():
            print(name, "no trades"); continue
        s = book.summary(eq, tr, bench, bc.slots)
        s["sim"] = name
        rows.append(s)
        tr.write_csv(out / f"trades_{name}.csv")
        eq.write_csv(out / f"equity_{name}.csv")
        book.yearly(eq, bench).write_csv(out / f"yearly_{name}.csv")
        print(name, json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in s.items()}), flush=True)
    pl.DataFrame(rows).write_csv(out / "book_summary.csv")
    print(pl.read_csv(out / "yearly_ml_top.csv"))
    print(pl.read_csv(out / "yearly_ml_top_gated.csv"))
    print(f"done {time.time()-t0:.0f}s")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
