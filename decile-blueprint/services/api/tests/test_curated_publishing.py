"""SC3 contracts for atomic curated-version publishing and bounded timelines."""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import api_helpers
import httpx
import pytest
import pytest_asyncio
import screener_helpers
from sqlalchemy import event, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from baskfy_api.app import create_app
from baskfy_api.curated_baskets import CuratedBasketRepository
from baskfy_api.curated_publishing import (
    ForeignInvestmentError,
    PublishingConflictError,
    PublishingNotFoundError,
    PublishingSourceError,
    get_version_timeline,
    publish_manual_version,
    publish_scan_version,
)
from baskfy_api.db import get_session
from baskfy_api.routers.curated_publishing import router as publishing_router
from baskfy_api.seed import CURATED_SCAN_SCREEN_PUBLIC_ID, _curated_scan_definition
from baskfy_core.curated_baskets import ConstituentDraft, VersionDraft
from baskfy_core.models import (
    AppUser,
    BasketConstituent,
    BasketInvestment,
    BasketPendingAction,
    BasketUpdatePost,
    BasketUserRebalanceState,
    BasketVersion,
    CuratedBasket,
    CuratedManager,
    Exchange,
    Instrument,
    Screen,
    ScreenRun,
)

pytestmark = [pytest.mark.db, screener_helpers.requires_db]

API_SOURCE = Path(__file__).resolve().parents[1] / "src" / "baskfy_api"
TODAY = dt.date(2026, 8, 23)


def _constituent(instrument_id: int, weight: str, segment: str = "Equity") -> ConstituentDraft:
    return ConstituentDraft(
        instrument_id=instrument_id,
        segment=segment,
        weight=Decimal(weight),
    )


def _draft(
    on: dt.date,
    *constituents: ConstituentDraft,
    notes_md: str | None = None,
) -> VersionDraft:
    return VersionDraft(
        effective_date=on,
        constituents=constituents,
        notes_md=notes_md,
    )


async def _operator(session: AsyncSession) -> AppUser:
    return (
        await session.execute(
            select(AppUser).where(AppUser.public_id == "baskfy-sole-user")
        )
    ).scalar_one()


async def _manager(session: AsyncSession, slug: str = "maulik") -> CuratedManager:
    return (
        await session.execute(select(CuratedManager).where(CuratedManager.slug == slug))
    ).scalar_one()


async def _basket(
    session: AsyncSession,
    suffix: str,
    *,
    source: str = "MANUAL",
    scan_strategy_key: str | None = None,
) -> CuratedBasket:
    manager = await _manager(session, "baskfy-engine" if source == "SCAN" else "maulik")
    basket = CuratedBasket(
        slug=f"sc3-{suffix}",
        name=f"SC3 {suffix}",
        manager_id=manager.id,
        type="STOCK",
        access="FREE",
        visibility="PUBLISHED",
        categories=["test"],
        description_md="Fixture basket",
        rationale_md="Fixture rationale",
        rebalance_frequency="WEEKLY",
        benchmark_instrument_id=None,
        launched_at=dt.date(2026, 1, 1),
        next_review_at=dt.date(2026, 9, 4),
        source=source,
        scan_strategy_key=scan_strategy_key,
        archived_at=None,
    )
    session.add(basket)
    await session.flush()
    return basket


async def _instrument(session: AsyncSession, suffix: str) -> Instrument:
    instrument_id = await screener_helpers.add_instrument(session, f"SC3{suffix}"[:20])
    return await session.get_one(Instrument, instrument_id)


async def _genesis(
    session: AsyncSession,
    basket: CuratedBasket,
    instrument: Instrument,
    on: dt.date = dt.date(2026, 8, 1),
) -> BasketVersion:
    return await CuratedBasketRepository(session).create_version(
        basket.id,
        _draft(on, _constituent(instrument.id, "1.0000"), notes_md="genesis"),
    )


async def _investment(
    session: AsyncSession,
    user: AppUser,
    basket: CuratedBasket,
    version: BasketVersion,
) -> BasketInvestment:
    investment = BasketInvestment(
        user_id=user.id,
        basket_id=basket.id,
        status="ACTIVE",
        version_applied_id=version.id,
        exited_at=None,
        last_invested_at=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
    )
    session.add(investment)
    await session.flush()
    return investment


async def _reserved_scan(
    session: AsyncSession,
    user: AppUser,
    instruments: list[Instrument],
    *,
    as_of: dt.date = dt.date(2026, 8, 8),
    results: list[dict[str, object]] | None = None,
    owner_id: int | None = None,
    public_id: str = CURATED_SCAN_SCREEN_PUBLIC_ID,
) -> ScreenRun:
    definition = _curated_scan_definition()
    screen = Screen(
        public_id=f"{public_id}-{uuid.uuid4().hex[:8]}",
        user_id=user.id if owner_id is None else owner_id,
        name="SC3 reserved source",
        definition=definition.model_dump(mode="json", by_alias=True),
        columns=["symbol"],
        is_example=False,
    )
    # The production reserved source has a fixed public id.  A suffix keeps independently rolled
    # back fixtures unique; tests that need the valid identity restore the exact value after flush.
    session.add(screen)
    await session.flush()
    if public_id == CURATED_SCAN_SCREEN_PUBLIC_ID:
        screen.public_id = CURATED_SCAN_SCREEN_PUBLIC_ID
        await session.flush()
    rows = results or [
        {"rank": rank, "instrument_id": instrument.id, "factor_value": None}
        for rank, instrument in enumerate(instruments, start=1)
    ]
    run = ScreenRun(
        screen_id=screen.id,
        as_of=as_of,
        definition_hash=definition.definition_hash(),
        result_count=len(rows),
        results=rows,
    )
    session.add(run)
    await session.flush()
    return run


async def _version_counts(session: AsyncSession, basket_id: int) -> tuple[int, int, int]:
    version_ids = select(BasketVersion.id).where(BasketVersion.basket_id == basket_id)
    versions = await session.scalar(
        select(func.count()).select_from(BasketVersion).where(BasketVersion.basket_id == basket_id)
    )
    constituents = await session.scalar(
        select(func.count())
        .select_from(BasketConstituent)
        .where(BasketConstituent.version_id.in_(version_ids))
    )
    posts = await session.scalar(
        select(func.count())
        .select_from(BasketUpdatePost)
        .where(BasketUpdatePost.basket_id == basket_id)
    )
    return int(versions or 0), int(constituents or 0), int(posts or 0)


@pytest.mark.asyncio
async def test_manual_publish_appends_validated_immutable_version_and_exact_replay(
    screener_session: AsyncSession,
) -> None:
    operator = await _operator(screener_session)
    basket = await _basket(screener_session, "manual")
    first = await _instrument(screener_session, "MAN1")
    second = await _instrument(screener_session, "MAN2")
    draft = _draft(
        dt.date(2026, 8, 7),
        _constituent(first.id, "0.6000", "Largecap"),
        _constituent(second.id, "0.4000", "Midcap"),
        notes_md="Hand-authored change",
    )

    created = await publish_manual_version(screener_session, operator.id, basket.slug, draft)
    replay = await publish_manual_version(screener_session, operator.id, basket.slug, draft)

    assert created.model_dump(exclude={"idempotent_replay"}) == replay.model_dump(
        exclude={"idempotent_replay"}
    )
    assert created.idempotent_replay is False
    assert replay.idempotent_replay is True
    assert (created.version_no, created.label, created.added_count, created.removed_count) == (
        1,
        "GENESIS",
        2,
        0,
    )
    assert await _version_counts(screener_session, basket.id) == (1, 2, 1)


@pytest.mark.asyncio
async def test_manual_publish_refuses_scan_basket_and_scan_provenance(
    screener_session: AsyncSession,
) -> None:
    operator = await _operator(screener_session)
    instrument = await _instrument(screener_session, "MANSRC")
    scan_basket = await _basket(
        screener_session,
        "manual-on-scan",
        source="SCAN",
        scan_strategy_key="MomentumScan",
    )
    with pytest.raises(PublishingSourceError, match="MANUAL baskets"):
        await publish_manual_version(
            screener_session,
            operator.id,
            scan_basket.slug,
            _draft(TODAY, _constituent(instrument.id, "1.0000")),
        )

    manual_basket = await _basket(screener_session, "manual-provenance")
    contaminated = VersionDraft(
        effective_date=TODAY,
        constituents=(_constituent(instrument.id, "1.0000"),),
        source_scan_run_id=123,
    )
    with pytest.raises(PublishingSourceError, match="cannot carry scan"):
        await publish_manual_version(
            screener_session, operator.id, manual_basket.slug, contaminated
        )
    assert await _version_counts(screener_session, manual_basket.id) == (0, 0, 0)


@pytest.mark.asyncio
async def test_manual_idempotent_replay_refuses_conflicting_content_or_corrupt_evidence(
    screener_session: AsyncSession,
) -> None:
    operator = await _operator(screener_session)
    basket = await _basket(screener_session, "manual-conflict")
    first = await _instrument(screener_session, "CON1")
    second = await _instrument(screener_session, "CON2")
    original = _draft(TODAY, _constituent(first.id, "1.0000"), notes_md="exact")
    await publish_manual_version(screener_session, operator.id, basket.slug, original)

    with pytest.raises(PublishingConflictError, match="different immutable facts"):
        await publish_manual_version(
            screener_session,
            operator.id,
            basket.slug,
            _draft(TODAY, _constituent(second.id, "1.0000"), notes_md="exact"),
        )

    post = (
        await screener_session.execute(
            select(BasketUpdatePost).where(BasketUpdatePost.basket_id == basket.id)
        )
    ).scalar_one()
    post.body_md = "tampered"
    await screener_session.flush()
    with pytest.raises(PublishingConflictError, match="update-post evidence"):
        await publish_manual_version(screener_session, operator.id, basket.slug, original)


@pytest.mark.asyncio
async def test_publish_side_effect_is_one_per_user_despite_multiple_active_investments(
    screener_session: AsyncSession,
) -> None:
    operator = await _operator(screener_session)
    basket = await _basket(screener_session, "side-effect")
    first = await _instrument(screener_session, "SIDE1")
    second = await _instrument(screener_session, "SIDE2")
    genesis = await _genesis(screener_session, basket, first)
    await _investment(screener_session, operator, basket, genesis)
    await _investment(screener_session, operator, basket, genesis)

    result = await publish_manual_version(
        screener_session,
        operator.id,
        basket.slug,
        _draft(dt.date(2026, 8, 8), _constituent(second.id, "1.0000"), notes_md="rotate"),
    )
    posts = (
        (
            await screener_session.execute(
                select(BasketUpdatePost).where(BasketUpdatePost.basket_id == basket.id)
            )
        )
        .scalars()
        .all()
    )
    actions = (
        (
            await screener_session.execute(
                select(BasketPendingAction).where(
                    BasketPendingAction.user_id == operator.id,
                    BasketPendingAction.type == "REBALANCE_AVAILABLE",
                )
            )
        )
        .scalars()
        .all()
    )
    states = (
        (
            await screener_session.execute(
                select(BasketUserRebalanceState).where(
                    BasketUserRebalanceState.version_id == result.version_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(posts) == len(actions) == len(states) == 1
    assert actions[0].payload == {
        "basket_id": basket.id,
        "basket_slug": basket.slug,
        "version_id": result.version_id,
        "version_no": 2,
        "effective_date": "2026-08-08",
        "label": "CHANGED",
        "added_count": 1,
        "removed_count": 1,
    }
    assert (states[0].user_id, states[0].state, states[0].decided_at) == (
        operator.id,
        "PENDING",
        None,
    )


@pytest.mark.asyncio
async def test_new_publish_supersedes_unresolved_pending_action_without_faking_skip(
    screener_session: AsyncSession,
) -> None:
    operator = await _operator(screener_session)
    basket = await _basket(screener_session, "pending-supersede")
    first = await _instrument(screener_session, "PEND1")
    second = await _instrument(screener_session, "PEND2")
    genesis = await _genesis(screener_session, basket, first)
    await _investment(screener_session, operator, basket, genesis)
    v2 = await publish_manual_version(
        screener_session,
        operator.id,
        basket.slug,
        _draft(dt.date(2026, 8, 8), _constituent(second.id, "1.0000")),
    )
    v3 = await publish_manual_version(
        screener_session,
        operator.id,
        basket.slug,
        _draft(dt.date(2026, 8, 15), _constituent(first.id, "1.0000")),
    )

    actions = (
        (
            await screener_session.execute(
                select(BasketPendingAction)
                .where(BasketPendingAction.type == "REBALANCE_AVAILABLE")
                .order_by(BasketPendingAction.id)
            )
        )
        .scalars()
        .all()
    )
    states = {
        row.version_id: row.state
        for row in (
            (
                await screener_session.execute(
                    select(BasketUserRebalanceState).where(
                        BasketUserRebalanceState.version_id.in_([v2.version_id, v3.version_id])
                    )
                )
            )
            .scalars()
            .all()
        )
    }
    assert [row.resolved_at is None for row in actions] == [False, True]
    assert states == {v2.version_id: "PENDING", v3.version_id: "PENDING"}


@pytest.mark.asyncio
async def test_foreign_active_investment_forbidden_and_publish_rolls_back_savepoint(
    screener_session: AsyncSession,
) -> None:
    operator = await _operator(screener_session)
    basket = await _basket(screener_session, "foreign-forbidden")
    first = await _instrument(screener_session, "FOREIGN1")
    second = await _instrument(screener_session, "FOREIGN2")
    genesis = await _genesis(screener_session, basket, first)
    foreign = AppUser(public_id="foreign-user", email="foreign@users.invalid", name="Foreign")
    screener_session.add(foreign)
    await screener_session.flush()
    await _investment(screener_session, foreign, basket, genesis)

    savepoint = await screener_session.begin_nested()
    with pytest.raises(ForeignInvestmentError, match="Track C"):
        await publish_manual_version(
            screener_session,
            operator.id,
            basket.slug,
            _draft(dt.date(2026, 8, 8), _constituent(second.id, "1.0000")),
        )
    await savepoint.rollback()
    assert await _version_counts(screener_session, basket.id) == (1, 1, 0)
    assert (
        await screener_session.scalar(
            select(func.count()).select_from(BasketPendingAction)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_publish_rollback_removes_version_constituents_post_action_and_state(
    screener_session: AsyncSession,
) -> None:
    operator = await _operator(screener_session)
    basket = await _basket(screener_session, "rollback")
    first = await _instrument(screener_session, "ROLL1")
    second = await _instrument(screener_session, "ROLL2")
    genesis = await _genesis(screener_session, basket, first)
    await _investment(screener_session, operator, basket, genesis)

    savepoint = await screener_session.begin_nested()
    published = await publish_manual_version(
        screener_session,
        operator.id,
        basket.slug,
        _draft(dt.date(2026, 8, 8), _constituent(second.id, "1.0000")),
    )
    assert await screener_session.get(BasketUserRebalanceState, (operator.id, published.version_id))
    await savepoint.rollback()

    assert await _version_counts(screener_session, basket.id) == (1, 1, 0)
    assert (
        await screener_session.scalar(
            select(func.count()).select_from(BasketPendingAction)
        )
        == 0
    )
    assert (
        await screener_session.scalar(
            select(func.count()).select_from(BasketUserRebalanceState)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_scan_publish_projects_exact_private_reserved_run_and_replays(
    screener_session: AsyncSession,
) -> None:
    operator = await _operator(screener_session)
    basket = await _basket(
        screener_session,
        "scan",
        source="SCAN",
        scan_strategy_key="MomentumScan",
    )
    instruments = [
        await _instrument(screener_session, suffix) for suffix in ("SCANZ", "SCANA", "SCANM")
    ]
    run = await _reserved_scan(screener_session, operator, instruments)

    created = await publish_scan_version(
        screener_session, operator.id, basket.slug, run.id, "weekly cut"
    )
    replay = await publish_scan_version(
        screener_session, operator.id, basket.slug, run.id, "weekly cut"
    )
    rows = (
        (
            await screener_session.execute(
                select(BasketConstituent.instrument_id, BasketConstituent.weight)
                .where(BasketConstituent.version_id == created.version_id)
                .order_by(BasketConstituent.instrument_id)
            )
        )
        .tuples()
        .all()
    )
    assert created.effective_date == run.as_of
    assert created.source_scan_run_id == run.id
    assert replay.idempotent_replay is True
    assert sum((weight for _, weight in rows), start=Decimal(0)) == Decimal("1.0000")
    assert sorted(weight for _, weight in rows) == [
        Decimal("0.3333"),
        Decimal("0.3333"),
        Decimal("0.3334"),
    ]


@pytest.mark.asyncio
async def test_scan_publish_refuses_missing_foreign_and_non_scan_sources(
    screener_session: AsyncSession,
) -> None:
    operator = await _operator(screener_session)
    instrument = await _instrument(screener_session, "SCANSRC")
    manual = await _basket(screener_session, "scan-on-manual")
    with pytest.raises(PublishingNotFoundError, match="does not exist"):
        await publish_scan_version(screener_session, operator.id, manual.slug, 999_999_999)

    foreign = AppUser(public_id="scan-foreign", email="scan-foreign@users.invalid", name="Other")
    screener_session.add(foreign)
    await screener_session.flush()
    run = await _reserved_scan(
        screener_session,
        operator,
        [instrument],
        owner_id=foreign.id,
        public_id="foreign-scan-source",
    )
    with pytest.raises(PublishingSourceError, match="reserved private"):
        await publish_scan_version(screener_session, operator.id, manual.slug, run.id)

    valid_run = await _reserved_scan(screener_session, operator, [instrument], as_of=TODAY)
    with pytest.raises(PublishingSourceError, match="SCAN basket"):
        await publish_scan_version(screener_session, operator.id, manual.slug, valid_run.id)


@pytest.mark.parametrize(
    "results",
    [
        [{"rank": 0, "instrument_id": 1}],
        [{"rank": 2, "instrument_id": 1}],
        [{"rank": 1, "instrument_id": 1}, {"rank": 1, "instrument_id": 2}],
        [{"rank": 1, "instrument_id": 1}, {"rank": 2, "instrument_id": 1}],
        [{"rank": True, "instrument_id": 1}],
        [{"rank": 1, "instrument_id": True}],
    ],
)
@pytest.mark.asyncio
async def test_scan_publish_refuses_non_contiguous_or_duplicate_provenance_rows(
    screener_session: AsyncSession,
    results: list[dict[str, object]],
) -> None:
    operator = await _operator(screener_session)
    basket = await _basket(
        screener_session,
        f"scan-malformed-{uuid.uuid4().hex[:6]}",
        source="SCAN",
        scan_strategy_key="MomentumScan",
    )
    instrument = await _instrument(screener_session, f"BAD{uuid.uuid4().hex[:5]}")
    # Substitute real positive instrument ids while retaining each rank/duplication shape.
    normalized = [
        {
            **row,
            "instrument_id": (
                row["instrument_id"]
                if isinstance(row["instrument_id"], bool)
                else instrument.id + int(row["instrument_id"]) - 1
            ),
        }
        for row in results
    ]
    run = await _reserved_scan(screener_session, operator, [instrument], results=normalized)
    with pytest.raises(PublishingSourceError):
        await publish_scan_version(screener_session, operator.id, basket.slug, run.id)


@pytest.mark.asyncio
async def test_scan_publish_refuses_stale_run_and_replay_conflicting_notes(
    screener_session: AsyncSession,
) -> None:
    operator = await _operator(screener_session)
    basket = await _basket(
        screener_session,
        "scan-stale",
        source="SCAN",
        scan_strategy_key="MomentumScan",
    )
    instrument = await _instrument(screener_session, "STALE")
    new_run = await _reserved_scan(
        screener_session, operator, [instrument], as_of=dt.date(2026, 8, 15)
    )
    await publish_scan_version(screener_session, operator.id, basket.slug, new_run.id, "new")

    with pytest.raises(PublishingConflictError, match="immutable facts"):
        await publish_scan_version(screener_session, operator.id, basket.slug, new_run.id, "changed")

    # Reuse the reserved source screen because the identity is intentionally unique.
    screen = await screener_session.get_one(Screen, new_run.screen_id)
    old_run = ScreenRun(
        screen_id=screen.id,
        as_of=dt.date(2026, 8, 8),
        definition_hash=new_run.definition_hash,
        result_count=1,
        results=new_run.results,
    )
    screener_session.add(old_run)
    await screener_session.flush()
    with pytest.raises(PublishingConflictError, match="strictly later"):
        await publish_scan_version(screener_session, operator.id, basket.slug, old_run.id, "old")


@pytest.mark.asyncio
async def test_scan_idempotent_replay_refuses_corrupt_action_state_pair(
    screener_session: AsyncSession,
) -> None:
    operator = await _operator(screener_session)
    basket = await _basket(
        screener_session,
        "scan-corrupt-replay",
        source="SCAN",
        scan_strategy_key="MomentumScan",
    )
    first = await _instrument(screener_session, "SCANC1")
    second = await _instrument(screener_session, "SCANC2")
    genesis = await _genesis(screener_session, basket, first)
    await _investment(screener_session, operator, basket, genesis)
    run = await _reserved_scan(screener_session, operator, [second])
    result = await publish_scan_version(screener_session, operator.id, basket.slug, run.id)
    state = await screener_session.get_one(
        BasketUserRebalanceState, (operator.id, result.version_id)
    )
    await screener_session.delete(state)
    await screener_session.flush()

    with pytest.raises(PublishingConflictError, match="incomplete"):
        await publish_scan_version(screener_session, operator.id, basket.slug, run.id)


@pytest.mark.asyncio
async def test_timeline_is_chronological_bounded_latest_correct_and_labels_scheduled(
    screener_session: AsyncSession,
) -> None:
    basket = await _basket(screener_session, "timeline")
    start = dt.date(2026, 6, 1)
    versions = [
        BasketVersion(
            basket_id=basket.id,
            version_no=number,
            effective_date=start + dt.timedelta(days=number - 1),
            label="GENESIS" if number == 1 else "NO_CHANGE",
            added_count=1 if number == 1 else 0,
            removed_count=0,
            notes_md=f"v{number}",
            source_scan_run_id=None,
        )
        for number in range(1, 106)
    ]
    screener_session.add_all(versions)
    await screener_session.flush()

    first_page = await get_version_timeline(
        screener_session,
        basket.slug,
        limit=100,
        as_of=TODAY,
    )
    assert first_page.total == 105
    assert first_page.latest_version_no == 105
    assert first_page.has_more_older is True
    assert first_page.next_before_version_no == 6
    assert [row.version_no for row in first_page.versions] == list(range(6, 106))
    assert [row.activation for row in first_page.versions[-3:]] == [
        "SCHEDULED",
        "SCHEDULED",
        "SCHEDULED",
    ]

    older = await get_version_timeline(
        screener_session,
        basket.slug,
        limit=5,
        before_version_no=first_page.next_before_version_no,
        as_of=TODAY,
    )
    assert [row.version_no for row in older.versions] == [1, 2, 3, 4, 5]
    assert older.latest_version_no == 105
    assert older.has_more_older is False


@pytest.mark.asyncio
async def test_timeline_query_budget_is_constant_and_has_no_tenant_selector(
    screener_session: AsyncSession,
) -> None:
    basket = await _basket(screener_session, "timeline-budget")
    screener_session.add_all(
        [
            BasketVersion(
                basket_id=basket.id,
                version_no=number,
                effective_date=dt.date(2026, 8, number),
                label="GENESIS" if number == 1 else "NO_CHANGE",
                added_count=1 if number == 1 else 0,
                removed_count=0,
                notes_md=None,
                source_scan_run_id=None,
            )
            for number in range(1, 11)
        ]
    )
    await screener_session.flush()
    connection = await screener_session.connection()
    statements: list[str] = []

    def record(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(connection.sync_connection, "before_cursor_execute", record)
    try:
        await get_version_timeline(screener_session, basket.slug, limit=3, as_of=TODAY)
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", record)
    assert len(statements) == 2
    assert "user_id" not in str(get_version_timeline.__annotations__)


@pytest_asyncio.fixture
async def publishing_api(
    seeded_url: str, screener_session: AsyncSession
) -> AsyncIterator[httpx.AsyncClient]:
    settings = api_helpers.api_settings(
        seeded_url,
        rate_limit_enabled=False,
        sole_user_id="baskfy-sole-user",
    )
    app = create_app(settings)
    if not any(getattr(route, "path", "") == "/api/v1/catalog/{slug}/timeline" for route in app.routes):
        app.include_router(publishing_router, prefix="/api/v1")

    async def session_override() -> AsyncIterator[AsyncSession]:
        yield screener_session

    app.dependency_overrides[get_session] = session_override
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client


@pytest.mark.asyncio
async def test_timeline_http_requires_auth_and_hides_surface_from_other_user(
    screener_session: AsyncSession,
    publishing_api: httpx.AsyncClient,
) -> None:
    basket = await _basket(screener_session, "timeline-auth")
    instrument = await _instrument(screener_session, "TLHTTP")
    await _genesis(screener_session, basket, instrument)

    anonymous = await publishing_api.get(f"/api/v1/catalog/{basket.slug}/timeline")
    assert anonymous.status_code == 401

    _, foreign_public_id = await api_helpers.make_user(
        screener_session, "timeline-other@example.invalid"
    )
    foreign = await publishing_api.get(
        f"/api/v1/catalog/{basket.slug}/timeline",
        headers=api_helpers.bearer(foreign_public_id),
    )
    assert foreign.status_code == 404

    sole = await publishing_api.get(
        f"/api/v1/catalog/{basket.slug}/timeline",
        headers=api_helpers.bearer("baskfy-sole-user"),
    )
    assert sole.status_code == 200
    assert sole.json()["basket_slug"] == basket.slug


def test_forbidden_execution_payment_provider_network_imports_are_absent() -> None:
    paths = [
        API_SOURCE / "curated_publishing.py",
        API_SOURCE / "routers" / "curated_publishing.py",
    ]
    forbidden_roots = {
        "baskfy_execution",
        "baskfy_providers",
        "httpx",
        "requests",
        "razorpay",
    }
    for path in paths:
        tree = ast.parse(path.read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", maxsplit=1)[0])
        assert imported.isdisjoint(forbidden_roots), (path, imported & forbidden_roots)


@pytest.mark.asyncio
async def test_concurrent_manual_idempotent_publish_creates_one_version_and_one_post(
    seeded_url: str,
) -> None:
    """Two committed writers serialize on the basket row; one creates and one replays."""
    engine: AsyncEngine = create_async_engine(seeded_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    slug = f"sc3-race-{uuid.uuid4().hex[:10]}"
    try:
        async with maker() as setup, setup.begin():
            operator = (
                await setup.execute(
                    select(AppUser).where(AppUser.public_id == "baskfy-sole-user")
                )
            ).scalar_one()
            manager = (
                await setup.execute(select(CuratedManager).where(CuratedManager.slug == "maulik"))
            ).scalar_one()
            instrument = (
                await setup.execute(
                    select(Instrument)
                    .join(Exchange, Exchange.id == Instrument.exchange_id)
                    .where(Exchange.code == "NSE", Instrument.is_active.is_(True))
                    .order_by(Instrument.id)
                    .limit(1)
                )
            ).scalar_one()
            basket = CuratedBasket(
                slug=slug,
                name="SC3 race",
                manager_id=manager.id,
                type="STOCK",
                access="FREE",
                visibility="PUBLISHED",
                categories=[],
                description_md="race",
                rationale_md="race",
                rebalance_frequency="WEEKLY",
                benchmark_instrument_id=None,
                launched_at=TODAY,
                next_review_at=None,
                source="MANUAL",
                scan_strategy_key=None,
                archived_at=None,
            )
            setup.add(basket)
            await setup.flush()
            operator_id = operator.id
            instrument_id = instrument.id
            basket_id = basket.id

        draft = _draft(TODAY, _constituent(instrument_id, "1.0000"), notes_md="race")

        async def publish() -> bool:
            async with maker() as session, session.begin():
                result = await publish_manual_version(session, operator_id, slug, draft)
                return result.idempotent_replay

        outcomes = await asyncio.gather(publish(), publish())
        assert sorted(outcomes) == [False, True]
        async with maker() as verify:
            assert await _version_counts(verify, basket_id) == (1, 1, 1)
    finally:
        # The SC schema is intentionally append-only, including in tests.  This leaf owns a
        # disposable private database, so the random fixture remains until the next schema reset.
        await engine.dispose()


def test_router_has_no_order_execute_payment_or_tenant_surface() -> None:
    paths = {route.path for route in publishing_router.routes}
    assert paths == {
        "/catalog/{slug}/versions/manual",
        "/catalog/{slug}/versions/from-scan",
        "/catalog/{slug}/timeline",
    }
    forbidden = ("order", "execute", "payment", "checkout", "user_id", "tenant")
    assert all(token not in path for path in paths for token in forbidden)
