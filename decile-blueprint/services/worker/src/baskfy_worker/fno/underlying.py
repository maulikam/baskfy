"""The seam for ``fo_underlying_daily`` (``docs/fno/03`` §2, ``04`` §4): read, derive, write.

The derivation itself is pure and lives in ``baskfy_core.fno.series`` (FO1, written concurrently
with FO2). This module does the I/O around it: it reads a lookback window of ``fo_contract_daily``
ending at ``trade_date``, hands it to the pure function, and upserts that session's rows —
**without touching ``in_ban``**, which the nightly writes from the ban list before the derivation
may have run (``baskfy_worker.fno.ingest.store_ban_list``).

The pure function is passed in (:data:`DeriveUnderlying`), so the tests can drive the seam with a
stand-in; the nightly passes :func:`derive_for_night`'s binding of
``baskfy_core.fno.underlying.derive_underlying`` to the exchange calendar.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Final

import polars as pl
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.fno.config import SeriesConfig
from baskfy_core.fno.underlying import derive_underlying
from baskfy_core.models import FoContractDaily, FoUnderlyingDaily, TradingDay
from baskfy_core.seed_data import NSE_EXCHANGE_ID

#: Sessions of history the derivation reads: ATR14, RV20 and the 20-session turnover median need
#: 20, the 5-session corporate-action exclusion and the previous session's held expiry a few more.
#: Calendar days: ~80 sessions, enough for F2's 50-session average (``04`` §10) to be read from a
#: re-derived window as well.
LOOKBACK_DAYS: Final = 120
#: Calendar days of future calendar ``iv_atm`` needs (the nearest monthly with >= 8 sessions
#: left can be two expiries out).
CALENDAR_AHEAD_DAYS: Final = 100

#: The derived columns :func:`derive_underlying_daily` writes (``03`` §2), ``in_ban`` excluded.
DERIVED_COLUMNS: Final[tuple[str, ...]] = (
    "held_expiry",
    "level_o",
    "level_h",
    "level_l",
    "level_c",
    "ret",
    "atr14",
    "oi_total",
    "fut_turnover_20d",
    "iv_atm",
    "rv20",
    "basis_ann",
    "ca_flag",
)

#: The contract ``derive_underlying_daily`` assumes of FO1's pure function: ``fo_contract_daily``
#: rows (column names as the table's) for a window ending at ``trade_date`` in, one row per
#: underlying for ``trade_date`` out, with ``symbol`` and the :data:`DERIVED_COLUMNS`, already
#: rounded to their storage precision (house rule 8).
DeriveUnderlying = Callable[[pl.DataFrame, dt.date], pl.DataFrame]


async def load_window(session: AsyncSession, trade_date: dt.date) -> pl.DataFrame:
    """``fo_contract_daily`` for ``(trade_date - LOOKBACK_DAYS, trade_date]``, as a frame."""
    start = trade_date - dt.timedelta(days=LOOKBACK_DAYS)
    columns = [c for c in FoContractDaily.__table__.columns]
    rows = (
        await session.execute(
            select(*columns).where(
                FoContractDaily.trade_date > start, FoContractDaily.trade_date <= trade_date
            )
        )
    ).all()
    return pl.DataFrame(
        [tuple(r) for r in rows], schema=[c.name for c in columns], orient="row", strict=False
    )


async def derive_underlying_daily(
    session: AsyncSession, trade_date: dt.date, derive: DeriveUnderlying
) -> int:
    """Derive and upsert ``fo_underlying_daily`` for ``trade_date``; return rows written.

    Idempotent (house rule 7): the upsert is keyed on ``(trade_date, symbol)`` and a re-run over
    the same ``fo_contract_daily`` writes the same values. ``in_ban`` is never in the SET list.
    """
    window = await load_window(session, trade_date)
    if window.is_empty():
        return 0
    derived = derive(window, trade_date)
    if derived.is_empty():
        return 0
    rows: list[dict[str, object]] = [
        {"trade_date": trade_date, "symbol": row["symbol"], **{c: row[c] for c in DERIVED_COLUMNS}}
        for row in derived.iter_rows(named=True)
    ]
    stmt = insert(FoUnderlyingDaily).values(rows)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["trade_date", "symbol"],
            set_={
                **{c: stmt.excluded[c] for c in DERIVED_COLUMNS},
                "updated_at": func.now(),
            },
        )
    )
    await session.flush()
    return len(rows)


async def trading_days(session: AsyncSession, start: dt.date, end: dt.date) -> list[dt.date]:
    """NSE sessions in ``[start, end]`` from the ``trading_day`` calendar, oldest first."""
    rows = await session.execute(
        select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date >= start,
            TradingDay.date <= end,
        )
        .order_by(TradingDay.date)
    )
    return [row[0] for row in rows]


async def derive_for_night(session: AsyncSession, trade_date: dt.date) -> int:
    """The nightly's call: bind the pure derivation to the calendar and upsert ``trade_date``."""
    sessions = await trading_days(
        session,
        trade_date - dt.timedelta(days=LOOKBACK_DAYS),
        trade_date + dt.timedelta(days=CALENDAR_AHEAD_DAYS),
    )
    config = SeriesConfig()

    def derive(window: pl.DataFrame, day: dt.date) -> pl.DataFrame:
        return derive_underlying(window, day, sessions, config)

    return await derive_underlying_daily(session, trade_date, derive)
