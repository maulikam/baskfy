"""Walk-forward lambdarank on the multibagger label. Per test year: does the top of the list
double more often than the universe, and what does holding it 12/24 months return?

usage: python explore_multibagger.py data/dataset_mb_u100_h250.parquet [years...] [--first-test-year=2014]"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl
from sklearn.metrics import roc_auc_score

from portfolioml.features import ALL_FEATURES

PARAMS = {"objective": "lambdarank", "metric": "ndcg", "eval_at": [20], "lambdarank_truncation_level": 60,
          "learning_rate": 0.03, "num_leaves": 31, "min_child_samples": 200, "feature_fraction": 0.6,
          "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 10.0, "verbose": -1, "num_threads": 2, "seed": 7}
ROUNDS = 300
TRAIN_STRIDE = 5  # one signal date in five: 12-month labels on adjacent days are ~identical
BASELINES = {"bl_mom12_1": "mom_12_1_cs", "bl_r252": "r252_cs", "bl_near_high": "dist_hi252_cs",
             "bl_small_turnover": "log_turnover20_cs", "bl_far_from_ath": "dist_ath_cs"}


def grp(df):
    return df.group_by("date", maintain_order=True).agg(pl.len())["len"].to_numpy()


def summarize(d: pl.DataFrame, col: str, descending: bool = True) -> dict:
    d = d.filter(pl.col(col).is_not_null()).with_columns(
        pl.col(col).rank("ordinal", descending=descending).over("date").alias("rk"), pl.len().over("date").alias("n"))
    top, dec = d.filter(pl.col("rk") <= 20), d.filter(pl.col("rk") <= pl.col("n") * 0.1)
    return {
        "auc": roc_auc_score(d["label"], d[col] if descending else -d[col]) if 0 < d["label"].mean() < 1 else np.nan,
        "base": d["label"].mean(), "top20_hit": top["label"].mean(), "topdec_hit": dec["label"].mean(),
        "top20_held": top["held"].mean(), "univ_held": d["held"].mean(),
        "top20_clean": ((top["label"] == 1) & (top["fwd_min_close_ret"] > -0.4)).mean(),
        "univ_clean": ((d["label"] == 1) & (d["fwd_min_close_ret"] > -0.4)).mean(),
        "top20_rt": ((top["label"] == 1) & (top["fwd_min_close_ret"] <= -0.4)).mean(),
        "top20_fwd_mean": top["fwd_ret_h"].mean(), "top20_fwd_median": top["fwd_ret_h"].median(),
        "univ_fwd_mean": d["fwd_ret_h"].mean(), "univ_fwd_median": d["fwd_ret_h"].median(),
        "top20_min_mean": top["fwd_min_ret"].mean(), "univ_min_mean": d["fwd_min_ret"].mean(),
    }


def target(df: pl.DataFrame, kind: str) -> np.ndarray:
    """touch: hit 2x intraday at any point; held: ≥2x at the horizon close; graded: 0/1/2/3 by
    horizon return (<0, 0–50%, 50–100%, ≥100%) for lambdarank's label_gain."""
    if kind == "touch":
        return df["label"].to_numpy()
    if kind == "held":
        return df["held"].to_numpy()
    if kind.startswith("clean"):  # clean40 → touched 2× and never closed below −40%
        dd = -int(kind[5:] or 40) / 100
        return ((df["label"] == 1) & (df["fwd_min_close_ret"] > dd)).to_numpy().astype(np.int8)
    r = df["fwd_ret_h"].to_numpy()
    return np.select([r >= 1.0, r >= 0.5, r >= 0.0], [3, 2, 1], 0).astype(np.int8)


def main(path: str, years: list[int], kind: str = "touch", first_test_year: int = 2020) -> None:
    tag = path.split("dataset_")[1].replace(".parquet", "") + f"_{kind}"
    horizon = int(tag.split("_h")[1].split("_")[0])
    lf = pl.scan_parquet(path)
    sessions = lf.select("date").unique().collect()["date"].sort().to_list()
    cols = ["instrument_id", "date", "label", "held", "fwd_ret_h", "fwd_max_ret", "fwd_min_ret", "fwd_min_close_ret", *ALL_FEATURES]
    cols += [c for c in BASELINES.values() if c not in cols]
    univ = lf.filter(pl.col("universe")).select(cols).collect()
    lab = univ.filter(pl.col("label").is_not_null())
    years = years or sorted({d.year for d in sessions if d.year >= first_test_year})
    out_dir = "out" if first_test_year >= 2020 else f"out/from{first_test_year}"
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    rows, imps = [], []
    for y in years:
        t = time.time()
        before = [d for d in sessions if d.year < y]
        if len(before) < horizon + 300:
            continue
        train_end = before[-(horizon + 6)]
        stride_dates = set(before[::TRAIN_STRIDE])
        train = lab.filter((pl.col("date") <= train_end) & pl.col("date").is_in(list(stride_dates))).sort("date")
        test = lab.filter(pl.col("date").dt.year() == y).sort("date")
        if test.is_empty() or (target(train, kind) > 0).sum() < 50:
            continue
        x = lambda df: df.select(ALL_FEATURES).to_numpy().astype(np.float32)  # noqa: E731
        params = {**PARAMS, "label_gain": [0, 1, 3, 7]} if kind == "graded" else PARAMS
        m = lgb.train(params, lgb.Dataset(x(train), target(train, kind), group=grp(train)), ROUNDS)
        test = test.with_columns(pl.Series("score", m.predict(x(test))))
        imps.append(pl.DataFrame({"feature": ALL_FEATURES, "gain": m.feature_importance("gain"), "year": y}))
        r = {"geometry": tag, "year": y, "train_rows": train.shape[0], "test_rows": test.shape[0], "ranker": "ml", **summarize(test, "score")}
        rows.append(r)
        print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()}, f"{time.time()-t:.0f}s", flush=True)
        for name, col in BASELINES.items():
            desc = name not in ("bl_small_turnover", "bl_far_from_ath")
            rows.append({"geometry": tag, "year": y, "train_rows": 0, "test_rows": test.shape[0], "ranker": name, **summarize(test, col, desc)})
        test.select("instrument_id", "date", "score").write_parquet(f"{out_dir}/mb_scores_{tag}_{y}.parquet")
    out = pl.DataFrame(rows)
    out.write_csv(f"{out_dir}/multibagger_{tag}.csv")
    pl.concat(imps).write_csv(f"{out_dir}/multibagger_{tag}_importance.csv")
    print(out.group_by("ranker").agg(pl.col("auc").mean(), pl.col("base").mean(), pl.col("top20_hit").mean(), pl.col("topdec_hit").mean(),
                                     pl.col("top20_fwd_mean").mean(), pl.col("univ_fwd_mean").mean(), pl.col("top20_fwd_median").mean(),
                                     pl.col("univ_fwd_median").mean()).sort("ranker"))


if __name__ == "__main__":
    kind = next((a.split("=")[1] for a in sys.argv if a.startswith("--target=")), "touch")
    first = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--first-test-year=")), 2020)
    main(sys.argv[1], [int(a) for a in sys.argv[2:] if a.isdigit()], kind, first)
