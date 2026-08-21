"""Step 6 — ``refresh_index_snapshots`` (Prompt 4 deliverable 2).

    "~145 indices with level, absolute change, % change, PE, PB, div yield into
     index_snapshot_daily. Handle indices that publish no fundamentals (store NULL, render as
     '-')."

docs/01 §7 describes the surface: "~145 index rows, each: name, % change, level, absolute change,
PE, PB, Div Yield. Sorted by % change descending. Includes derived indices with no fundamentals
(`Nifty50 PR 1x Inverse`, `India VIX`) → PE/PB/DivYield shown as `-`."

Where the ~145 come from
------------------------
The bundle names exactly two of them. Hardcoding a list of 145 index names would be inventing
data — and it would go stale the first time NSE adds an index. So ``index_def`` is *populated from
what NSE publishes*: any index in the snapshot file that we have no row for is registered on the
spot as a dashboard-only index (``is_universe = false``), with an id allocated from
``FIRST_NON_UNIVERSE_INDEX_ID`` upward.

That keeps the 14 selectable universes hand-pinned (they carry ``factor_daily`` mask bits, so
their ids are load-bearing — see ``baskfy_core.universes``) while the dashboard grows to whatever
the exchange actually publishes.

NULL, not zero
--------------
An index with no fundamentals stores NULL. Zero would render as "0.00" on the dashboard and read
as a P/E of zero — a claim about the index, rather than the absence of one. docs/01 §7 says the
reference product renders these as `-`, which is what NULL becomes in the UI.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import IndexDef, IndexSnapshotDaily
from baskfy_core.universes import FIRST_NON_UNIVERSE_INDEX_ID, UNIVERSES, slugify_index
from baskfy_providers.errors import ProviderError
from baskfy_providers.records import IndexSnapshot
from baskfy_worker.steps import StepOutcome

#: smallint tops out here; docs/01 §7's ~145 indices sit comfortably inside it.
MAX_INDEX_ID: int = 32767


@dataclass(slots=True)
class SnapshotResult:
    rows_written: int = 0
    registered: list[str] = field(default_factory=list)
    unnamed: int = 0


async def run_refresh_index_snapshots(
    session: AsyncSession, provider: object, outcome: StepOutcome, on: dt.date
) -> int:
    fetch = getattr(provider, "index_snapshots", None)
    if not callable(fetch):
        outcome.note(reason="no provider offers index_snapshots")
        return 0

    try:
        snapshots = fetch(on)
    except ProviderError as exc:
        outcome.note(error=str(exc))
        raise

    result = await store_snapshots(session, on, list(snapshots))
    outcome.rows_in = len(snapshots)
    outcome.rows_out = result.rows_written
    outcome.note(
        date=on.isoformat(),
        # New indices are an event worth seeing: the dashboard grew, and nobody asked it to.
        newly_registered=result.registered or None,
        unnamed_rows=result.unnamed or None,
    )
    return result.rows_written


async def store_snapshots(
    session: AsyncSession, on: dt.date, snapshots: list[IndexSnapshot]
) -> SnapshotResult:
    """Upsert one row per published index, registering any we have not seen before."""
    result = SnapshotResult()
    if not snapshots:
        return result

    known = dict((await session.execute(select(IndexDef.slug, IndexDef.id))).tuples().all())
    next_id = await _next_dashboard_id(session)

    values = []
    for snapshot in snapshots:
        slug = (
            snapshot.index_slug
            if snapshot.index_slug in known
            else slugify_index(snapshot.index_slug)
        )
        if not slug:
            result.unnamed += 1
            continue
        index_id = known.get(slug)
        if index_id is None:
            index_id = next_id
            next_id += 1
            await _register_dashboard_index(session, index_id, slug, snapshot.index_slug)
            known[slug] = index_id
            result.registered.append(slug)
        values.append(
            {
                "index_id": index_id,
                "date": on,
                "level": snapshot.level,
                "change_abs": snapshot.change_abs,
                "change_pct": snapshot.change_pct,
                # NULL where the index publishes no fundamentals (docs/01 §7).
                "pe": snapshot.pe,
                "pb": snapshot.pb,
                "div_yield": snapshot.div_yield,
            }
        )

    if not values:
        return result

    stmt = insert(IndexSnapshotDaily).values(values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[IndexSnapshotDaily.index_id, IndexSnapshotDaily.date],
            set_={
                "level": stmt.excluded.level,
                "change_abs": stmt.excluded.change_abs,
                "change_pct": stmt.excluded.change_pct,
                "pe": stmt.excluded.pe,
                "pb": stmt.excluded.pb,
                "div_yield": stmt.excluded.div_yield,
            },
        )
    )
    result.rows_written = len(values)
    return result


async def _next_dashboard_id(session: AsyncSession) -> int:
    """The next free id at or above ``FIRST_NON_UNIVERSE_INDEX_ID``.

    Dashboard indices are kept out of the 1..31 range because ``factor_daily.universe_mask`` sets
    one bit per ``index_def.id`` and a PostgreSQL ``integer`` has 31 usable bits
    (``baskfy_core.universes``). A dashboard index that stole a bit would silently change which
    universe a stock appears to belong to.
    """
    highest = (
        await session.execute(
            select(func.max(IndexDef.id)).where(IndexDef.id >= FIRST_NON_UNIVERSE_INDEX_ID)
        )
    ).scalar_one_or_none()
    candidate = FIRST_NON_UNIVERSE_INDEX_ID if highest is None else int(highest) + 1
    if candidate > MAX_INDEX_ID:
        raise ValueError(
            f"no index_def id available below {MAX_INDEX_ID}; index_def has outgrown smallint"
        )
    return candidate


async def _register_dashboard_index(
    session: AsyncSession, index_id: int, slug: str, name: str
) -> None:
    stmt = insert(IndexDef).values(
        id=index_id,
        slug=slug,
        name=name.strip(),
        # Not selectable in the screener: docs/01 §2.1 fixes that list at 14, and only those
        # carry mask bits.
        is_universe=False,
        sort_order=index_id,
    )
    await session.execute(stmt.on_conflict_do_nothing(index_elements=[IndexDef.id]))


async def dashboard_rows(session: AsyncSession, on: dt.date) -> list[dict[str, object]]:
    """docs/01 §7: the dashboard, sorted by % change descending.

    Ordering lives here rather than in the API so the "sorted by %chg desc" contract has one
    implementation. NULL fundamentals travel as NULL; rendering them as `-` is the UI's job.
    """
    rows = await session.execute(
        select(
            IndexDef.slug,
            IndexDef.name,
            IndexSnapshotDaily.level,
            IndexSnapshotDaily.change_abs,
            IndexSnapshotDaily.change_pct,
            IndexSnapshotDaily.pe,
            IndexSnapshotDaily.pb,
            IndexSnapshotDaily.div_yield,
        )
        .join(IndexDef, IndexDef.id == IndexSnapshotDaily.index_id)
        .where(IndexSnapshotDaily.date == on)
        .order_by(IndexSnapshotDaily.change_pct.desc().nullslast(), IndexDef.name)
    )
    return [
        {
            "slug": row[0],
            "name": row[1],
            "level": row[2],
            "change_abs": row[3],
            "change_pct": row[4],
            "pe": row[5],
            "pb": row[6],
            "div_yield": row[7],
        }
        for row in rows.tuples()
    ]


async def index_count(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(IndexDef))).scalar_one())


UNIVERSE_SLUGS_PINNED: frozenset[str] = frozenset(u.slug for u in UNIVERSES)
