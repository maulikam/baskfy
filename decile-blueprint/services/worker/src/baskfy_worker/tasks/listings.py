"""``refresh_listings`` (Prompt 4 deliverable 4).

    "NSE listing dates and series for the full universe (~3,500 rows), upserted."

docs/01 §1 puts the surface at ``/listings`` with 3,524 rows; docs/07 serves it as
``GET /listings?from&to&series=&cursor=``, newest first.

There is no ``listing`` table in docs/04, and there should not be: a listing *is* an instrument,
and the fields the register carries — listed_on, series, ISIN, face value, market lot — are all
columns docs/04 already puts on ``instrument``. So this upserts onto ``instrument`` and the
listings page is a query over it.

Its relationship to docs/03 step 1: ``refresh_instruments`` merges the Kite dump with the NSE
register in one pass, which is the nightly path. This task is the register on its own, for the
backfill and for a targeted re-read when NSE republishes the file — which it does whenever a
company relists or changes series.

Never deletes. docs/04: "delisted_on date -- NULL = active; NEVER delete rows", because
point-in-time screens over a delisted name are what keep a backtest free of survivorship bias
(docs/01 §10).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import Instrument
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_providers.errors import ProviderError
from baskfy_providers.records import ListingRecord
from baskfy_worker.steps import StepOutcome

#: Columns the NSE register is authoritative for. Kite supplies identity and the provider token;
#: it does not publish any of these.
REGISTER_COLUMNS: tuple[str, ...] = ("name", "series", "isin", "listed_on", "face_value")


@dataclass(slots=True)
class ListingsResult:
    rows_in: int = 0
    rows_written: int = 0
    new_symbols: list[str] = field(default_factory=list)


async def run_refresh_listings(
    session: AsyncSession, provider: object, outcome: StepOutcome
) -> int:
    fetch = getattr(provider, "listings", None)
    if not callable(fetch):
        outcome.note(reason="no provider offers listings")
        return 0

    try:
        records = list(fetch())
    except ProviderError as exc:
        outcome.note(error=str(exc))
        raise

    # The Emerge (SME) register is a second file, and it is additive: every symbol in it is one
    # the main register does not carry. A failure to read it must not cost us the main-board
    # refresh, so it is recorded and carried rather than raised — but it IS recorded, because a
    # silently short SME universe looks exactly like a quiet day on Emerge.
    sme_error: str | None = None
    sme_count = 0
    fetch_sme = getattr(provider, "sme_listings", None)
    if callable(fetch_sme):
        try:
            sme = list(fetch_sme())
        except ProviderError as exc:
            sme_error = str(exc)
        else:
            sme_count = len(sme)
            records.extend(sme)
    else:
        sme_error = "no provider offers sme_listings"

    result = await store_listings(session, records)
    outcome.rows_in = result.rows_in
    outcome.rows_out = result.rows_written
    outcome.note(
        # A symbol appearing for the first time is a new NSE listing — the thing /listings exists
        # to show. Worth surfacing rather than silently absorbing.
        new_symbols=result.new_symbols[:50] or None,
        new_symbol_count=len(result.new_symbols),
        sme_rows=sme_count,
        sme_error=sme_error,
    )
    return result.rows_written


async def store_listings(session: AsyncSession, records: list[ListingRecord]) -> ListingsResult:
    result = ListingsResult(rows_in=len(records))
    if not records:
        return result

    existing = {
        row[0]
        for row in await session.execute(
            select(Instrument.symbol).where(Instrument.symbol.in_([r.symbol for r in records]))
        )
    }
    result.new_symbols = sorted({r.symbol for r in records} - existing)

    values = [
        {
            "exchange_id": NSE_EXCHANGE_ID,
            "symbol": record.symbol,
            "name": record.name,
            "series": record.series,
            # The register lists cash-market securities; ETFs are distinguished by the Kite dump,
            # so an instrument already typed as an ETF keeps that type (see the upsert below).
            "instrument_type": "EQ",
            "isin": record.isin,
            "listed_on": record.listed_on,
            "face_value": record.face_value,
            "is_active": True,
        }
        for record in records
    ]

    stmt = insert(Instrument).values(values)
    changed = or_(
        *[
            getattr(Instrument, column).is_distinct_from(stmt.excluded[column])
            for column in REGISTER_COLUMNS
        ]
    )
    await session.execute(
        stmt.on_conflict_do_update(
            # 0039: `(exchange_id, symbol)`. `series` is already one of REGISTER_COLUMNS above —
            # this upsert always meant to UPDATE it — but while it was also in the conflict key a
            # stock moving between EQ and BE could never be matched, so the register inserted a
            # second row instead of correcting the first. See 0039 for the 120 that resulted.
            index_elements=[Instrument.exchange_id, Instrument.symbol],
            # `instrument_type` is deliberately absent: the register cannot tell an ETF from an
            # equity, and overwriting a Kite-derived ETF classification with 'EQ' would move it
            # out of the `etf` universe (docs/06 §"Step 2").
            set_={
                **{column: stmt.excluded[column] for column in REGISTER_COLUMNS},
                "updated_at": dt.datetime.now(tz=dt.UTC),
            },
            # Only touch a row that actually differs, so a nightly re-read of an unchanged
            # register is a no-op (docs/02 rule 3).
            where=changed,
        )
    )
    result.rows_written = len(values)
    return result


async def listings_page(
    session: AsyncSession,
    *,
    listed_from: dt.date | None = None,
    listed_to: dt.date | None = None,
    series: list[str] | None = None,
    limit: int = 100,
) -> list[Instrument]:
    """docs/01 §1: "/listings — All NSE listed securities by listing date, paginated 100/page"."""
    query = select(Instrument).where(Instrument.listed_on.is_not(None))
    if listed_from is not None:
        query = query.where(Instrument.listed_on >= listed_from)
    if listed_to is not None:
        query = query.where(Instrument.listed_on <= listed_to)
    if series:
        query = query.where(Instrument.series.in_(series))
    rows = await session.execute(
        query.order_by(Instrument.listed_on.desc(), Instrument.symbol).limit(limit)
    )
    return list(rows.scalars())


async def listed_count(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(Instrument)
                .where(Instrument.listed_on.is_not(None))
            )
        ).scalar_one()
    )
