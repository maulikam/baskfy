"""``desk_score_daily`` for one session — the weekly book's SCORE, as the book computes it (C2).

docs/ranking/PLAN.md C2: the worker writes this table by calling **the book's service**,
``baskfy_core.desk_score_service.score_day``, and never re-scores anything itself. This module is
only the I/O around that call: read the inputs the way the desk reads them, write the rows.

The inputs, and where each comes from
-------------------------------------
* **Bars** — the desk's own ``kite-momentum-rebalancer/app/scan_source.py:_BARS_SQL``: symbol,
  instrument id, date, OHLC, ``volume``, ``close_raw``, ``volume_raw``, **all history on or
  before the as-of date** (DECISIONS-MERGE "Ranking 2.B" 2B.1 — a 270-session history matched and
  260 did not, so nothing is truncated), numbers as floats with NULL read as 0.0 exactly as
  ``_fetch`` does. Two narrowings, neither of which changes a scored row: only NSE instruments,
  because the book keys by symbol and ``score_day`` refuses a symbol that names two instruments;
  and only instruments with a ``factor_daily`` row on the as-of date, because the scan inner-joins
  on ``carried`` and every factor is computed per instrument, so a name outside ``carried`` could
  only cost memory.
* **Trading days** — ``_DAYS_SQL``: the distinct ``ohlcv_daily`` dates on or before the as-of date.
* **Carried** — 2B.2's table, from the as-of day's ``factor_daily`` row (point-in-time):
  ``series``, ``marketcap_cr`` as ``marketcap``, ``beta_12m`` as ``beta``, ``circuits_3m`` and
  ``circuits_12m`` as ``circuits_three_months``/``circuits_one_year``, and ``is_nifty_fno`` as
  ``1 if universe_mask & nifty-fno else 0``. NULL passes through as NULL, never zero-filled.

Rows are upserted on ``(instrument_id, date)`` and any row for the date that the scan no longer
produces is deleted, so re-running a day leaves exactly the rows a first run would (house rule 7).
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from typing import Final

import pandas as pd
import polars as pl
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.desk_score_service import (
    COMPONENT_COLUMNS,
    DESK_SCORE_VERSION,
    score_day,
)
from baskfy_core.models import DeskScoreDaily, FactorDaily, Instrument, OhlcvDaily
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.universes import UNIVERSE_BY_SLUG
from baskfy_worker.steps import StepOutcome

#: `ranking.ensure_nifty_fno_flag`'s rule: the F&O bit of the denormalised universe mask.
NIFTY_FNO_MASK: Final = UNIVERSE_BY_SLUG["nifty-fno"].mask_value

#: `desk_score_daily` is narrow (13 columns), so a 2,000-row statement binds 26,000 parameters,
#: under PostgreSQL's 32,767.
UPSERT_CHUNK: Final = 2000

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


async def load_desk_bars(session: AsyncSession, as_of: dt.date) -> pl.DataFrame:
    """All history on or before ``as_of`` for the NSE instruments with a factor row that day."""
    scanned = select(FactorDaily.instrument_id).where(FactorDaily.date == as_of)
    rows = (
        await session.execute(
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
                OhlcvDaily.date <= as_of,
                OhlcvDaily.instrument_id.in_(scanned),
            )
            .order_by(Instrument.symbol, OhlcvDaily.date)
        )
    ).all()
    return pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "instrument_id": [int(r[1]) for r in rows],
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


async def load_desk_trading_days(session: AsyncSession, as_of: dt.date) -> list[dt.date]:
    """``scan_source._DAYS_SQL``: every date ``ohlcv_daily`` holds on or before ``as_of``."""
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

    bars = await load_desk_bars(session, on)
    trading_days = await load_desk_trading_days(session, on)
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
    "DESK_BAR_SCHEMA",
    "NIFTY_FNO_MASK",
    "desk_rows",
    "load_carried",
    "load_desk_bars",
    "load_desk_trading_days",
    "run_compute_desk_score",
]
