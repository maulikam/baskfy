"""Synthetic bar series for the swing tests — shapes the method describes, drawn on purpose.

Every builder returns rows for one instrument over ``n`` calendar days (the swing engine never
touches a calendar; dates only need to be increasing). Prices are floats because the detectors
read floats; money in the sizing tests is ``Decimal`` because house rule 9 says so.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

START = dt.date(2026, 1, 1)


def _rows(  # noqa: PLR0913 - one keyword per shape parameter
    instrument_id: int,
    symbol: str,
    close: np.ndarray,
    volume: np.ndarray,
    *,
    open_: np.ndarray | None = None,
    high: np.ndarray | None = None,
    low: np.ndarray | None = None,
    upper_circuit: np.ndarray | None = None,
) -> list[dict[str, object]]:
    n = len(close)
    open_ = close if open_ is None else open_
    high = close * 1.02 if high is None else high
    low = close * 0.98 if low is None else low
    rows: list[dict[str, object]] = []
    for i in range(n):
        row: dict[str, object] = {
            "instrument_id": instrument_id,
            "symbol": symbol,
            "date": START + dt.timedelta(days=i),
            "open": float(open_[i]),
            "high": float(high[i]),
            "low": float(low[i]),
            "close": float(close[i]),
            "volume": float(volume[i]),
        }
        if upper_circuit is not None:
            row["upper_circuit"] = float(upper_circuit[i])
        rows.append(row)
    return rows


def flag_series(  # noqa: PLR0913 - one keyword per shape parameter
    instrument_id: int = 1,
    symbol: str = "FLAGCO",
    *,
    n: int = 140,
    pole_gain: float = 0.5,
    base_bars: int = 35,
    base_noise: float = 1.5,
    dryup: bool = True,
    seed: int = 1,
) -> list[dict[str, object]]:
    """Flat, then a 20-bar pole of ``pole_gain``, then a base of ``base_bars`` bars.

    The base is a contracting zig-zag with a gentle upward drift: lows rise, the swings shrink
    from ±3% to ±0.5%, and the last bars sit on the rising 20-day MA — the shape he calls
    "textbook". ``base_noise`` (in price units) is added on top, seeded, so a noisy base can be
    asked for explicitly.
    """
    rng = np.random.default_rng(seed)
    pole_bars = 20
    flat = n - pole_bars - base_bars
    top = 100.0 * (1 + pole_gain)
    i = np.arange(base_bars)
    swing = np.linspace(0.03, 0.005, base_bars) * np.where(i % 2 == 0, -1.0, 1.0)
    drift = np.linspace(0.0, 0.02, base_bars)
    base = top * (0.95 + drift + swing) + rng.normal(0, base_noise, base_bars) * (base_noise > 2.0)
    close = np.concatenate([np.full(flat, 100.0), np.linspace(100.0, top, pole_bars), base])
    base_vol = np.linspace(1.5e6, 0.5e6, base_bars) if dryup else np.full(base_bars, 1.5e6)
    volume = np.concatenate([np.full(flat, 1e6), np.full(pole_bars, 3e6), base_vol])
    return _rows(instrument_id, symbol, close, volume)


def flat_series(
    instrument_id: int = 2, symbol: str = "FLATCO", *, n: int = 140
) -> list[dict[str, object]]:
    return _rows(instrument_id, symbol, np.full(n, 100.0), np.full(n, 1e6))


def ep_series(  # noqa: PLR0913 - one keyword per shape parameter
    instrument_id: int = 3,
    symbol: str = "EPCO",
    *,
    n: int = 140,
    gap: float = 0.15,
    rvol: float = 5.0,
    close_position: float = 0.7,
    prior_gain: float = 0.0,
    locked: bool = False,
    seed: int = 2,
) -> list[dict[str, object]]:
    """A neglected base, then a gap day that holds."""
    rng = np.random.default_rng(seed)
    prior_bars = 60
    flat = np.full(n - 1 - prior_bars, 100.0)
    drift = np.linspace(100.0, 100.0 * (1 + prior_gain), prior_bars)
    close = np.concatenate([flat, drift, [0.0]]) + np.concatenate(
        [rng.normal(0, 0.3, n - 1), [0.0]]
    )
    prev = close[-2]
    open_ = close.copy()
    open_[-1] = prev * (1 + gap)
    day_low = open_[-1] * 0.98
    day_high = open_[-1] * 1.05
    close[-1] = day_low + close_position * (day_high - day_low)
    high = close * 1.02
    low = close * 0.98
    high[-1] = day_high
    low[-1] = day_low
    volume = np.concatenate([np.full(n - 1, 1e6), [rvol * 1e6]])
    circuit = None
    if locked:
        circuit = np.full(n, 1e9)
        circuit[-1] = day_high
    return _rows(
        instrument_id, symbol, close, volume, open_=open_, high=high, low=low, upper_circuit=circuit
    )


def parabolic_series(  # noqa: PLR0913 - one keyword per shape parameter
    instrument_id: int = 4,
    symbol: str = "PARACO",
    *,
    n: int = 140,
    up_days: int = 6,
    daily_gain: float = 0.12,
    red_last_day: bool = False,
) -> list[dict[str, object]]:
    price = 100.0
    close = np.full(n, 100.0)
    for i in range(n - up_days, n):
        price *= 1 + daily_gain
        close[i] = price
    if red_last_day:
        close[-1] = close[-2] * 0.97
    open_ = close / 1.04
    high = close * 1.015
    low = close / 1.03
    volume = np.where(np.arange(n) >= n - up_days, 2e6, 1e6)
    return _rows(instrument_id, symbol, close, volume, open_=open_, high=high, low=low)


def frame(*series: list[dict[str, object]]) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for s in series:
        rows.extend(s)
    return pl.DataFrame(rows)


def last_date(n: int = 140) -> dt.date:
    return START + dt.timedelta(days=n - 1)
