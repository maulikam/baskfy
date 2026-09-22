"""Monthly partitions of ``op_chain_snapshot`` (``docs/options/03`` §5, DECISIONS-OP OP2.5).

``0050_options`` creates Sep 2026 → Dec 2027. There is deliberately no DEFAULT partition: a row
for an unpartitioned month fails loudly instead of landing where a later ``PARTITION OF`` would be
refused. The collector (OP3) calls :func:`ensure_chain_partition` for the current and next month
before it writes, so the table never reaches the end of its partitions. Idempotent.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def month_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    """The first day of ``day``'s month and of the month after."""
    start = day.replace(day=1)
    if start.month == 12:  # noqa: PLR2004 - December rolls the year
        return start, dt.date(start.year + 1, 1, 1)
    end = start.replace(month=start.month + 1)
    return start, end


def partition_name(day: dt.date) -> str:
    """Same naming as the migration: ``op_chain_snapshot_YYYYMM``."""
    start, _ = month_bounds(day)
    return f"op_chain_snapshot_{start.year:04d}{start.month:02d}"


async def ensure_chain_partition(session: AsyncSession, day: dt.date) -> str:
    """Create ``day``'s month partition if it does not exist; return its name.

    Bounds are IST midnights, the migration's convention, so a month holds its sessions exactly.
    The name and bounds come from a date, never from input text, so the DDL cannot be injected.
    """
    start, end = month_bounds(day)
    name = partition_name(day)
    await session.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF op_chain_snapshot "
            f"FOR VALUES FROM ('{start.isoformat()} 00:00:00+05:30') "
            f"TO ('{end.isoformat()} 00:00:00+05:30')"
        )
    )
    return name
