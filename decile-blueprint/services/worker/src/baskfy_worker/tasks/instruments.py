"""Step 1 — ``refresh_instruments`` (docs/03: "Kite instruments dump + NSE series/listing files").

Kite carries the tradable universe and its provider tokens; NSE's listings register carries the
series code, ISIN and listing date that Kite does not publish (docs/02 §"Why Kite ... and why it
is not sufficient alone"). Both are merged onto ``instrument``, keyed by symbol.

Rows are never deleted (docs/04): an instrument that has left the exchange is marked with
``delisted_on`` and stays, because point-in-time screens over a delisted name are the difference
between an honest backtest and a survivorship-biased one (docs/01 §10).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import Instrument
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
        mutable = (
            "name",
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
                index_elements=[Instrument.exchange_id, Instrument.symbol, Instrument.series],
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
    """``(id, symbol, kite_token)`` for every instrument still listed.

    ``delisted_on IS NULL`` rather than ``is_active`` because the former is the fact docs/04
    records and the latter is a cache of it.
    """
    rows = await session.execute(
        select(Instrument.id, Instrument.symbol, Instrument.kite_token)
        .where(Instrument.delisted_on.is_(None))
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
