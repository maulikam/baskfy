"""The weekly CMO scan — evaluated once a week, on the last session of each ISO week.

    Monthly CMO(10) > 21
    Weekly  CMO(10) crossed above 21          (last week <= 21, this week > 21)
    Weekly Volume / 15 > Yearly Volume / 252  (this week's volume is ~3x a normal week)
    Market Cap > 1000 crore                   (no shares data in the export: proxied, see cscan_verify.py)

CMO(n) = 100 * (sum of up-moves - sum of down-moves) / (sum of up + sum of down) over the last n
closes. Weekly candles are complete weeks (the signal is read at Friday's close); monthly candles
are the nine completed months plus the month in progress (the reading that matches Chartink).
"""
import numpy as np
import pandas as pd


def cmo(closes: np.ndarray, n: int) -> np.ndarray:
    """closes: (N, T) along axis 1; returns (N, T) CMO(n), NaN until n+1 finite closes."""
    d = np.diff(closes, axis=1)
    up = np.where(d > 0, d, 0.0); dn = np.where(d < 0, -d, 0.0)
    k = np.ones(n)
    def rs(x):
        out = np.full_like(x, np.nan)
        cs = np.cumsum(np.nan_to_num(x), axis=1)
        out[:, n - 1:] = cs[:, n - 1:] - np.concatenate([np.zeros((x.shape[0], 1)), cs[:, :-n]], axis=1)
        return out
    su, sd = rs(up), rs(dn)
    valid = np.isfinite(d)
    # a window containing any NaN diff is invalid
    bad = rs((~valid).astype(float)) > 0
    with np.errstate(invalid="ignore", divide="ignore"):
        c = 100 * (su - sd) / (su + sd)
    c[bad] = np.nan
    out = np.full_like(closes, np.nan); out[:, 1:] = c
    return out


def week_ends(p):
    dates = pd.to_datetime(p.dates); iso = dates.isocalendar()
    wk = (iso.year.to_numpy() * 100 + iso.week.to_numpy())
    last = np.r_[wk[1:] != wk[:-1], True]           # last session of each week
    return np.nonzero(last)[0], wk


def weekly_frame(p):
    """Per-week arrays at week-end sessions: close, volume sum, and the daily column index."""
    ends, wk = week_ends(p)
    starts = np.r_[0, ends[:-1] + 1]
    close = p.close[:, ends]
    vol = np.stack([np.nansum(p.volume[:, s:e + 1], axis=1) for s, e in zip(starts, ends)], axis=1)
    return ends, close, vol


def monthly_cmo_at(p, ends, n=10):
    """Monthly CMO(10) read at each week-end session: completed months plus the month to date."""
    dates = pd.to_datetime(p.dates); mk = dates.year.to_numpy() * 12 + dates.month.to_numpy() - 1
    close = pd.DataFrame(p.close.T, index=dates)
    mclose = close.groupby(mk).last()                 # completed-month closes (last available bar)
    mids = mclose.index.to_numpy(); mc = mclose.to_numpy().T      # N x M
    out = np.full((p.n, len(ends)), np.nan)
    for i, e in enumerate(ends):
        m = mk[e]; pos = np.searchsorted(mids, m)
        if pos < n: continue
        window = np.concatenate([mc[:, pos - n:pos], p.close[:, e][:, None]], axis=1)   # 10 completed + current partial
        out[:, i] = cmo(window, n)[:, -1]
    return out


def yearly_volume_at(p, ends, sessions=252):
    cs = np.cumsum(np.nan_to_num(p.volume), axis=1)
    out = np.full((p.n, len(ends)), np.nan)
    for i, e in enumerate(ends):
        if e - sessions + 1 < 0: continue
        out[:, i] = cs[:, e] - (cs[:, e - sessions] if e - sessions >= 0 else 0)
    return out


def cmo_signal(p, ind, *, thr=21.0, wdiv=15.0, ydiv=252.0, mcap_mask=None):
    ends, wclose, wvol = weekly_frame(p)
    wc = cmo(wclose, 10)
    prev = np.roll(wc, 1, axis=1); prev[:, 0] = np.nan
    cross = (wc > thr) & (prev <= thr)
    mc = monthly_cmo_at(p, ends)
    yv = yearly_volume_at(p, ends)
    with np.errstate(invalid="ignore", divide="ignore"):
        volrule = (wvol / wdiv) > (yv / ydiv)
        mrule = mc > thr
    sig_w = np.nan_to_num(cross & mrule & volrule, nan=False) & ~p.is_etf[:, None]
    if mcap_mask is not None:
        sig_w &= mcap_mask[:, ends]
    # daily-aligned signal at the week-end session
    sig = np.zeros_like(p.close, dtype=bool); sig[:, ends] = sig_w
    return sig, {"ends": ends, "wcmo": wc, "mcmo": mc, "wvol": wvol, "yvol": yv, "cross": cross, "mrule": mrule, "volrule": volrule}
