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
from baskfy_providers.errors import CredentialsMissing, ProviderError
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
        return await _fetch_from_bhavcopy(
            session, provider, outcome, window, reason="no provider offers daily_bars"
        )

    written = 0
    failures: dict[str, str] = {}
    skipped_no_token = 0
    for instrument_id, symbol, token in instruments:
        if _credentials_are_missing(failures):
            # Stop after the first credentials failure instead of asking 10,514 times.
            #
            # `CredentialsMissing` is a property of the *deployment*, not of the instrument: it
            # will be raised identically for every remaining name, each one through the retry
            # policy's backoff. On this box that turned a step that should fail in a second into
            # one that ran for many minutes and then failed anyway — which is why a hand-run of
            # the pipeline appeared to hang with no output.
            #
            # Rate-limit and payload errors are *not* treated this way: those are per-symbol and
            # the next instrument may well succeed.
            break
        if token is None:
            # docs/02: Kite does not carry every NSE listing. Not an error — the instrument
            # simply has no bar source yet, and its factors will be NULL (docs/05).
            skipped_no_token += 1
            continue
        try:
            frame = fetch(token, window.start, window.end)
        except CredentialsMissing as exc:
            # Recorded like any other failure so the note still names it, then caught by the
            # guard at the top of the next iteration.
            failures[symbol] = f"CredentialsMissing: {exc}"
            continue
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

    if written == 0 and instruments:
        # Every instrument failed, or none had a Kite token. Either way this day has no bars and
        # the quality gate is about to refuse the run — so try the source that does not need Kite
        # before giving up on the night.
        return await _fetch_from_bhavcopy(
            session, provider, outcome, window, reason="daily_bars produced no rows"
        )
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


def _credentials_are_missing(failures: dict[str, str]) -> bool:
    """Has the provider already told us it has no credentials?

    Read off the recorded failures rather than held in a flag, so the note the operator reads
    afterwards and the decision to stop are the same fact rather than two that can disagree.
    """
    return any(message.startswith("CredentialsMissing") for message in failures.values())


async def _fetch_from_bhavcopy(
    session: AsyncSession,
    provider: object,
    outcome: StepOutcome,
    window: DateWindow,
    *,
    reason: str,
) -> int:
    """Fall back to the NSE bhavcopy when Kite cannot serve the day's bars.

    WHY THIS EXISTS
    ---------------
    `daily_bars` is Kite's, and Kite needs credentials this deployment does not have. On
    2026-08-28 the step recorded *"no provider could serve 'daily_bars' … kite:
    BASKFY_KITE_API_KEY is …"*, wrote zero bars, and the quality gate then failed the run on
    "0 bars against a 10-day median of 2532". The nightly pipeline had not published since
    18 Aug for that reason, and the whole app was serving a ten-day-old session.

    The bhavcopy needs no credentials, and `baskfy_worker.bhavcopy_backfill` already knows how to
    turn one into rows — this calls that rather than restating it. It is also the *better* source
    for this window: it carries `turnover` and both circuit bands natively, which Kite does not
    (docs/05 §12, §13).

    **A fallback, not a replacement.** Kite stays first because it reaches back before 2024, where
    the bhavcopy's UDiFF archive begins. A deployment with Kite configured never gets here.
    """
    from baskfy_worker.bhavcopy_backfill import backfill_bars_from_bhavcopy

    if not callable(getattr(provider, "bhavcopy", None)):
        outcome.note(reason=reason, fallback="unavailable: no provider offers bhavcopy")
        return 0

    report = await backfill_bars_from_bhavcopy(provider, window, progress_every=0)
    outcome.rows_out = report.bars_written
    outcome.note(
        fallback="bhavcopy",
        fallback_reason=reason,
        window=str(window),
        days_written=report.days_written,
        missing_days=[d.isoformat() for d in report.missing_days] or None,
        unmatched_symbol_count=len(report.unmatched_symbols) or None,
        failures=report.failures or None,
    )
    return report.bars_written
