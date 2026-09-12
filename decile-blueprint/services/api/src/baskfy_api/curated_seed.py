"""Idempotent seed rows for the curated-basket layer (SC1-SC2).

Managers are written by ``make seed`` via ``seed_reference``. The Momentum Scan basket
(``source=SCAN``) is seeded when enough instruments exist for the fixture ranking.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.problems import pipeline_degraded
from baskfy_core.curated_baskets import (
    MANAGER_SLUG_BASKFY_ENGINE,
    MANAGER_SLUG_MAULIK,
    SOLE_USER_ENV,
    assert_weights_sum_to_one,
)
from baskfy_core.models import (
    AppUser,
    CbBasket,
    CbBasketVersion,
    CbCollection,
    CbConstituent,
    CbManager,
    CbMetrics,
    Instrument,
)
from baskfy_core.scan_projection import (
    DEFAULT_SCAN_TOP_N,
    FIXTURE_SCAN_SYMBOLS,
    MOMENTUM_SCAN_BASKET_SLUG,
    project_scan_top_n,
)

log = logging.getLogger(__name__)

MANAGER_SEED_ROWS: tuple[dict[str, object], ...] = (
    {
        "slug": MANAGER_SLUG_BASKFY_ENGINE,
        "name": "Baskfy Engine",
        "kind": "ENGINE",
        # 0020 gave managers a lifecycle and backfilled the rows that already existed. A FRESH
        # seed skips that backfill and would take the column default (DRAFT), which cannot
        # publish -- so a rebuilt database would come up with both seed managers unable to list
        # the baskets they own. Stated here so seeding and migrating agree.
        "state": "APPROVED",
        "bio": "Automated momentum strategies from the nightly MomentumScan pipeline.",
        "strategies": ["momentum-scan"],
        "disclosures_md": None,
    },
    {
        "slug": MANAGER_SLUG_MAULIK,
        "name": "Maulik",
        "kind": "HUMAN",
        "state": "APPROVED",
        "bio": "Operator-curated baskets.",
        "strategies": [],
        "disclosures_md": None,
    },
)

#: Stable scan-run id for the fixture genesis cut (deterministic seed).
FIXTURE_SCAN_RUN_ID = "fixture-momentum-scan-sc2"


async def resolve_sole_user_id(session: AsyncSession) -> int:
    """Return the sole tenant's ``app_user.id``. **Reads only — never creates an account.**

    Prefers ``BASKFY_SOLE_USER_ID``; otherwise looks the seeded account up by public id.

    ## Why this no longer seeds

    It used to call ``seed_e2e_account`` when the variable was unset, and it was reachable from
    request handlers. So a plain ``GET /watchlist`` created an ``app_user`` whose password is a
    constant this repository publishes on purpose, with a pre-verified email and an active
    subscription — and, because the seed is an upsert, *reset that password on every call*. It
    also spent ~205 ms of Argon2id and wrote to ``app_user`` on a read.

    ``seed.py`` says it plainly: ``seed e2e`` "is not a command anything but a test database
    should ever be pointed at". Seeding belongs in ``make seed``. A request path may look the
    account up; it must not conjure it.

    ## Why a blank or mistyped value is "not set" rather than a crash

    `gates/sleeve-read-contract.md` C5/C6. This used to be ``int(os.environ.get(SOLE_USER_ENV))``
    with no strip and no empty check, so one env line — ``BASKFY_SOLE_USER_ID=`` or a typo — meant
    three different things across the product: the worker stripped it, read "no tenant", and every
    detector logged *skipped*; this function raised ``ValueError`` **out of a request handler**,
    which is a 500 rather than the 503 the "not configured" branch two lines below exists to give;
    and the desk raised at import. One typo, three failure modes, and only one of them said what
    was wrong.

    It now reads the variable exactly as `baskfy_worker.providers._sole_user_id` does — strip,
    empty is unset, a non-number is a warning and unset — so the writer and the reader answer the
    same question the same way. Unset then takes the path below, which ends in an operator-shaped
    503 naming the variable.
    """
    raw = (os.environ.get(SOLE_USER_ENV) or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            log.warning(
                "%s is not a number (%r); reading it as 'no sole tenant configured', which is "
                "what baskfy_worker.providers._sole_user_id does with the same value",
                SOLE_USER_ENV,
                raw,
            )
    # Local import breaks ``seed`` <-> ``curated_seed`` circular dependency.
    from baskfy_api.seed import E2E_PUBLIC_ID  # noqa: PLC0415

    found = (
        await session.execute(select(AppUser.id).where(AppUser.public_id == E2E_PUBLIC_ID))
    ).scalar_one_or_none()
    if found is None:
        raise pipeline_degraded(
            "The sole-tenant account is not configured on this deployment. Set "
            f"{SOLE_USER_ENV}, or run `make seed` to create the development account."
        )
    return found


async def seed_curated_managers(session: AsyncSession) -> int:
    """Upsert the two seed managers by slug (docs/smallcase/03)."""
    for row in MANAGER_SEED_ROWS:
        stmt = insert(CbManager).values(**row)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[CbManager.slug],
                set_={
                    "name": stmt.excluded.name,
                    "kind": stmt.excluded.kind,
                    "bio": stmt.excluded.bio,
                    "strategies": stmt.excluded.strategies,
                    "disclosures_md": stmt.excluded.disclosures_md,
                },
            )
        )
    return len(MANAGER_SEED_ROWS)


@dataclass(frozen=True, slots=True)
class CollectionSeed:
    """One editorial shelf, and the rule that fills it.

    A dataclass rather than a dict so the predicate fields are typed: with ``dict[str, object]``
    every ``row.get("categories")`` is an ``object`` that has to be cast back before use, and a
    cast is where a typo stops being a type error.
    """

    slug: str
    title: str
    subtitle: str
    position: int
    #: Match baskets carrying any of these categories. Empty means "do not filter on category".
    categories: tuple[str, ...] = ()
    #: Match baskets run by any of these manager slugs.
    managers: tuple[str, ...] = ()
    rebalance_frequency: str | None = None
    #: ``min_amount`` (cheapest first) or ``name``.
    ordering: str = "name"
    #: Keep only the first N after ordering. ``None`` means "every basket that matches".
    #:
    #: A shelf with no predicate and no cap is not a shelf — it is the catalogue under a second
    #: title, so it can never group anything and every other shelf hides inside it. ``start-here``
    #: was exactly that, which is why three shelves and the grid all drew the same baskets. A cap
    #: is what makes "the smallest cheque that still buys a whole basket" an editorial claim
    #: rather than a re-sort of everything.
    limit: int | None = None


#: The editorial shelves ``/baskets`` is browsed by. docs/smallcase/03 — smallcase's browse
#: experience is mostly collections, and ``cb_collection`` has held nothing since 0014.
#:
#: **Membership is a rule, not a hand-written list of slugs.** A curated shelf is editorial, so
#: the tempting shape is ``basket_slugs=("momentum-scan", ...)``. It was rejected: exactly one
#: basket exists today, so every hand-written shelf would seed empty and stay empty until somebody
#: remembered to edit this file — which is how ``cb_collection`` came to hold nothing for six
#: migrations. A predicate over what actually exists fills itself as baskets are added, and
#: re-running the seed refreshes membership rather than duplicating it.
#: How many of the cheapest baskets "Start here" holds. Small enough that the shelf is a
#: recommendation, large enough to fill a row of cards at every breakpoint.
START_HERE_LIMIT = 6

COLLECTION_SEED_ROWS: tuple[CollectionSeed, ...] = (
    CollectionSeed(
        slug="start-here",
        title="Start here",
        subtitle="The smallest cheque that still buys a whole basket.",
        position=10,
        ordering="min_amount",
        limit=START_HERE_LIMIT,
    ),
    CollectionSeed(
        slug="momentum",
        title="Momentum",
        subtitle="Baskets that hold what is already working, and sell what stops.",
        position=20,
        categories=("momentum",),
    ),
    CollectionSeed(
        slug="run-by-the-engine",
        title="Run by the engine",
        subtitle="Rebalanced by the nightly pipeline, not by a person's conviction.",
        position=30,
        managers=(MANAGER_SLUG_BASKFY_ENGINE,),
    ),
    CollectionSeed(
        slug="quarterly",
        title="Rebalanced quarterly",
        subtitle="Four decisions a year, not twelve.",
        position=40,
        rebalance_frequency="QUARTERLY",
    ),
)


async def _collection_member_ids(session: AsyncSession, shelf: CollectionSeed) -> list[int]:
    """Resolve one shelf's membership against the baskets that exist right now.

    Only baskets a browsing user could actually open are eligible — the same two conditions
    ``explore._visible()`` applies, spelled here rather than imported because a seeder importing a
    router would invert the dependency. If those two ever diverge, ``test_collections.py`` fails:
    it asserts a PRIVATE basket named by a collection is not returned by the API.

    A shelf whose predicate matches nothing is still created. An empty shelf is a true statement
    about the catalogue ("nothing here yet") and its own page renders it as one; a *missing* shelf
    would be a false statement about the product. Whether a browse page stacks that shelf or lists
    it as a tile is a rendering decision, taken in ``lib/collections/select.ts`` — not here, where
    hiding it would destroy the fact instead of presenting it.
    """
    stmt = select(CbBasket.id).where(
        CbBasket.archived_at.is_(None), CbBasket.visibility == "PUBLISHED"
    )
    if shelf.categories:
        stmt = stmt.where(CbBasket.categories.overlap(list(shelf.categories)))
    if shelf.managers:
        stmt = stmt.join(CbManager, CbManager.id == CbBasket.manager_id).where(
            CbManager.slug.in_(list(shelf.managers))
        )
    if shelf.rebalance_frequency is not None:
        stmt = stmt.where(CbBasket.rebalance_frequency == shelf.rebalance_frequency)

    if shelf.ordering == "min_amount":
        # Cheapest first, and a basket with no metrics row yet sorts last rather than vanishing:
        # "we do not know the minimum" is not the same as "there is no basket".
        #
        # Joined through *one* row per basket, deliberately. ``cb_metrics`` is a history — the
        # momentum-scan basket already carries two ``as_of_date`` rows — so a plain join fans the
        # basket out once per row and the shelf then names it twice. It was silent until this
        # shelf grew a ``limit``, at which point the duplicate also pushed a real basket off the
        # end. Latest metrics wins, which is the same row ``_collection_out`` puts on the card.
        latest = (
            select(
                CbMetrics.basket_id.label("basket_id"),
                func.max(CbMetrics.as_of_date).label("as_of_date"),
            )
            .group_by(CbMetrics.basket_id)
            .subquery()
        )
        stmt = (
            stmt.outerjoin(latest, latest.c.basket_id == CbBasket.id)
            .outerjoin(
                CbMetrics,
                (CbMetrics.basket_id == CbBasket.id)
                & (CbMetrics.as_of_date == latest.c.as_of_date),
            )
            .order_by(CbMetrics.min_amount.asc().nullslast(), CbBasket.name)
        )
    else:
        stmt = stmt.order_by(CbBasket.name)

    if shelf.limit is not None:
        stmt = stmt.limit(shelf.limit)

    return [int(value) for value in (await session.execute(stmt)).scalars().all()]


async def seed_curated_collections(session: AsyncSession) -> int:
    """Upsert the editorial shelves by slug, recomputing membership from current baskets.

    Idempotent (house rule 7): re-running produces identical rows, because membership is derived
    rather than appended and the upsert keys on ``slug``.
    """
    for shelf in COLLECTION_SEED_ROWS:
        basket_ids = await _collection_member_ids(session, shelf)
        stmt = insert(CbCollection).values(
            slug=shelf.slug,
            title=shelf.title,
            subtitle=shelf.subtitle,
            position=shelf.position,
            basket_ids=basket_ids,
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[CbCollection.slug],
                set_={
                    "title": stmt.excluded.title,
                    "subtitle": stmt.excluded.subtitle,
                    "position": stmt.excluded.position,
                    "basket_ids": stmt.excluded.basket_ids,
                },
            )
        )
    return len(COLLECTION_SEED_ROWS)


async def count_curated_collections(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(CbCollection))).scalar_one())


async def count_curated_managers(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(CbManager))).scalar_one())


async def seed_momentum_scan_basket(
    session: AsyncSession,
    *,
    ranked_symbols: tuple[str, ...] = FIXTURE_SCAN_SYMBOLS,
    top_n: int = DEFAULT_SCAN_TOP_N,
    as_of: dt.date | None = None,
    scan_run_id: str = FIXTURE_SCAN_RUN_ID,
) -> int:
    """Project a MomentumScan-like top-N into ``cb_basket(source=SCAN)`` + genesis version.

    Idempotent: re-running does not add a second version when the genesis row already exists.
    Returns 0 when fewer than ``top_n`` of the ranked symbols exist in ``instrument``.
    """
    await seed_curated_managers(session)
    projection = project_scan_top_n(ranked_symbols, top_n=top_n, scan_run_id=scan_run_id)
    assert_weights_sum_to_one(projection.weights)

    symbols = [c.symbol for c in projection.constituents]
    rows = (
        await session.execute(
            select(Instrument.symbol, Instrument.id).where(Instrument.symbol.in_(symbols))
        )
    ).all()
    by_symbol = {str(sym): int(iid) for sym, iid in rows}
    if len(by_symbol) < top_n:
        return 0

    manager_id = (
        await session.execute(
            select(CbManager.id).where(CbManager.slug == MANAGER_SLUG_BASKFY_ENGINE)
        )
    ).scalar_one()
    effective = as_of or dt.date(2024, 1, 2)

    basket_stmt = insert(CbBasket).values(
        slug=projection.slug,
        name=projection.name,
        manager_id=manager_id,
        type="STOCK",
        access="FREE",
        visibility="PUBLISHED",
        categories=["momentum"],
        description_md="Equal-weight top-N projection of the MomentumScan strategy.",
        rationale_md=None,
        rebalance_frequency="WEEKLY",
        benchmark_instrument_id=None,
        launched_at=effective,
        next_review_at=None,
        source="SCAN",
        scan_strategy_key=projection.strategy_key,
        archived_at=None,
    )
    await session.execute(
        basket_stmt.on_conflict_do_update(
            index_elements=[CbBasket.slug],
            set_={
                "name": basket_stmt.excluded.name,
                "description_md": basket_stmt.excluded.description_md,
                "categories": basket_stmt.excluded.categories,
                "rebalance_frequency": basket_stmt.excluded.rebalance_frequency,
                "scan_strategy_key": basket_stmt.excluded.scan_strategy_key,
            },
        )
    )
    basket_id = (
        await session.execute(select(CbBasket.id).where(CbBasket.slug == MOMENTUM_SCAN_BASKET_SLUG))
    ).scalar_one()

    existing = (
        await session.execute(
            select(CbBasketVersion.id).where(
                CbBasketVersion.basket_id == basket_id,
                CbBasketVersion.version_no == projection.version_no,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return 1

    version = CbBasketVersion(
        basket_id=basket_id,
        version_no=projection.version_no,
        effective_date=effective,
        label=projection.label,
        added_count=len(projection.constituents),
        removed_count=0,
        notes_md="SC2 fixture genesis cut.",
        source_scan_run_id=projection.source_scan_run_id,
    )
    session.add(version)
    await session.flush()
    for constituent in projection.constituents:
        session.add(
            CbConstituent(
                version_id=version.id,
                instrument_id=by_symbol[constituent.symbol],
                segment=constituent.segment,
                weight=constituent.weight,
            )
        )
    await session.flush()
    return 1
