"""PM0.10: the downside model's target, scoring and filter. Frames in, frames out."""

from __future__ import annotations

import numpy as np
import polars as pl

ALPHAS = (0.1, 0.5, 0.9)
HALVED, LOST = -0.5, -0.25


def relative_return(frame: pl.DataFrame) -> pl.DataFrame:
    """`y`: the horizon return minus that day's median of the rows in `frame`."""
    return frame.with_columns((pl.col("fwd_ret_h") - pl.col("fwd_ret_h").median().over("date")).alias("y"))


def pinball(y: np.ndarray, q: np.ndarray, alpha: float) -> float:
    e = y - q
    return float(np.mean(np.maximum(alpha * e, (alpha - 1) * e)))


def coverage(y: np.ndarray, p10: np.ndarray, p90: np.ndarray) -> tuple[float, float]:
    """Share of outcomes below P10, share inside P10–P90."""
    return float(np.mean(y < p10)), float(np.mean((y >= p10) & (y <= p90)))


def drop_worst(frame: pl.DataFrame, frac: float) -> pl.DataFrame:
    """Each date, removes the `frac` of `frame` with the lowest predicted P10."""
    ranked = frame.with_columns(pl.col("p10").rank("ordinal").over("date").alias("_r"), pl.len().over("date").alias("_n"))
    return ranked.filter(pl.col("_r") > pl.col("_n") * frac).drop("_r", "_n")


def outcomes(picks: pl.DataFrame) -> dict:
    return {"n": picks.height, "hit": picks["label"].mean(), "halved": (picks["fwd_ret_h"] <= HALVED).mean(),
            "lost_25": (picks["fwd_ret_h"] < LOST).mean(), "median_12m": picks["fwd_ret_h"].median()}


def improved_years(yearly: pl.DataFrame, base: str, name: str) -> int:
    """Years where `name` has a lower halved rate than `base` and a lift no lower."""
    a, b = (yearly.filter(pl.col("arm") == k).select("year", "halved", "lift") for k in (base, name))
    j = a.join(b, on="year", suffix="_f")
    return j.filter((pl.col("halved_f") < pl.col("halved")) & (pl.col("lift_f") >= pl.col("lift"))).height
