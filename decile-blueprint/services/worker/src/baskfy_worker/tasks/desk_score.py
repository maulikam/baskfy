"""``desk_score_daily`` for one session — the weekly book's SCORE, as the book computes it (C2).

docs/ranking/PLAN.md C2: the worker writes this table by calling **the book's service**,
``baskfy_core.desk_score_service.score_day``, and never re-scores anything itself. This module is
only the I/O around that call: read the inputs the way the desk reads them, write the rows.

The inputs, and where each comes from
-------------------------------------
* **Bars** — the desk's own ``kite-momentum-rebalancer/app/scan_source.py:_BARS_SQL``: symbol,
  instrument id, date, OHLC, ``volume``, ``close_raw``, ``volume_raw``, numbers as floats with
  NULL read as 0.0 exactly as ``_fetch`` does. Three narrowings, none of which changes a scored
  row: only NSE instruments, because the book keys by symbol and ``score_day`` refuses a symbol
  that names two instruments; only instruments with a ``factor_daily`` row on the as-of date,
  because the scan inner-joins on ``carried``; and **only each instrument's last
  :func:`desk_bar_lookback` bars on or before the as-of date**, not all history.
* **Trading days** — ``_DAYS_SQL``: the distinct ``ohlcv_daily`` dates on or before the as-of date.

Why the bars are bounded, and why that is exact
-----------------------------------------------
The desk's ``_BARS_SQL`` reads all history, and so did this module until 14 Sep 2026, when a
35-day ``backfill-ranking`` on the production box (7.8 GB) read ~3,100 instruments x up to ~3,900
sessions since 2011 — ~8-12 M rows as Python ``Row`` objects of Decimals (~1 KB each) and then a
Polars frame of ~1.4 KB per row inside ``compute_factors`` — and took the box down before the
first day finished. The nightly ran the same load.

DECISIONS-MERGE 2B.1 allowed a truncation "proven identical first". The proof: every column
``score.required`` takes from bars is a window over **one instrument's own rows**
(``.over("instrument_id")``), and the longest is the 12-month volatility, ``N`` returns and so
``N + 1`` bars, ``N`` being the trading days in ``(as_of - 12 months, as_of]``; the 1-year high and
median volume read 252 bars and ``ma_200`` 200. ``rsi_one_month`` reads the 22 shared-date pivot
rows before the as-of row, which the last bars of every instrument supply. So each instrument's
last ``max(N + 1, 252) + DESK_BAR_MARGIN`` bars give the same answer as its whole history. Measured
on synthetic history with gaps, suspensions and late listings: identical at ``N + 1``, different
at ``N``. The bound is **per instrument, by rows**, not by date: a name suspended for years keeps
its pre-suspension bars, exactly as the full history would have handed them to the rolling windows.
DECISIONS-MERGE "Ranking 2.C" 2C.5 records it.
* **Carried** — 2B.2's table, from the as-of day's ``factor_daily`` row (point-in-time):
  ``series``, ``marketcap_cr`` as ``marketcap``, ``beta_12m`` as ``beta``, ``circuits_3m`` and
  ``circuits_12m`` as ``circuits_three_months``/``circuits_one_year``, and ``is_nifty_fno`` as
  ``1 if universe_mask & nifty-fno else 0``. NULL passes through as NULL, never zero-filled.

Rows are upserted on ``(instrument_id, date)`` and any row for the date that the scan no longer
produces is deleted, so re-running a day leaves exactly the rows a first run would (house rule 7).
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

import pandas as pd
import polars as pl
from sqlalchemy import BigInteger, Integer, column, delete, select, true, values
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import Executable

from baskfy_core.desk_score_service import (
    COMPONENT_COLUMNS,
    DESK_SCORE_VERSION,
    score_day,
)
from baskfy_core.models import DeskScoreDaily, FactorDaily, Instrument, OhlcvDaily
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.universes import UNIVERSE_BY_SLUG
from baskfy_core.windows import (
    HIGH_1Y_BARS,
    MA_LENGTHS,
    MEDIAN_VOL_BARS,
    VOL_AVG_BARS,
    subtract_months,
)
from baskfy_worker.steps import StepOutcome

#: `ranking.ensure_nifty_fno_flag`'s rule: the F&O bit of the denormalised universe mask.
NIFTY_FNO_MASK: Final = UNIVERSE_BY_SLUG["nifty-fno"].mask_value

#: `desk_score_daily` is narrow (13 columns), so a 2,000-row statement binds 26,000 parameters,
#: under PostgreSQL's 32,767.
UPSERT_CHUNK: Final = 2000

#: Bars kept per instrument beyond the longest window :func:`desk_bar_lookback` derives: insurance
#: against a row-count window added to the engine later. The worker test that compares a bounded
#: load's scores with the whole history's is what would notice one outgrowing it.
DESK_BAR_MARGIN: Final = 20

#: Rows fetched per round trip when bars are streamed, so a load never holds more than this many
#: driver rows (Decimals, ~1 KB each) at once — only the typed columns accumulate.
STREAM_PARTITION: Final = 50_000

#: The bar frame `momentum_scan.build` is handed, in the desk's column order and types.
DESK_BAR_SCHEMA: Final[dict[str, pl.DataType]] = {
    "symbol": pl.String(),
    "instrument_id": pl.Int64(),
    "date": pl.Date(),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Float64(),
    "close_raw": pl.Float64(),
    "volume_raw": pl.Float64(),
}

#: `carried`, typed as the desk's upload reads it (floats for marketcap and beta, integers for
#: the circuit counts and the F&O flag).
CARRIED_SCHEMA: Final[dict[str, pl.DataType]] = {
    "symbol": pl.String(),
    "series": pl.String(),
    "marketcap": pl.Float64(),
    "beta": pl.Float64(),
    "circuits_three_months": pl.Int64(),
    "circuits_one_year": pl.Int64(),
    "is_nifty_fno": pl.Int64(),
}

_WRITTEN_COLUMNS: Final[tuple[str, ...]] = (
    "score",
    "score_rank",
    *COMPONENT_COLUMNS.values(),
    "reject",
    "score_version",
)


def _desk_float(value: object) -> float:
    """``scan_source._fetch``'s ``column()``: a number as float, and NULL as 0.0."""
    return float(str(value)) if value is not None else 0.0


def desk_bar_lookback(as_of: dt.date, trading_days: Sequence[dt.date]) -> int:
    """How many of each instrument's latest bars (on or before ``as_of``) the book's scan reads.

    ``max(N + 1, 252, ...) + DESK_BAR_MARGIN``, where ``N`` is the 12-month window's length on this
    calendar — the module docstring has the argument. Counted with ``bisect`` rather than
    ``resolve_window`` so a calendar that lacks ``as_of`` still yields a bound instead of raising
    here; the engine then refuses that day exactly as it did before.
    """
    ordered = sorted(trading_days)
    anniversary = subtract_months(as_of, 12)
    window_12m = bisect.bisect_right(ordered, as_of) - bisect.bisect_right(ordered, anniversary)
    longest = max(
        window_12m + 1,
        HIGH_1Y_BARS,
        MEDIAN_VOL_BARS,
        *MA_LENGTHS,
        *VOL_AVG_BARS.values(),
    )
    return longest + DESK_BAR_MARGIN


def desk_bar_window_start(
    as_of: dt.date, trading_days: Sequence[dt.date], lookback: int
) -> dt.date:
    """The ``lookback``-th latest trading day on or before ``as_of``: the date lower bound.

    An instrument with a bar on every one of those days has all ``lookback`` of its bars inside
    it; only a name with a hole in that span needs the per-instrument top-up.
    """
    ordered = [day for day in sorted(trading_days) if day <= as_of]
    if not ordered:
        return as_of
    return ordered[max(0, len(ordered) - lookback)]


def _desk_bar_frame(rows: Sequence[Row[tuple[object, ...]]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "instrument_id": [int(str(r[1])) for r in rows],
            "date": [r[2] for r in rows],
            **{
                name: [_desk_float(r[index]) for r in rows]
                for index, name in enumerate(
                    ("open", "high", "low", "close", "volume", "close_raw", "volume_raw"), start=3
                )
            },
        },
        schema=DESK_BAR_SCHEMA,
    )


async def _stream_desk_bars(session: AsyncSession, stmt: Executable) -> pl.DataFrame:
    """Run a bar query ``STREAM_PARTITION`` rows at a time, keeping only the typed columns."""
    result = await session.stream(stmt.execution_options(yield_per=STREAM_PARTITION))
    frames = [_desk_bar_frame(rows) async for rows in result.partitions(STREAM_PARTITION)]
    return pl.concat(frames) if frames else pl.DataFrame(schema=DESK_BAR_SCHEMA)


async def load_desk_bars(
    session: AsyncSession,
    as_of: dt.date,
    trading_days: Sequence[dt.date] | None = None,
) -> pl.DataFrame:
    """Each scanned NSE instrument's last :func:`desk_bar_lookback` bars on or before ``as_of``.

    Two bounded reads. The first takes every scanned instrument's bars from
    :func:`desk_bar_window_start` to ``as_of`` — a date range, which a hypertable answers from its
    newest chunks. The second tops up only the instruments that came back with fewer than the
    lookback, each with its own latest bars before that date (``LATERAL ... LIMIT``), so a name with
    a suspension in the span gets the same rows its whole history would have supplied.
    """
    days = (
        list(trading_days)
        if trading_days is not None
        else await load_desk_trading_days(session, as_of)
    )
    lookback = desk_bar_lookback(as_of, days)
    window_start = desk_bar_window_start(as_of, days, lookback)

    scanned_rows = await session.execute(
        select(FactorDaily.instrument_id)
        .join(Instrument, Instrument.id == FactorDaily.instrument_id)
        .where(Instrument.exchange_id == NSE_EXCHANGE_ID, FactorDaily.date == as_of)
    )
    scanned = sorted(int(row[0]) for row in scanned_rows)
    if not scanned:
        return pl.DataFrame(schema=DESK_BAR_SCHEMA)

    recent = await _stream_desk_bars(
        session,
        select(
            Instrument.symbol,
            OhlcvDaily.instrument_id,
            OhlcvDaily.date,
            OhlcvDaily.open,
            OhlcvDaily.high,
            OhlcvDaily.low,
            OhlcvDaily.close,
            OhlcvDaily.volume,
            OhlcvDaily.close_raw,
            OhlcvDaily.volume_raw,
        )
        .join(Instrument, Instrument.id == OhlcvDaily.instrument_id)
        .where(
            Instrument.exchange_id == NSE_EXCHANGE_ID,
            OhlcvDaily.date >= window_start,
            OhlcvDaily.date <= as_of,
            OhlcvDaily.instrument_id.in_(scanned),
        )
        .order_by(Instrument.symbol, OhlcvDaily.date),
    )

    have = dict(recent.group_by("instrument_id").len().iter_rows())
    short = [
        (instrument_id, lookback - int(have.get(instrument_id, 0)))
        for instrument_id in scanned
        if int(have.get(instrument_id, 0)) < lookback
    ]
    if not short:
        return recent

    need = values(
        column("instrument_id", BigInteger), column("need", Integer), name="desk_need"
    ).data(short)
    older = (
        select(
            OhlcvDaily.instrument_id,
            OhlcvDaily.date,
            OhlcvDaily.open,
            OhlcvDaily.high,
            OhlcvDaily.low,
            OhlcvDaily.close,
            OhlcvDaily.volume,
            OhlcvDaily.close_raw,
            OhlcvDaily.volume_raw,
        )
        .where(OhlcvDaily.instrument_id == need.c.instrument_id, OhlcvDaily.date < window_start)
        .order_by(OhlcvDaily.date.desc())
        .limit(need.c.need)
        .lateral("desk_older")
    )
    topped_up = await _stream_desk_bars(
        session,
        select(
            Instrument.symbol,
            older.c.instrument_id,
            older.c.date,
            older.c.open,
            older.c.high,
            older.c.low,
            older.c.close,
            older.c.volume,
            older.c.close_raw,
            older.c.volume_raw,
        )
        .select_from(need)
        .join(older, true())
        .join(Instrument, Instrument.id == older.c.instrument_id),
    )
    if topped_up.is_empty():
        return recent
    # Row order is not an input: `compute_factors` sorts by (instrument_id, date) before anything
    # reads the frame. Sorted anyway so the frame reads like the desk's query.
    return pl.concat([topped_up, recent]).sort("symbol", "date")


async def load_desk_trading_days(session: AsyncSession, as_of: dt.date) -> list[dt.date]:
    """``scan_source._DAYS_SQL``: every date ``ohlcv_daily`` holds on or before ``as_of``.

    Unbounded on purpose and cheap: a few thousand dates, aggregated in the database. The calendar
    decides whether a window "spans" (``FactorWindow.spans_full_window``), so it stays whole.
    """
    rows = await session.execute(
        select(OhlcvDaily.date)
        .where(OhlcvDaily.date <= as_of)
        .group_by(OhlcvDaily.date)
        .order_by(OhlcvDaily.date)
    )
    return [row[0] for row in rows]


async def load_carried(session: AsyncSession, as_of: dt.date) -> pl.DataFrame:
    """2B.2's carried columns, from each NSE instrument's ``factor_daily`` row on ``as_of``."""
    rows = (
        await session.execute(
            select(
                Instrument.symbol,
                FactorDaily.series,
                FactorDaily.marketcap_cr,
                FactorDaily.beta_12m,
                FactorDaily.circuits_3m,
                FactorDaily.circuits_12m,
                FactorDaily.universe_mask,
            )
            .join(Instrument, Instrument.id == FactorDaily.instrument_id)
            .where(Instrument.exchange_id == NSE_EXCHANGE_ID, FactorDaily.date == as_of)
            .order_by(Instrument.symbol)
        )
    ).all()
    return pl.DataFrame(
        [
            {
                "symbol": r[0],
                "series": r[1],
                "marketcap": float(r[2]) if r[2] is not None else None,
                "beta": float(r[3]) if r[3] is not None else None,
                "circuits_three_months": int(r[4]) if r[4] is not None else None,
                "circuits_one_year": int(r[5]) if r[5] is not None else None,
                "is_nifty_fno": 1 if int(r[6] or 0) & NIFTY_FNO_MASK else 0,
            }
            for r in rows
        ],
        schema=CARRIED_SCHEMA,
    )


def _stored(value: object) -> object:
    """A pandas cell as the value the column stores: NA is NULL, a float its exact decimal."""
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float):
        return None if math.isnan(value) else Decimal(str(value))
    return value


def desk_rows(scored: pd.DataFrame) -> list[dict[str, object]]:
    """``score_day``'s frame as ``desk_score_daily`` parameter rows."""
    rows: list[dict[str, object]] = []
    for record in scored.to_dict(orient="records"):
        rank = record["score_rank"]
        rows.append(
            {
                "instrument_id": int(str(record["instrument_id"])),
                "date": record["date"],
                "score": _stored(record["score"]),
                "score_rank": None if rank is None or rank is pd.NA else int(str(rank)),
                **{column: _stored(record[column]) for column in COMPONENT_COLUMNS.values()},
                "reject": str(record["reject"]),
                "score_version": str(record["score_version"]),
            }
        )
    return rows


async def run_compute_desk_score(session: AsyncSession, outcome: StepOutcome, on: dt.date) -> int:
    """Score ``on`` with the book's service and write ``desk_score_daily``; returns rows written."""
    carried = await load_carried(session, on)
    outcome.rows_in = carried.height
    if carried.height == 0:
        outcome.note(reason="no factor_daily rows for this date, so nothing to scan")
        return 0

    trading_days = await load_desk_trading_days(session, on)
    bars = await load_desk_bars(session, on, trading_days)
    scored = score_day(bars, on, trading_days, carried)
    rows = desk_rows(scored)

    for offset in range(0, len(rows), UPSERT_CHUNK):
        chunk = rows[offset : offset + UPSERT_CHUNK]
        stmt = insert(DeskScoreDaily).values(chunk)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[DeskScoreDaily.instrument_id, DeskScoreDaily.date],
                set_={column: stmt.excluded[column] for column in _WRITTEN_COLUMNS},
            )
        )
    written_ids = [int(str(row["instrument_id"])) for row in rows]
    stale = await session.execute(
        delete(DeskScoreDaily).where(
            DeskScoreDaily.date == on, DeskScoreDaily.instrument_id.not_in(written_ids)
        )
    )

    outcome.rows_out = len(rows)
    outcome.note(
        date=on.isoformat(),
        score_version=DESK_SCORE_VERSION,
        scored=sum(1 for row in rows if row["score"] is not None),
        rejected=sum(1 for row in rows if row["reject"]),
        stale_rows_deleted=int(getattr(stale, "rowcount", 0) or 0),
    )
    return len(rows)


__all__ = [
    "CARRIED_SCHEMA",
    "DESK_BAR_MARGIN",
    "DESK_BAR_SCHEMA",
    "NIFTY_FNO_MASK",
    "desk_bar_lookback",
    "desk_bar_window_start",
    "desk_rows",
    "load_carried",
    "load_desk_bars",
    "load_desk_trading_days",
    "run_compute_desk_score",
]
