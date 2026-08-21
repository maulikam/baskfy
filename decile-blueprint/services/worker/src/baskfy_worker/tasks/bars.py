"""Step 2 — ``fetch_daily_bars``.

docs/03: "Kite historical, per instrument, chunked, rate-limited".

docs/09: "Kite returns **unadjusted** OHLC by default. Treat everything from Kite as raw; our own
adjustment step produces the adjusted series." So this step writes ``*_raw`` columns and leaves
``close`` equal to ``close_raw`` with ``adj_factor = 1``. Step 4 is what makes them diverge.

Idempotent by upsert on ``(instrument_id, date)`` — docs/02 rule 3: "Re-running any day's job
produces identical rows."
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import polars as pl
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import OhlcvDaily
from baskfy_providers.errors import ProviderError
from baskfy_worker.steps import StepOutcome
from baskfy_worker.window import DateWindow

#: Batched so one instrument's fifteen-year history is not a single statement.
UPSERT_CHUNK: int = 2000


async def run_fetch_daily_bars(
    session: AsyncSession,
    provider: object,
    outcome: StepOutcome,
    instruments: Sequence[tuple[int, str, int | None]],
    window: DateWindow,
) -> int:
    """Fetch and upsert raw bars for ``instruments`` over ``window``."""
    fetch = getattr(provider, "daily_bars", None)
    if not callable(fetch):
        outcome.note(reason="no provider offers daily_bars")
        return 0

    written = 0
    failures: dict[str, str] = {}
    skipped_no_token = 0
    for instrument_id, symbol, token in instruments:
        if token is None:
            # docs/02: Kite does not carry every NSE listing. Not an error — the instrument
            # simply has no bar source yet, and its factors will be NULL (docs/05).
            skipped_no_token += 1
            continue
        try:
            frame = fetch(token, window.start, window.end)
        except ProviderError as exc:
            failures[symbol] = str(exc)
            continue
        written += await upsert_bars(session, instrument_id, frame)

    outcome.rows_in = len(instruments)
    outcome.rows_out = written
    outcome.note(
        window=str(window),
        instruments_without_token=skipped_no_token,
        failures=failures or None,
    )
    if failures:
        # Loud but not fatal: docs/09 leaves "is this day publishable" to the quality gate, which
        # sees the resulting bar count and decides.
        outcome.note(failure_count=len(failures))
    return written


async def upsert_bars(session: AsyncSession, instrument_id: int, frame: pl.DataFrame) -> int:
    """Upsert one instrument's bars. Raw in, raw out — no adjustment happens here."""
    if frame.height == 0:
        return 0
    values = [
        {
            "instrument_id": instrument_id,
            "date": row["date"],
            # `close` starts equal to `close_raw`; apply_adjustments rewrites it (docs/09).
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "close_raw": row["close"],
            "volume_raw": row["volume"],
            "adj_factor": Decimal(1),
            "source": row["source"],
        }
        for row in frame.iter_rows(named=True)
    ]
    written = 0
    for offset in range(0, len(values), UPSERT_CHUNK):
        chunk = values[offset : offset + UPSERT_CHUNK]
        stmt = insert(OhlcvDaily).values(chunk)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[OhlcvDaily.instrument_id, OhlcvDaily.date],
                # `close`, `volume` and `adj_factor` are deliberately NOT overwritten here.
                # They are outputs of step 4; refetching raw bars must not silently un-adjust a
                # series that has already been adjusted.
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close_raw": stmt.excluded.close_raw,
                    "volume_raw": stmt.excluded.volume_raw,
                    "source": stmt.excluded.source,
                },
            )
        )
        written += len(chunk)
    return written
