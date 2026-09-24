"""The seam for ``fo_underlying_daily`` (``docs/fno/03`` §2, ``04`` §4): read, derive, write.

The derivation itself is pure and lives in ``baskfy_core.fno.series`` (FO1, written concurrently
with FO2). This module does the I/O around it: it reads a lookback window of ``fo_contract_daily``
ending at ``trade_date``, hands it to the pure function, and upserts that session's rows —
**without touching ``in_ban``**, which the nightly writes from the ban list before the derivation
may have run (``baskfy_worker.fno.ingest.store_ban_list``).

TODO(FO1-wire): the pure function is passed in rather than imported, so this module builds and
type-checks before ``baskfy_core.fno.series`` exists. Wiring it means (a) confirming or adapting
:class:`DeriveUnderlying` to FO1's real signature, (b) passing
``baskfy_core.fno.series.derive_underlying`` from ``baskfy_worker.fno.nightly.run_night`` (which
carries the same marker, and serves both the Beat task and ``fno_cli ingest``), and (c) removing
the ``skip`` on ``test_fno_underlying.py``. Until then nothing calls
:func:`derive_underlying_daily`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Final

import polars as pl
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import FoContractDaily, FoUnderlyingDaily

#: Sessions of history the derivation reads: ATR14, RV20 and the 20-session turnover median need
#: 20, the 5-session corporate-action exclusion and the previous session's held expiry a few more.
#: Calendar days, generous: ~60 sessions. TODO(FO1-wire): take FO1's own lookback if it names one.
LOOKBACK_DAYS: Final = 90

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
#: rounded to their storage precision (house rule 8). TODO(FO1-wire): confirm against FO1.
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
