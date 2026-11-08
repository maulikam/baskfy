"""A 10-slot book that buys the top of the ranked list and lets the triple barrier manage exits.

Signals are read at the close of t; entries fill at t+1 open; exits follow ``exit_k``/``exit_ret``
from the labels (barrier or horizon). Each slot compounds its own cash. Costs per side."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl


@dataclass(frozen=True)
class BookConfig:
    slots: int = 10
    max_new_per_session: int = 3
    cost_per_side: float = 0.0025
    gate_col: str | None = None  # e.g. "breadth200"
    gate_min: float = 0.0
    min_score: float | None = None  # absolute model-probability floor, if any


def simulate(ds: pl.DataFrame, scores: pl.DataFrame, score_col: str, cfg: BookConfig) -> tuple[pl.DataFrame, pl.DataFrame]:
    cols = ["instrument_id", "date", "exit_k", "exit_ret", "entry_px", "label"]
    if cfg.gate_col:
        cols.append(cfg.gate_col)
    d = (
        ds.select(cols)
        .join(scores.select("instrument_id", "date", score_col), on=["instrument_id", "date"])
        .filter(pl.col("label").is_not_null() & pl.col(score_col).is_not_null())
        .sort("date", score_col, descending=[False, True])
    )
    sessions = d["date"].unique().sort().to_list()
    sidx = {s: i for i, s in enumerate(sessions)}
    by_day = {k[0]: g for k, g in d.group_by("date", maintain_order=True)}

    cash = np.ones(cfg.slots)
    open_pos: dict[int, dict] = {}  # slot -> position
    trades = []
    for i, day in enumerate(sessions):
        for s, p in list(open_pos.items()):
            if p["exit_idx"] <= i:
                before = cash[s]
                cash[s] = before * (1 - cfg.cost_per_side) * (1 + p["exit_ret"]) * (1 - cfg.cost_per_side)
                trades.append({**p, "slot": s, "exit_date": sessions[min(p["exit_idx"], len(sessions) - 1)],
                               "cash_before": before, "cash_after": cash[s]})
                del open_pos[s]
        g = by_day.get(day)
        if g is None:
            continue
        if cfg.gate_col and (g[cfg.gate_col][0] is None or g[cfg.gate_col][0] < cfg.gate_min):
            continue
        free = [s for s in range(cfg.slots) if s not in open_pos]
        if not free:
            continue
        held = {p["instrument_id"] for p in open_pos.values()}
        n_new = 0
        for row in g.iter_rows(named=True):
            if n_new >= cfg.max_new_per_session or not free:
                break
            if row["instrument_id"] in held:
                continue
            if cfg.min_score is not None and row[score_col] < cfg.min_score:
                break
            s = free.pop(0)
            open_pos[s] = {
                "instrument_id": row["instrument_id"], "signal_date": day, "entry_idx": i + 1,
                "exit_idx": i + int(row["exit_k"]), "exit_ret": float(row["exit_ret"]),
                "entry_px": float(row["entry_px"]), "score": float(row[score_col]), "label": int(row["label"]),
            }
            held.add(row["instrument_id"])
            n_new += 1
    tr = pl.DataFrame(trades) if trades else pl.DataFrame()
    eq = _equity(tr, ds, sessions, cfg.slots, cfg.cost_per_side) if trades else pl.DataFrame()
    return tr, eq


def _equity(tr: pl.DataFrame, ds: pl.DataFrame, sessions: list, slots: int, cost: float) -> pl.DataFrame:
    """Daily marks: idle slots at cash, open slots at cash_before·(1−cost)·close/entry."""
    n = len(sessions)
    sidx = {s: i for i, s in enumerate(sessions)}
    eq = np.ones((n, slots))
    closes = ds.select("instrument_id", "date", "close").filter(pl.col("instrument_id").is_in(tr["instrument_id"].unique()))
    close_map = {(a, b): c for a, b, c in closes.iter_rows()}
    for s in range(slots):
        ts = tr.filter(pl.col("slot") == s).sort("entry_idx")
        level = 1.0
        last_exit = 0
        for t in ts.iter_rows(named=True):
            e0, e1 = t["entry_idx"], min(t["exit_idx"], n - 1)
            eq[last_exit:e0, s] = level
            base = t["cash_before"] * (1 - cost)
            for i in range(e0, e1):
                c = close_map.get((t["instrument_id"], sessions[i]))
                eq[i, s] = base * (c / t["entry_px"]) if c else eq[i - 1, s]
            level = t["cash_after"]
            last_exit = e1
            eq[e1, s] = level
        eq[last_exit:, s] = level
    return pl.DataFrame({"date": sessions, "equity": eq.sum(axis=1) / slots})


def summary(eq: pl.DataFrame, tr: pl.DataFrame, bench: pl.DataFrame | None = None, slots: int = 10) -> dict:
    e = eq["equity"].to_numpy()
    dates = eq["date"].to_list()
    years = (dates[-1] - dates[0]).days / 365.25
    r = np.diff(np.log(e))
    dd = e / np.maximum.accumulate(e) - 1
    out = {
        "start": str(dates[0]), "end": str(dates[-1]), "cagr": e[-1] ** (1 / years) - 1, "max_dd": dd.min(),
        "sharpe": r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else np.nan,
        "trades": tr.shape[0], "win_rate": tr["label"].mean(), "avg_hold": tr.select((pl.col("exit_idx") - pl.col("entry_idx") + 1).mean()).item(),
        "avg_trade_ret": tr["exit_ret"].mean(),
        "pct_invested": tr.select((pl.col("exit_idx") - pl.col("entry_idx") + 1).sum()).item() / (len(dates) * slots),
    }
    if bench is not None:
        b = eq.select("date").join(bench, on="date", how="left").sort("date")
        for k in [c for c in bench.columns if c != "date"]:
            v = b[k].fill_null(strategy="forward").to_numpy()
            if np.isnan(v[0]):
                continue
            bdd = v / np.maximum.accumulate(v) - 1
            out[f"{k}_cagr"] = (v[-1] / v[0]) ** (1 / years) - 1
            out[f"{k}_max_dd"] = bdd.min()
    return out


def yearly(eq: pl.DataFrame, bench: pl.DataFrame | None = None) -> pl.DataFrame:
    e = eq.with_columns(pl.col("date").dt.year().alias("year"))
    if bench is not None:
        e = e.join(bench, on="date", how="left").fill_null(strategy="forward")
    cols = [c for c in e.columns if c not in ("date", "year", "invested_slots")]
    return e.group_by("year").agg([(pl.col(c).last() / pl.col(c).first() - 1).alias(c) for c in cols]).sort("year")
