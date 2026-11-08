"""PM0.5: what moved signal-year 2024. Same model and test rows, three data sets:
A the 10 Sep export (bars 2017→, before the seam repair); B today's bars cut to 2017→; C today's bars
from 2011. A→B is the repair and other revisions; B→C is more history.

usage: python explore_2024_split.py <old-export-dir> <work-dir>      (writes out/split2024.csv)"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl

import build_dataset as B
from explore_multibagger import PARAMS, ROUNDS, TRAIN_STRIDE, grp, summarize
from portfolioml import tranches
from portfolioml.features import ALL_FEATURES
from portfolioml.labels import MultibaggerConfig

YEAR, HORIZON = 2024, 250
COLS = list(dict.fromkeys(["instrument_id", "date", "label", "held", "fwd_ret_h", "fwd_max_ret", "fwd_min_ret", "fwd_min_close_ret",
                           "entry_px", "close", "close_raw", "turnover20", *ALL_FEATURES]))


def build(data: Path) -> pl.DataFrame:
    B.DATA = data
    t0 = time.time()
    feat = B.build_features(t0)
    labels = B.build_labels(feat.select("instrument_id", "date", "open", "high", "low", "close"), MultibaggerConfig(up=1.0, horizon=HORIZON), t0)
    return feat.join(labels, on=["instrument_id", "date"], how="left").filter(pl.col("universe")).select(COLS).sort("date", "instrument_id")


def fit_score(frame: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    sessions = frame["date"].unique().sort().to_list()
    before = [d for d in sessions if d.year < YEAR]
    train_end = before[-(HORIZON + 6)]
    lab = frame.filter(pl.col("label").is_not_null())
    train = lab.filter((pl.col("date") <= train_end) & pl.col("date").is_in(before[::TRAIN_STRIDE])).sort("date")
    x = lambda df: df.select(ALL_FEATURES).to_numpy().astype(np.float32)  # noqa: E731
    model = lgb.train(PARAMS, lgb.Dataset(x(train), train["label"].to_numpy(), group=grp(train)), ROUNDS)
    test = lab.filter(pl.col("date").dt.year() == YEAR).sort("date")
    return test.with_columns(pl.Series("score", model.predict(x(test)))), train.height


def link(src: Path, dst: Path, names: list[str]) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for n in names:
        if not (dst / n).exists():
            os.symlink(src.resolve() / n, dst / n)


def main(old: Path, work: Path) -> None:
    new = Path("data")
    meta = ["instrument.csv.gz", "index_member_daily.csv.gz", "index_def.csv.gz"]
    a, b, c = work / "a_old", work / "b_2017", work / "c_2011"
    link(new, a, meta); link(old, a, ["ohlcv_daily.csv.gz", "index_series.csv"])
    link(new, b, [*meta, "index_series.csv"]); link(new, c, [*meta, "index_series.csv", "ohlcv_daily.csv.gz"])
    if not (b / "ohlcv_daily.parquet").exists():
        pl.read_csv(new / "ohlcv_daily.csv.gz", schema_overrides={"upper_circuit": pl.Float64, "lower_circuit": pl.Float64}).with_columns(
            pl.col("date").str.to_date()).filter(pl.col("date") >= pl.date(2017, 1, 1)).write_parquet(b / "ohlcv_daily.parquet")

    runs, frames = {}, {}
    for name, d in (("A_10sep_2017", a), ("B_repaired_2017", b), ("C_repaired_2011", c)):
        frame = build(d)
        test, n = fit_score(frame)
        own = summarize(test, "score")
        print(name, "train rows", n, "own-rows auc", round(own["auc"], 3), "top20", round(own["top20_hit"], 3), flush=True)
        runs[name] = (test, n)
        if name.startswith("C"):
            frames["c"] = frame
    common = None
    for test, _ in runs.values():
        keys = test.select("instrument_id", "date")
        common = keys if common is None else common.join(keys, on=["instrument_id", "date"])
    truth = runs["C_repaired_2011"][0].drop("score").join(common, on=["instrument_id", "date"])
    print("common 2024 test rows", truth.height, flush=True)
    invest = frames["c"].filter((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7))
    flat = truth.select("instrument_id", "date", pl.lit(0.0).alias("score"))
    u, _ = tranches.simulate(invest, flat, "score", tranches.TrancheConfig(top_n=10_000))
    rows = []
    for name, (test, n) in runs.items():
        scored = truth.join(test.select("instrument_id", "date", "score"), on=["instrument_id", "date"])
        s = summarize(scored, "score")
        p, _ = tranches.simulate(invest, scored.select("instrument_id", "date", "score"), "score", tranches.TrancheConfig())
        rows.append({"data": name, "train_rows": n, "auc": s["auc"], "base": s["base"], "top20_hit": s["top20_hit"], "top20_mean_12m": s["top20_fwd_mean"],
                     "universe_mean_12m": s["univ_fwd_mean"], "inv_top20_mean_12m": p["fwd_ret_h"].mean(), "inv_universe_mean_12m": u["fwd_ret_h"].mean(),
                     "inv_top20_hit": p["label"].mean(), "inv_universe_hit": u["label"].mean()})
    out = pl.DataFrame(rows); out.write_csv("out/split2024.csv")
    pl.Config.set_tbl_cols(20); pl.Config.set_tbl_width_chars(240); pl.Config.set_float_precision(3)
    print(out)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
