"""Monthly-tranche portfolio for a 12-month horizon ranker: on the first session of each month,
put 1/12 of capital into the top-N names equal-weight and hold them ``horizon`` sessions.
Twelve overlapping tranches ≈ a fully invested, continuously rolling book. Frames in, frames out."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl


@dataclass(frozen=True)
class TrancheConfig:
    top_n: int = 20
    horizon: int = 250
    cost_per_side: float = 0.0025
    gate_col: str | None = None
    gate_min: float = 0.0
    stop: float | None = None  # e.g. -0.35: a tranche line is closed at the first close below entry×(1+stop)
    weighting: str = "equal"  # or "rank": top pick gets ~2× the weight of the last
    keep_if_top_frac: float | None = None  # "re-rank, don't sell": a maturing name still in this top fraction rolls over at no cost
    max_per_sector: int | None = 3  # applies only when ds has a "sector" column; None switches it off


def month_starts(sessions: list) -> list:
    out, last = [], None
    for d in sessions:
        if last is None or (d.year, d.month) != (last.year, last.month):
            out.append(d)
        last = d
    return out


def _capped(day: pl.DataFrame, cap: int, n: int) -> pl.DataFrame:
    """``day`` is score-sorted. At most ``cap`` per known sector; if that leaves fewer than ``n``,
    the next best excluded names fill the gap. Names with no sector are not one sector."""
    ranked = day.with_columns(pl.int_range(pl.len()).over("sector").alias("_k"), pl.int_range(pl.len()).alias("_pos"))
    within = (pl.col("_k") < cap) | pl.col("sector").is_null()
    first = ranked.filter(within).head(n)
    extra = ranked.filter(~pl.col("_pos").is_in(first["_pos"].implode())).head(n - first.height)
    return pl.concat([first, extra]).sort("_pos").drop("_k", "_pos")


def simulate(ds: pl.DataFrame, scores: pl.DataFrame, score_col: str, cfg: TrancheConfig, descending: bool = True) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Returns (picks with their 12-month outcome, daily equity of the rolling book)."""
    sessions = ds["date"].unique().sort().to_list()
    sidx = {d: i for i, d in enumerate(sessions)}
    starts = [d for d in month_starts(sessions) if d in set(scores["date"].to_list())]
    cols = ["instrument_id", "date", "entry_px", "fwd_ret_h", "fwd_min_ret", "label"]
    cap = cfg.max_per_sector if "sector" in ds.columns else None
    if cfg.gate_col:
        cols.append(cfg.gate_col)
    if cap:
        cols.append("sector")
    base = ds.select(cols).join(scores.select("instrument_id", "date", score_col), on=["instrument_id", "date"])
    picks = []
    for j, d in enumerate(starts):
        day = base.filter(pl.col("date") == d).filter(pl.col(score_col).is_not_null() & pl.col("fwd_ret_h").is_not_null())
        if day.is_empty():
            continue
        if cfg.gate_col and (day[cfg.gate_col][0] is None or day[cfg.gate_col][0] < cfg.gate_min):
            continue
        day = day.sort(score_col, descending=descending).with_columns(
            (pl.int_range(pl.len()) / pl.len()).alias("_pct"), pl.lit(False).alias("kept"))
        chosen = []
        if cfg.keep_if_top_frac is not None and j >= 12 and len(picks) >= 1:
            prev = picks[j - 12] if j - 12 < len(picks) and picks[j - 12]["date"][0] == starts[j - 12] else None
            if prev is not None:
                keep = day.filter(pl.col("instrument_id").is_in(prev["instrument_id"].implode()) & (pl.col("_pct") < cfg.keep_if_top_frac))
                chosen.append(keep.with_columns(pl.lit(True).alias("kept")))
        n_left = cfg.top_n - sum(c.shape[0] for c in chosen)
        rest = day
        if chosen:
            rest = rest.filter(~pl.col("instrument_id").is_in(chosen[0]["instrument_id"].implode()))
        if cap:
            rest = _capped(rest, cap, max(n_left, 0))
        chosen.append(rest.head(max(n_left, 0)))
        sel = pl.concat(chosen).sort(score_col, descending=descending)
        n = sel.shape[0]
        w = (np.linspace(2.0, 1.0, n) / np.linspace(2.0, 1.0, n).sum()) if cfg.weighting == "rank" else np.full(n, 1.0 / n)
        picks.append(sel.with_columns(pl.Series("w", w)))
    if not picks:
        return pl.DataFrame(), pl.DataFrame()
    p = pl.concat(picks)
    # mark every pick daily over its holding window on the *global* session grid; a line with no
    # bar that day (illiquid, suspended, delisted) carries its last close instead of vanishing
    sess = pl.DataFrame({"date": sessions, "i": list(range(len(sessions)))})
    held = (
        p.select("instrument_id", "date", "entry_px", "w", "kept").rename({"date": "signal_date"})
        .with_columns(pl.col("signal_date").replace_strict(sidx, return_dtype=pl.Int32).alias("i0"))
        .with_columns(pl.int_ranges(pl.col("i0") + 1, pl.col("i0") + cfg.horizon + 1).alias("i"))
        .explode("i").filter(pl.col("i") < len(sessions))
        .join(sess, on="i")
    )
    closes = ds.select("instrument_id", "date", "close").filter(pl.col("instrument_id").is_in(p["instrument_id"].unique().implode()))
    path = held.join(closes, on=["instrument_id", "date"], how="left").sort("instrument_id", "signal_date", "i")
    path = path.with_columns(
        (pl.col("close") / pl.col("entry_px")).fill_null(strategy="forward").over("instrument_id", "signal_date").fill_null(1.0).alias("mult")
    )
    if cfg.stop is not None:
        # first close below the stop freezes the line at the stop level thereafter
        path = path.with_columns(
            (pl.col("mult") <= 1 + cfg.stop).cast(pl.Int8).cum_max().over("instrument_id", "signal_date").alias("stopped"))
        path = path.with_columns(pl.when(pl.col("stopped") == 1).then(1 + cfg.stop).otherwise(pl.col("mult")).alias("mult"))
    # each line carries its weight; entry and exit costs on the line (a kept line pays no entry cost)
    c2, c1 = (1 - cfg.cost_per_side) ** 2, (1 - cfg.cost_per_side)
    path = path.with_columns(pl.when(pl.col("kept")).then(c1).otherwise(c2).alias("c"))
    tv = path.group_by("signal_date", "date").agg((pl.col("mult") * pl.col("w") * pl.col("c")).sum().alias("v")).sort("signal_date", "date")
    # twelve slices of capital; tranche j lives in slice j % 12 and compounds whatever that slice holds
    tranche_dates = tv["signal_date"].unique().sort().to_list()
    i_first = sidx[tranche_dates[0]]
    days = sessions[i_first:]
    n = len(days)
    slice_val = np.ones((n, 12))
    level = np.ones(12)
    cursor = np.zeros(12, dtype=int)  # first day index not yet written for the slice
    for j, sd in enumerate(tranche_dates):
        k = j % 12
        t = tv.filter(pl.col("signal_date") == sd)
        idx = np.array([sidx[d] - i_first for d in t["date"].to_list()])
        vals = t["v"].to_numpy() * level[k]
        slice_val[cursor[k]:idx[0], k] = level[k]
        slice_val[idx, k] = vals
        level[k] = vals[-1]
        cursor[k] = idx[-1] + 1
    for k in range(12):
        slice_val[cursor[k]:, k] = level[k]
    daily = pl.DataFrame({"date": days, "equity": slice_val.mean(axis=1)})
    return p, daily


def summary(eq: pl.DataFrame, picks: pl.DataFrame, bench: pl.DataFrame | None = None, start=None) -> dict:
    """``start``: measure from this date (the book takes 12 months to load; measuring from the first
    signal date flatters drawdowns)."""
    if start is not None:
        eq = eq.filter(pl.col("date") >= start)
    e = eq["equity"].to_numpy() / eq["equity"][0]
    dates = eq["date"].to_list()
    years = (dates[-1] - dates[0]).days / 365.25
    r = np.diff(np.log(e))
    dd = e / np.maximum.accumulate(e) - 1
    out = {"start": str(dates[0]), "end": str(dates[-1]), "cagr": e[-1] ** (1 / years) - 1, "max_dd": dd.min(),
           "sharpe": r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else np.nan, "picks": picks.shape[0],
           "pct_doubled_touch": picks["label"].mean(), "pct_pos_12m": (picks["fwd_ret_h"] > 0).mean(),
           "pct_2x_at_12m": (picks["fwd_ret_h"] >= 1).mean(), "mean_12m": picks["fwd_ret_h"].mean(), "median_12m": picks["fwd_ret_h"].median()}
    if bench is not None:
        b = eq.select("date").join(bench, on="date", how="left").sort("date")
        for k in [c for c in bench.columns if c != "date"]:
            v = b[k].fill_null(strategy="forward").fill_null(strategy="backward").to_numpy()
            out[f"{k}_cagr"] = (v[-1] / v[0]) ** (1 / years) - 1
            out[f"{k}_max_dd"] = (v / np.maximum.accumulate(v) - 1).min()
    return out


def yearly(eq: pl.DataFrame, bench: pl.DataFrame | None = None) -> pl.DataFrame:
    e = eq.with_columns(pl.col("date").dt.year().alias("year"))
    if bench is not None:
        e = e.join(bench, on="date", how="left").fill_null(strategy="forward")
    cols = [c for c in e.columns if c not in ("date", "year")]
    return e.group_by("year").agg([(pl.col(c).last() / pl.col(c).first() - 1).alias(c) for c in cols]).sort("year")
