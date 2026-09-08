"""Step 2 — ``fetch_daily_bars``.

docs/03: "Kite historical, per instrument, chunked, rate-limited".

docs/09: "Kite returns **unadjusted** OHLC by default. Treat everything from Kite as raw; our own
adjustment step produces the adjusted series." So this step writes ``*_raw`` columns and leaves
``close`` equal to ``close_raw`` with ``adj_factor = 1``. Step 4 is what makes them diverge.

Idempotent by upsert on ``(instrument_id, date)`` — docs/02 rule 3: "Re-running any day's job
produces identical rows."
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

import polars as pl
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import OhlcvDaily
from baskfy_providers.errors import CredentialsMissing, ProviderError
from baskfy_worker.steps import StepOutcome
from baskfy_worker.window import DateWindow

#: Batched so one instrument's fifteen-year history is not a single statement.
UPSERT_CHUNK: int = 2000


#: How many bar rows may be buffered before the step writes them out mid-fetch. Reached only by
#: a multi-year backfill window; a single session's pass buffers roughly one row per instrument
#: and flushes once at the end, holding row locks for seconds instead of the whole fetch.
_FLUSH_ROWS = 250_000


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

    bhavcopy_first, instruments = await _lead_with_bhavcopy(
        session, provider, outcome, window, instruments
    )
    written += bhavcopy_first

    # FETCH FIRST, WRITE AT THE END — SO THE CHAIN DOES NOT HOLD ROW LOCKS ACROSS THE NETWORK
    # (8 Sep 2026).
    #
    # This loop used to `await upsert_bars(...)` immediately after each `fetch(...)`. Every write
    # takes row locks that Postgres holds until the transaction commits, and the chain is
    # deliberately ONE transaction (M84.1, so a night is all-or-nothing). The fetch is throttled
    # to the bulk lane's 2 req/s (M85), so ~3,000 instruments is 25-45 minutes — and the old
    # shape took its first lock in the first second and held everything until the end.
    #
    # Measured on 8 Sep: the live swing scan sat blocked on `Lock/transactionid` for 44 minutes
    # trying to write `sw_setup_daily` while the chain crawled through the bars, and because the
    # worker runs `--concurrency=2` both slots were then occupied by tasks doing nothing. py-spy
    # put the chain's stack in `RedisCallSpacer.acquire` — sleeping for its slot, exactly as
    # designed, with the transaction open the whole time.
    #
    # Buffering moves every lock to the end of the step: the network phase now takes none, and
    # the writes run back-to-back in seconds just before the chain moves on. `_FLUSH_ROWS` bounds
    # the memory a long backfill window could otherwise accumulate; the daily chain never reaches
    # it, which is the case that shares the box with the live scan.
    pending: list[tuple[int, pl.DataFrame]] = []
    pending_rows = 0

    async def _flush() -> int:
        nonlocal pending, pending_rows
        rows = 0
        for pending_id, pending_frame in pending:
            rows += await upsert_bars(session, pending_id, pending_frame)
        pending = []
        pending_rows = 0
        return rows

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
        pending.append((instrument_id, frame))
        pending_rows += frame.height
        if pending_rows >= _FLUSH_ROWS:
            # Only a long backfill window gets here. A single session's ~3,000 one-row frames
            # stay buffered to the end, which is the whole point of the change.
            written += await _flush()

    written += await _flush()

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

    # M84: A PARTIAL KITE PASS IS THE CASE THE OLD FALLBACK COULD NOT SEE.
    #
    # The branch above asks for the bhavcopy only when Kite wrote *nothing*. A session that dies
    # halfway — a token that expires mid-run, a burst of 429s, an instrument list Kite has stopped
    # carrying — writes something, so it never reached the fallback and the gate judged a half
    # day. For one session that is not a fallback at all: the bhavcopy is the exchange's own
    # end-of-day record, it needs no credential, and it is one 200 KB file. So when Kite left any
    # gap at all, the day is completed from it.
    #
    # Only for a single-session window. A multi-year backfill walks the archive day by day and
    # belongs to `bhavcopy_backfill` on its own terms; running it as a "top-up" inside the chain
    # would turn one night into hours. Kite also stays FIRST, unchanged: it reaches back before
    # 2024, where the UDiFF archive begins.
    # Not when the bhavcopy already led: it has been read once for this session and re-reading
    # it would download the same file to write the same rows.
    if window.days == 1 and not bhavcopy_first and (failures or skipped_no_token):
        await _fetch_from_bhavcopy(
            session,
            provider,
            outcome,
            window,
            reason=(f"kite left gaps: {len(failures)} failed, {skipped_no_token} without a token"),
            top_up=written,
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


async def _lead_with_bhavcopy(
    session: AsyncSession,
    provider: object,
    outcome: StepOutcome,
    window: DateWindow,
    instruments: Sequence[tuple[int, str, int | None]],
) -> tuple[int, list[tuple[int, str, int | None]]]:
    """Take the session's bhavcopy first; return its rows and the names Kite must still fetch.

    THE BHAVCOPY GOES FIRST FOR A COMPLETED SESSION (8 Sep 2026).

    Maulik: "why are we waiting so much?" Because `historical_data` has no batch form — Kite
    serves ONE instrument per HTTP request, so ~3,000 instruments is ~3,000 requests, and at the
    bulk lane's 2 req/s that is ~25 minutes. It is what made the 7 Sep chain take 73 minutes.
    The quote endpoints do batch 500-1,000, but they answer "what is the price now", not "what
    was this day's candle".

    NSE's bhavcopy answers the whole question in ONE request. Measured for 7 Sep 2026: a 207 KB
    zip carrying 3,704 rows, 3,405 of them equity series — on its own above the quality gate's
    3,171 threshold that day. `_fetch_from_bhavcopy`'s own docstring has always called it "the
    *better* source for this window", because it carries `turnover` and both circuit bands
    natively where Kite does not; it was simply used last instead of first.

    Kite still matters: the finished 7 Sep held 4,477 bars against the bhavcopy's 3,405. So this
    does not replace the Kite pass, it shrinks it — roughly a thousand names instead of three
    thousand, for the same coverage.

    Only for a single completed session. A multi-year backfill walks the archive day by day and
    belongs to `bhavcopy_backfill` on its own terms; a window that includes today may have no
    file published yet. Both fall through with the instrument list untouched, so the Kite pass
    behaves exactly as it did before.
    """
    remaining = list(instruments)
    if window.days != 1:
        return 0, remaining
    try:
        rows = await _fetch_from_bhavcopy(
            session,
            provider,
            outcome,
            window,
            reason="the exchange's own record, one request for the session",
        )
    except Exception as exc:
        # No file yet, or NSE refused. Never fatal: Kite is about to do the whole pass anyway.
        outcome.note(bhavcopy_first_error=f"{type(exc).__name__}: {exc}")
        return 0, remaining
    if rows <= 0:
        return 0, remaining
    covered = await _instruments_with_bars(session, window.end)
    kept = [row for row in remaining if row[0] not in covered]
    outcome.note(
        bhavcopy_first=rows,
        kite_calls_saved=len(remaining) - len(kept),
        kite_calls_remaining=len(kept),
    )
    return rows, kept


async def _instruments_with_bars(session: AsyncSession, on: dt.date) -> set[int]:
    """Which instrument ids already hold a bar for ``on`` — including rows written but not yet
    committed by this transaction, which is the point: the bhavcopy has just landed them."""
    rows = await session.execute(
        select(OhlcvDaily.instrument_id).where(OhlcvDaily.date == on).distinct()
    )
    return {int(value) for value in rows.scalars()}


async def _fetch_from_bhavcopy(  # noqa: PLR0913 - a source, a window, and how to report it
    session: AsyncSession,
    provider: object,
    outcome: StepOutcome,
    window: DateWindow,
    *,
    reason: str,
    top_up: int | None = None,
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
    from baskfy_worker.bhavcopy_backfill import backfill_bars_from_bhavcopy  # noqa: PLC0415

    if not callable(getattr(provider, "bhavcopy", None)):
        outcome.note(reason=reason, fallback="unavailable: no provider offers bhavcopy")
        return 0

    # `session=` is load-bearing: this runs inside the chain's open transaction, and a
    # second connection here waits on the instrument rows step 1 has not committed while
    # the chain waits for this call. `_working_session` carries the incident.
    report = await backfill_bars_from_bhavcopy(provider, window, progress_every=0, session=session)
    if top_up is None:
        outcome.rows_out = report.bars_written
    else:
        # A top-up ran BESIDE a Kite pass that already wrote rows, and the two overlap by however
        # many symbols both sources carry. Neither a sum nor a replacement would be true, so
        # `rows_out` stays what Kite wrote and the second number is reported as its own fact.
        outcome.note(kite_rows=top_up, bhavcopy_rows=report.bars_written)
    outcome.note(
        **{"top_up" if top_up is not None else "fallback": "bhavcopy"},
        fallback_reason=reason,
        window=str(window),
        days_written=report.days_written,
        missing_days=[d.isoformat() for d in report.missing_days] or None,
        unmatched_symbol_count=len(report.unmatched_symbols) or None,
        # NOT `failures=` (M84.2). The Kite pass has already written its own per-symbol failures
        # under that key, and reusing it overwrites the only record of why Kite produced nothing.
        # That is not hypothetical: the 3 Sep re-run's step note was 289 KB of one bhavcopy
        # DBAPIError keyed by date, and the ~2,265 Kite reasons underneath it were gone — so
        # "why did Kite return zero rows" had to be reconstructed by probing the box afterwards.
        bhavcopy_failures=report.failures or None,
    )
    return report.bars_written
