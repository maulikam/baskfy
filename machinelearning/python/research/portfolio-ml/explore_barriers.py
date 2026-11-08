"""Which barrier geometry is learnable *after costs*? Lambdarank, folds 2021/2022/2023/2025,
top-20-per-day hit rate and mean exit return minus 50 bps round trip."""

from __future__ import annotations

import glob
import sys

import lightgbm as lgb
import numpy as np
import polars as pl
from sklearn.metrics import roc_auc_score

from portfolioml.features import ALL_FEATURES

COST = 0.005
PARAMS = {"objective": "lambdarank", "metric": "ndcg", "eval_at": [20], "lambdarank_truncation_level": 40,
          "learning_rate": 0.05, "num_leaves": 63, "min_child_samples": 300, "feature_fraction": 0.7,
          "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 5.0, "verbose": -1, "num_threads": 2, "seed": 7}
YEARS = [int(y) for y in sys.argv[1:] if not y.startswith("data/")] or [2021, 2022, 2023, 2025]


def grp(df):
    return df.group_by("date", maintain_order=True).agg(pl.len())["len"].to_numpy()


def fold(lab, sessions, year):
    before = [d for d in sessions if d.year < year]
    train = lab.filter(pl.col("date") <= before[-45]).sort("date")  # purge 45 ≥ longest horizon + slack
    test = lab.filter(pl.col("date").dt.year() == year).sort("date")
    dates = train["date"].unique().sort()
    cut = dates[int(len(dates) * 0.9)]
    inner, valid = train.filter(pl.col("date") < cut), train.filter(pl.col("date") >= cut)
    X = lambda df: df.select(ALL_FEATURES).to_numpy().astype(np.float32)  # noqa: E731
    m = lgb.train(PARAMS, lgb.Dataset(X(inner), inner["label"].to_numpy(), group=grp(inner)), 600,
                  valid_sets=[lgb.Dataset(X(valid), valid["label"].to_numpy(), group=grp(valid))],
                  callbacks=[lgb.early_stopping(60, verbose=False)])
    p = m.predict(X(test), num_iteration=m.best_iteration)
    d = test.select("date", "label", "exit_ret", "exit_k").with_columns(pl.Series("p", p)).with_columns(
        pl.col("p").rank("ordinal", descending=True).over("date").alias("rk"))
    top = d.filter(pl.col("rk") <= 20)
    return {
        "year": year, "iters": m.best_iteration, "auc": roc_auc_score(d["label"], d["p"]), "base": d["label"].mean(),
        "top20_hit": top["label"].mean(), "top20_ret": top["exit_ret"].mean(), "top20_net": top["exit_ret"].mean() - COST,
        "univ_ret": d["exit_ret"].mean(), "avg_k": top["exit_k"].mean(),
        # expectancy per slot-day: net return / days held — what the book actually compounds
        "top20_net_per_day": (top["exit_ret"].mean() - COST) / top["exit_k"].mean(),
    }


rows = []
for path in sorted(glob.glob(sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].startswith("data/") else "data/dataset_u*.parquet")):
    tag = path.split("dataset_")[1].replace(".parquet", "")
    lf = pl.scan_parquet(path)
    sessions = lf.select("date").unique().collect()["date"].sort().to_list()
    lab = lf.filter(pl.col("universe") & pl.col("label").is_not_null()).select(
        "date", "label", "exit_ret", "exit_k", *ALL_FEATURES).collect()
    for y in YEARS:
        r = {"geometry": tag, **fold(lab, sessions, y)}
        rows.append(r)
        print({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()}, flush=True)
out = pl.DataFrame(rows)
out.write_csv(f"out/barrier_geometry_{tag}.csv")
print(out.group_by("geometry").agg(pl.col("auc").mean(), pl.col("top20_hit").mean(), pl.col("base").mean(),
                                   pl.col("top20_net").mean(), pl.col("top20_net_per_day").mean(), pl.col("univ_ret").mean()).sort("geometry"))
