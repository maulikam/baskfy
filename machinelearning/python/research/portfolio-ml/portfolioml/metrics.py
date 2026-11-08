"""Ranking quality out of sample: does the top of the list win more often than the universe?"""

from __future__ import annotations

import numpy as np
import polars as pl
from sklearn.metrics import roc_auc_score


def rank_quality(ds: pl.DataFrame, scores: pl.DataFrame, score_col: str, top_frac: float = 0.1, top_n: int = 20) -> pl.DataFrame:
    """Per year: AUC, base rate, hit rate and mean exit return in the top decile and the top-N per day."""
    d = (
        ds.select("instrument_id", "date", "label", "exit_ret", "fwd_ret_h")
        .join(scores.select("instrument_id", "date", score_col), on=["instrument_id", "date"])
        .filter(pl.col("label").is_not_null() & pl.col(score_col).is_not_null())
        .with_columns(
            pl.col(score_col).rank("ordinal", descending=True).over("date").alias("_rk"),
            pl.len().over("date").alias("_n"),
        )
        .with_columns(
            (pl.col("_rk") <= (pl.col("_n") * top_frac).ceil()).alias("top_dec"),
            (pl.col("_rk") <= top_n).alias("top_n"),
            pl.col("date").dt.year().alias("year"),
        )
    )
    rows = []
    for (y,), g in d.group_by("year", maintain_order=True):
        yl, s = g["label"].to_numpy(), g[score_col].to_numpy()
        auc = roc_auc_score(yl, s) if 0 < yl.mean() < 1 else np.nan
        td, tn = g.filter(pl.col("top_dec")), g.filter(pl.col("top_n"))
        rows.append({
            "year": y, "rows": g.shape[0], "auc": auc, "base_rate": float(yl.mean()),
            "hit_top_dec": td["label"].mean(), "hit_top_n": tn["label"].mean(),
            "exit_ret_univ": g["exit_ret"].mean(), "exit_ret_top_dec": td["exit_ret"].mean(), "exit_ret_top_n": tn["exit_ret"].mean(),
            "fwd10_univ": g["fwd_ret_h"].mean(), "fwd10_top_n": tn["fwd_ret_h"].mean(),
        })
    out = pl.DataFrame(rows).sort("year")
    yl, s = d["label"].to_numpy(), d[score_col].to_numpy()
    allrow = pl.DataFrame([{
        "year": 0, "rows": d.shape[0], "auc": roc_auc_score(yl, s), "base_rate": float(yl.mean()),
        "hit_top_dec": d.filter(pl.col("top_dec"))["label"].mean(), "hit_top_n": d.filter(pl.col("top_n"))["label"].mean(),
        "exit_ret_univ": d["exit_ret"].mean(), "exit_ret_top_dec": d.filter(pl.col("top_dec"))["exit_ret"].mean(),
        "exit_ret_top_n": d.filter(pl.col("top_n"))["exit_ret"].mean(),
        "fwd10_univ": d["fwd_ret_h"].mean(), "fwd10_top_n": d.filter(pl.col("top_n"))["fwd_ret_h"].mean(),
    }])
    return pl.concat([out, allrow.cast(out.schema)])


def decile_table(ds: pl.DataFrame, scores: pl.DataFrame, score_col: str) -> pl.DataFrame:
    """Hit rate and mean exit return by score decile (per-day deciles), pooled over all test years."""
    d = (
        ds.select("instrument_id", "date", "label", "exit_ret")
        .join(scores.select("instrument_id", "date", score_col), on=["instrument_id", "date"])
        .filter(pl.col("label").is_not_null() & pl.col(score_col).is_not_null())
        .with_columns(((pl.col(score_col).rank("ordinal").over("date") - 1) * 10 // pl.len().over("date")).alias("decile"))
    )
    return d.group_by("decile").agg(pl.len().alias("rows"), pl.col("label").mean().alias("hit_rate"),
                                    pl.col("exit_ret").mean().alias("mean_exit_ret")).sort("decile")
