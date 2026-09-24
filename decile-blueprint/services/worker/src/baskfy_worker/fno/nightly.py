"""One night of the FO data layer: the bhavcopy, then the next session's ban list (FO2).

Shared by the 18:30 Beat task (``baskfy.fno.ingest_bhavcopy``, behind
``BASKFY_FNO_SCAN_ENABLED``) and ``fno_cli ingest``. Beat fires it hourly 18:30-23:30 on weekdays;
each run is cheap once the night is done — an ``INGESTED`` day and a stored ban list are answered
from the database without an NSE request — so the retries cost nothing after the first success.

The order matters: the bhavcopy for ``trade_date``, then the ban list NSE published for the
session **after** it (``02`` Track C §9), stored on ``trade_date``'s rows. ``fo_underlying_daily``
is derived after each ingested night (``06`` FO2; ``baskfy_worker.fno.underlying``).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import TradingDay
from baskfy_core.models.base import JsonObject
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_worker.fno.ingest import (
    STATUS_INGESTED,
    FoBhavcopyReader,
    ingest_day,
    is_final_attempt,
    store_ban_list,
)
from baskfy_worker.fno.underlying import derive_for_night
from baskfy_worker.ops import is_trading_day


async def next_session(session: AsyncSession, after: dt.date) -> dt.date | None:
    """The next NSE session after ``after``, from ``trading_day``; ``None`` past the calendar."""
    return (
        await session.execute(
            select(TradingDay.date)
            .where(
                TradingDay.exchange_id == NSE_EXCHANGE_ID,
                TradingDay.is_trading_day.is_(True),
                TradingDay.date > after,
            )
            .order_by(TradingDay.date)
            .limit(1)
        )
    ).scalar_one_or_none()


async def run_night(
    session: AsyncSession,
    reader: FoBhavcopyReader,
    trade_date: dt.date,
    *,
    now_ist: dt.datetime,
) -> JsonObject:
    """Ingest ``trade_date`` and store the next session's ban list. Never raises for a provider
    refusal; the outcome, including a ``PENDING`` or ``MISSING`` day, is in the result."""
    if not await is_trading_day(session, trade_date):
        return {"trade_date": trade_date.isoformat(), "skipped": "not an NSE trading day"}
    ingest = await ingest_day(
        session, reader, trade_date, final_attempt=is_final_attempt(trade_date, now_ist)
    )
    out: JsonObject = {"trade_date": trade_date.isoformat(), "ingest": ingest.as_dict()}
    following = await next_session(session, trade_date)
    if following is None:
        out["ban_list"] = {"skipped": "the trading_day calendar has no session after this one"}
    else:
        out["ban_list"] = (await store_ban_list(session, reader, trade_date, following)).as_dict()
    out["underlying_rows"] = (
        await derive_for_night(session, trade_date) if ingest.status == STATUS_INGESTED else None
    )
    return out
