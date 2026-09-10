"""The Chartink scan, point-in-time, on the panel — plus the indicators the exit rules use.

Chartink's five lines, read literally ("Daily" = the session's own closed bar, SMA includes it):

    Volume            >  Sma(Volume, 50) * 3
    Close             >  30                      (an exchange price, so close_raw)
    % Change          >= 6.5                     (close / prev close - 1)
    Sma(Volume, 50)   >= 25,000
    Volume            >  50,000

A signal on session t is known at t's close and can first be traded at t+1's open.
"""
from __future__ import annotations

import dataclasses as dc

import numpy as np
import pandas as pd

from .data import Panel


@dc.dataclass(frozen=True)
class ScanParams:
    vol_mult: float = 3.0
    vol_sma_bars: int = 50
    min_close_raw: float = 30.0
    min_change_pct: float = 6.5
    min_vol_sma: float = 25_000
    min_volume: float = 50_000
    # Extras that are NOT in the Chartink scan (default off) — tested as variants.
    min_close_position: float | None = None   # close within the day's range, 0..1 (e.g. 0.6)
    max_change_pct: float | None = None       # skip circuit-locked / parabolic prints
    min_turnover_inr: float | None = None     # close_raw * volume
    require_above_sma: int | None = None      # close > SMA(close, n)
    exclude_upper_circuit: bool = False       # close_raw >= upper_circuit band


def _roll(m: np.ndarray, n: int, fn: str) -> np.ndarray:
    """Rolling stat over sessions; an illiquid name's occasional no-trade day (NaN) is tolerated
    up to 10% of the window, the way a screener that only sees traded bars would compute it."""
    df = pd.DataFrame(m.T)  # sessions x instruments
    r = df.rolling(n, min_periods=max(2, int(round(n * 0.9))))
    out = getattr(r, fn)()
    return out.to_numpy().T


def _ewm(m: np.ndarray, n: int) -> np.ndarray:
    return pd.DataFrame(m.T).ewm(span=n, adjust=False, min_periods=n).mean().to_numpy().T


@dc.dataclass(frozen=True)
class Indicators:
    vol_sma: np.ndarray
    rvol: np.ndarray
    change_pct: np.ndarray
    atr14: np.ndarray
    adr20_pct: np.ndarray       # average daily range %, (high/low - 1) mean over 20
    sma20: np.ndarray
    sma50: np.ndarray
    ema10: np.ndarray
    ema21: np.ndarray
    hi20: np.ndarray            # rolling 20-session high of close (incl. today)
    close_pos: np.ndarray       # (close - low) / (high - low)
    turnover: np.ndarray        # close_raw * volume
    turnover_sma20: np.ndarray
    lo10: np.ndarray            # rolling 10-session low of low (incl. today)


def compute_indicators(p: Panel, vol_sma_bars: int = 50) -> Indicators:
    prev_close = np.roll(p.close, 1, axis=1); prev_close[:, 0] = np.nan
    change_pct = (p.close / prev_close - 1.0) * 100.0
    tr = np.maximum.reduce([p.high - p.low, np.abs(p.high - prev_close), np.abs(p.low - prev_close)])
    rng = p.high - p.low
    with np.errstate(invalid="ignore", divide="ignore"):
        close_pos = np.where(rng > 0, (p.close - p.low) / rng, 0.5)
        adr = (p.high / p.low - 1.0) * 100.0
    vol_sma = _roll(p.volume, vol_sma_bars, "mean")
    with np.errstate(invalid="ignore", divide="ignore"):
        rvol = p.volume / vol_sma
    turnover = p.close_raw * p.volume
    return Indicators(
        turnover_sma20=_roll(turnover, 20, "mean"), lo10=_roll(p.low, 10, "min"),
        vol_sma=vol_sma, rvol=rvol, change_pct=change_pct,
        atr14=_roll(tr, 14, "mean"), adr20_pct=_roll(adr, 20, "mean"),
        sma20=_roll(p.close, 20, "mean"), sma50=_roll(p.close, 50, "mean"),
        ema10=_ewm(p.close, 10), ema21=_ewm(p.close, 21),
        hi20=_roll(p.close, 20, "max"), close_pos=close_pos,
        turnover=turnover,
    )


def scan(p: Panel, ind: Indicators, sp: ScanParams = ScanParams()) -> np.ndarray:
    """Boolean (N, D): the scan fires on instrument i at session j's close."""
    with np.errstate(invalid="ignore"):
        sig = (
            (p.volume > ind.vol_sma * sp.vol_mult)
            & (p.close_raw > sp.min_close_raw)
            & (ind.change_pct >= sp.min_change_pct)
            & (ind.vol_sma >= sp.min_vol_sma)
            & (p.volume > sp.min_volume)
        )
        if sp.min_close_position is not None:
            sig &= ind.close_pos >= sp.min_close_position
        if sp.max_change_pct is not None:
            sig &= ind.change_pct <= sp.max_change_pct
        if sp.min_turnover_inr is not None:
            sig &= ind.turnover >= sp.min_turnover_inr
        if sp.require_above_sma:
            sma = _roll(p.close, sp.require_above_sma, "mean")
            sig &= p.close > sma
        if sp.exclude_upper_circuit:
            uc = p.upper_circuit
            locked = np.isfinite(uc) & (p.close_raw >= uc * 0.999)
            sig &= ~locked
    sig &= ~p.is_etf[:, None]
    return np.nan_to_num(sig, nan=False).astype(bool)


def signals_frame(p: Panel, sig: np.ndarray, ind: Indicators) -> pd.DataFrame:
    rows, cols = np.nonzero(sig)
    return pd.DataFrame({
        "date": p.dates[cols], "symbol": p.symbols[rows], "row": rows, "col": cols,
        "close": p.close[rows, cols], "close_raw": p.close_raw[rows, cols],
        "change_pct": ind.change_pct[rows, cols], "rvol": ind.rvol[rows, cols],
        "close_pos": ind.close_pos[rows, cols], "turnover_cr": ind.turnover[rows, cols] / 1e7,
        "adr20_pct": ind.adr20_pct[rows, cols],
    })


def forward_returns(p: Panel, sig: np.ndarray, horizons=(1, 2, 3, 5, 10, 20, 40, 60)) -> pd.DataFrame:
    """Event study: buy at t+1 open, mark at t+k close. Also the t+1 open gap vs signal close."""
    rows, cols = np.nonzero(sig)
    out = {"symbol": p.symbols[rows], "date": p.dates[cols]}
    D = p.d
    c1 = np.minimum(cols + 1, D - 1)
    entry = p.open[rows, c1]
    entry = np.where(cols + 1 < D, entry, np.nan)
    out["gap_open_pct"] = (entry / p.close[rows, cols] - 1) * 100
    for k in horizons:
        ck = cols + k
        ok = ck < D
        px = np.full(len(rows), np.nan)
        px[ok] = p.close[rows[ok], ck[ok]]
        out[f"r{k}"] = (px / entry - 1) * 100
    # max adverse / favourable excursion over 20 sessions from entry
    mae = np.full(len(rows), np.nan); mfe = np.full(len(rows), np.nan)
    for i, (r, c) in enumerate(zip(rows, cols)):
        a, b = c + 1, min(c + 21, D)
        if a >= D or not np.isfinite(entry[i]):
            continue
        lo = np.nanmin(p.low[r, a:b]); hi = np.nanmax(p.high[r, a:b])
        mae[i] = (lo / entry[i] - 1) * 100; mfe[i] = (hi / entry[i] - 1) * 100
    out["mae20"] = mae; out["mfe20"] = mfe
    return pd.DataFrame(out)
