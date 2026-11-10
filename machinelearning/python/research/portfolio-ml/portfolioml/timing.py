"""PM0.15: entry timing. A cash-pool book with per-line sizing, past-only terciles, and period statistics. Arrays in, numbers out."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np

CASH_RATE, COST = 0.06, 0.0025
MIN_HISTORY = 252
SIZES = (0.5, 1.0, 1.5)


def past_tercile_size(scores: np.ndarray, i: int) -> float:
    """Tranche size at position ``i`` from terciles of ``scores[:i]`` only; 1× until MIN_HISTORY sessions of history exist."""
    if i < MIN_HISTORY:
        return 1.0
    low, high = np.quantile(scores[:i], [1 / 3, 2 / 3])
    return SIZES[0] if scores[i] <= low else SIZES[2] if scores[i] > high else SIZES[1]


@dataclass(frozen=True)
class Line:
    start: int  # first session held (index into the session grid)
    path: np.ndarray  # value multiple of the entry price on each session held
    weight: float  # share of account equity to allot at entry


def pool_book(lines: list[Line], n_sessions: int, first: int) -> tuple[np.ndarray, float]:
    """Daily equity from session ``first``, and the share of wanted money actually allotted. Allocation is capped by cash; idle cash earns CASH_RATE."""
    daily = (1 + CASH_RATE) ** (1 / 252)
    by_start: dict[int, list[Line]] = {}
    for line in lines:
        by_start.setdefault(line.start, []).append(line)
    cash, active, out = 1.0, [], np.ones(n_sessions - first)
    wanted = granted = 0.0
    for t in range(first, n_sessions):
        cash *= daily
        held = sum(u * p[t - s] for u, p, s in active) if t > first else 0.0
        starts = by_start.get(t, [])
        if starts:
            prior = sum(u * p[t - 1 - s] for u, p, s in active if t - 1 >= s)
            equity = cash + prior
            want = np.array([x.weight * equity for x in starts])
            scale = min(1.0, cash / want.sum()) if want.sum() > 0 else 0.0
            wanted += float(want.sum())
            granted += float(want.sum() * scale)
            for x, amount in zip(starts, want * scale):
                cash -= amount
                active.append((amount * (1 - COST), x.path, x.start))
        still = []
        for u, p, s in active:
            if t - s == len(p) - 1:
                cash += u * p[-1] * (1 - COST)
            else:
                still.append((u, p, s))
        active = still
        out[t - first] = cash + sum(u * p[t - s] for u, p, s in active)
    return out, granted / wanted if wanted else 1.0


def period_stats(equity: np.ndarray, days: list[dt.date]) -> dict:
    v = equity / equity[0]
    years = (days[-1] - days[0]).days / 365.25
    r = np.diff(np.log(v))
    return {"cagr": float(v[-1] ** (1 / years) - 1), "max_dd": float((v / np.maximum.accumulate(v) - 1).min()), "sharpe": float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else None}


def helps(base: dict, arm: dict, min_cagr: float = 0.02) -> bool:
    """At least ``min_cagr`` more CAGR with a drawdown no deeper."""
    return arm["cagr"] - base["cagr"] >= min_cagr and arm["max_dd"] >= base["max_dd"]


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    ok = ~(np.isnan(a) | np.isnan(b))
    ra, rb = np.argsort(np.argsort(a[ok])), np.argsort(np.argsort(b[ok]))
    return float(np.corrcoef(ra, rb)[0, 1])
