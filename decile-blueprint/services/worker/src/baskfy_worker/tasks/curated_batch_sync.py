"""Promote PLANNED curated batches to EXECUTED from the desk journal (T8.2).

Match ``cb_order_batch.desk_plan_id`` to ``desk.rebalance_versions.version_id``.
Synthetic ``cb-sim-*`` ids never match a desk row and stay PLANNED. This module
reads fills; it does not place an order.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import CbOrderBatch
from baskfy_core.models.base import JsonObject

log = logging.getLogger(__name__)

DESK_SCHEMA = "desk"
_SIM_PREFIX = "cb-sim-"
UTC = dt.UTC


@dataclass(frozen=True, slots=True)
class DeskOrderFill:
    planned_qty: Decimal
    filled_qty: Decimal


class DeskFillReader(Protocol):
    """Seam so tests do not need a live desk schema."""

    async def fills_for_plan(self, desk_plan_id: str) -> Sequence[DeskOrderFill] | None:
        """Fills for a desk ``version_id``, or ``None`` when the plan is unknown."""


class SqlDeskFillReader:
    """Reads ``desk.rebalance_orders`` for one ``version_id``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fills_for_plan(self, desk_plan_id: str) -> Sequence[DeskOrderFill] | None:
        if desk_plan_id.startswith(_SIM_PREFIX):
            return None
        try:
            exists = (
                await self._session.execute(
                    text(
                        f'select version_id from "{DESK_SCHEMA}".rebalance_versions '
                        "where version_id = :vid"
                    ),
                    {"vid": desk_plan_id},
                )
            ).first()
            if exists is None:
                return None
            rows = (
                await self._session.execute(
                    text(
                        f"select planned_qty, filled_qty "
                        f'from "{DESK_SCHEMA}".rebalance_orders where version_id = :vid'
                    ),
                    {"vid": desk_plan_id},
                )
            ).mappings()
        except (ProgrammingError, SQLAlchemyError):
            log.warning("desk journal unavailable for plan %s", desk_plan_id)
            return None
        return tuple(
            DeskOrderFill(
                planned_qty=Decimal(str(row["planned_qty"] or 0)),
                filled_qty=Decimal(str(row["filled_qty"] or 0)),
            )
            for row in rows
        )


def classify_fills(fills: Sequence[DeskOrderFill]) -> str | None:
    """``EXECUTED`` if every planned lot is filled; ``PARTIAL`` if any fill landed.

    Returns ``None`` when nothing has filled yet (stay PLANNED).
    """
    planned_rows = [row for row in fills if row.planned_qty > 0]
    if not planned_rows:
        return None
    complete = all(row.filled_qty >= row.planned_qty for row in planned_rows)
    any_fill = any(row.filled_qty > 0 for row in fills)
    if complete:
        return "EXECUTED"
    if any_fill:
        return "PARTIAL"
    return None


async def run_curated_batch_sync(
    session: AsyncSession,
    *,
    reader: DeskFillReader | None = None,
    now: dt.datetime | None = None,
) -> JsonObject:
    """Promote PLANNED (and PARTIAL) batches from the desk journal. Idempotent."""
    moment = now or dt.datetime.now(tz=UTC)
    fill_reader: DeskFillReader = reader if reader is not None else SqlDeskFillReader(session)
    batches = list(
        (
            await session.scalars(
                select(CbOrderBatch).where(
                    CbOrderBatch.status.in_(("PLANNED", "PARTIAL")),
                    CbOrderBatch.desk_plan_id.is_not(None),
                )
            )
        ).all()
    )
    executed = 0
    partial = 0
    skipped = 0
    for batch in batches:
        plan_id = batch.desk_plan_id
        if plan_id is None or plan_id.startswith(_SIM_PREFIX):
            skipped += 1
            continue
        fills = await fill_reader.fills_for_plan(plan_id)
        if fills is None:
            skipped += 1
            continue
        status = classify_fills(fills)
        if status is None:
            skipped += 1
            continue
        if status == "EXECUTED":
            batch.status = "EXECUTED"
            batch.executed_at = moment
            executed += 1
        else:
            batch.status = "PARTIAL"
            partial += 1
    await session.flush()
    return {
        "examined": len(batches),
        "executed": executed,
        "partial": partial,
        "skipped": skipped,
    }
