"""The "3 Week Tight Close" scan — a *state*, true while the last three weekly closes sit within 3 %.

    Close > 30                                            (exchange price → close_raw)
    Close >= 1.3 * Low[monthly bar, 3 months ago]
    |Max(3 weekly closes) / Min(3 weekly closes) - 1| * 100 <= 3.01
    Market cap > 1                                        (a no-op)
    Sma(Volume, 50) >= 10,000

Chartink's weekly and monthly candles include the bar in progress, so on a daily run "Weekly
Close" of the current week is today's close and the two before it are the closes of the last
sessions of the two previous ISO weeks. "3 months ago Low" is the low of the calendar month three
months before the current one. Both readings are checked against Chartink's own export in
`tscan_verify.py` (the alternatives — completed weeks only, or a rolling 63-session low — score worse).
"""
import numpy as np
import pandas as pd


def weekly_closes(p, n_weeks: int = 3):
    """(n_weeks, N, D): the close of the current (partial) week and of the n_weeks-1 completed
    weeks before it, aligned to every session."""
    dates = pd.to_datetime(p.dates)
    wk = dates.isocalendar().year.to_numpy() * 100 + dates.isocalendar().week.to_numpy()
    close = pd.DataFrame(p.close.T, index=dates)                       # D x N
    last_of_week = close.groupby(wk).last()                            # W x N (NaN-tolerant last)
    week_ids = last_of_week.index.to_numpy()
    pos = np.searchsorted(week_ids, wk)                                # session -> week row
    out = np.empty((n_weeks, p.n, p.d))
    out[0] = p.close                                                   # current week so far = today
    lw = last_of_week.to_numpy()                                       # W x N
    for k in range(1, n_weeks):
        idx = pos - k
        vals = np.where(idx[:, None] >= 0, lw[np.clip(idx, 0, None)], np.nan)   # D x N
        out[k] = vals.T
    return out


def monthly_low_months_ago(p, months_ago: int = 3):
    """(N, D): the low of the calendar month `months_ago` before each session's month."""
    dates = pd.to_datetime(p.dates)
    mk = dates.year.to_numpy() * 12 + dates.month.to_numpy() - 1
    low = pd.DataFrame(p.low.T, index=dates)
    mlow = low.groupby(mk).min()
    mids = mlow.index.to_numpy(); ml = mlow.to_numpy()
    target = mk - months_ago
    idx = np.searchsorted(mids, target)
    ok = (idx < len(mids)) & (mids[np.clip(idx, 0, len(mids) - 1)] == target)
    vals = np.where(ok[:, None], ml[np.clip(idx, 0, len(mids) - 1)], np.nan)
    return vals.T


def tight_state(p, ind, *, min_close_raw=30.0, min_vol_sma=10_000.0, tight_pct=3.01, low_mult=1.3, months_ago=3,
                include_current_week=True):
    w = weekly_closes(p, 4)
    ws = w[0:3] if include_current_week else w[1:4]
    with np.errstate(invalid="ignore", divide="ignore"):
        wmax = np.nanmax(ws, axis=0); wmin = np.nanmin(ws, axis=0)
        have3 = np.isfinite(ws).all(axis=0)
        tight = have3 & (np.abs(wmax / wmin - 1) * 100 <= tight_pct)
        mlow = monthly_low_months_ago(p, months_ago)
        up = p.close >= mlow * low_mult
        base = (p.close_raw > min_close_raw) & (ind.vol_sma >= min_vol_sma) & ~p.is_etf[:, None]
    state = np.nan_to_num(base & tight & up, nan=False).astype(bool)
    with np.errstate(invalid="ignore"):
        range_pct = (wmax / wmin - 1) * 100
    return state, {"tight": tight, "up": np.nan_to_num(up, nan=False), "range_pct": range_pct, "mlow": mlow}


def entries(state: np.ndarray, lookback: int) -> np.ndarray:
    out = np.zeros_like(state); was_in = np.zeros(state.shape[0], bool); since_out = np.full(state.shape[0], 10_000)
    for j in range(state.shape[1]):
        s = state[:, j]; out[:, j] = s & ~was_in & (since_out >= lookback)
        since_out = np.where(s, 0, since_out + 1); was_in = s
    return out
