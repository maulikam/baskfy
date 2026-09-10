"""Synthetic bars for the VBT-1 tests (``docs/vbt/04``).

Small, hand-built and deliberately boring: every test that asserts a rule should be able to say
which number it changed. The one *large* fixture is the real research export, and that lives in
``test_vbt_goldens.py`` where it can be skipped when the export is absent.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl

from baskfy_core.vbt.calendar import SessionCalendar, build_calendar

START = dt.date(2024, 1, 1)


def sessions(count: int, start: dt.date = START) -> list[dt.date]:
    """``count`` consecutive weekday dates. The calendar is whatever the bars contain."""
    out: list[dt.date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def flat_bars(  # noqa: PLR0913 - a fixture is its knobs
    *,
    instrument_id: int = 1,
    symbol: str = "TESTCO",
    count: int = 260,
    close: float = 100.0,
    volume: float = 100_000.0,
    start: dt.date = START,
) -> pl.DataFrame:
    """A perfectly flat instrument: every bar identical. The baseline every test perturbs."""
    days = sessions(count, start)
    return pl.DataFrame(
        {
            "instrument_id": [instrument_id] * count,
            "symbol": [symbol] * count,
            "date": days,
            "open": [close] * count,
            "high": [close] * count,
            "low": [close] * count,
            "close": [close] * count,
            "close_raw": [close] * count,
            "volume": [volume] * count,
            "upper_circuit": [None] * count,
            "adj_factor": [1.0] * count,
        },
        schema_overrides={"upper_circuit": pl.Float64},
    )


def set_bar(  # noqa: PLR0913 - a bar is its five numbers, and one of them is `open`
    bars: pl.DataFrame,
    session: dt.date,
    *,
    open: float | None = None,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    volume: float | None = None,
    close_raw: float | None = None,
    upper_circuit: float | None = None,
) -> pl.DataFrame:
    """Replace one session's bar. ``close_raw`` follows ``close`` unless it is given."""
    updates = {
        "open": open,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "close_raw": close_raw if close_raw is not None else close,
        "upper_circuit": upper_circuit,
    }
    frame = bars
    for column, value in updates.items():
        if value is None:
            continue
        frame = frame.with_columns(
            pl.when(pl.col("date") == session)
            .then(pl.lit(value, dtype=pl.Float64))
            .otherwise(pl.col(column))
            .alias(column)
        )
    return frame


def rising_bars(  # noqa: PLR0913 - a fixture is its knobs
    *,
    instrument_id: int = 1,
    symbol: str = "RISER",
    count: int = 260,
    start_close: float = 50.0,
    step: float = 0.2,
    volume: float = 100_000.0,
    start: dt.date = START,
) -> pl.DataFrame:
    """A steady uptrend, so filter A and the 21-EMA are satisfied without any special pleading."""
    days = sessions(count, start)
    closes = [start_close + step * i for i in range(count)]
    return pl.DataFrame(
        {
            "instrument_id": [instrument_id] * count,
            "symbol": [symbol] * count,
            "date": days,
            "open": closes,
            "high": [c * 1.002 for c in closes],
            "low": [c * 0.998 for c in closes],
            "close": closes,
            "close_raw": closes,
            "volume": [volume] * count,
            "upper_circuit": [None] * count,
            "adj_factor": [1.0] * count,
        },
        schema_overrides={"upper_circuit": pl.Float64},
    )


def calendar_for(bars: pl.DataFrame) -> SessionCalendar:
    return build_calendar(bars)


def money(value: str) -> Decimal:
    return Decimal(value)


BACKGROUND_ID = 9_999


def with_background(bars: pl.DataFrame, *, count: int, start: dt.date = START) -> pl.DataFrame:
    """Add a name that prints **every** session, so the calendar survives a hole in the subject.

    Without it, punching a hole in a one-instrument fixture deletes the *session* rather than the
    bar — there is no calendar independent of the bars — and a test meaning to exercise the
    missing-bar tolerance would silently be exercising a shorter history instead.
    """
    background = flat_bars(instrument_id=BACKGROUND_ID, symbol="BGCO", count=count, start=start)
    for column in bars.columns:
        if column not in background.columns:
            background = background.with_columns(
                pl.lit(None, dtype=bars.schema[column]).alias(column)
            )
    return pl.concat([bars, background.select(bars.columns)], how="vertical_relaxed")
