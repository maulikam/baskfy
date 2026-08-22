"""Deterministic bars for the `MomentumScan` fixture — no network, no database, no randomness.

The decile suite is network-blocked by design (Prompt 2 acceptance criterion 1) and the real
271-row corpus needs a seeded Postgres, so the fixture cannot be built from either. These bars are
generated from a closed-form expression instead: same values on every machine, every run, forever,
which is what "byte-identity" has to mean if it is going to catch a regression.

The paths are deliberately *unlike* each other — a riser, a faller, a flat name, a volatile one —
so a change that only affects one shape of series still moves the fixture.
"""

from __future__ import annotations

import datetime as dt
import math

import polars as pl

SYMBOLS = ("ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO")
BARS = 320  # comfortably past the 245 a twelve-month window needs
START = dt.date(2025, 1, 1)

#: Turnover has to clear the desk's ₹5 crore median-liquidity floor or every symbol is rejected as
#: illiquid, the plan comes back empty, and "both paths produce the same plan" passes by comparing
#: nothing to nothing. At ~₹6 crore a day these names are thinly but genuinely tradeable.
VOLUME_BASE = 600_000.0


def _close(index: int, bar: int) -> float:
    drift = (index - 2) * 0.0006  # ALPHA falls, CHARLIE flat, ECHO rises
    wave = math.sin(bar / (7.0 + index * 3)) * (0.02 + index * 0.012)
    return round(100.0 * math.exp(drift * bar) * (1.0 + wave), 2)


def trading_days() -> list[dt.date]:
    """Weekdays only. A calendar with weekends in it would make every window the wrong length."""
    days: list[dt.date] = []
    day = START
    while len(days) < BARS:
        if day.weekday() < 5:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


def bars() -> pl.DataFrame:
    days = trading_days()
    rows: list[dict[str, object]] = []
    for index, symbol in enumerate(SYMBOLS):
        for bar, day in enumerate(days):
            close = _close(index, bar)
            rows.append(
                {
                    "symbol": symbol,
                    "instrument_id": index + 1,
                    "date": day,
                    "open": round(close * 0.995, 2),
                    "high": round(close * 1.012, 2),  # a real intraday high, above the close
                    "low": round(close * 0.988, 2),
                    "close": close,
                    "volume": VOLUME_BASE + bar * 60 + index,
                    "close_raw": close,
                    "volume_raw": VOLUME_BASE + bar * 60 + index,
                }
            )
    return pl.DataFrame(rows)


def carried() -> pl.DataFrame:
    """The four columns no bar series can produce, plus `series`. Fixed, so the fixture is fixed."""
    return pl.DataFrame(
        [
            {
                "symbol": symbol,
                "series": "EQ",
                "marketcap": 5000.0 + index * 1500,
                "beta": round(0.8 + index * 0.1, 2),
                "circuits_three_months": 0,
                "circuits_one_year": index,
                "is_nifty_fno": index % 2,
            }
            for index, symbol in enumerate(SYMBOLS)
        ]
    )
