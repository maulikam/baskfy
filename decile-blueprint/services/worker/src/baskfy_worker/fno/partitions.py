"""Monthly partitions of ``fo_contract_daily`` (``docs/fno/03`` §1).

``0052_fno`` creates Jan 2022 → Dec 2027. There is deliberately no DEFAULT partition (the reason
DECISIONS-OP OP2.5 gave for the chain): a row for an unpartitioned month fails loudly. The ingest
calls :func:`ensure_contract_partition` for a session's month before it writes, so the table never
reaches the end of its partitions. Idempotent.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_worker.options.partitions import month_bounds


def partition_name(day: dt.date) -> str:
    """Same naming as the migration: ``fo_contract_daily_YYYYMM``."""
    start, _ = month_bounds(day)
    return f"fo_contract_daily_{start.year:04d}{start.month:02d}"


async def ensure_contract_partition(session: AsyncSession, day: dt.date) -> str:
    """Create ``day``'s month partition if it does not exist; return its name.

    The name and bounds come from a date, never from input text, so the DDL cannot be injected.
    """
    start, end = month_bounds(day)
    name = partition_name(day)
    await session.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF fo_contract_daily "
            f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
        )
    )
    return name
