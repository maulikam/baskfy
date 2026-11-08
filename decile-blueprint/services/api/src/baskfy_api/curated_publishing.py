"""Atomic, sole-operator publishing and bounded version timelines.

This module only appends product facts.  It deliberately has no broker, execution, payment, or
network dependency: publishing a model version can create a desk *opportunity* later, but can
never create or send an order.  The request/job boundary owns the database transaction, so the
version, its post, and every affected-user side effect commit or roll back together.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Final, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.curated_baskets import (
    CuratedBasketRepository,
    EffectiveDateNotIncreasingError,
)
from baskfy_api.seed import (
    CURATED_SCAN_SCREEN_PUBLIC_ID,
    MAX_CURATED_SCAN_CONSTITUENTS,
    _curated_scan_definition,
    _equal_four_decimal_weights,
)
from baskfy_core.curated_baskets import ConstituentDraft, VersionDraft
from baskfy_core.models import (
    BasketConstituent,
    BasketInvestment,
    BasketPendingAction,
    BasketUpdatePost,
    BasketUserRebalanceState,
    BasketVersion,
    CuratedBasket,
    Exchange,
    Instrument,
    Screen,
    ScreenRun,
)

IST: Final = ZoneInfo("Asia/Kolkata")
SCAN_STRATEGY_KEY: Final = "MomentumScan"
REBALANCE_ACTION_TYPE: Final = "REBALANCE_AVAILABLE"
TIMELINE_DEFAULT_LIMIT: Final = 50
TIMELINE_MAX_LIMIT: Final = 100


class CuratedPublishingError(RuntimeError):
    """Base class for an expected, fail-closed publishing refusal."""


class PublishingNotFoundError(CuratedPublishingError):
    """The basket or scan run does not exist in the caller's private scope."""


class PublishingConflictError(CuratedPublishingError):
    """A requested immutable identity already names different facts."""


class PublishingSourceError(CuratedPublishingError):
    """The basket/source/provenance contract does not match the requested operation."""


class ForeignInvestmentError(CuratedPublishingError):
    """Runtime data would require the forbidden multi-tenant publishing path."""


class PublishedVersionOut(BaseModel):
    basket_slug: str
    version_id: int
    version_no: int
    effective_date: dt.date
    label: str
    added_count: int
    removed_count: int
    source_scan_run_id: int | None
    idempotent_replay: bool


class TimelineVersionOut(BaseModel):
    version_no: int
    effective_date: dt.date
    activation: Literal["ACTIVE", "SCHEDULED"]
    label: str
    added_count: int
    removed_count: int
    notes_md: str | None
    created_at: dt.datetime


class VersionTimelineOut(BaseModel):
    basket_slug: str
    launched_at: dt.date
    next_review_at: dt.date | None
    latest_version_no: int | None
    total: int
    limit: int
    before_version_no: int | None
    has_more_older: bool
    next_before_version_no: int | None
    versions: list[TimelineVersionOut]


def _post_copy(basket: CuratedBasket, version: BasketVersion) -> tuple[str, str]:
    title = f"{basket.name}: version {version.version_no} published"
    body = (
        f"Version {version.version_no} is effective {version.effective_date.isoformat()}. "
        f"Added {version.added_count}; removed {version.removed_count}."
    )
    if version.notes_md:
        body = f"{body}\n\n{version.notes_md}"
    return title, body


def _action_payload(basket: CuratedBasket, version: BasketVersion) -> dict[str, object]:
    return {
        "basket_id": basket.id,
        "basket_slug": basket.slug,
        "version_id": version.id,
        "version_no": version.version_no,
        "effective_date": version.effective_date.isoformat(),
        "label": version.label,
        "added_count": version.added_count,
        "removed_count": version.removed_count,
    }


async def _locked_basket(session: AsyncSession, basket_slug: str) -> CuratedBasket:
    basket = await session.scalar(
        select(CuratedBasket)
        .where(CuratedBasket.slug == basket_slug, CuratedBasket.archived_at.is_(None))
        .with_for_update()
    )
    if basket is None:
        raise PublishingNotFoundError(f"curated basket {basket_slug!r} does not exist")
    return basket


async def _stored_constituents(
    session: AsyncSession, version_id: int
) -> tuple[ConstituentDraft, ...]:
    rows = (
        (
            await session.execute(
                select(
                    BasketConstituent.instrument_id,
                    BasketConstituent.segment,
                    BasketConstituent.weight,
                )
                .where(BasketConstituent.version_id == version_id)
                .order_by(BasketConstituent.instrument_id)
            )
        )
        .tuples()
        .all()
    )
    return tuple(
        ConstituentDraft(instrument_id=instrument_id, segment=segment, weight=weight)
        for instrument_id, segment, weight in rows
    )


def _canonical_constituents(
    rows: tuple[ConstituentDraft, ...],
) -> tuple[tuple[int, str, Decimal], ...]:
    return tuple(sorted((row.instrument_id, row.segment, row.weight) for row in rows))


async def _matching_existing_version(
    session: AsyncSession,
    basket: CuratedBasket,
    draft: VersionDraft,
) -> BasketVersion | None:
    existing = await session.scalar(
        select(BasketVersion).where(
            BasketVersion.basket_id == basket.id,
            BasketVersion.effective_date == draft.effective_date,
        )
    )
    if existing is None:
        return None
    stored = await _stored_constituents(session, existing.id)
    if (
        _canonical_constituents(stored) != _canonical_constituents(draft.constituents)
        or existing.notes_md != draft.notes_md
        or existing.source_scan_run_id != draft.source_scan_run_id
    ):
        raise PublishingConflictError(
            "that basket/effective-date identity already contains different immutable facts"
        )
    return existing


async def _assert_replay_side_effects(
    session: AsyncSession,
    basket: CuratedBasket,
    version: BasketVersion,
    user_id: int,
) -> None:
    """Accept a replay only when the original atomic publish is fully recognizable.

    State and actions may legitimately have moved on after publication: APPLIED/SKIPPED states
    and resolved actions remain valid evidence.  Missing halves, duplicate actions, or altered
    payload/copy fail closed instead of attempting an impossible partial repair.
    """
    title, body = _post_copy(basket, version)
    posts = (
        (
            await session.execute(
                select(BasketUpdatePost).where(
                    BasketUpdatePost.basket_id == basket.id,
                    BasketUpdatePost.title == title,
                )
            )
        )
        .scalars()
        .all()
    )
    if len(posts) != 1 or posts[0].source != "ENGINE" or posts[0].body_md != body:
        raise PublishingConflictError(
            "the existing version has missing, duplicate, or conflicting update-post evidence"
        )

    states = (
        (
            await session.execute(
                select(BasketUserRebalanceState).where(
                    BasketUserRebalanceState.version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    if any(state.user_id != user_id for state in states):
        raise ForeignInvestmentError("version state contains a non-operator user")

    expected_payload = _action_payload(basket, version)
    actions = (
        (
            await session.execute(
                select(BasketPendingAction).where(
                    BasketPendingAction.type == REBALANCE_ACTION_TYPE,
                    BasketPendingAction.payload["version_id"].as_integer() == version.id,
                )
            )
        )
        .scalars()
        .all()
    )
    if any(action.user_id != user_id for action in actions):
        raise ForeignInvestmentError("version action contains a non-operator user")
    if len(states) != len(actions) or len(states) > 1:
        raise PublishingConflictError(
            "the existing version's action and rebalance-state evidence is incomplete"
        )
    if actions and actions[0].payload != expected_payload:
        raise PublishingConflictError("the existing version's action payload conflicts")
    if states and states[0].state == "PENDING" and actions[0].resolved_at is not None:
        newer = await session.scalar(
            select(func.count())
            .select_from(BasketVersion)
            .where(
                BasketVersion.basket_id == basket.id,
                BasketVersion.version_no > version.version_no,
            )
        )
        if not newer:
            raise PublishingConflictError(
                "a latest PENDING version cannot have a resolved rebalance action"
            )


async def _affected_user_ids(
    session: AsyncSession, basket_id: int, sole_user_id: int
) -> tuple[int, ...]:
    user_ids = tuple(
        (
            await session.execute(
                select(BasketInvestment.user_id)
                .where(
                    BasketInvestment.basket_id == basket_id,
                    BasketInvestment.status == "ACTIVE",
                )
                .distinct()
                .order_by(BasketInvestment.user_id)
            )
        )
        .scalars()
        .all()
    )
    if any(user_id != sole_user_id for user_id in user_ids):
        raise ForeignInvestmentError(
            "publishing for a non-operator investment is forbidden before Track C"
        )
    return user_ids


async def _create_side_effects(
    session: AsyncSession,
    basket: CuratedBasket,
    version: BasketVersion,
    sole_user_id: int,
) -> None:
    affected = await _affected_user_ids(session, basket.id, sole_user_id)
    title, body = _post_copy(basket, version)
    session.add(
        BasketUpdatePost(
            basket_id=basket.id,
            manager_id=basket.manager_id,
            title=title,
            body_md=body,
            source="ENGINE",
        )
    )
    if not affected:
        await session.flush()
        return

    now = dt.datetime.now(tz=dt.UTC)
    await session.execute(
        update(BasketPendingAction)
        .where(
            BasketPendingAction.user_id == sole_user_id,
            BasketPendingAction.type == REBALANCE_ACTION_TYPE,
            BasketPendingAction.resolved_at.is_(None),
            BasketPendingAction.payload["basket_id"].as_integer() == basket.id,
        )
        .values(resolved_at=now)
    )
    session.add(
        BasketPendingAction(
            user_id=sole_user_id,
            type=REBALANCE_ACTION_TYPE,
            payload=_action_payload(basket, version),
            dismissed_at=None,
            resolved_at=None,
        )
    )
    session.add(
        BasketUserRebalanceState(
            user_id=sole_user_id,
            version_id=version.id,
            state="PENDING",
            decided_at=None,
        )
    )
    await session.flush()


def _published(
    basket: CuratedBasket, version: BasketVersion, *, replay: bool
) -> PublishedVersionOut:
    return PublishedVersionOut(
        basket_slug=basket.slug,
        version_id=version.id,
        version_no=version.version_no,
        effective_date=version.effective_date,
        label=version.label,
        added_count=version.added_count,
        removed_count=version.removed_count,
        source_scan_run_id=version.source_scan_run_id,
        idempotent_replay=replay,
    )


async def _append_or_replay(
    session: AsyncSession,
    basket: CuratedBasket,
    draft: VersionDraft,
    sole_user_id: int,
) -> PublishedVersionOut:
    existing = await _matching_existing_version(session, basket, draft)
    if existing is not None:
        await _assert_replay_side_effects(session, basket, existing, sole_user_id)
        return _published(basket, existing, replay=True)

    try:
        version = await CuratedBasketRepository(session).create_version(basket.id, draft)
    except EffectiveDateNotIncreasingError as exc:
        raise PublishingConflictError(str(exc)) from exc
    await _create_side_effects(session, basket, version, sole_user_id)
    return _published(basket, version, replay=False)


async def publish_manual_version(
    session: AsyncSession,
    sole_user_id: int,
    basket_slug: str,
    draft: VersionDraft,
) -> PublishedVersionOut:
    """Append a hand-authored version and its atomic product side effects."""
    basket = await _locked_basket(session, basket_slug)
    if basket.source != "MANUAL" or basket.scan_strategy_key is not None:
        raise PublishingSourceError("manual publishing is allowed only for MANUAL baskets")
    if draft.source_scan_run_id is not None:
        raise PublishingSourceError("a manual version cannot carry scan provenance")
    return await _append_or_replay(session, basket, draft, sole_user_id)


def _validated_scan_results(run: ScreenRun) -> tuple[int, ...]:
    results = run.results
    if not isinstance(results, list) or not results:
        raise PublishingSourceError("the scan run has no result rows")
    if len(results) != run.result_count:
        raise PublishingSourceError("the scan run result_count does not match its rows")
    if len(results) > MAX_CURATED_SCAN_CONSTITUENTS:
        raise PublishingSourceError(
            f"a scan version is capped at {MAX_CURATED_SCAN_CONSTITUENTS} constituents"
        )
    ranks: list[int] = []
    instrument_ids: list[int] = []
    for row in results:
        if not isinstance(row, dict):
            raise PublishingSourceError("every scan result must be an object")
        rank = row.get("rank")
        instrument_id = row.get("instrument_id")
        if type(rank) is not int or rank <= 0:
            raise PublishingSourceError("every scan rank must be a positive integer")
        if type(instrument_id) is not int or instrument_id <= 0:
            raise PublishingSourceError(
                "every scan instrument_id must be a positive integer"
            )
        ranks.append(rank)
        instrument_ids.append(instrument_id)
    expected_ranks = list(range(1, len(results) + 1))
    if ranks != expected_ranks:
        raise PublishingSourceError("scan ranks must be unique, contiguous, and stored in order")
    if len(set(instrument_ids)) != len(instrument_ids):
        raise PublishingSourceError("scan instrument_ids must be unique")
    return tuple(instrument_ids)


async def _scan_draft(
    session: AsyncSession,
    run: ScreenRun,
    instrument_ids: tuple[int, ...],
    notes_md: str | None,
) -> VersionDraft:
    rows = (
        (
            await session.execute(
                select(Instrument.id, Instrument.symbol)
                .join(Exchange, Exchange.id == Instrument.exchange_id)
                .where(
                    Instrument.id.in_(instrument_ids),
                    Instrument.is_active.is_(True),
                    Instrument.series == "EQ",
                    Exchange.code == "NSE",
                )
            )
        )
        .tuples()
        .all()
    )
    symbols_by_id = {instrument_id: symbol for instrument_id, symbol in rows}
    if len(symbols_by_id) != len(instrument_ids):
        raise PublishingSourceError("every scan result must resolve to an active NSE EQ instrument")
    weights_by_symbol = _equal_four_decimal_weights(tuple(symbols_by_id.values()))
    constituents = tuple(
        ConstituentDraft(
            instrument_id=instrument_id,
            segment="Equity",
            weight=weights_by_symbol[symbols_by_id[instrument_id]],
        )
        for instrument_id in sorted(instrument_ids, key=lambda value: symbols_by_id[value])
    )
    return VersionDraft(
        effective_date=run.as_of,
        constituents=constituents,
        notes_md=notes_md,
        source_scan_run_id=run.id,
    )


async def publish_scan_version(
    session: AsyncSession,
    sole_user_id: int,
    basket_slug: str,
    source_scan_run_id: int,
    notes_md: str | None = None,
) -> PublishedVersionOut:
    """Append an exact projection of one private, reserved MomentumScan source run."""
    if type(source_scan_run_id) is not int or source_scan_run_id <= 0:
        raise PublishingSourceError("source_scan_run_id must be a positive integer")
    source = (
        await session.execute(
            select(ScreenRun, Screen)
            .join(Screen, Screen.id == ScreenRun.screen_id)
            .where(ScreenRun.id == source_scan_run_id)
        )
    ).one_or_none()
    if source is None:
        raise PublishingNotFoundError("the private scan run does not exist")
    run, screen = source
    expected_definition = _curated_scan_definition()
    if (
        screen.public_id != CURATED_SCAN_SCREEN_PUBLIC_ID
        or screen.user_id != sole_user_id
        or screen.is_example
        or screen.definition != expected_definition.model_dump(mode="json", by_alias=True)
        or run.definition_hash != expected_definition.definition_hash()
    ):
        raise PublishingSourceError(
            "the scan run is not owned by the reserved private MomentumScan source"
        )
    instrument_ids = _validated_scan_results(run)

    # A scan run is a global immutable provenance identity.  The transaction advisory lock closes
    # the cross-basket race without adding a schema feature solely for this single-tenant run.
    await session.execute(select(func.pg_advisory_xact_lock(run.id)))
    basket = await _locked_basket(session, basket_slug)
    if basket.source != "SCAN" or basket.scan_strategy_key != SCAN_STRATEGY_KEY:
        raise PublishingSourceError(
            "scan publishing requires a SCAN basket backed by MomentumScan"
        )

    used_by = (
        (
            await session.execute(
                select(BasketVersion).where(BasketVersion.source_scan_run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    if any(version.basket_id != basket.id for version in used_by) or len(used_by) > 1:
        raise PublishingConflictError("the scan run provenance is already bound elsewhere")

    draft = await _scan_draft(session, run, instrument_ids, notes_md)
    return await _append_or_replay(session, basket, draft, sole_user_id)


async def get_version_timeline(
    session: AsyncSession,
    basket_slug: str,
    *,
    limit: int = TIMELINE_DEFAULT_LIMIT,
    before_version_no: int | None = None,
    as_of: dt.date | None = None,
) -> VersionTimelineOut:
    """Read one bounded timeline page; scheduled versions are explicitly labelled."""
    if type(limit) is not int or not 1 <= limit <= TIMELINE_MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {TIMELINE_MAX_LIMIT}")
    if before_version_no is not None and (
        type(before_version_no) is not int or before_version_no <= 0
    ):
        raise ValueError("before_version_no must be a positive integer or None")

    basket = await session.scalar(
        select(CuratedBasket).where(
            CuratedBasket.slug == basket_slug,
            CuratedBasket.archived_at.is_(None),
        )
    )
    if basket is None:
        raise PublishingNotFoundError(f"curated basket {basket_slug!r} does not exist")

    total_subquery = (
        select(func.count())
        .select_from(BasketVersion)
        .where(BasketVersion.basket_id == basket.id)
        .scalar_subquery()
    )
    latest_subquery = (
        select(func.max(BasketVersion.version_no))
        .where(BasketVersion.basket_id == basket.id)
        .scalar_subquery()
    )
    statement = select(BasketVersion, total_subquery, latest_subquery).where(
        BasketVersion.basket_id == basket.id
    )
    if before_version_no is not None:
        statement = statement.where(BasketVersion.version_no < before_version_no)
    rows = (
        await session.execute(
            statement.order_by(BasketVersion.version_no.desc()).limit(limit + 1)
        )
    ).all()
    has_more_older = len(rows) > limit
    page = rows[:limit]
    total = int(page[0][1]) if page else int(await session.scalar(total_subquery) or 0)
    latest_version_no = int(page[0][2]) if page and page[0][2] is not None else None
    effective_as_of = as_of or dt.datetime.now(tz=IST).date()
    chronological = list(reversed(page))
    versions = [
        TimelineVersionOut(
            version_no=version.version_no,
            effective_date=version.effective_date,
            activation="SCHEDULED" if version.effective_date > effective_as_of else "ACTIVE",
            label=version.label,
            added_count=version.added_count,
            removed_count=version.removed_count,
            notes_md=version.notes_md,
            created_at=version.created_at,
        )
        for version, _, _ in chronological
    ]
    next_before = versions[0].version_no if has_more_older and versions else None
    return VersionTimelineOut(
        basket_slug=basket.slug,
        launched_at=basket.launched_at,
        next_review_at=basket.next_review_at,
        latest_version_no=latest_version_no,
        total=total,
        limit=limit,
        before_version_no=before_version_no,
        has_more_older=has_more_older,
        next_before_version_no=next_before,
        versions=versions,
    )
