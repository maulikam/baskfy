"""The cross-sectional and cross-date ranking columns, and the Phase-2 column writer.

docs/ranking/PLAN.md C1 names two ``factor_daily`` columns that no single instrument's bars can
produce, so ``compute_factors`` leaves them NULL and the worker fills them once the day's rows are
written:

* ``mom_pctile`` — ``baskfy_core.factors_ranking.cross_sectional_pctile`` over the date's rows
  **as written**: the four Sharpe components and ``universe_mask`` are read back from
  ``factor_daily`` rather than taken from the frame that was upserted, because the blend it ranks
  is the screener's mean over the stored (rounded) components, and ranking anything else would
  disagree with the screen on ties. SQL ``PERCENT_RANK`` x 100, i.e. ``(rank - 1) / (n - 1)``
  (DECISIONS-MERGE "Ranking 2.A" 2A.4c).
* ``rank_persist_20`` — ``factors_ranking.rank_persistence`` over the last 20 distinct
  ``factor_daily`` dates on or before the as-of date, with every prior date's ``mom_pctile`` read
  from the database. Nothing dated after the as-of date is read (house rule 5).

Both updates touch only their own column, and running them twice leaves identical rows (house
rule 7). :func:`write_phase2_columns` is the backfill's writer: it updates exactly the C1 columns
on rows that already exist and never inserts, so every pre-Phase-2 column stays byte-identical.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable
from typing import Final

import polars as pl
from sqlalchemy import bindparam, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from baskfy_core.factors_ranking import (
    MOM_PCTILE_COMPONENTS,
    RANK_PERSIST_DATES,
    RANKING_FACTOR_COLUMNS,
    cross_sectional_pctile,
    rank_persistence,
)
from baskfy_core.models import FactorDaily

#: The two columns this module fills after the day's rows exist.
CROSS_SECTIONAL_COLUMNS: Final[tuple[str, ...]] = ("mom_pctile", "rank_persist_20")

#: Every C1 column that ``compute_factors`` itself produces per instrument.
PER_INSTRUMENT_PHASE2_COLUMNS: Final[tuple[str, ...]] = tuple(
    column for column in RANKING_FACTOR_COLUMNS if column not in CROSS_SECTIONAL_COLUMNS
)

#: Every column Phase 2 added to ``factor_daily``: C1 plus the two cross-sectional ones.
PHASE2_COLUMNS: Final[tuple[str, ...]] = RANKING_FACTOR_COLUMNS

#: The table itself, for a Core executemany UPDATE (an ORM-enabled UPDATE with a parameter list
#: switches to bulk-by-primary-key mode, which refuses the WHERE clause this needs).
_TABLE: Final = FactorDaily.metadata.tables[FactorDaily.__tablename__]
_KEY_ID: Final = "k_instrument_id"
_KEY_DATE: Final = "k_date"


def _update_by_key(columns: Iterable[str]) -> Update:
    """``UPDATE factor_daily SET <columns> WHERE instrument_id = :id AND date = :date``.

    Bound per column through ``bindparam`` against the table's own column, so a value takes the
    column's type exactly as the nightly upsert binds it. The parameter names are prefixed because
    SQLAlchemy reserves a bare column name for its own SET clause.
    """
    return (
        update(_TABLE)
        .where(
            _TABLE.c.instrument_id == bindparam(_KEY_ID),
            _TABLE.c.date == bindparam(_KEY_DATE),
        )
        .values({column: bindparam(f"v_{column}") for column in columns})
    )


def _finite(value: object) -> object:
    """NaN and infinity are not numbers a ``numeric`` column can state; they are NULL."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


async def _execute_updates(
    session: AsyncSession, columns: tuple[str, ...], rows: list[dict[str, object]]
) -> int:
    if not rows:
        return 0
    await session.execute(_update_by_key(columns), rows)
    return len(rows)


async def fill_mom_pctile(session: AsyncSession, on: dt.date) -> int:
    """Write ``mom_pctile`` for every ``factor_daily`` row dated ``on``; returns rows updated."""
    rows = (
        await session.execute(
            select(
                FactorDaily.instrument_id,
                FactorDaily.universe_mask,
                *(getattr(FactorDaily, column) for column in MOM_PCTILE_COMPONENTS),
            ).where(FactorDaily.date == on)
        )
    ).all()
    if not rows:
        return 0
    frame = pl.DataFrame(
        [
            {
                "instrument_id": int(row[0]),
                "universe_mask": int(row[1]),
                **{
                    column: float(value) if value is not None else None
                    for column, value in zip(MOM_PCTILE_COMPONENTS, row[2:], strict=True)
                },
            }
            for row in rows
        ],
        schema={
            "instrument_id": pl.Int64(),
            "universe_mask": pl.Int64(),
            **{column: pl.Float64() for column in MOM_PCTILE_COMPONENTS},
        },
    )
    ranked = cross_sectional_pctile(frame)
    return await _execute_updates(
        session,
        ("mom_pctile",),
        [
            {_KEY_ID: instrument_id, _KEY_DATE: on, "v_mom_pctile": _finite(value)}
            for instrument_id, value in ranked.select("instrument_id", "mom_pctile").iter_rows()
        ],
    )


async def persistence_dates(session: AsyncSession, on: dt.date) -> list[dt.date]:
    """The last ``RANK_PERSIST_DATES`` distinct ``factor_daily`` dates on or before ``on``."""
    rows = await session.execute(
        select(FactorDaily.date)
        .where(FactorDaily.date <= on)
        .group_by(FactorDaily.date)
        .order_by(FactorDaily.date.desc())
        .limit(RANK_PERSIST_DATES)
    )
    return sorted(row[0] for row in rows)


async def fill_rank_persist_20(session: AsyncSession, on: dt.date) -> int:
    """Write ``rank_persist_20`` for every row dated ``on``, from the stored ``mom_pctile`` history.

    Must run after :func:`fill_mom_pctile` for ``on``, and — in a backfill — after every earlier
    date's, which is why the backfill walks dates oldest first.
    """
    dates = await persistence_dates(session, on)
    if not dates:
        return 0
    rows = (
        await session.execute(
            select(FactorDaily.instrument_id, FactorDaily.date, FactorDaily.mom_pctile).where(
                FactorDaily.date.in_(dates)
            )
        )
    ).all()
    history = pl.DataFrame(
        [
            {
                "instrument_id": int(row[0]),
                "date": row[1],
                "mom_pctile": float(row[2]) if row[2] is not None else None,
            }
            for row in rows
        ],
        schema={"instrument_id": pl.Int64(), "date": pl.Date(), "mom_pctile": pl.Float64()},
    )
    persistence = rank_persistence(history, on)
    return await _execute_updates(
        session,
        ("rank_persist_20",),
        [
            {_KEY_ID: instrument_id, _KEY_DATE: on, "v_rank_persist_20": _finite(value)}
            for instrument_id, value in persistence.iter_rows()
        ],
    )


async def run_rank_columns(session: AsyncSession, on: dt.date) -> dict[str, int]:
    """Both cross-sectional columns for ``on``, in the order they depend on each other."""
    return {
        "mom_pctile_rows": await fill_mom_pctile(session, on),
        "rank_persist_20_rows": await fill_rank_persist_20(session, on),
    }


async def write_phase2_columns(session: AsyncSession, frame: pl.DataFrame, on: dt.date) -> int:
    """Update the per-instrument C1 columns of rows that already exist for ``on``.

    Never inserts: a row the nightly never wrote is not the backfill's to invent, and an insert
    would have to supply every pre-Phase-2 column. Never touches a column outside
    :data:`PER_INSTRUMENT_PHASE2_COLUMNS`, which is what keeps the rest of the row byte-identical.
    """
    existing = {
        int(row[0])
        for row in (
            await session.execute(select(FactorDaily.instrument_id).where(FactorDaily.date == on))
        ).all()
    }
    missing = [column for column in PER_INSTRUMENT_PHASE2_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"the factor frame for {on} carries no {missing}")
    updates = [
        {
            _KEY_ID: int(row["instrument_id"]),
            _KEY_DATE: on,
            **{f"v_{column}": _finite(row[column]) for column in PER_INSTRUMENT_PHASE2_COLUMNS},
        }
        for row in frame.select("instrument_id", *PER_INSTRUMENT_PHASE2_COLUMNS).iter_rows(
            named=True
        )
        if int(row["instrument_id"]) in existing
    ]
    return await _execute_updates(session, PER_INSTRUMENT_PHASE2_COLUMNS, updates)


__all__ = [
    "CROSS_SECTIONAL_COLUMNS",
    "PER_INSTRUMENT_PHASE2_COLUMNS",
    "PHASE2_COLUMNS",
    "fill_mom_pctile",
    "fill_rank_persist_20",
    "persistence_dates",
    "run_rank_columns",
    "write_phase2_columns",
]
