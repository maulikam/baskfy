"""Walk-forward LightGBM. Splits are by calendar year with a purge gap so no training label's
forward path overlaps the test period (López de Prado ch. 7). Frames in, arrays/frames out."""

from __future__ import annotations

from dataclasses import dataclass, field

import lightgbm as lgb
import numpy as np
import polars as pl

from .features import ALL_FEATURES


@dataclass(frozen=True)
class ModelConfig:
    features: tuple[str, ...] = tuple(ALL_FEATURES)
    purge_sessions: int = 15  # > horizon + 1 (entry next open) + slack
    first_test_year: int = 2019
    # lambdarank with one query per session: the model learns *which names today*, so the
    # year-to-year drift in the base rate (27%–37%) cannot masquerade as signal (explore_objectives.py)
    params: dict = field(default_factory=lambda: {
        "objective": "lambdarank", "metric": "ndcg", "eval_at": [20], "lambdarank_truncation_level": 40,
        "learning_rate": 0.03, "num_leaves": 63, "min_child_samples": 300,
        "feature_fraction": 0.7, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 5.0,
        "verbose": -1, "num_threads": 2, "seed": 7,
    })
    rounds: int = 250
    early_stop: int = 0  # 0 = fixed rounds; NDCG early stopping on a 10% tail slice was too noisy (2–190 iters)


def year_splits(sessions: list, cfg: ModelConfig) -> list[tuple[int, object, object, object]]:
    """(test_year, train_end_date, test_start, test_end). train_end is purged back from Jan 1."""
    years = sorted({d.year for d in sessions})
    out = []
    for y in years:
        if y < cfg.first_test_year:
            continue
        test = [d for d in sessions if d.year == y]
        before = [d for d in sessions if d.year < y]
        if len(before) < 300 or not test:
            continue
        out.append((y, before[-cfg.purge_sessions - 1], test[0], test[-1]))
    return out


def _dataset(df: pl.DataFrame, cfg: ModelConfig, ref: lgb.Dataset | None = None) -> lgb.Dataset:
    df = df.sort("date")  # lambdarank needs each session's rows contiguous
    x = df.select(cfg.features).to_numpy().astype(np.float32)
    y = df["label"].to_numpy().astype(np.int8)
    group = df.group_by("date", maintain_order=True).agg(pl.len())["len"].to_numpy()
    kw = {"group": group} if cfg.params.get("objective") == "lambdarank" else {}
    return lgb.Dataset(x, y, feature_name=list(cfg.features), reference=ref, free_raw_data=True, **kw)


def fit(train: pl.DataFrame, cfg: ModelConfig) -> lgb.Booster:
    """Early-stops on the last ~10% of training *time* (never a random slice)."""
    dates = train["date"].unique().sort()
    cut = dates[int(len(dates) * 0.9)]
    inner, valid = train.filter(pl.col("date") < cut), train.filter(pl.col("date") >= cut)
    if not cfg.early_stop:
        return lgb.train(cfg.params, _dataset(train, cfg), cfg.rounds)
    dtr = _dataset(inner, cfg)
    dva = _dataset(valid, cfg, ref=dtr)
    return lgb.train(cfg.params, dtr, cfg.rounds, valid_sets=[dva],
                     callbacks=[lgb.early_stopping(cfg.early_stop, verbose=False)])


def predict(model: lgb.Booster, df: pl.DataFrame, cfg: ModelConfig) -> np.ndarray:
    x = df.select(cfg.features).to_numpy().astype(np.float32)
    return model.predict(x, num_iteration=model.best_iteration or None)


def walk_forward(ds: pl.DataFrame, cfg: ModelConfig) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Out-of-sample scores for every universe row from first_test_year on, plus gain importance."""
    keep = ["instrument_id", "date", "label", *cfg.features]
    labelled = ds.filter(pl.col("universe") & pl.col("label").is_not_null()).select(keep)
    sessions = ds["date"].unique().sort().to_list()
    scored, imps = [], []
    for y, train_end, t0, t1 in year_splits(sessions, cfg):
        train = labelled.filter(pl.col("date") <= train_end)
        test = ds.filter(pl.col("universe") & (pl.col("date") >= t0) & (pl.col("date") <= t1)).select(keep)
        m = fit(train, cfg)
        p = predict(m, test, cfg)
        scored.append(test.select("instrument_id", "date").with_columns(pl.Series("score", p), pl.lit(y).alias("fold")))
        imps.append(pl.DataFrame({"feature": list(cfg.features), "gain": m.feature_importance("gain"), "fold": y}))
        print(f"fold {y}: train {train.shape[0]:,} rows ≤ {train_end}, test {test.shape[0]:,}, iters {m.best_iteration or m.current_iteration()}", flush=True)
    return pl.concat(scored), pl.concat(imps)


def baselines(ds: pl.DataFrame) -> pl.DataFrame:
    """Naive rankers to beat: 12-1 momentum, 3-month return, and 'closest to 52-week high'."""
    return ds.filter(pl.col("universe")).select(
        "instrument_id", "date",
        pl.col("mom_12_1_cs").alias("bl_mom12_1"),
        pl.col("r63_cs").alias("bl_r63"),
        pl.col("dist_hi252_cs").alias("bl_near_high"),
    )
