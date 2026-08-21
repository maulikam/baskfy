"""Step 5 — ``refresh_index_membership`` (Prompt 4 deliverable 1).

docs/03: "NSE constituent files -> index_member_daily, PIT".

Point-in-time membership is what docs/04 calls "the anti-look-ahead spine", and docs/06 §2 is
blunt about why:

    "Never resolve a historical universe from today's membership — that is the single most common
     source of backtest look-ahead bias."

So each date gets its own rows and earlier dates are never rewritten. Resolving NIFTY 500 for
2019 must return the 2019 constituents, including the ones that have since been dropped.

Three ways a row gets here, recorded in ``source`` (docs/09 §Backfill)
---------------------------------------------------------------------
``nse_file``      read from a published constituent file for that exact date. Certain.
``derived``       resolved by rule: docs/06 §"Step 2" defines ``nifty-allcap`` as every EQ
                  instrument with a bar on the date and ``etf`` as every ETF. Certain, but not
                  from a file.
``reconstructed`` carried back from the earliest file we have, because NSE publishes none for
                  that date. docs/09: "where unavailable pre-2018, reconstruct from the earliest
                  available file and **record the reconstruction in `index_member_daily.source`
                  so backtests can exclude uncertain periods**".

The distinction is the whole point. A reconstructed 2013 NIFTY 500 is today's index projected
backwards — survivorship bias in its purest form — and a backtest that cannot see the difference
will report a number it has not earned.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import IndexMemberDaily, Instrument, OhlcvDaily
from baskfy_core.universes import UNIVERSES, Universe
from baskfy_providers.errors import ProviderError, UnexpectedPayload
from baskfy_worker.steps import StepOutcome

SOURCE_NSE_FILE = "nse_file"
SOURCE_RECONSTRUCTED = "reconstructed"
SOURCE_DERIVED = "derived"

#: docs/06 §"Step 2" — universes defined by rule rather than by a published file.
DERIVED_BY_RULE: frozenset[str] = frozenset({"nifty-allcap", "etf"})

#: docs/09 §Backfill: NSE constituent files are unavailable "pre-2018". Before this, membership
#: can only be reconstructed, and is marked as such.
EARLIEST_PUBLISHED_CONSTITUENTS: dt.date = dt.date(2018, 1, 1)


@dataclass(slots=True)
class MembershipResult:
    rows_written: int = 0
    per_universe: dict[str, int] = field(default_factory=dict)
    per_source: dict[str, int] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)


async def run_refresh_index_membership(
    session: AsyncSession,
    provider: object,
    outcome: StepOutcome,
    on: dt.date,
    *,
    allow_reconstruction: bool = True,
) -> int:
    """Write point-in-time membership for every selectable universe on ``on``."""
    result = await refresh_membership(
        session, provider, on, allow_reconstruction=allow_reconstruction
    )
    outcome.rows_in = len(UNIVERSES)
    outcome.rows_out = result.rows_written
    outcome.note(
        date=on.isoformat(),
        per_universe=result.per_universe,
        # Surfaced on every run: an operator should be able to see at a glance how much of a
        # backfilled date is observed and how much is projected backwards.
        per_source=result.per_source,
        failures=result.failures or None,
    )
    return result.rows_written


async def refresh_membership(
    session: AsyncSession,
    provider: object,
    on: dt.date,
    *,
    allow_reconstruction: bool = True,
) -> MembershipResult:
    ids = dict((await session.execute(select(Instrument.symbol, Instrument.id))).tuples().all())
    result = MembershipResult()

    for universe in UNIVERSES:
        try:
            symbols, source = await _members_for(
                session, provider, universe, on, allow_reconstruction=allow_reconstruction
            )
        except ProviderError as exc:
            result.failures[universe.slug] = str(exc)
            continue

        rows = [
            {
                "index_id": universe.index_id,
                "date": on,
                "instrument_id": ids[symbol],
                "source": source,
            }
            for symbol in symbols
            if symbol in ids
        ]
        result.per_universe[universe.slug] = len(rows)
        if not rows:
            continue

        stmt = insert(IndexMemberDaily).values(rows)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[
                    IndexMemberDaily.index_id,
                    IndexMemberDaily.date,
                    IndexMemberDaily.instrument_id,
                ],
                # A row already recorded as observed must never be downgraded to reconstructed by
                # a later backfill pass; a genuine file always wins.
                set_={"source": stmt.excluded.source},
                where=IndexMemberDaily.source != SOURCE_NSE_FILE,
            )
        )
        result.rows_written += len(rows)
        result.per_source[source] = result.per_source.get(source, 0) + len(rows)

    return result


async def _members_for(
    session: AsyncSession,
    provider: object,
    universe: Universe,
    on: dt.date,
    *,
    allow_reconstruction: bool,
) -> tuple[list[str], str]:
    """Resolve one universe's members for ``on``, and say where the answer came from."""
    if universe.slug in DERIVED_BY_RULE:
        return await _derived_members(session, universe, on), SOURCE_DERIVED

    symbols = _from_provider(provider, universe, on)
    if symbols:
        return symbols, SOURCE_NSE_FILE

    if not allow_reconstruction:
        return [], SOURCE_NSE_FILE

    reconstructed = await _reconstruct(session, universe, on)
    return reconstructed, SOURCE_RECONSTRUCTED


def _from_provider(provider: object, universe: Universe, on: dt.date) -> list[str]:
    fetch = getattr(provider, "index_constituents", None)
    if not callable(fetch):
        return []
    try:
        result = fetch(universe.slug, on)
    except UnexpectedPayload:
        # The provider recognises the slug but publishes no file for it (docs/06 §"Step 2"
        # derives some universes by rule). Not a failure of the run.
        return []
    return [str(s) for s in result] if isinstance(result, list) else []


async def _derived_members(session: AsyncSession, universe: Universe, on: dt.date) -> list[str]:
    """docs/06 §"Step 2": allcap is every EQ instrument with a bar on ``on``; etf every ETF.

    Requiring a bar is what keeps a suspended, delisted or not-yet-listed name out of the universe
    on that date — which is the same point-in-time discipline the file-backed universes get for
    free from the file itself.
    """
    instrument_type = "ETF" if universe.slug == "etf" else "EQ"
    rows = await session.execute(
        select(Instrument.symbol)
        .join(OhlcvDaily, OhlcvDaily.instrument_id == Instrument.id)
        .where(Instrument.instrument_type == instrument_type, OhlcvDaily.date == on)
        .distinct()
    )
    return [row[0] for row in rows]


async def _reconstruct(session: AsyncSession, universe: Universe, on: dt.date) -> list[str]:
    """Carry membership back from the earliest observed file (docs/09 §Backfill).

    Deliberately only ever looks *forward* in time for its source, and only at rows marked
    ``nse_file``. Reconstructing from another reconstruction would compound the uncertainty
    without recording that it had, and reconstructing from a *later* observed file is exactly the
    survivorship bias this is designed to make visible rather than to hide.
    """
    earliest = (
        await session.execute(
            select(IndexMemberDaily.date)
            .where(
                IndexMemberDaily.index_id == universe.index_id,
                IndexMemberDaily.source == SOURCE_NSE_FILE,
                IndexMemberDaily.date > on,
            )
            .order_by(IndexMemberDaily.date)
            .limit(1)
        )
    ).scalar_one_or_none()
    if earliest is None:
        return []

    rows = await session.execute(
        select(Instrument.symbol)
        .join(IndexMemberDaily, IndexMemberDaily.instrument_id == Instrument.id)
        .where(
            IndexMemberDaily.index_id == universe.index_id,
            IndexMemberDaily.date == earliest,
        )
    )
    return [row[0] for row in rows]


async def resolve_universe(session: AsyncSession, universe: Universe, on: dt.date) -> list[int]:
    """The point-in-time membership of ``universe`` on ``on`` — the screener's step 2 (docs/06).

    Reads only rows stamped with that exact date. It cannot fall back to "the latest membership"
    because that is precisely the look-ahead docs/06 §2 forbids, and a screen returning nothing
    for an un-ingested date is a visible failure rather than a silently wrong answer.
    """
    rows = await session.execute(
        select(IndexMemberDaily.instrument_id)
        .where(IndexMemberDaily.index_id == universe.index_id, IndexMemberDaily.date == on)
        .order_by(IndexMemberDaily.instrument_id)
    )
    return [row[0] for row in rows]


async def membership_sources(
    session: AsyncSession, universe: Universe, on: dt.date
) -> dict[str, int]:
    """How much of a date's membership is observed and how much reconstructed.

    docs/09 wants backtests able to "exclude uncertain periods"; this is the query that lets
    Prompt 15 do it.
    """
    rows = await session.execute(
        select(IndexMemberDaily.source, IndexMemberDaily.instrument_id).where(
            IndexMemberDaily.index_id == universe.index_id, IndexMemberDaily.date == on
        )
    )
    counts: dict[str, int] = {}
    for source, _instrument_id in rows.tuples():
        counts[source] = counts.get(source, 0) + 1
    return counts
