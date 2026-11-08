"""EOD curated-basket metrics job - docs/smallcase/04 section 2-3, SC2.

Thin wrapper: pure compute lives in ``baskfy_core.curated_metrics``; DB upsert in
``baskfy_api.curated_metrics_service``. Re-running for a date is a no-op (same values).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.curated_metrics_service import compute_all_metrics
from baskfy_core.models.base import JsonObject


async def run_curated_metrics(
    session: AsyncSession,
    trade_date: dt.date,
    *,
    now: dt.datetime | None = None,
) -> JsonObject:
    """Upsert ``cb_metrics`` for every non-archived basket on *trade_date*."""
    return await compute_all_metrics(session, trade_date, now=now)
