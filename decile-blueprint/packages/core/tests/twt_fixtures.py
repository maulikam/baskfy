"""Synthetic bars for the TWT-1 tests (``docs/twt/04``).

Small, hand-built and deliberately boring: every test that asserts a rule should be able to say
which number it changed. The one *large* fixture is the research export, and that lives in TW2's
``test_twt_goldens.py`` where it can be skipped when the export is absent.

The subject instrument is flat at ``base_close`` for its whole history and then has exactly three
weekly closes placed on it — today's, and the last close of each of the two preceding ISO weeks.
That shape is what ``04`` §3.2 is about, and it means a test that wants to break rule 3 moves one
of three numbers rather than reasoning about where a week ended.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl

from baskfy_core.twt.calendar import SessionCalendar, build_calendar

START = dt.date(2024, 1, 1)
BACKGROUND_ID = 9_999


def sessions(count: int, start: dt.date = START) -> list[dt.date]:
    """``count`` consecutive weekday dates. The calendar is whatever the bars contain."""
    out: list[dt.date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def week_key(day: dt.date) -> int:
    """``iso_year x 100 + iso_week`` — the fixture's own copy of the production key.

    Deliberately a second implementation: a fixture that imported the expression it is testing
    could not catch the calendar-year bug the year-boundary case exists for.
    """
    iso = day.isocalendar()
    return iso.year * 100 + iso.week


def last_session_of_week_before(days: list[dt.date], anchor: dt.date, weeks_back: int) -> dt.date:
    """The last session of the ``weeks_back``-th week bucket before ``anchor``'s, in ``days``.

    "Week buckets the data holds", exactly as ``04`` §3.2 defines them — the distinct ISO week keys
    present, in ascending order — so the helper answers the same question the code does without
    borrowing its machinery.
    """
    keys = sorted({week_key(day) for day in days})
    here = keys.index(week_key(anchor))
    target = keys[here - weeks_back]
    return max(day for day in days if week_key(day) == target)


def flat_bars(  # noqa: PLR0913 - a fixture is its knobs
    *,
    instrument_id: int = 1,
    symbol: str = "TWTCO",
    count: int = 260,
    close: float = 60.0,
    volume: float = 100_000.0,
    start: dt.date = START,
    is_etf: bool = False,
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
            "adj_factor": [1.0] * count,
            "is_etf": [is_etf] * count,
        },
        schema_overrides={"instrument_id": pl.Int64, "is_etf": pl.Boolean},
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
    instrument_id: int = 1,
) -> pl.DataFrame:
    """Replace one session's bar for one instrument.

    ``close`` moves ``open``, ``high``, ``low`` and ``close_raw`` with it unless they are given —
    the fixture's bars are flat, so a test that says "this close was 92" means a bar at 92 and not
    a bar with a 92 close inside a 60 range.
    """
    updates: dict[str, float | None] = {
        "close": close,
        "open": open if open is not None else close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "close_raw": close_raw if close_raw is not None else close,
        "volume": volume,
    }
    frame = bars
    picked = (pl.col("date") == session) & (pl.col("instrument_id") == instrument_id)
    for column, value in updates.items():
        if value is None:
            continue
        frame = frame.with_columns(
            pl.when(picked)
            .then(pl.lit(value, dtype=pl.Float64))
            .otherwise(pl.col(column))
            .alias(column)
        )
    return frame


def drop_bar(bars: pl.DataFrame, session: dt.date, instrument_id: int = 1) -> pl.DataFrame:
    """Delete one instrument's bar for one session — the name did not print that day."""
    return bars.filter(~((pl.col("date") == session) & (pl.col("instrument_id") == instrument_id)))


def tight_bars(  # noqa: PLR0913 - a fixture is its knobs, and this one is the whole of `04` §3
    *,
    count: int = 260,
    start: dt.date = START,
    base_close: float = 60.0,
    close: float = 90.0,
    previous_week_close: float | None = None,
    week_before_close: float | None = None,
    close_raw: float | None = None,
    volume: float = 100_000.0,
    signal_volume: float | None = None,
    symbol: str = "TWTCO",
    is_etf: bool = False,
) -> pl.DataFrame:
    """One instrument whose last three weekly closes are exactly the three numbers given.

    Flat at ``base_close`` throughout, so the month-3 low is ``base_close`` and rule 2 is a single
    comparison; then today's close, and the last close of each of the two preceding ISO week
    buckets, placed on their own sessions. ``previous_week_close`` and ``week_before_close``
    default to ``close``, which makes the baseline a 0 % range.
    """
    days = sessions(count, start)
    today = days[-1]
    frame = flat_bars(
        count=count, start=start, close=base_close, volume=volume, symbol=symbol, is_etf=is_etf
    )
    frame = set_bar(
        frame,
        today,
        close=close,
        close_raw=close_raw,
        volume=signal_volume if signal_volume is not None else volume,
    )
    for back, value in (
        (1, previous_week_close if previous_week_close is not None else close),
        (2, week_before_close if week_before_close is not None else close),
    ):
        frame = set_bar(frame, last_session_of_week_before(days, today, back), close=value)
    return frame


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


def calendar_for(bars: pl.DataFrame) -> SessionCalendar:
    return build_calendar(bars)


def money(value: str) -> Decimal:
    return Decimal(value)
