"""The market-data surfaces — docs/01 §6 and §7, docs/07 §"Market data surfaces" (Prompt 11).

Three reads, all of them over tables the nightly pipeline writes and none of them recomputing
anything:

* ``index_snapshot_daily`` powers the dashboard (docs/04's own comment says so).
* ``market_health_daily`` powers the breadth gauges. docs/01 §6: "Because history starts
  1 Nov 2024, these are stored as a daily snapshot table, **not recomputed**."
* ``instrument`` powers the listings register — docs/04 has no ``listing`` table, and
  ``baskfy_worker.tasks.listings`` explains why there should not be one: a listing *is* an
  instrument.

**Snapping backwards, not requiring an exact row.** ``resolve_as_of`` hands us a published trading
day, but neither snapshot table is guaranteed to have a row for it — the index file can be late,
and breadth is only as old as the factor engine's first run. Both readers take the newest row on
or before the as-of date and report the date they actually found, so a missing file shows
yesterday's dashboard with yesterday's date rather than an empty page.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import (
    IndexDef,
    IndexSnapshotDaily,
    Instrument,
    MarketHealthDaily,
)
from baskfy_core.universes import UNIVERSE_BY_SLUG, Universe

#: docs/08 §Dashboard: "a sparkline per index (30-day)".
SPARKLINE_DAYS: Final = 30

#: How far back to look for those thirty *trading* days. Thirty trading days is 42-46 calendar
#: days on the NSE calendar; ninety is roughly double that, which absorbs a Diwali-length holiday
#: run and a pipeline that missed a week without ever shortening a sparkline. It exists to bound
#: the scan, not to define the series — :data:`SPARKLINE_DAYS` still does that.
SPARKLINE_WINDOW: Final = dt.timedelta(days=SPARKLINE_DAYS * 3)

#: How far back to look for the newest snapshot on or before the as-of date. A dashboard that
#: silently showed a six-week-old level would be worse than one that showed nothing, so the search
#: is bounded and the date it found is always reported.
MAX_SNAPSHOT_STALENESS: Final = dt.timedelta(days=14)

#: docs/01 §6's four gauges, in the order the reference product renders them, with its wording.
GAUGES: Final[tuple[tuple[str, str], ...]] = (
    ("pct_above_200dma", "Above 200 DMA"),
    ("pct_above_50dma", "Above 50 DMA"),
    ("pct_within_10pct_ath", "Within 10% of ATH"),
    ("pct_ret_1y_positive", "1Y Return > 0%"),
)

#: Sorts before every real listing date, so a row with no ``listed_on`` keyset-paginates like any
#: other instead of needing a second code path. `NULLS LAST` in the ORDER BY and a sentinel in the
#: cursor comparison would be two rules that can disagree; this is one rule used by both.
NO_LISTING_DATE: Final = dt.date(1, 1, 1)


class UnknownUniverse(LookupError):
    """A universe slug that is not one of the fourteen."""


class InvalidCursor(ValueError):
    """A cursor that did not come from us."""


@dataclass(frozen=True, slots=True)
class IndexRow:
    slug: str
    name: str
    is_universe: bool
    level: Decimal | None
    change_abs: Decimal | None
    change_pct: Decimal | None
    pe: Decimal | None
    pb: Decimal | None
    div_yield: Decimal | None
    sparkline: tuple[Decimal, ...] = ()


@dataclass(frozen=True, slots=True)
class Dashboard:
    as_of: dt.date
    rows: Sequence[IndexRow]


@dataclass(frozen=True, slots=True)
class Gauge:
    key: str
    label: str
    value: Decimal | None


@dataclass(frozen=True, slots=True)
class MarketHealth:
    as_of: dt.date
    universe: Universe
    gauges: Sequence[Gauge]
    constituent_count: int | None
    data_available_from: dt.date | None


@dataclass(frozen=True, slots=True)
class HealthPoint:
    date: dt.date
    pct_above_200dma: Decimal | None
    pct_above_50dma: Decimal | None
    pct_within_10pct_ath: Decimal | None
    pct_ret_1y_positive: Decimal | None
    constituent_count: int | None
    index_level: Decimal | None


@dataclass(frozen=True, slots=True)
class ListingPage:
    rows: Sequence[Instrument]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class ListingQuery:
    """docs/07's `?from&to&series=&cursor=`, plus Prompt 11's search box, as one value.

    A record rather than six keyword arguments: the four filters travel together from the query
    string to the WHERE clause, and passing them as a unit is what keeps the reader and the router
    agreeing about which of them are optional.
    """

    limit: int
    cursor: str | None = None
    series: str | None = None
    search: str | None = None
    start: dt.date | None = None
    end: dt.date | None = None


def universe_or_raise(slug: str) -> Universe:
    universe = UNIVERSE_BY_SLUG.get(slug)
    if universe is None:
        raise UnknownUniverse(slug)
    return universe


# ---------------------------------------------------------------------------
# /indices/dashboard
# ---------------------------------------------------------------------------


async def index_dashboard(session: AsyncSession, as_of: dt.date) -> Dashboard:
    """docs/01 §7 — every index we have a snapshot for, newest row on or before ``as_of``.

    ``DISTINCT ON`` rather than a correlated subquery per index: one pass over the date range,
    ordered so the first row per index is the newest one, which is exactly what PostgreSQL's
    ``DISTINCT ON`` keeps. 145 rows come back in one statement.

    The row order is the API's default — docs/01 §7: "Sorted by % change descending" — and the
    table re-sorts client-side from there without asking again (Prompt 11 acceptance criterion 2).
    """
    floor = as_of - MAX_SNAPSHOT_STALENESS
    snapshots = (
        select(
            IndexSnapshotDaily.index_id,
            IndexSnapshotDaily.date,
            IndexSnapshotDaily.level,
            IndexSnapshotDaily.change_abs,
            IndexSnapshotDaily.change_pct,
            IndexSnapshotDaily.pe,
            IndexSnapshotDaily.pb,
            IndexSnapshotDaily.div_yield,
        )
        .where(IndexSnapshotDaily.date <= as_of, IndexSnapshotDaily.date >= floor)
        .distinct(IndexSnapshotDaily.index_id)
        .order_by(IndexSnapshotDaily.index_id, IndexSnapshotDaily.date.desc())
        .subquery()
    )

    statement = (
        select(
            IndexDef.id,
            IndexDef.slug,
            IndexDef.name,
            IndexDef.is_universe,
            snapshots.c.date,
            snapshots.c.level,
            snapshots.c.change_abs,
            snapshots.c.change_pct,
            snapshots.c.pe,
            snapshots.c.pb,
            snapshots.c.div_yield,
        )
        .join(snapshots, snapshots.c.index_id == IndexDef.id)
        .order_by(
            snapshots.c.change_pct.desc().nullslast(),
            IndexDef.sort_order,
            IndexDef.name,
        )
    )
    # Named access, not positional: this row has eleven columns and an off-by-one would put a
    # dividend yield in the P/E column silently.
    rows = (await session.execute(statement)).mappings().all()
    series = await _sparklines(session, as_of)

    latest = max((row["date"] for row in rows), default=as_of)
    return Dashboard(
        as_of=latest,
        rows=[
            IndexRow(
                slug=row["slug"],
                name=row["name"],
                is_universe=row["is_universe"],
                level=row["level"],
                change_abs=row["change_abs"],
                change_pct=row["change_pct"],
                pe=row["pe"],
                pb=row["pb"],
                div_yield=row["div_yield"],
                sparkline=series.get(row["id"], ()),
            )
            for row in rows
        ],
    )


async def _sparklines(session: AsyncSession, as_of: dt.date) -> dict[int, tuple[Decimal, ...]]:
    """The last ``SPARKLINE_DAYS`` levels per index, oldest first.

    Served with the dashboard rather than from a per-index endpoint: 145 sparklines is 145 requests
    otherwise, and the whole payload is one number per index per day. docs/11 §Performance budgets
    is about the page, not about the row.
    """
    ranked = (
        select(
            IndexSnapshotDaily.index_id,
            IndexSnapshotDaily.date,
            IndexSnapshotDaily.level,
            func.row_number()
            .over(
                partition_by=IndexSnapshotDaily.index_id,
                order_by=IndexSnapshotDaily.date.desc(),
            )
            .label("recency"),
        )
        .where(
            IndexSnapshotDaily.date <= as_of,
            # Prompt 16 deliverable 2. Without a floor this window function sorts *every*
            # snapshot row ever written — 145 indices x 15 years — to keep the newest 30 per
            # index, and the cost grows with the archive rather than with the answer. The floor
            # bounds it to the chunks that can contain the answer.
            IndexSnapshotDaily.date >= as_of - SPARKLINE_WINDOW,
            IndexSnapshotDaily.level.is_not(None),
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(ranked.c.index_id, ranked.c.level)
            .where(ranked.c.recency <= SPARKLINE_DAYS)
            .order_by(ranked.c.index_id, ranked.c.date)
        )
    ).all()

    series: dict[int, list[Decimal]] = {}
    for index_id, level in rows:
        series.setdefault(index_id, []).append(level)
    return {index_id: tuple(values) for index_id, values in series.items()}


# ---------------------------------------------------------------------------
# /market-health
# ---------------------------------------------------------------------------


async def market_health(session: AsyncSession, slug: str, as_of: dt.date) -> MarketHealth:
    """docs/01 §6 — the four gauges for one universe on one date.

    Read, never recomputed: docs/01 §6 says the snapshot table exists precisely so that these are
    not derived on the fly, and ``baskfy_worker.tasks.market_health`` is what writes it.
    """
    universe = universe_or_raise(slug)
    row = (
        await session.execute(
            select(MarketHealthDaily)
            .where(
                MarketHealthDaily.index_id == universe.index_id,
                MarketHealthDaily.date <= as_of,
            )
            .order_by(MarketHealthDaily.date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    earliest = (
        await session.execute(
            select(func.min(MarketHealthDaily.date)).where(
                MarketHealthDaily.index_id == universe.index_id
            )
        )
    ).scalar_one()

    return MarketHealth(
        as_of=row.date if row is not None else as_of,
        universe=universe,
        gauges=[
            Gauge(key=key, label=label, value=getattr(row, key) if row is not None else None)
            for key, label in GAUGES
        ],
        constituent_count=row.constituent_count if row is not None else None,
        data_available_from=earliest,
    )


async def market_health_history(
    session: AsyncSession, slug: str, start: dt.date, end: dt.date
) -> list[HealthPoint]:
    """docs/07: "the four series for charting", with the universe's own index level beside them.

    docs/08 §"Market Health" asks for "a history chart of each breadth series with the Nifty
    overlaid". The overlay is the *selected* universe's level, not always NIFTY 50: comparing
    NIFTY MICROCAP 250's breadth against the large-cap index would be the wrong denominator, and
    every universe in the selector is itself an index with a snapshot row.

    A LEFT JOIN, because breadth and the index file are written by different steps and either can
    be missing for a day without the other being wrong.
    """
    universe = universe_or_raise(slug)
    statement = (
        select(MarketHealthDaily, IndexSnapshotDaily.level)
        .outerjoin(
            IndexSnapshotDaily,
            (IndexSnapshotDaily.index_id == MarketHealthDaily.index_id)
            & (IndexSnapshotDaily.date == MarketHealthDaily.date),
        )
        .where(
            MarketHealthDaily.index_id == universe.index_id,
            MarketHealthDaily.date >= start,
            MarketHealthDaily.date <= end,
        )
        .order_by(MarketHealthDaily.date)
    )
    return [
        HealthPoint(
            date=row.date,
            pct_above_200dma=row.pct_above_200dma,
            pct_above_50dma=row.pct_above_50dma,
            pct_within_10pct_ath=row.pct_within_10pct_ath,
            pct_ret_1y_positive=row.pct_ret_1y_positive,
            constituent_count=row.constituent_count,
            index_level=level,
        )
        for row, level in (await session.execute(statement)).all()
    ]


# ---------------------------------------------------------------------------
# /listings
# ---------------------------------------------------------------------------


def encode_cursor(listed_on: dt.date | None, symbol: str) -> str:
    """An opaque cursor naming the *last row of this page*, not an offset.

    Keyset, because an offset repeats and skips rows as the register grows between pages — and the
    register grows every night. The pair is the full sort key, which is what makes the page
    boundary exact even though hundreds of instruments share a listing date.
    """
    raw = f"{(listed_on or NO_LISTING_DATE).isoformat()}|{symbol}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[dt.date, str]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        date_text, _, symbol = raw.partition("|")
        return dt.date.fromisoformat(date_text), symbol
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursor(cursor) from exc


async def listings(session: AsyncSession, query: ListingQuery) -> ListingPage:
    """docs/07: `GET /listings?from&to&series=&cursor=` — "NSE listings, newest first".

    Newest first means ``listed_on DESC``, and ties are broken by symbol so the order is total.
    Without that tie-break the order between two instruments listed the same day is whatever the
    planner chose last, and a cursor into the middle of a listing date would skip and repeat rows
    — which is Prompt 11's third acceptance criterion, and not a theoretical concern: NSE lists
    dozens of instruments on the same day.
    """
    statement = listings_statement(query)
    rows = (await session.execute(statement)).scalars().all()
    page = list(rows[: query.limit])
    has_more = len(rows) > query.limit
    return ListingPage(
        rows=page,
        next_cursor=(
            encode_cursor(page[-1].listed_on, page[-1].symbol) if has_more and page else None
        ),
    )


def listings_statement(query: ListingQuery) -> Select[tuple[Instrument]]:
    """The register's statement, on its own so ``EXPLAIN`` can be run against the real thing.

    Prompt 16 deliverable 2 asks for ``EXPLAIN ANALYZE`` on "every hot query"; a plan taken from a
    hand-written approximation would tune a statement we do not issue. ``baskfy_api.query_plans``
    calls this.
    """
    sort_date = func.coalesce(Instrument.listed_on, NO_LISTING_DATE)
    statement: Select[tuple[Instrument]] = select(Instrument).where(Instrument.is_active.is_(True))

    if query.series:
        statement = statement.where(Instrument.series == query.series.upper())
    if query.search:
        needle = f"%{query.search.strip()}%"
        statement = statement.where(
            or_(Instrument.symbol.ilike(needle), Instrument.name.ilike(needle))
        )
    if query.start is not None:
        statement = statement.where(Instrument.listed_on >= query.start)
    if query.end is not None:
        statement = statement.where(Instrument.listed_on <= query.end)
    if query.cursor is not None:
        after_date, after_symbol = decode_cursor(query.cursor)
        # Row-value comparison against the *whole* sort key. Written the same way round as the
        # ORDER BY below so the two cannot drift apart.
        statement = statement.where(
            or_(
                sort_date < after_date,
                (sort_date == after_date) & (Instrument.symbol > after_symbol),
            )
        )

    # `+ 1` so the caller learns whether another page exists without a second COUNT.
    return statement.order_by(sort_date.desc(), Instrument.symbol.asc()).limit(query.limit + 1)


async def listing_series(session: AsyncSession) -> list[str]:
    """The distinct series present, so the filter offers what exists rather than a guessed list."""
    rows = (
        await session.execute(
            select(Instrument.series)
            .where(Instrument.is_active.is_(True), Instrument.series.is_not(None))
            .distinct()
            .order_by(Instrument.series)
        )
    ).scalars()
    return [row for row in rows if row]
