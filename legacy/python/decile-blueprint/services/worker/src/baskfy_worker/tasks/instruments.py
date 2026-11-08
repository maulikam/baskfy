"""Step 1 — ``refresh_instruments`` (docs/03: "Kite instruments dump + NSE series/listing files").

Kite carries the tradable universe and its provider tokens; NSE's listings register carries the
series code, ISIN and listing date that Kite does not publish (docs/02 §"Why Kite ... and why it
is not sufficient alone"). Both are merged onto ``instrument``, keyed by symbol.

Rows are never deleted (docs/04): an instrument that has left the exchange is marked with
``delisted_on`` and stays, because point-in-time screens over a delisted name are the difference
between an honest backtest and a survivorship-biased one (docs/01 §10).

**"Keyed by symbol" is now true of the code as well as of this sentence.** Until 0039 the upsert
keyed on ``(exchange_id, symbol, series)``, so a stock moving between EQ and BE grew a second row
and both stayed active — see the comment on ``mutable`` below, and 0039 for the 120 symbols that
had to be merged because of it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import Instrument, OhlcvDaily
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_providers.errors import ProviderError
from baskfy_providers.ports import Capability
from baskfy_providers.records import InstrumentRecord, ListingRecord
from baskfy_worker.steps import StepOutcome

#: Series codes the screener works with (docs/01 §2.9). Others are ingested but never screened.
SCREENABLE_SERIES: tuple[str, ...] = ("EQ", "BE")

#: PostgreSQL's wire protocol allows at most 32,767 bind parameters in one statement, and a
#: single-statement upsert of the whole instrument master exceeds it.
#:
#: This was invisible until 22 Aug 2026, because the step had only ever run against the
#: 40-instrument fixture. The first run against the real Kite dump — which carries every NSE
#: instrument, not the ~2,300 the screener ends up keeping — failed with
#:
#:     asyncpg.exceptions._base.InterfaceError:
#:     the number of query arguments cannot exceed 32767
#:
#: Eleven columns per row, so 2,048 rows is 22,528 parameters: comfortably under the ceiling with
#: room for a column to be added without silently reintroducing the bug.
#: PostgreSQL's hard ceiling, not a tunable.
MAX_BIND_PARAMETERS: int = 32767
#: One per key in the `values` dict below. `test_instruments.py` asserts the two stay in step, so
#: adding a column cannot quietly walk the batch back over the ceiling.
UPSERT_COLUMNS_PER_ROW: int = 11
UPSERT_BATCH_ROWS: int = 2048


def _batched[T](rows: list[T], size: int) -> Iterator[list[T]]:
    """Successive slices of ``rows``, at most ``size`` long. Empty input yields nothing.

    Generic over the row type rather than over ``dict[str, object]``: the values dict below is
    inferred as ``dict[str, str | int | Decimal | date | None]``, and widening it at the call site
    would need either a cast or a suppression. House rule 3 allows neither.
    """
    if size < 1:
        raise ValueError(f"batch size must be at least 1; got {size}")
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


async def run_refresh_instruments(
    session: AsyncSession,
    provider: object,
    outcome: StepOutcome,
    *,
    as_of: dt.date | None = None,
) -> int:
    """Upsert the instrument master. Returns the number of rows written."""
    records = _list_instruments(provider, outcome)
    listings = _listings(provider, outcome)
    outcome.rows_in = len(records) + len(listings)
    if not records and not listings:
        outcome.note(reason="no provider could serve instruments or listings")
        return 0

    merged = _merge(records, listings)
    values = [
        {
            "exchange_id": NSE_EXCHANGE_ID,
            "symbol": record.symbol,
            "name": record.name,
            "series": record.series,
            "instrument_type": record.instrument_type,
            "isin": record.isin,
            "kite_token": record.kite_token,
            "lot_size": record.lot_size,
            "face_value": record.face_value,
            "listed_on": record.listed_on,
            "is_active": True,
        }
        for record in merged
    ]
    for batch in _batched(values, UPSERT_BATCH_ROWS):
        stmt = insert(Instrument).values(batch)
        # `series` IS MUTABLE, and 0039 is the migration that had to clean up believing otherwise.
        #
        # It used to sit in the conflict key instead, which made it part of the instrument's
        # identity. NSE moves stocks between EQ and BE whenever trade-to-trade surveillance turns
        # on or off, and every such move inserted a SECOND row: the key `(NSE, GAYAPROJ, 'EQ')`
        # matched nothing, and the old BE row — absent from the dump, so never updated — kept
        # `is_active = true` for good. 120 symbols were in that state, and `resolve_symbols`
        # answers AMBIGUOUS for any of them, which the holdings sync reports to the user as
        # "symbol not recognised". Maulik hit it on GAYAPROJ, 11 Sep 2026.
        mutable = (
            "name",
            "series",
            "instrument_type",
            "isin",
            "kite_token",
            "lot_size",
            "face_value",
            "listed_on",
            "is_active",
        )
        # The DO UPDATE fires only when a column actually differs. Without the WHERE, a nightly
        # re-run would rewrite `updated_at` on all ~2,300 rows even though nothing changed —
        # which breaks docs/02 rule 3 ("re-running any day's job produces identical rows") and
        # turns `updated_at` from a signal into noise.
        changed = or_(
            *[
                getattr(Instrument, column).is_distinct_from(stmt.excluded[column])
                for column in mutable
            ]
        )
        await session.execute(
            stmt.on_conflict_do_update(
                # 0039: `(exchange_id, symbol)`. One symbol on one exchange is one instrument,
                # whatever series it happens to trade in today.
                index_elements=[Instrument.exchange_id, Instrument.symbol],
                set_={
                    **{column: stmt.excluded[column] for column in mutable},
                    "updated_at": dt.datetime.now(tz=dt.UTC),
                },
                where=changed,
            )
        )

    outcome.rows_out = len(values)
    outcome.note(
        from_kite=len(records), from_listings=len(listings), merged=len(values), as_of=str(as_of)
    )
    return len(values)


async def active_instruments(session: AsyncSession) -> list[tuple[int, str, int | None]]:
    """``(id, symbol, kite_token)`` for every instrument worth asking Kite about tonight.

    ``delisted_on IS NULL`` rather than ``is_active`` because the former is the fact docs/04
    records and the latter is a cache of it.

    **Why this is not simply "everything still listed" (M78).** It was, and on 1 Sep 2026 that
    meant 41,443 instruments with a `kite_token`, one historical call each at Kite's 3 req/s —
    **3.8 hours** for a single night. Measured against the same database that evening: 10,130 of
    them have ever produced a bar and **31,603 have never produced one, ever**. Kite's NSE dump
    carries every contract on the exchange, not the cash names Baskfy screens, so roughly three of
    every four calls asked about something that has never returned a row and never will.

    That was not merely slow. It made the night longer than Celery's Redis `visibility_timeout`,
    so the broker redelivered the nightly and a second copy ran against the same tables; and it
    held the chain's single transaction open for hours, during which any user write touching
    `instrument` — a holdings sync, a CSV import, creating a portfolio — blocked on the foreign
    key. Both were reported as separate defects before this cause was found.

    So: **a cash-market name, or anything with history.** ``series`` is what the NSE register sets
    and Kite's dump does not — it is exactly the 3,191 rows that carry EQ/BE/SM/ST/SZ/BZ — so a
    newly listed equity is fetched from its first night, with no bars to its name. Anything that
    has ever returned a bar is in permanently, which covers the 6,459 seriesless instruments that
    do trade and keeps a delisted-then-relisted name in place.

    **Two earlier versions of this were wrong, and measuring caught both.** Keying the exception on
    `created_at` matched 41,721 of 41,733, because the table was repopulated between 20 and 31 Aug
    2026 and every row looked new. Keying it on a recent `listed_on` excluded any instrument with
    an old listing date and no bars yet — which on a fresh database is *every* instrument, so the
    pipeline could never have fetched its first bar at all.

    Verified on staging before shipping: selects 10,142 instruments, and the number that produced
    a bar on 31 Aug and would now be skipped is **zero**.
    """
    traded = (
        select(OhlcvDaily.instrument_id).where(OhlcvDaily.instrument_id == Instrument.id).exists()
    )
    rows = await session.execute(
        select(Instrument.id, Instrument.symbol, Instrument.kite_token)
        .where(
            Instrument.delisted_on.is_(None),
            or_(
                # A cash-market name. Set by the NSE register, absent on Kite's dump rows.
                Instrument.series.is_not(None),
                # Or anything with history, whatever its series.
                traded,
            ),
        )
        .order_by(Instrument.symbol)
    )
    return [(row[0], row[1], row[2]) for row in rows]


def _list_instruments(provider: object, outcome: StepOutcome) -> list[InstrumentRecord]:
    lister = getattr(provider, "list_instruments", None)
    if not callable(lister):
        return []
    try:
        result = lister()
    except ProviderError as exc:
        # Recorded, not swallowed: the step continues on whatever the other source gave us, and
        # the operator sees which half of the merge was missing.
        outcome.note(list_instruments_error=str(exc))
        return []
    return list(result) if isinstance(result, list) else []


def _listings(provider: object, outcome: StepOutcome) -> list[ListingRecord]:
    lister = getattr(provider, "listings", None)
    if not callable(lister):
        return []
    try:
        result = lister()
    except ProviderError as exc:
        outcome.note(listings_error=str(exc))
        return []
    return list(result) if isinstance(result, list) else []


def _merge(
    records: list[InstrumentRecord], listings: list[ListingRecord]
) -> list[InstrumentRecord]:
    """Kite supplies identity and the provider token; NSE supplies series, ISIN and listing date.

    Symbols NSE lists but Kite does not carry still enter the universe — they appear on
    ``/listings`` (docs/01 §1) and can be screened once bars exist.
    """
    by_symbol = {r.symbol: r for r in records}
    for listing in listings:
        existing = by_symbol.get(listing.symbol)
        if existing is None:
            by_symbol[listing.symbol] = InstrumentRecord(
                symbol=listing.symbol,
                name=listing.name,
                instrument_type="EQ",
                series=listing.series,
                isin=listing.isin,
                listed_on=listing.listed_on,
                face_value=listing.face_value,
            )
            continue
        by_symbol[listing.symbol] = existing.model_copy(
            update={
                "series": listing.series or existing.series,
                "isin": listing.isin or existing.isin,
                "listed_on": listing.listed_on or existing.listed_on,
                "face_value": listing.face_value or existing.face_value,
            }
        )
    return sorted(by_symbol.values(), key=lambda r: r.symbol)


CAPABILITIES = (Capability.LIST_INSTRUMENTS, Capability.LISTINGS)
