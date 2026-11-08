"""The MOMENTUM STOCKS scan — a *state* scan, true on every session the condition holds.

    Close > 30                       (exchange price → close_raw)
    Sma(Volume, 50) >= 50,000
    any of:  Close >= Low[t-5]  * 1.2
             Close >= Low[t-30] * 1.3
             Close >= Low[t-90] * 1.3
"5 days ago Low" is the low of the single bar five sessions earlier, not the 5-day min.
"""
import numpy as np


def shift(m: np.ndarray, k: int) -> np.ndarray:
    out = np.full_like(m, np.nan); out[:, k:] = m[:, :-k]; return out


def momentum_state(p, ind, *, min_close_raw=30.0, min_vol_sma=50_000.0):
    with np.errstate(invalid="ignore"):
        r5 = p.close >= shift(p.low, 5) * 1.2
        r30 = p.close >= shift(p.low, 30) * 1.3
        r90 = p.close >= shift(p.low, 90) * 1.3
        base = (p.close_raw > min_close_raw) & (ind.vol_sma >= min_vol_sma) & ~p.is_etf[:, None]
    r5, r30, r90, base = [np.nan_to_num(x, nan=False).astype(bool) for x in (r5, r30, r90, base)]
    state = base & (r5 | r30 | r90)
    return state, {"r5": base & r5, "r30": base & r30, "r90": base & r90}


def entries(state: np.ndarray, lookback: int) -> np.ndarray:
    """First session in the scan after >= `lookback` sessions out of it (an event, not a state)."""
    out = np.zeros_like(state)
    was_in = np.zeros(state.shape[0], dtype=bool)
    since_out = np.full(state.shape[0], 10_000)
    for j in range(state.shape[1]):
        s = state[:, j]
        out[:, j] = s & ~was_in & (since_out >= lookback)
        since_out = np.where(s, 0, since_out + 1)
        was_in = s
    return out
