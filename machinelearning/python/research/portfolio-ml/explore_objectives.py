"""One-fold experiment: which objective ranks best out of sample? (binary/auc-stop, lambdarank,
regression on cross-sectionally demeaned exit return.)"""

from __future__ import annotations

import sys
import time

import lightgbm as lgb
import numpy as np
import polars as pl
from sklearn.metrics import roc_auc_score

from portfolioml.features import ALL_FEATURES

ds = pl.read_parquet("data/dataset.parquet")
lab = ds.filter(pl.col("universe") & pl.col("label").is_not_null())
year = int(sys.argv[1]) if len(sys.argv) > 1 else 2022
sessions = ds["date"].unique().sort().to_list()
before = [d for d in sessions if d.year < year]
train_end = before[-16]
train = lab.filter(pl.col("date") <= train_end).sort("date")
test = lab.filter(pl.col("date").dt.year() == year).sort("date")
dates = train["date"].unique().sort()
cut = dates[int(len(dates) * 0.9)]
inner, valid = train.filter(pl.col("date") < cut), train.filter(pl.col("date") >= cut)


def xy(df, target):
    return df.select(ALL_FEATURES).to_numpy().astype(np.float32), df[target].to_numpy()


def grp(df):
    return df.group_by("date", maintain_order=True).agg(pl.len())["len"].to_numpy()


def evaluate(name, p, df):
    d = df.select("date", "label", "exit_ret").with_columns(pl.Series("p", p)).with_columns(
        pl.col("p").rank("ordinal", descending=True).over("date").alias("rk"))
    top = d.filter(pl.col("rk") <= 20)
    dec = d.filter(pl.col("rk") <= pl.len().over("date") * 0.1)
    print(f"{name:28s} auc {roc_auc_score(d['label'], d['p']):.4f}  base {d['label'].mean():.3f}  "
          f"top20 hit {top['label'].mean():.3f} ret {top['exit_ret'].mean()*100:+.2f}%  "
          f"topdec hit {dec['label'].mean():.3f} ret {dec['exit_ret'].mean()*100:+.2f}%  univ ret {d['exit_ret'].mean()*100:+.2f}%", flush=True)


base = {"learning_rate": 0.05, "num_leaves": 63, "min_child_samples": 300, "feature_fraction": 0.7,
        "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 5.0, "verbose": -1, "num_threads": 2, "seed": 7}

t = time.time()
# 1. binary, early stop on AUC
xi, yi = xy(inner, "label"); xv, yv = xy(valid, "label"); xt, _ = xy(test, "label")
m = lgb.train({**base, "objective": "binary", "metric": "auc"}, lgb.Dataset(xi, yi), 800,
              valid_sets=[lgb.Dataset(xv, yv)], callbacks=[lgb.early_stopping(60, verbose=False)])
evaluate(f"binary/auc it={m.best_iteration}", m.predict(xt, num_iteration=m.best_iteration), test)
print(f"  {time.time()-t:.0f}s", flush=True)

# 2. lambdarank, groups = dates
t = time.time()
m = lgb.train({**base, "objective": "lambdarank", "metric": "ndcg", "eval_at": [20], "lambdarank_truncation_level": 40},
              lgb.Dataset(xi, yi, group=grp(inner)), 800,
              valid_sets=[lgb.Dataset(xv, yv, group=grp(valid))], callbacks=[lgb.early_stopping(60, verbose=False)])
evaluate(f"lambdarank it={m.best_iteration}", m.predict(xt, num_iteration=m.best_iteration), test)
print(f"  {time.time()-t:.0f}s", flush=True)

# 3. regression on cross-sectionally demeaned, clipped exit return
t = time.time()
def dm(df):
    return df.with_columns((pl.col("exit_ret").clip(-0.15, 0.15) - pl.col("exit_ret").clip(-0.15, 0.15).mean().over("date")).alias("y"))
ii, vv = dm(inner), dm(valid)
m = lgb.train({**base, "objective": "regression", "metric": "l2"}, lgb.Dataset(xi, ii["y"].to_numpy()), 800,
              valid_sets=[lgb.Dataset(xv, vv["y"].to_numpy())], callbacks=[lgb.early_stopping(60, verbose=False)])
evaluate(f"regression demeaned it={m.best_iteration}", m.predict(xt, num_iteration=m.best_iteration), test)
print(f"  {time.time()-t:.0f}s", flush=True)

# 4. binary, fixed 300 rounds, no early stopping (is the stop the problem?)
t = time.time()
m = lgb.train({**base, "objective": "binary", "learning_rate": 0.03}, lgb.Dataset(xi, yi), 300)
evaluate("binary fixed 300", m.predict(xt), test)
print(f"  {time.time()-t:.0f}s", flush=True)
