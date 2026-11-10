"""PM0.13: exit the whole book on a regime signal, cash while out. Arrays in, numbers out."""

from __future__ import annotations

import numpy as np

CASH_RATE, COST, TAX_RATE, HOLD_SESSIONS = 0.06, 0.005, 0.20, 5
SHORT_TERM_DAYS = 365


def debounce(raw: np.ndarray, sessions: int = HOLD_SESSIONS) -> np.ndarray:
    """The signal, flipping only once the opposite value has held ``sessions`` closes in a row."""
    state = np.empty(len(raw), dtype=bool)
    state[0], run = raw[0], 0
    for i in range(1, len(raw)):
        run = run + 1 if raw[i] != state[i - 1] else 0
        state[i] = raw[i] if run >= sessions else state[i - 1]
        run = 0 if state[i] != state[i - 1] else run
    return state


def positions(state: np.ndarray) -> np.ndarray:
    """Held during session i: a change at close s trades at the next session's close, s+1, so it shows from s+2."""
    out = state.copy()
    out[2:] = state[:-2]
    out[:2] = state[0]
    return out


def run(slices: np.ndarray, periods: list[tuple[int, int, int]], held: np.ndarray, days: list, tax: bool = False, cost: float = COST) -> np.ndarray:
    """Daily account value. Each slice earns its tranches when held, cash when not; a switch costs ``cost`` on the whole book.
    With ``tax``, net gains realised within 12 months each calendar year pay TAX_RATE at the year's last session; no loss carries over."""
    n, m = slices.shape
    daily_cash = (1 + CASH_RATE) ** (1 / 252)
    s, basis, bought = np.ones(m), np.ones(m), np.zeros(m, dtype=int)
    starts, ends = {}, {}
    for k, a, b in periods:
        starts.setdefault(a, []).append(k)
        ends.setdefault(b, []).append(k)
    prev, value, realised = np.ones(m), np.empty(n), 0.0

    def realise(k: int, t: int) -> None:
        nonlocal realised
        if (days[t] - days[bought[k]]).days <= SHORT_TERM_DAYS:
            realised += s[k] - basis[k]

    for t in range(n):
        if held[t]:
            for k in starts.get(t, []):
                basis[k], bought[k] = s[k], t
            s *= slices[t] / prev
        else:
            s *= daily_cash
        prev = slices[t]
        if held[t]:
            for k in ends.get(t, []):
                realise(k, t)
                basis[k] = s[k]
        if t + 1 < n and held[t + 1] != held[t]:
            if held[t]:
                s *= 1 - cost
                for k in range(m):
                    realise(k, t)
                basis[:] = s
            else:
                basis[:] = s
                bought[:] = t
                s *= 1 - cost
        year_end = t + 1 == n or days[t + 1].year != days[t].year
        if tax and year_end:
            bill = TAX_RATE * max(realised, 0.0) / m
            total = s.sum() / m
            scale = (total - bill) / total
            s *= scale
            basis *= scale
        if year_end:
            realised = 0.0
        value[t] = s.mean()
    return value


def stats(value: np.ndarray, days: list, held: np.ndarray) -> dict:
    v = value / value[0]
    r = np.diff(np.log(v))
    years = (days[-1] - days[0]).days / 365.25
    return {"cagr": float(v[-1] ** (1 / years) - 1), "max_dd": float((v / np.maximum.accumulate(v) - 1).min()), "sharpe": float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else None,
            "time_invested": float(held.mean()), "switches_per_year": float(np.abs(np.diff(held.astype(int))).sum() / years)}
