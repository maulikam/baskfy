"""PM0.10: quantile downside model beside the ranker, its filter on the book, and the touch-label check on closes.

usage: PM_DATA=<export> python explore_downside.py fit|filter|label|all       (writes out/downside/; touches no network)"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl
from sklearn.metrics import roc_auc_score

from build_dataset import build_labels
from explore_multibagger import PARAMS, ROUNDS, TRAIN_STRIDE, grp
from portfolioml import downside as D, tranches
from portfolioml.features import ALL_FEATURES
from portfolioml.labels import MultibaggerConfig
from run_longrun import CFG, load

DATA = Path(os.environ.get("PM_DATA", "data"))
SOURCE = DATA / "dataset_mb_u100_h250_tb.parquet"
OUT = Path("out/downside")
HORIZON, FIRST_YEAR = 250, 2014
TERMINAL = "b"
CUTS = {"worst_decile": 0.10, "worst_quintile": 0.20, "worst_third": 1 / 3}
JUDGED = "worst_quintile"
MIN_YEARS = 9
QUANTILE = {k: v for k, v in PARAMS.items() if k not in ("objective", "metric", "eval_at", "lambdarank_truncation_level")} | {"objective": "quantile", "metric": "quantile", "num_threads": 4}
pl.Config.set_tbl_rows(80); pl.Config.set_tbl_width_chars(210); pl.Config.set_float_precision(3); pl.Config.set_tbl_cols(30)


def x(df: pl.DataFrame) -> np.ndarray:
    return df.select(ALL_FEATURES).to_numpy().astype(np.float32)


def universe(extra: tuple = ()) -> tuple[pl.DataFrame, list]:
    lf = pl.scan_parquet(SOURCE)
    sessions = lf.select("date").unique().collect()["date"].sort().to_list()
    frame = lf.filter(pl.col("universe") & pl.col("label").is_not_null()).select("instrument_id", "date", "label", "fwd_ret_h", *extra, *ALL_FEATURES).collect()
    return frame.filter(pl.col("fwd_ret_h").is_not_null()), sessions


def folds(lab: pl.DataFrame, sessions: list):
    """The twelve-year walk-forward's own folds: purge, stride and test years as explore_multibagger."""
    for y in sorted({d.year for d in sessions if d.year >= FIRST_YEAR}):
        before = [d for d in sessions if d.year < y]
        if len(before) < HORIZON + 300:
            continue
        train_end = before[-(HORIZON + 6)]
        stride = list(set(before[::TRAIN_STRIDE]))
        train = lab.filter((pl.col("date") <= train_end) & pl.col("date").is_in(stride)).sort("date")
        test = lab.filter(pl.col("date").dt.year() == y).sort("date")
        if not test.is_empty() and train["label"].sum() >= 50:
            yield y, train, test


def fit() -> None:
    lab, sessions = universe()
    lab = D.relative_return(lab)
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for y, train, test in folds(lab, sessions):
        t = time.time()
        X, Y, Xt, Yt = x(train), train["y"].to_numpy().astype(np.float32), x(test), test["y"].to_numpy().astype(np.float32)
        preds = {a: lgb.train({**QUANTILE, "alpha": a}, lgb.Dataset(X, Y), ROUNDS).predict(Xt) for a in D.ALPHAS}
        naive = {a: np.full(len(Yt), np.quantile(Y, a)) for a in D.ALPHAS}
        below, inside = D.coverage(Yt, preds[0.1], preds[0.9])
        rows.append({"year": y, "train_rows": train.height, "test_rows": test.height, **{f"pinball_{int(a*100)}": D.pinball(Yt, preds[a], a) for a in D.ALPHAS},
                     **{f"naive_{int(a*100)}": D.pinball(Yt, naive[a], a) for a in D.ALPHAS}, "below_p10": below, "inside_p10_p90": inside, "below_p50": float(np.mean(Yt < preds[0.5])),
                     "crossed_p10_p90": float(np.mean(preds[0.1] > preds[0.9]))})
        test.select("instrument_id", "date", pl.Series("p10", preds[0.1]), pl.Series("p50", preds[0.5]), pl.Series("p90", preds[0.9])).write_parquet(OUT / f"quantiles_tb_{y}.parquet")
        print(rows[-1], f"{time.time()-t:.0f}s", flush=True)
    out = pl.DataFrame(rows)
    out.write_csv(OUT / "quantile_by_year.csv")
    print(out)


def arm_rows(arm: str, picks: pl.DataFrame, universe_rows: pl.DataFrame) -> list[dict]:
    rows = []
    frame = picks.with_columns(pl.col("date").dt.year().alias("year"))
    for (y,), g in frame.group_by("year", maintain_order=True):
        base = universe_rows.filter(pl.col("date").is_in(g["date"].unique().implode()))
        rows.append({"arm": arm, "year": y, **D.outcomes(g), "base_hit": base["label"].mean(), "lift": g["label"].mean() / base["label"].mean()})
    return rows


def run_filter() -> None:
    ds, scores, marks, idx = load(TERMINAL, True)
    q = pl.concat([pl.read_parquet(p) for p in sorted(OUT.glob("quantiles_tb_*.parquet"))])
    keyed = ds.select("instrument_id", "date").join(scores.select("instrument_id", "date", "score"), on=["instrument_id", "date"]).join(q, on=["instrument_id", "date"])
    cfg = CFG(top_n=20, skip_held=True, terminal=TERMINAL)
    arms = {"none": keyed, **{k: D.drop_worst(keyed, f) for k, f in CUTS.items()}}
    base_rows = ds.select("instrument_id", "date", "label", "fwd_ret_h").join(keyed.select("instrument_id", "date"), on=["instrument_id", "date"]).filter(pl.col("fwd_ret_h").is_not_null())
    yearly, pooled = [], []
    for name, sel in arms.items():
        picks, eq = tranches.simulate(ds, sel.select("instrument_id", "date", "score"), "score", cfg, marks=marks)
        yearly += arm_rows(name, picks, base_rows)
        s = tranches.summary(eq, picks, idx)
        days = base_rows.filter(pl.col("date").is_in(picks["date"].unique().implode()))
        pooled.append({"arm": name, **D.outcomes(picks), "base_hit": days["label"].mean(), "lift": picks["label"].mean() / days["label"].mean(), **{k: s[k] for k in ("cagr", "max_dd", "sharpe", "start", "end")}})
        print(name, flush=True)
    y, p = pl.DataFrame(yearly), pl.DataFrame(pooled)
    y.write_csv(OUT / "filter_by_year.csv"); p.write_csv(OUT / "filter_pooled.csv")
    print(p)
    for col in ("hit", "lift", "halved", "lost_25", "median_12m"):
        print(col); print(y.pivot(on="arm", index="year", values=col).sort("year"))
    rule = pl.DataFrame([{"cut": k, "years_halved_lower_and_lift_held": D.improved_years(y, "none", k), "years": y["year"].n_unique()} for k in CUTS])
    rule.write_csv(OUT / "rule.csv"); print(rule)
    n = rule.filter(pl.col("cut") == JUDGED)["years_halved_lower_and_lift_held"][0]
    print(f"RULE on {JUDGED}: {n} of {y['year'].n_unique()} years, need {MIN_YEARS}: {'ADD THE FIFTH ARM' if n >= MIN_YEARS else 'RECORD AS TESTED'}")


def by_year_auc(frame: pl.DataFrame, score: str, label: str) -> pl.DataFrame:
    rows = []
    for (y,), g in frame.with_columns(pl.col("date").dt.year().alias("year")).group_by("year", maintain_order=True):
        ranked = g.with_columns(pl.col(score).rank("ordinal", descending=True).over("date").alias("rk"))
        top, base = ranked.filter(pl.col("rk") <= 20)[label].mean(), g[label].mean()
        rows.append({"year": y, "auc": float(roc_auc_score(g[label], g[score])), "top20_hit": top, "base": base, "lift": top / base})
    return pl.DataFrame(rows)


def label_check() -> None:
    t0 = time.time()
    bars = pl.scan_parquet(SOURCE).select("instrument_id", "date", "open", pl.col("close").alias("high"), "low", "close", "terminal").collect()
    new = build_labels(bars, MultibaggerConfig(up=1.0, horizon=HORIZON), t0, TERMINAL).select("instrument_id", "date", pl.col("label").alias("label_close"))
    del bars
    lab, sessions = universe()
    lab = lab.join(new, on=["instrument_id", "date"], how="left").filter(pl.col("label_close").is_not_null())
    loss = lab.group_by(pl.col("date").dt.year().alias("year")).agg(pl.len().alias("rows"), pl.col("label").sum().alias("hits_on_high"), pl.col("label_close").sum().alias("hits_on_close"),
        ((pl.col("label") == 1) & (pl.col("label_close") == 0)).sum().alias("disappeared"), ((pl.col("label") == 0) & (pl.col("label_close") == 1)).sum().alias("appeared")).sort("year").with_columns(
        (pl.col("disappeared") / pl.col("hits_on_high")).alias("share_gone"))
    loss.write_csv(OUT / "label_hits_by_year.csv"); print(loss)
    scored, models = [], []
    old = pl.concat([pl.read_parquet(p) for p in sorted(Path("out/from2014").glob("mb_scores_mb_u100_h250_tb_touch_*.parquet"))]).select("instrument_id", "date", pl.col("score").alias("score_high"))
    for y, train, test in folds(lab, sessions):
        t = time.time()
        m = lgb.train(PARAMS, lgb.Dataset(x(train), train["label_close"].to_numpy(), group=grp(train)), ROUNDS)
        scored.append(test.select("instrument_id", "date", "label", "label_close", pl.Series("score_close", m.predict(x(test)))))
        print(y, f"{time.time()-t:.0f}s", flush=True)
    frame = pl.concat(scored).join(old, on=["instrument_id", "date"])
    cases = {"high_model_on_high_label": ("score_high", "label"), "high_model_on_close_label": ("score_high", "label_close"), "close_model_on_close_label": ("score_close", "label_close"), "close_model_on_high_label": ("score_close", "label")}
    t = pl.concat([by_year_auc(frame, s, l).with_columns(pl.lit(k).alias("case")) for k, (s, l) in cases.items()])
    t.write_csv(OUT / "label_auc_by_year.csv")
    print(t.pivot(on="case", index="year", values="auc").sort("year")); print(t.pivot(on="case", index="year", values="lift").sort("year"))
    print(t.group_by("case").agg(pl.col("auc").mean(), pl.col("top20_hit").mean(), pl.col("lift").mean()).sort("case"))


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("fit", "all"):
        fit()
    if what in ("filter", "all"):
        run_filter()
    if what in ("label", "all"):
        label_check()
