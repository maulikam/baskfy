"""Swing label with XGBoost (Maulik, 11 Nov 2026): does a name touch +15 % before −4 % within 10–15 sessions?

usage: python explore_xgb_swing.py [u15_d4_h15 u15_d4_h10]     (datasets from build_dataset.py 0.15 0.04 H)
Walk-forward by calendar year (train on every third session before the year, purged by horizon + 1 sessions; fixed
parameters, never tuned on a test year). XGBoost and LightGBM on identical rows. Trades: each session's top-N
investable names (close ≥ ₹50, ≥ ₹5 cr a day), entered at the next open, never a name still held, exit at the
barrier (a stop gapped through fills at the open) or at the horizon's close, 0.5 % round trip.
Writes out/xgb_swing/{folds,trades,importance}.csv. Research only: nothing here reaches the product."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl
import xgboost as xgb
from sklearn.metrics import roc_auc_score

from portfolioml.features import ALL_FEATURES

OUT = Path("out/xgb_swing")
STRIDE, FIRST_TEST, COST = 3, 2016, 0.005
TOP = (5, 10, 20)
XGB = dict(n_estimators=400, max_depth=6, learning_rate=0.05, subsample=0.8, colsample_bytree=0.6, min_child_weight=50,
           reg_lambda=10.0, tree_method="hist", n_jobs=8, random_state=7, eval_metric="auc")
LGB = {"objective": "binary", "learning_rate": 0.05, "num_leaves": 63, "min_child_samples": 300, "feature_fraction": 0.6,
       "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 10.0, "verbose": -1, "num_threads": 8, "seed": 7}
pl.Config.set_tbl_rows(80); pl.Config.set_tbl_width_chars(220); pl.Config.set_float_precision(3); pl.Config.set_tbl_cols(20)


def trades(day_scores: pl.DataFrame, horizon: int, n: int) -> pl.DataFrame:
    """Each session's top n investable names not still held; a pick is held for its exit_k sessions."""
    sessions = day_scores["date"].unique().sort().to_list()
    sidx = {d: i for i, d in enumerate(sessions)}
    busy: dict[int, int] = {}
    out = []
    for (d,), g in day_scores.filter(pl.col("investable")).sort("date", "p", descending=[False, True]).group_by("date", maintain_order=True):
        i, k = sidx[d], 0
        for iid, lab, ret, ek in g.select("instrument_id", "label", "exit_ret", "exit_k").iter_rows():
            if k >= n:
                break
            if busy.get(iid, -1) >= i:
                continue
            busy[iid] = i + int(ek)
            out.append((d, iid, lab, ret, ek))
            k += 1
    return pl.DataFrame(out, schema=["date", "instrument_id", "label", "exit_ret", "exit_k"], orient="row")


def run(tag: str) -> tuple[list[dict], list[dict], pl.DataFrame]:
    horizon = int(tag.rsplit("_h", 1)[1])
    lf = pl.scan_parquet(f"data/dataset_{tag}.parquet").filter(pl.col("universe") & pl.col("label").is_not_null())
    df = lf.select("instrument_id", "date", "label", "exit_ret", "exit_k", "close_raw", "turnover20", *ALL_FEATURES).collect()
    df = df.with_columns(((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7)).alias("investable"))
    sessions = df["date"].unique().sort()
    folds, trade_rows, imp = [], [], []
    for year in range(FIRST_TEST, sessions.max().year + 1):
        test = df.filter(pl.col("date").dt.year() == year)
        before = sessions.filter(sessions.dt.year() < year)
        if test.is_empty() or len(before) < 500:
            continue
        cut = before[-(horizon + 2)]  # no training label's path reaches the test year
        tr_days = before.filter(before <= cut)[::STRIDE]
        train = df.filter(pl.col("date").is_in(tr_days.implode()))
        x, y, xt = train.select(ALL_FEATURES).to_numpy().astype(np.float32), train["label"].to_numpy(), test.select(ALL_FEATURES).to_numpy().astype(np.float32)
        x, xt = np.where(np.isfinite(x), x, np.nan), np.where(np.isfinite(xt), xt, np.nan)  # a ratio over zero is missing, for both models
        t0 = time.time()
        mx = xgb.XGBClassifier(**XGB).fit(x, y)
        px = mx.predict_proba(xt)[:, 1]
        t1 = time.time()
        ml = lgb.train(LGB, lgb.Dataset(x, y), 400)
        pg = ml.predict(xt)
        t2 = time.time()
        inv = test["investable"].to_numpy()
        for name, p, secs in (("xgboost", px, t1 - t0), ("lightgbm", pg, t2 - t1)):
            scored = test.select("date", "instrument_id", "label", "exit_ret", "exit_k", "investable").with_columns(pl.Series("p", p))
            folds.append({"tag": tag, "year": year, "model": name, "train_rows": train.height, "train_base": float(y.mean()),
                          "test_rows": test.height, "base": float(test["label"].mean()), "auc": roc_auc_score(test["label"], p),
                          "auc_investable": roc_auc_score(test["label"].to_numpy()[inv], p[inv]),
                          "investable_mean_ret": float(test.filter(pl.col("investable"))["exit_ret"].mean()), "fit_s": round(secs, 1)})
            for n in TOP:
                t = trades(scored, horizon, n)
                trade_rows.append({"tag": tag, "year": year, "model": name, "top": n, "trades": t.height,
                                   "hit": float(t["label"].mean()), "gross": float(t["exit_ret"].mean()),
                                   "net": float(t["exit_ret"].mean()) - COST, "stopped": float((t["exit_ret"] < -0.035).mean()),
                                   "worst": float(t["exit_ret"].min()), "avg_hold": float(t["exit_k"].mean())})
        imp.append(pl.DataFrame({"feature": ALL_FEATURES, "gain": mx.feature_importances_, "year": year, "tag": tag}))
        print(f"{tag} {year}: base {folds[-1]['base']:.3f}  auc xgb {folds[-2]['auc']:.3f} lgb {folds[-1]['auc']:.3f}  "
              f"top10 xgb hit {trade_rows[-5]['hit']:.3f} net {trade_rows[-5]['net']:+.4f}  ({t2 - t0:.0f}s)", flush=True)
    return folds, trade_rows, pl.concat(imp)


def pooled(t: pl.DataFrame) -> pl.DataFrame:
    return t.group_by("tag", "model", "top").agg(
        pl.col("trades").sum(), (pl.col("hit") * pl.col("trades")).sum().alias("h"), (pl.col("gross") * pl.col("trades")).sum().alias("g"),
        (pl.col("net") > 0).sum().alias("years_net_positive"), pl.len().alias("years"),
    ).with_columns((pl.col("h") / pl.col("trades")).alias("hit"), (pl.col("g") / pl.col("trades")).alias("gross")).with_columns(
        (pl.col("gross") - COST).alias("net")).drop("h", "g").sort("tag", "model", "top")


def main(tags: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    F, T, I = [], [], []
    for tag in tags:
        f, t, i = run(tag)
        F += f; T += t; I.append(i)
    folds, tr = pl.DataFrame(F), pl.DataFrame(T)
    folds.write_csv(OUT / "folds.csv"); tr.write_csv(OUT / "trades.csv")
    imp = pl.concat(I).group_by("tag", "feature").agg(pl.col("gain").mean()).sort("tag", "gain", descending=[False, True])
    imp.write_csv(OUT / "importance.csv")
    print(folds.select("tag", "year", "model", "base", "auc", "auc_investable", "investable_mean_ret"))
    print(tr.filter(pl.col("top") == 10))
    print(pooled(tr))
    print(imp.group_by("tag", maintain_order=True).head(12))


if __name__ == "__main__":
    main(sys.argv[1:] or ["u15_d4_h15", "u15_d4_h10"])
