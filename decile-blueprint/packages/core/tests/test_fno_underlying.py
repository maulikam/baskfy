"""``docs/fno/03`` §2 — one session's ``fo_underlying_daily`` rows, anchored and rounded."""

from __future__ import annotations

import datetime as dt
import math

import polars as pl
import pytest

from baskfy_core.fno.config import SeriesConfig
from baskfy_core.fno.underlying import DERIVED_COLUMNS, derive_underlying

CFG = SeriesConfig()
EXPIRY = dt.date(2024, 9, 26)


def _sessions(start: dt.date, n: int) -> list[dt.date]:
    out: list[dt.date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def _window(days: list[dt.date]) -> pl.DataFrame:
    rows = []
    for i, d in enumerate(days):
        settle = round(1000.0 * math.exp(0.01 * math.sin(i)), 2)
        rows.append(
            {
                "trade_date": d,
                "instrument": "FUTSTK",
                "symbol": "ABC",
                "expiry": EXPIRY,
                "strike": 0.0,
                "option_type": "XX",
                "open": settle,
                "high": settle * 1.01,
                "low": settle * 0.99,
                "close": settle,
                "settle": settle,
                "underlying": settle - 2.0,
                "open_interest": 1000 + i,
                "turnover": 1e7,
                "lot_size": 100,
                "volume": 10,
            }
        )
    return pl.DataFrame(rows)


def test_the_session_row_is_anchored_to_the_held_settle_in_rupees() -> None:
    days = _sessions(dt.date(2024, 7, 15), 30)
    window = _window(days)
    out = derive_underlying(window, days[-1], _sessions(days[0], 80), CFG)
    assert out.height == 1
    row = out.row(0, named=True)
    last_settle = window.filter(pl.col("trade_date") == days[-1])["settle"][0]
    assert row["level_c"] == pytest.approx(last_settle, abs=0.005)
    # ATR in rupees on a ~1,000 future with a 2 % daily range, not a unitless 0.02 that would
    # round to nothing at numeric(18,2).
    assert row["atr14"] > 5
    assert row["held_expiry"] == EXPIRY
    assert row["ca_flag"] is False
    assert set(out.columns) == {"symbol", *DERIVED_COLUMNS}


def test_the_row_is_rounded_to_its_storage_precision() -> None:
    days = _sessions(dt.date(2024, 7, 15), 30)
    row = derive_underlying(_window(days), days[-1], _sessions(days[0], 80), CFG).row(0, named=True)
    for column in ("level_o", "level_h", "level_l", "level_c", "atr14"):
        assert round(row[column], 2) == row[column]
    assert round(row["rv20"], 6) == row["rv20"]


def test_a_window_holding_a_later_session_is_refused() -> None:
    days = _sessions(dt.date(2024, 7, 15), 30)
    with pytest.raises(ValueError, match="look-ahead"):
        derive_underlying(_window(days), days[-2], days, CFG)


def test_an_empty_window_gives_an_empty_frame_with_the_columns() -> None:
    out = derive_underlying(_window([]).clear(), dt.date(2024, 7, 15), [], CFG)
    assert out.is_empty()
    assert out.columns == ["symbol", *DERIVED_COLUMNS]
