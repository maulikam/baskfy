"""One federated catalog search — the ⌘K palette's single query.

`baskfynavrefactorreport.md` §F11 ("Fragmented search") is the reason this module exists:

    "Header search is stock-only … the indices table has its own separate search; baskets and
     screens aren't searchable at all from the header. One global search should cover stocks,
     indices, baskets, and screens."

The palette used to fan out to nothing — it called ``GET /instruments?search=`` and filtered
``NAV_ITEMS`` in the browser. Four scoped calls per keystroke would have been the lazy fix: four
round trips, four abort controllers, four failure modes, and a ranking the client has to invent.
This is the other shape — one round trip, one ranking, one place where "who may see this" is
decided.

Three things are worth reading before changing anything here.

**Visibility is inherited, never re-invented.** Each kind's searcher applies exactly the predicate
its own list route already applies, and says so at the call site:

* instruments — public. ``GET /instruments?search=`` is already open to anonymous callers
  ("it names public securities"), and this reuses that module's ``search_instruments`` verbatim
  rather than writing a second ranking for the same table.
* indices — public. ``index_def`` is the list behind ``/indices/dashboard``, which needs no
  principal at all.
* baskets — authenticated, and only ``visibility = 'PUBLISHED'`` and not archived. That is
  ``routers.explore._visible()`` plus that router's ``principal.require_user()``. Search must not
  be the one door into the catalog that skips the lock.
* screens — the ``routers.screens`` rule: a screen with ``user_id IS NULL`` is an example everyone
  may read, and a screen with a ``user_id`` belongs to that user alone.

A caller who is not signed in therefore gets stocks, indices and example screens, which is exactly
what they can already reach by navigating. Nothing here widens an existing boundary.

**A hit carries identity, not a URL.** ``CatalogHit`` is ``kind`` + ``id`` + ``title`` +
``subtitle``. Where ``/basket/{slug}`` lives is the web app's business — Tree 6 moved half these
routes and would have moved them again with an API deploy attached if the href had been minted
here. ``apps/web/src/lib/search/hrefs.ts`` owns the mapping, and a vitest asserts every kind has
one. (``docs/DECISIONS-MERGE.md`` M46.1.)

**Ranking is per kind, and it is the same rule four times.** Exact match first, then prefix, then
contains, ties broken by title so the order is stable between identical requests. Nothing tries to
score a stock against a basket: the palette draws one group per kind, so a cross-kind ordering
would be a number nobody reads. What *is* cross-kind is the per-kind limit — ask for five and you
get at most five of each, so one prolific kind can never crowd the others out of the dialog.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy import Case, Select, case, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from baskfy_api.auth import Principal
from baskfy_api.instruments import search_instruments as search_instrument_rows
from baskfy_core.models import CbBasket, CbManager, IndexDef, Screen

#: The four things a catalog hit can be. The palette draws one group per kind, in this order.
CatalogKind = Literal["instrument", "index", "basket", "screen"]

#: Group order in the dialog: what you searched for is usually a stock, and "Go to" (which the web
#: app appends client-side from `NAV_ITEMS`) sits after all of them.
KIND_ORDER: Final[tuple[CatalogKind, ...]] = ("instrument", "index", "basket", "screen")

#: Hits per kind, not in total — see the module docstring. Five fills a palette without scrolling.
DEFAULT_LIMIT: Final = 5
MAX_LIMIT: Final = 20

#: Below this the query is not a query. One character is enough for a ticker ("M"), and the
#: instrument searcher already treats a blank needle as "no results" rather than "everything".
MIN_QUERY_LENGTH: Final = 1
MAX_QUERY_LENGTH: Final = 64

#: Rank buckets, lowest first. Someone typing "CUP" wants CUPID before "ACUPID SYSTEMS", and
#: someone typing "nifty 50" wants NIFTY 50 before NIFTY 500 — the same three-way rule either way.
_EXACT: Final = 0
_PREFIX: Final = 1
_CONTAINS: Final = 2


@dataclass(frozen=True, slots=True)
class CatalogHit:
    """One row in the palette, identified but not routed.

    ``id`` is the kind's stable public handle — the symbol for an instrument, the slug for an
    index or a basket, the ``public_id`` for a screen. It is what the web app turns into a href,
    and it is deliberately never a database id: those are not stable across a restore and are not
    the identifiers any of these resources are addressed by anywhere else in the API.
    """

    kind: CatalogKind
    id: str
    title: str
    subtitle: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogResults:
    """Every hit, grouped by kind in ``KIND_ORDER``, plus what was asked for.

    ``query`` travels back with the results because the palette debounces: a response is only
    worth rendering if it answers the prefix the user has finished typing, and echoing the query
    lets the client decide that without keeping a parallel map of in-flight requests.
    """

    query: str
    limit: int
    hits: tuple[CatalogHit, ...]

    def of(self, kind: CatalogKind) -> tuple[CatalogHit, ...]:
        """Just one kind's hits — the shape the tests assert against."""
        return tuple(hit for hit in self.hits if hit.kind == kind)


def _rank(column: InstrumentedAttribute[str], needle: str) -> Case[int]:
    """Exact, then prefix, then contains — as a sortable integer.

    One expression, applied to four different tables' name columns, because the rule genuinely is
    the same rule: `baskfy_api.instruments.search_instruments` had already written it out for
    symbols, and a second hand-rolled copy per kind is how ranking quietly diverges between them.
    """
    return case(
        (column == needle, _EXACT),
        (column.ilike(f"{needle}%"), _PREFIX),
        else_=_CONTAINS,
    )


def normalise(query: str) -> str:
    """The needle every searcher works from: trimmed, and never longer than the documented bound.

    The router declares `max_length`, so over-long input is refused at the boundary and this bound
    is never reached over HTTP. It is kept because this function is the service's entry point and
    a future caller — a worker, a test, a scoped palette — should not be able to hand four
    kilobytes of pasted text to four `ILIKE '%…%'` scans by skipping the router.
    """
    return query.strip()[:MAX_QUERY_LENGTH]


async def search_instruments(
    session: AsyncSession, needle: str, limit: int
) -> tuple[CatalogHit, ...]:
    """Stocks — `baskfy_api.instruments.search_instruments`, unchanged.

    Public: docs/07 puts the typeahead outside auth because "it names public securities". Delisted
    and inactive instruments are already excluded there, which is the right rule here too — a
    palette is for navigating to something, and there is nowhere useful to navigate.
    """
    rows = await search_instrument_rows(session, needle, limit)
    return tuple(
        CatalogHit(kind="instrument", id=row.symbol, title=row.symbol, subtitle=row.name)
        for row in rows
    )


async def search_indices(session: AsyncSession, needle: str, limit: int) -> tuple[CatalogHit, ...]:
    """Indices — `index_def`, the same ~145 rows `/indices/dashboard` draws.

    Public, for the same reason the dashboard is. Matching is on name *and* slug because the two
    diverge by punctuation ("NIFTY MIDCAP 150" vs `nifty-midcap-150`) and a person types either.
    """
    if not needle:
        return ()
    pattern = f"%{needle}%"
    rank = _rank(IndexDef.name, needle)
    statement: Select[tuple[IndexDef, int]] = (
        select(IndexDef, rank.label("rank"))
        .where(IndexDef.name.ilike(pattern) | IndexDef.slug.ilike(pattern))
        # `is_universe` first at equal rank: `sort_order` is 1..14 for the docs/01 §6 universes
        # and 0 for every dashboard-only row registered later, so ordering on it alone put
        # "NIFTY50 PR 1x Inverse" above "NIFTY 50". A universe is the row a person means.
        .order_by(
            rank.asc(), IndexDef.is_universe.desc(), IndexDef.sort_order.asc(), IndexDef.name.asc()
        )
        .limit(limit)
    )
    rows = (await session.execute(statement)).all()
    return tuple(
        CatalogHit(
            kind="index",
            id=row[0].slug,
            title=row[0].name,
            # docs/01 §6's selector calls the twelve breadth indices "universes"; the rest are
            # dashboard-only. The distinction decides which page the hit is worth opening, so it
            # travels with the hit rather than being re-derived from a list in the browser.
            subtitle="Universe" if row[0].is_universe else "Index",
        )
        for row in rows
    )


async def search_baskets(
    session: AsyncSession, needle: str, limit: int, principal: Principal
) -> tuple[CatalogHit, ...]:
    """Curated baskets — `routers.explore`'s predicate, including its `require_user()`.

    An anonymous caller gets an empty tuple rather than a 401: the palette is a mixed surface, and
    failing the whole search because one of its four kinds is behind a login would make ⌘K useless
    on the marketing pages. "Nothing to show you here" is the honest answer, and it is the same
    answer `/explore` gives that caller after the redirect to sign-in.
    """
    if not needle or principal.user_id is None:
        return ()
    pattern = f"%{needle}%"
    rank = _rank(CbBasket.name, needle)
    statement: Select[tuple[CbBasket, CbManager, int]] = (
        select(CbBasket, CbManager, rank.label("rank"))
        .join(CbManager, CbManager.id == CbBasket.manager_id)
        # `routers.explore._visible()`, spelled out rather than imported to avoid a router →
        # service import cycle. The pair must stay in step; `test_api_search.py` asserts an
        # archived and a PRIVATE basket are both absent, which is what would catch a drift.
        .where(CbBasket.archived_at.is_(None), CbBasket.visibility == "PUBLISHED")
        .where(CbBasket.name.ilike(pattern) | CbBasket.slug.ilike(pattern))
        .order_by(rank.asc(), CbBasket.name.asc())
        .limit(limit)
    )
    rows = (await session.execute(statement)).all()
    return tuple(
        CatalogHit(kind="basket", id=row[0].slug, title=row[0].name, subtitle=row[1].name)
        for row in rows
    )


async def search_screens(
    session: AsyncSession, needle: str, limit: int, principal: Principal
) -> tuple[CatalogHit, ...]:
    """Saved screens — `routers.screens`' visibility rule, verbatim.

    Examples (``user_id IS NULL``) for everyone, own screens for the signed-in user, and nothing
    else. Examples sort first when the rank ties, matching `GET /screens`' own ordering: a stranger
    to the product is looking for the template, not for their own copy of it.
    """
    if not needle:
        return ()
    visible = (
        Screen.user_id.is_(None)
        if principal.user_id is None
        else (Screen.user_id.is_(None) | (Screen.user_id == principal.user_id))
    )
    rank = _rank(Screen.name, needle)
    statement: Select[tuple[Screen, int]] = (
        select(Screen, rank.label("rank"))
        .where(visible, Screen.name.ilike(f"%{needle}%"))
        .order_by(rank.asc(), Screen.is_example.desc(), Screen.name.asc())
        .limit(limit)
    )
    rows = (await session.execute(statement)).all()
    return tuple(
        CatalogHit(
            kind="screen",
            id=row[0].public_id,
            title=row[0].name,
            subtitle="Example" if row[0].is_example else None,
        )
        for row in rows
    )


async def search_catalog(
    session: AsyncSession, query: str, limit: int, principal: Principal
) -> CatalogResults:
    """Every kind the caller may see, in `KIND_ORDER`, at most ``limit`` of each.

    Sequential rather than `asyncio.gather`: one `AsyncSession` is not safe to use concurrently,
    and four statements against indexed columns with a `LIMIT 5` are not what a palette's latency
    budget is spent on. The measured cost is dominated by the round trip, which is the thing this
    endpoint exists to have exactly one of.

    No `kinds` parameter. A scoped palette ("just baskets") is the obvious next request and the
    obvious place to add one — but an argument nothing passes is a branch nothing tests, and the
    first draft of this function had exactly that.
    """
    needle = normalise(query)
    if len(needle) < MIN_QUERY_LENGTH:
        return CatalogResults(query=needle, limit=limit, hits=())

    return CatalogResults(
        query=needle,
        limit=limit,
        hits=(
            *await search_instruments(session, needle, limit),
            *await search_indices(session, needle, limit),
            *await search_baskets(session, needle, limit, principal),
            *await search_screens(session, needle, limit, principal),
        ),
    )
