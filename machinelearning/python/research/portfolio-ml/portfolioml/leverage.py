"""PM0.17: the cash pool with a gross-exposure cap, interest on borrowed money and forced liquidation. Arrays in, numbers out."""

from __future__ import annotations

import numpy as np

from portfolioml.timing import CASH_RATE, COST, Line

BORROW_RATE, MARGIN, TOLERANCE = 0.12, 0.5, 0.001


def pool_levered(lines: list[Line], n_sessions: int, first: int, cap: float = 1.0) -> tuple[np.ndarray, np.ndarray, int]:
    """Daily equity, gross exposure over equity, and forced liquidations. Allocation stops at ``cap`` × equity. When equity
    closes below MARGIN of gross, the excess is sold pro rata at the next close, paying COST."""
    earn, owe = (1 + CASH_RATE) ** (1 / 252), (1 + BORROW_RATE) ** (1 / 252)
    by_start: dict[int, list[Line]] = {}
    for line in lines:
        by_start.setdefault(line.start, []).append(line)
    n = n_sessions - first
    cash, active, out, gross_share = 1.0, [], np.ones(n), np.zeros(n)
    forced, breach = 0, False

    def value(t: int) -> float:
        return sum(a[0] * a[1][t - a[2]] for a in active if t >= a[2])

    for t in range(first, n_sessions):
        cash *= earn if cash >= 0 else owe
        starts = by_start.get(t, [])
        if starts:
            prior = value(t - 1)
            equity = cash + prior
            want = np.array([x.weight * max(equity, 0.0) for x in starts])
            room = max(cap * equity - prior, 0.0)
            scale = min(1.0, room / want.sum()) if want.sum() > 0 else 0.0
            for x, amount in zip(starts, want * scale):
                cash -= amount
                active.append([amount * (1 - COST), x.path, x.start])
        still = []
        for a in active:
            if t - a[2] == len(a[1]) - 1:
                cash += a[0] * a[1][-1] * (1 - COST)
            else:
                still.append(a)
        active = still
        gross = value(t)
        equity = cash + gross
        if breach and gross > 0 and equity < gross * MARGIN:
            sell = min(1.0, (gross * MARGIN - max(equity, 0.0)) / (gross * (MARGIN - COST)))  # sized so equity meets margin after the sale's cost
            for a in active:
                a[0] *= 1 - sell
            cash += gross * sell * (1 - COST)
            forced += 1
            gross = value(t)
            equity = cash + gross
        breach = gross > 0 and equity < gross * MARGIN * (1 - TOLERANCE)  # interest alone must not count as a fresh margin call
        out[t - first] = equity
        gross_share[t - first] = gross / equity if equity > 0 else 0.0
    return out, gross_share, forced
