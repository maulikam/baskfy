"""Curated-basket plan handoff and desk-journal reconciliation.

This module deliberately stops at durable order *intent*.  It neither imports the execution
package nor calls a broker/provider.  The desk console is the only component allowed to restore
the ``/analyze``-shaped payload written here and, after explicit confirmation, execute it.

The desk journal owns execution truth.  Product holdings are therefore rebuilt from an immutable
baseline embedded in ``plan_json`` plus only the quantities the journal marks FILLED/PARTIAL.
That makes reconciliation idempotent even while a partial fill grows across repeated syncs.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Final, Literal, cast
from zoneinfo import ZoneInfo

from sqlalchemy import RowMapping, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.curated_rebalance import (
    CuratedRebalanceError,
    IntendedHolding,
    PlanPreview,
    PriceQuote,
    QuoteFreshness,
    TargetAllocation,
    market_session_at,
    plan_apply_rebalance,
    plan_full_exit,
    plan_invest,
    plan_invest_more,
    plan_partial_exit,
)
from baskfy_core.models import (
    BasketConstituent,
    BasketInvestment,
    BasketInvestmentHolding,
    BasketOrderBatch,
    BasketPendingAction,
    BasketUserRebalanceState,
    BasketVersion,
    CuratedBasket,
    Instrument,
    OhlcvDaily,
    TradingDay,
)

IST: Final = ZoneInfo("Asia/Kolkata")
NSE_EXCHANGE_ID: Final = 1
PLAN_TTL: Final = dt.timedelta(minutes=30)
MAX_EOD_QUOTE_AGE: Final = dt.timedelta(days=8)
MAX_PLAN_INSTRUMENTS: Final = 500
MONEY_QUANTUM: Final = Decimal("0.01")
PRICE_QUANTUM: Final = Decimal("0.0001")
QUANTITY_QUANTUM: Final = Decimal("0.0001")
PLAN_SCHEMA: Final = "baskfy.curated-plan/v1"
DESK_SCHEMA: Final = "desk"

JournalOrderStatus = Literal[
    "PENDING",
    "SUBMITTED",
    "FILLED",
    "PARTIAL",
    "FAILED",
    "BLOCKED",
    "DUPLICATE",
    "DRY_RUN",
    "LAPSED",
    "ABORTED",
]

CONFIRMED_FILL_STATUSES: Final = frozenset({"FILLED", "PARTIAL"})
TERMINAL_NO_FILL_STATUSES: Final = frozenset(
    {"FAILED", "BLOCKED", "DUPLICATE", "LAPSED", "ABORTED"}
)


class CuratedPlanServiceError(RuntimeError):
    """An expected plan refusal safe to show to the sole operator."""


class CuratedPlanNotFoundError(CuratedPlanServiceError):
    """A basket, investment, or target version does not exist for this operator."""


class CuratedPlanConflictError(CuratedPlanServiceError):
    """A second plan would race an unresolved plan for the same investment."""


class CuratedPlanInputError(CuratedPlanServiceError):
    """The requested amount/version cannot form a valid plan."""


class PlanJournalUnavailableError(CuratedPlanServiceError):
    """The independently migrated desk plan journal is unavailable or outdated."""


@dataclass(frozen=True, slots=True)
class MarketClosedResult:
    observed_at_ist: dt.datetime
    next_open_at: dt.datetime | None
    reason: Literal["MARKET_CLOSED", "CALENDAR_UNAVAILABLE"]


@dataclass(frozen=True, slots=True)
class PlanCreatedResult:
    batch_id: int
    investment_id: int
    desk_plan_id: str
    created_at: dt.datetime
    expires_at: dt.datetime
    preview: PlanPreview


PlanGenerationResult = MarketClosedResult | PlanCreatedResult


@dataclass(frozen=True, slots=True)
class BatchSyncResult:
    batch_id: int
    desk_plan_id: str
    previous_status: str
    status: str
    confirmed_lines: int
    confirmed_quantity: int
    holdings_changed: bool


@dataclass(frozen=True, slots=True)
class ExpirySweepResult:
    expired_batch_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _VersionContext:
    basket: CuratedBasket
    version: BasketVersion
    targets: tuple[TargetAllocation, ...]
    symbols: Mapping[int, str]


@dataclass(frozen=True, slots=True)
class _HoldingState:
    instrument_id: int
    symbol: str
    quantity: Decimal
    avg_price: Decimal


PlanBuilder = Callable[[Sequence[PriceQuote]], PlanPreview]


def utc_now() -> dt.datetime:
    """One patchable clock seam; request input can never choose market time."""
    return dt.datetime.now(tz=dt.UTC)


async def create_invest_plan(
    session: AsyncSession,
    *,
    user_id: int,
    broker_account_id: str,
    basket_slug: str,
    requested_amount: Decimal,
    now: dt.datetime | None = None,
) -> PlanGenerationResult:
    """Create an initial BUY plan and a zero-holding investment shell."""
    instant = _aware(now or utc_now())
    closed = await _market_closed(session, instant)
    if closed is not None:
        return closed
    await _ensure_desk_contract(session)

    basket = await session.scalar(
        select(CuratedBasket)
        .where(
            CuratedBasket.slug == basket_slug,
            CuratedBasket.archived_at.is_(None),
            CuratedBasket.type == "STOCK",
        )
        .with_for_update()
    )
    if basket is None:
        raise CuratedPlanNotFoundError(f"curated basket {basket_slug!r} does not exist")
    existing = await session.scalar(
        select(BasketInvestment.id).where(
            BasketInvestment.user_id == user_id,
            BasketInvestment.basket_id == basket.id,
            BasketInvestment.status == "ACTIVE",
        )
    )
    if existing is not None:
        raise CuratedPlanConflictError("an active investment already exists; use invest-more")

    context = await _current_version(session, basket, instant.astimezone(IST).date())
    _validate_universe(context.targets)
    quotes = await _quotes(session, context.symbols, instant)
    preview = _build_preview(
        lambda loaded: plan_invest(
            requested_amount,
            context.targets,
            loaded,
            QuoteFreshness(instant, MAX_EOD_QUOTE_AGE),
        ),
        quotes,
    )
    _require_executable(preview)

    investment = BasketInvestment(
        user_id=user_id,
        basket_id=basket.id,
        status="ACTIVE",
        version_applied_id=context.version.id,
        exited_at=None,
        last_invested_at=instant,
    )
    session.add(investment)
    await session.flush()
    return await _persist_plan(
        session,
        user_id=user_id,
        broker_account_id=broker_account_id,
        investment=investment,
        kind="BUY",
        preview=preview,
        context=context,
        baseline=(),
        target_version_id=context.version.id,
        now=instant,
    )


async def create_invest_more_plan(
    session: AsyncSession,
    *,
    user_id: int,
    broker_account_id: str,
    investment_id: int,
    requested_amount: Decimal,
    now: dt.datetime | None = None,
) -> PlanGenerationResult:
    instant = _aware(now or utc_now())
    closed = await _market_closed(session, instant)
    if closed is not None:
        return closed
    await _ensure_desk_contract(session)
    investment = await _active_investment(session, user_id, investment_id)
    await _require_no_live_batch(session, investment, instant)
    context = await _version_by_id(session, investment.basket_id, investment.version_applied_id)
    _validate_universe(context.targets)
    baseline = await _holdings(session, investment.id)
    quotes = await _quotes(session, _symbols_for(context, baseline), instant)
    preview = _build_preview(
        lambda loaded: plan_invest_more(
            requested_amount,
            context.targets,
            loaded,
            QuoteFreshness(instant, MAX_EOD_QUOTE_AGE),
        ),
        quotes,
    )
    _require_executable(preview)
    return await _persist_plan(
        session,
        user_id=user_id,
        broker_account_id=broker_account_id,
        investment=investment,
        kind="INVEST_MORE",
        preview=preview,
        context=context,
        baseline=baseline,
        target_version_id=investment.version_applied_id,
        now=instant,
    )


async def create_apply_plan(
    session: AsyncSession,
    *,
    user_id: int,
    broker_account_id: str,
    investment_id: int,
    version_id: int,
    cash_contribution: Decimal = Decimal("0"),
    now: dt.datetime | None = None,
) -> PlanGenerationResult:
    instant = _aware(now or utc_now())
    closed = await _market_closed(session, instant)
    if closed is not None:
        return closed
    await _ensure_desk_contract(session)
    investment = await _active_investment(session, user_id, investment_id)
    await _require_no_live_batch(session, investment, instant)
    context = await _version_by_id(session, investment.basket_id, version_id)
    if context.version.id == investment.version_applied_id:
        raise CuratedPlanInputError("the investment already uses that basket version")
    state = await session.scalar(
        select(BasketUserRebalanceState).where(
            BasketUserRebalanceState.user_id == user_id,
            BasketUserRebalanceState.version_id == version_id,
        )
    )
    if state is None or state.state != "PENDING":
        raise CuratedPlanConflictError("the requested version is not pending for this investment")
    _validate_universe(context.targets)
    baseline = await _holdings(session, investment.id)
    quotes = await _quotes(session, _symbols_for(context, baseline), instant)
    intended = tuple(
        IntendedHolding(instrument_id=row.instrument_id, quantity=row.quantity)
        for row in baseline
    )
    preview = _build_preview(
        lambda loaded: plan_apply_rebalance(
            intended,
            context.targets,
            loaded,
            cash_contribution=cash_contribution,
            quote_freshness=QuoteFreshness(instant, MAX_EOD_QUOTE_AGE),
        ),
        quotes,
    )
    _require_executable(preview)
    return await _persist_plan(
        session,
        user_id=user_id,
        broker_account_id=broker_account_id,
        investment=investment,
        kind="REBALANCE",
        preview=preview,
        context=context,
        baseline=baseline,
        target_version_id=context.version.id,
        now=instant,
    )


async def create_exit_plan(
    session: AsyncSession,
    *,
    user_id: int,
    broker_account_id: str,
    investment_id: int,
    now: dt.datetime | None = None,
) -> PlanGenerationResult:
    return await _create_exit_like(
        session,
        user_id=user_id,
        broker_account_id=broker_account_id,
        investment_id=investment_id,
        requested_amount=None,
        now=now,
    )


async def create_partial_exit_plan(
    session: AsyncSession,
    *,
    user_id: int,
    broker_account_id: str,
    investment_id: int,
    requested_amount: Decimal,
    now: dt.datetime | None = None,
) -> PlanGenerationResult:
    return await _create_exit_like(
        session,
        user_id=user_id,
        broker_account_id=broker_account_id,
        investment_id=investment_id,
        requested_amount=requested_amount,
        now=now,
    )


async def _create_exit_like(
    session: AsyncSession,
    *,
    user_id: int,
    broker_account_id: str,
    investment_id: int,
    requested_amount: Decimal | None,
    now: dt.datetime | None,
) -> PlanGenerationResult:
    instant = _aware(now or utc_now())
    closed = await _market_closed(session, instant)
    if closed is not None:
        return closed
    await _ensure_desk_contract(session)
    investment = await _active_investment(session, user_id, investment_id)
    await _require_no_live_batch(session, investment, instant)
    context = await _version_by_id(session, investment.basket_id, investment.version_applied_id)
    baseline = await _holdings(session, investment.id)
    if not baseline or all(row.quantity == 0 for row in baseline):
        raise CuratedPlanInputError("the investment has no holdings to exit")
    _validate_universe(tuple(baseline))
    quotes = await _quotes(session, _symbols_for(context, baseline), instant)
    intended = tuple(
        IntendedHolding(instrument_id=row.instrument_id, quantity=row.quantity)
        for row in baseline
    )
    if requested_amount is None:
        preview = _build_preview(
            lambda loaded: plan_full_exit(
                intended,
                loaded,
                QuoteFreshness(instant, MAX_EOD_QUOTE_AGE),
            ),
            quotes,
        )
        kind = "EXIT"
    else:
        preview = _build_preview(
            lambda loaded: plan_partial_exit(
                intended,
                loaded,
                requested_amount,
                QuoteFreshness(instant, MAX_EOD_QUOTE_AGE),
            ),
            quotes,
        )
        kind = "PARTIAL_EXIT"
    _require_executable(preview)
    return await _persist_plan(
        session,
        user_id=user_id,
        broker_account_id=broker_account_id,
        investment=investment,
        kind=kind,
        preview=preview,
        context=context,
        baseline=baseline,
        target_version_id=investment.version_applied_id,
        now=instant,
    )


async def reconcile_plan_batches(
    session: AsyncSession,
    *,
    now: dt.datetime | None = None,
    batch_id: int | None = None,
) -> tuple[BatchSyncResult, ...]:
    """Fold confirmed journal quantities into product holdings, idempotently.

    ``DRY_RUN`` proves the path but confirms no trade, so it never changes holdings.  A journal
    failure or blocked order likewise contributes zero.  The product does not infer a fill from
    submission, elapsed time, or an execution response.
    """
    instant = _aware(now or utc_now())
    await _ensure_desk_contract(session)
    query = (
        select(BasketOrderBatch)
        .where(BasketOrderBatch.status.in_(("PLANNED", "PARTIAL")))
        .order_by(BasketOrderBatch.id)
        .with_for_update()
    )
    if batch_id is not None:
        query = query.where(BasketOrderBatch.id == batch_id)
    batches = (await session.execute(query)).scalars().all()
    results: list[BatchSyncResult] = []
    for batch in batches:
        if batch.desk_plan_id is None:
            raise PlanJournalUnavailableError(f"batch {batch.id} has no desk plan identifier")
        version, orders = await _journal_plan(session, batch.desk_plan_id)
        payload = _parse_plan_json(version["plan_json"], batch)
        previous = batch.status
        changed, confirmed_lines, confirmed_quantity = await _apply_confirmed_fills(
            session, batch, payload, orders, instant
        )
        status = _batch_status(orders)
        batch.status = status
        if status == "EXECUTED":
            batch.executed_at = instant
            await _finalize_investment(session, batch, payload, instant)
        elif status == "CANCELLED":
            await _retire_empty_initial_shell(session, batch, instant)
        await session.flush()
        results.append(
            BatchSyncResult(
                batch_id=batch.id,
                desk_plan_id=batch.desk_plan_id,
                previous_status=previous,
                status=batch.status,
                confirmed_lines=confirmed_lines,
                confirmed_quantity=confirmed_quantity,
                holdings_changed=changed,
            )
        )
    return tuple(results)


async def expire_stale_plans(
    session: AsyncSession, *, now: dt.datetime | None = None
) -> ExpirySweepResult:
    """Expire PLANNED batches strictly older than 30 minutes.

    Exactly 30:00 remains valid, matching the desk's ``age > 1800`` guard.  PARTIAL batches are
    not relabelled EXPIRED: confirmed fills already changed the portfolio and need an operator to
    settle the remaining journal rows honestly.
    """
    instant = _aware(now or utc_now())
    rows = (
        (
            await session.execute(
                select(BasketOrderBatch)
                .where(BasketOrderBatch.status == "PLANNED")
                .order_by(BasketOrderBatch.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    expired: list[int] = []
    for batch in rows:
        created_at = _aware(batch.created_at)
        if instant - created_at <= PLAN_TTL:
            continue
        batch.status = "EXPIRED"
        if batch.kind == "REBALANCE" and batch.desk_plan_id is not None:
            version = await _journal_version(session, batch.desk_plan_id)
            payload = _parse_plan_json(version["plan_json"], batch)
            target_version_id = _required_int(payload["curated"], "target_version_id")
            state = await session.scalar(
                select(BasketUserRebalanceState).where(
                    BasketUserRebalanceState.user_id == batch.user_id,
                    BasketUserRebalanceState.version_id == target_version_id,
                )
            )
            if state is not None:
                state.state = "PENDING"
                state.decided_at = None
        await _retire_empty_initial_shell(session, batch, instant)
        expired.append(batch.id)
    await session.flush()
    return ExpirySweepResult(expired_batch_ids=tuple(expired))


async def _market_closed(
    session: AsyncSession, instant: dt.datetime
) -> MarketClosedResult | None:
    local_day = instant.astimezone(IST).date()
    today = await session.scalar(
        select(TradingDay).where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.date == local_day,
        )
    )
    if today is None:
        return MarketClosedResult(
            observed_at_ist=instant.astimezone(IST),
            next_open_at=None,
            reason="CALENDAR_UNAVAILABLE",
        )
    sessions = (
        (
            await session.execute(
                select(TradingDay.date)
                .where(
                    TradingDay.exchange_id == NSE_EXCHANGE_ID,
                    TradingDay.date >= local_day,
                    TradingDay.is_trading_day.is_(True),
                )
                .order_by(TradingDay.date)
                .limit(64)
            )
        )
        .scalars()
        .all()
    )
    try:
        decision = market_session_at(instant, tuple(sessions))
    except CuratedRebalanceError:
        return MarketClosedResult(
            observed_at_ist=instant.astimezone(IST),
            next_open_at=None,
            reason="CALENDAR_UNAVAILABLE",
        )
    if decision.is_open:
        return None
    return MarketClosedResult(
        observed_at_ist=decision.observed_at_ist,
        next_open_at=decision.next_open_at,
        reason="MARKET_CLOSED" if decision.next_open_at is not None else "CALENDAR_UNAVAILABLE",
    )


async def _ensure_desk_contract(session: AsyncSession) -> None:
    row = (
        await session.execute(
            text(
                "select to_regclass('desk.rebalance_versions') as versions, "
                "to_regclass('desk.rebalance_orders') as orders, "
                "exists (select 1 from information_schema.columns "
                "where table_schema='desk' and table_name='rebalance_versions' "
                "and column_name='plan_json') as has_plan_json"
            )
        )
    ).mappings().one()
    if row["versions"] is None or row["orders"] is None or not bool(row["has_plan_json"]):
        raise PlanJournalUnavailableError(
            "the desk plan journal is unavailable or lacks rebalance_versions.plan_json"
        )


async def _current_version(
    session: AsyncSession, basket: CuratedBasket, local_day: dt.date
) -> _VersionContext:
    version = await session.scalar(
        select(BasketVersion)
        .where(BasketVersion.basket_id == basket.id, BasketVersion.effective_date <= local_day)
        .order_by(BasketVersion.version_no.desc())
        .limit(1)
    )
    if version is None:
        raise CuratedPlanNotFoundError("the basket has no effective version")
    return await _context(session, basket, version)


async def _version_by_id(
    session: AsyncSession, basket_id: int, version_id: int
) -> _VersionContext:
    basket = await session.scalar(
        select(CuratedBasket).where(
            CuratedBasket.id == basket_id,
            CuratedBasket.archived_at.is_(None),
            CuratedBasket.type == "STOCK",
        )
    )
    version = await session.scalar(
        select(BasketVersion).where(
            BasketVersion.id == version_id,
            BasketVersion.basket_id == basket_id,
        )
    )
    if basket is None or version is None:
        raise CuratedPlanNotFoundError("the requested basket version does not exist")
    return await _context(session, basket, version)


async def _context(
    session: AsyncSession, basket: CuratedBasket, version: BasketVersion
) -> _VersionContext:
    rows = (
        (
            await session.execute(
                select(BasketConstituent.instrument_id, BasketConstituent.weight, Instrument.symbol)
                .join(Instrument, Instrument.id == BasketConstituent.instrument_id)
                .where(BasketConstituent.version_id == version.id)
                .order_by(BasketConstituent.instrument_id)
            )
        )
        .tuples()
        .all()
    )
    if not rows:
        raise CuratedPlanInputError("the basket version has no constituents")
    return _VersionContext(
        basket=basket,
        version=version,
        targets=tuple(TargetAllocation(instrument_id=i, weight=w) for i, w, _ in rows),
        symbols={i: symbol for i, _, symbol in rows},
    )


async def _active_investment(
    session: AsyncSession, user_id: int, investment_id: int
) -> BasketInvestment:
    investment = await session.scalar(
        select(BasketInvestment)
        .where(
            BasketInvestment.id == investment_id,
            BasketInvestment.user_id == user_id,
            BasketInvestment.status == "ACTIVE",
        )
        .with_for_update()
    )
    if investment is None:
        raise CuratedPlanNotFoundError(f"active investment {investment_id} does not exist")
    return investment


async def _require_no_live_batch(
    session: AsyncSession, investment: BasketInvestment, now: dt.datetime
) -> None:
    batches = (
        (
            await session.execute(
                select(BasketOrderBatch)
                .where(
                    BasketOrderBatch.investment_id == investment.id,
                    BasketOrderBatch.status.in_(("PLANNED", "PARTIAL")),
                )
                .order_by(BasketOrderBatch.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for batch in batches:
        if batch.status == "PLANNED" and now - _aware(batch.created_at) > PLAN_TTL:
            batch.status = "EXPIRED"
            if batch.kind == "REBALANCE" and batch.desk_plan_id is not None:
                version = await _journal_version(session, batch.desk_plan_id)
                payload = _parse_plan_json(version["plan_json"], batch)
                target = _required_int(payload["curated"], "target_version_id")
                state = await session.scalar(
                    select(BasketUserRebalanceState).where(
                        BasketUserRebalanceState.user_id == batch.user_id,
                        BasketUserRebalanceState.version_id == target,
                    )
                )
                if state is not None:
                    state.state = "PENDING"
                    state.decided_at = None
            continue
        raise CuratedPlanConflictError(
            f"investment {investment.id} already has unresolved batch {batch.id}"
        )
    await session.flush()


async def _holdings(session: AsyncSession, investment_id: int) -> tuple[_HoldingState, ...]:
    rows = (
        (
            await session.execute(
                select(BasketInvestmentHolding, Instrument.symbol)
                .join(Instrument, Instrument.id == BasketInvestmentHolding.instrument_id)
                .where(BasketInvestmentHolding.investment_id == investment_id)
                .order_by(BasketInvestmentHolding.instrument_id)
            )
        )
        .all()
    )
    return tuple(
        _HoldingState(
            instrument_id=holding.instrument_id,
            symbol=symbol,
            quantity=holding.qty,
            avg_price=holding.avg_price,
        )
        for holding, symbol in rows
    )


def _symbols_for(
    context: _VersionContext, baseline: Sequence[_HoldingState]
) -> Mapping[int, str]:
    symbols = dict(context.symbols)
    for row in baseline:
        symbols[row.instrument_id] = row.symbol
    return symbols


async def _quotes(
    session: AsyncSession, symbols: Mapping[int, str], pricing_at: dt.datetime
) -> tuple[PriceQuote, ...]:
    ids = tuple(symbols)
    ranked = (
        select(
            OhlcvDaily.instrument_id.label("instrument_id"),
            OhlcvDaily.date.label("date"),
            OhlcvDaily.close_raw.label("price"),
            func.row_number()
            .over(partition_by=OhlcvDaily.instrument_id, order_by=OhlcvDaily.date.desc())
            .label("rank"),
        )
        .where(OhlcvDaily.instrument_id.in_(ids))
        .subquery()
    )
    rows = (
        await session.execute(
            select(ranked.c.instrument_id, ranked.c.date, ranked.c.price).where(
                ranked.c.rank == 1
            )
        )
    ).tuples()
    quotes: list[PriceQuote] = []
    for instrument_id, day, price in rows:
        observed_ist = dt.datetime.combine(day, dt.time(15, 30), tzinfo=IST)
        # A bar for a date after the request is invalid; retain the timestamp so core rejects it.
        quotes.append(
            PriceQuote(
                instrument_id=instrument_id,
                price=price,
                observed_at=observed_ist.astimezone(dt.UTC),
            )
        )
    return tuple(quotes)


def _build_preview(
    builder: PlanBuilder, quotes: Sequence[PriceQuote]
) -> PlanPreview:
    try:
        return builder(quotes)
    except CuratedRebalanceError as exc:
        raise CuratedPlanInputError(str(exc)) from exc


def _require_executable(preview: PlanPreview) -> None:
    if not preview.executable:
        raise CuratedPlanInputError(
            f"plan requires a top-up of {preview.top_up_amount.quantize(MONEY_QUANTUM)}"
        )


def _validate_universe(items: Sequence[object]) -> None:
    if len(items) > MAX_PLAN_INSTRUMENTS:
        raise CuratedPlanInputError(
            f"a plan may contain at most {MAX_PLAN_INSTRUMENTS} unique instruments"
        )


async def _persist_plan(
    session: AsyncSession,
    *,
    user_id: int,
    broker_account_id: str,
    investment: BasketInvestment,
    kind: str,
    preview: PlanPreview,
    context: _VersionContext,
    baseline: Sequence[_HoldingState],
    target_version_id: int,
    now: dt.datetime,
) -> PlanCreatedResult:
    requested = (
        preview.requested_amount
        if preview.requested_amount is not None and preview.requested_amount > 0
        else None
    )
    batch = BasketOrderBatch(
        investment_id=investment.id,
        user_id=user_id,
        broker_account_id=broker_account_id,
        kind=kind,
        requested_amount=requested,
        desk_plan_id=None,
        status="DRAFT",
        fee_entry_id=None,
        executed_at=None,
    )
    session.add(batch)
    await session.flush()

    plan_id = uuid.uuid4().hex
    payload = _desk_payload(
        plan_id=plan_id,
        batch=batch,
        preview=preview,
        context=context,
        baseline=baseline,
        target_version_id=target_version_id,
        now=now,
    )
    orders = cast(list[Mapping[str, object]], payload["orders"])
    symbols = [str(order["symbol"]) for order in orders]
    if len(symbols) != len(set(symbols)):
        raise CuratedPlanInputError("desk plans cannot contain duplicate trading symbols")

    weights = {context.symbols[target.instrument_id]: float(target.weight) for target in context.targets}
    await session.execute(
        text(
            f'insert into "{DESK_SCHEMA}".rebalance_versions '
            "(version_id, created_ts, constituents_json, weights_json, note, evaluation_id, "
            "plan_json) values (:plan_id, :created_ts, :constituents, :weights, :note, null, "
            ":plan_json)"
        ),
        {
            "plan_id": plan_id,
            "created_ts": now.timestamp(),
            "constituents": json.dumps(sorted(symbols), separators=(",", ":")),
            "weights": json.dumps(weights, sort_keys=True, separators=(",", ":")),
            "note": f"Baskfy curated {kind.lower()} · batch {batch.id}",
            "plan_json": json.dumps(payload, sort_keys=True, separators=(",", ":")),
        },
    )
    for order in orders:
        delta = _required_int(order, "delta")
        if delta == 0:
            continue
        await session.execute(
            text(
                f'insert into "{DESK_SCHEMA}".rebalance_orders '
                "(version_id, symbol, side, planned_qty, planned_ref_price, filled_qty, "
                "avg_fill_price, status) values (:plan_id, :symbol, :side, :qty, :price, "
                "0, null, 'PENDING')"
            ),
            {
                "plan_id": plan_id,
                "symbol": str(order["symbol"]),
                "side": "BUY" if delta > 0 else "SELL",
                "qty": abs(delta),
                "price": float(cast(float | int | str, order["ref_price"])),
            },
        )
    batch.desk_plan_id = plan_id
    batch.status = "PLANNED"
    await session.flush()
    return PlanCreatedResult(
        batch_id=batch.id,
        investment_id=investment.id,
        desk_plan_id=plan_id,
        created_at=now,
        expires_at=now + PLAN_TTL,
        preview=preview,
    )


def _desk_payload(
    *,
    plan_id: str,
    batch: BasketOrderBatch,
    preview: PlanPreview,
    context: _VersionContext,
    baseline: Sequence[_HoldingState],
    target_version_id: int,
    now: dt.datetime,
) -> dict[str, object]:
    before = {row.instrument_id: row for row in baseline}
    after = {row.instrument_id: row.quantity for row in preview.resulting_holdings}
    targets = {row.instrument_id: row.weight for row in context.targets}
    symbols = _symbols_for(context, baseline)
    orders: list[dict[str, object]] = []
    for line in preview.lines:
        qty_now = before.get(
            line.instrument_id,
            _HoldingState(line.instrument_id, symbols[line.instrument_id], Decimal("0"), Decimal("0")),
        ).quantity
        qty_final = after.get(line.instrument_id, Decimal("0"))
        delta = line.quantity if line.side.value == "BUY" else -line.quantity
        orders.append(
            {
                "symbol": symbols[line.instrument_id],
                "instrument_id": line.instrument_id,
                "action": "BUY" if delta > 0 else "SELL",
                "qty_now": int(qty_now),
                "delta": delta,
                "qty_final": int(qty_final),
                "ref_price": float(line.price),
                "value": float(qty_final * line.price),
                "weight": float(targets.get(line.instrument_id, Decimal("0"))),
                "stop": None,
                "pledged": 0,
                "rank": None,
                "score": None,
                "note": None,
            }
        )
    baseline_payload = [
        {
            "instrument_id": row.instrument_id,
            "symbol": row.symbol,
            "quantity": str(row.quantity),
            "avg_price": str(row.avg_price),
        }
        for row in baseline
    ]
    current_value = preview.current_value
    requested_amount = preview.requested_amount or Decimal("0")
    capital = current_value + requested_amount
    return {
        "plan_id": plan_id,
        "created_at": now.timestamp(),
        "capital": float(capital),
        "book_value": float(current_value),
        "cash": float(requested_amount),
        "buys_value": float(preview.buy_cost),
        "sells_value": float(preview.sell_proceeds),
        "net_cash_use": float(preview.buy_cost - preview.sell_proceeds),
        "breadth_above_20dma": None,
        "cash_target_pct": None,
        "pledged_sells_margin_note": [],
        "cluster_warnings": [],
        "excluded": [],
        "unpriced": [],
        "funding": {
            "checked": False,
            "reason": "product intent only; desk console must recheck broker margin",
        },
        "orders": orders,
        "curated": {
            "schema": PLAN_SCHEMA,
            "batch_id": batch.id,
            "investment_id": batch.investment_id,
            "target_version_id": target_version_id,
            "operation": batch.kind,
            "baseline": baseline_payload,
            "instruments": {symbol: instrument_id for instrument_id, symbol in symbols.items()},
            "residual_cash": str(preview.residual_cash),
            "top_up_amount": str(preview.top_up_amount),
        },
    }


async def _journal_version(session: AsyncSession, plan_id: str) -> RowMapping:
    row = (
        (
            await session.execute(
                text(
                    f'select version_id, created_ts, plan_json from "{DESK_SCHEMA}".'
                    "rebalance_versions where version_id = :plan_id"
                ),
                {"plan_id": plan_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise PlanJournalUnavailableError(f"desk plan {plan_id!r} is missing")
    return row


async def _journal_plan(
    session: AsyncSession, plan_id: str
) -> tuple[RowMapping, tuple[RowMapping, ...]]:
    version = await _journal_version(session, plan_id)
    orders = tuple(
        (
            (
                await session.execute(
                    text(
                        f"select symbol, side, planned_qty, planned_ref_price, filled_qty, "
                        f"avg_fill_price, status from \"{DESK_SCHEMA}\".rebalance_orders "
                        "where version_id = :plan_id order by symbol"
                    ),
                    {"plan_id": plan_id},
                )
            )
            .mappings()
            .all()
        )
    )
    return version, orders


def _parse_plan_json(raw: object, batch: BasketOrderBatch) -> Mapping[str, object]:
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise PlanJournalUnavailableError(f"desk plan {batch.desk_plan_id!r} has invalid JSON") from exc
    if not isinstance(payload, dict):
        raise PlanJournalUnavailableError("desk plan payload must be a JSON object")
    curated = payload.get("curated")
    if not isinstance(curated, dict) or curated.get("schema") != PLAN_SCHEMA:
        raise PlanJournalUnavailableError("desk plan lacks the curated-plan reconciliation envelope")
    if _required_int(curated, "batch_id") != batch.id:
        raise PlanJournalUnavailableError("desk plan points at a different product batch")
    if _required_int(curated, "investment_id") != batch.investment_id:
        raise PlanJournalUnavailableError("desk plan points at a different investment")
    return cast(Mapping[str, object], payload)


async def _apply_confirmed_fills(
    session: AsyncSession,
    batch: BasketOrderBatch,
    payload: Mapping[str, object],
    orders: Sequence[RowMapping],
    now: dt.datetime,
) -> tuple[bool, int, int]:
    curated = _mapping(payload["curated"], "curated")
    raw_baseline = curated.get("baseline")
    if not isinstance(raw_baseline, list):
        raise PlanJournalUnavailableError("curated plan baseline is missing")
    baseline: dict[str, _HoldingState] = {}
    for raw in raw_baseline:
        item = _mapping(raw, "baseline item")
        symbol = _required_str(item, "symbol")
        if symbol in baseline:
            raise PlanJournalUnavailableError(f"duplicate baseline symbol {symbol!r}")
        baseline[symbol] = _HoldingState(
            instrument_id=_required_int(item, "instrument_id"),
            symbol=symbol,
            quantity=_decimal(item.get("quantity"), "baseline quantity"),
            avg_price=_decimal(item.get("avg_price"), "baseline average price"),
        )

    plan_orders = payload.get("orders")
    if not isinstance(plan_orders, list):
        raise PlanJournalUnavailableError("curated plan orders are missing")
    intent: dict[str, Mapping[str, object]] = {}
    for raw in plan_orders:
        item = _mapping(raw, "plan order")
        symbol = _required_str(item, "symbol")
        if symbol in intent:
            raise PlanJournalUnavailableError(f"duplicate plan symbol {symbol!r}")
        intent[symbol] = item
    if len(orders) != len(intent):
        raise PlanJournalUnavailableError("desk order rows do not match the persisted plan")

    desired = dict(baseline)
    confirmed_lines = 0
    confirmed_quantity = 0
    for row in orders:
        symbol = str(row["symbol"])
        plan_order = intent.get(symbol)
        if plan_order is None:
            raise PlanJournalUnavailableError(f"unplanned desk order symbol {symbol!r}")
        planned = int(row["planned_qty"] or 0)
        expected = abs(_required_int(plan_order, "delta"))
        if planned != expected or str(row["side"]).upper() not in {"BUY", "SELL"}:
            raise PlanJournalUnavailableError(f"desk order {symbol!r} no longer matches intent")
        status = str(row["status"] or "PENDING").upper()
        filled = int(row["filled_qty"] or 0)
        if filled < 0 or filled > planned:
            raise PlanJournalUnavailableError(f"desk order {symbol!r} has impossible filled quantity")
        if status not in CONFIRMED_FILL_STATUSES or filled == 0:
            continue
        confirmed_lines += 1
        confirmed_quantity += filled
        instrument_id = _required_int(plan_order, "instrument_id") if "instrument_id" in plan_order else None
        if instrument_id is None:
            # Version 1 stores instrument identity in baseline for existing symbols and derives it
            # from the target context for new buys via the curated ``instruments`` map below.
            instruments = _mapping(curated.get("instruments"), "curated instruments")
            instrument_id = _required_int(instruments, symbol)
        base = desired.get(
            symbol,
            _HoldingState(instrument_id, symbol, Decimal("0"), Decimal("0")),
        )
        side = str(row["side"]).upper()
        if side == "BUY":
            fill_price = _decimal(row["avg_fill_price"], f"{symbol} average fill price")
            if fill_price <= 0:
                raise PlanJournalUnavailableError(f"confirmed buy {symbol!r} has no fill price")
            quantity = base.quantity + Decimal(filled)
            average = (
                (base.quantity * base.avg_price + Decimal(filled) * fill_price) / quantity
            ).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)
        else:
            quantity = base.quantity - Decimal(filled)
            if quantity < 0:
                raise PlanJournalUnavailableError(f"confirmed sell {symbol!r} exceeds baseline")
            average = base.avg_price if quantity > 0 else Decimal("0")
        desired[symbol] = _HoldingState(
            instrument_id=instrument_id,
            symbol=symbol,
            quantity=quantity.quantize(QUANTITY_QUANTUM),
            avg_price=average,
        )

    current = {
        row.instrument_id: row
        for row in (
            (
                await session.execute(
                    select(BasketInvestmentHolding).where(
                        BasketInvestmentHolding.investment_id == batch.investment_id
                    )
                )
            )
            .scalars()
            .all()
        )
    }
    changed = False
    wanted_ids = {row.instrument_id for row in desired.values()}
    for row in desired.values():
        existing = current.get(row.instrument_id)
        if row.quantity == 0:
            if existing is not None:
                await session.delete(existing)
                changed = True
            continue
        if existing is None:
            session.add(
                BasketInvestmentHolding(
                    investment_id=batch.investment_id,
                    instrument_id=row.instrument_id,
                    qty=row.quantity,
                    avg_price=row.avg_price,
                    updated_at=now,
                )
            )
            changed = True
        elif existing.qty != row.quantity or existing.avg_price != row.avg_price:
            existing.qty = row.quantity
            existing.avg_price = row.avg_price
            existing.updated_at = now
            changed = True
    for instrument_id, existing in current.items():
        if instrument_id not in wanted_ids:
            await session.delete(existing)
            changed = True
    return changed, confirmed_lines, confirmed_quantity


def _batch_status(orders: Sequence[RowMapping]) -> str:
    if not orders:
        return "EXECUTED"
    complete = True
    any_fill = False
    all_terminal_no_fill = True
    for row in orders:
        planned = int(row["planned_qty"] or 0)
        filled = int(row["filled_qty"] or 0)
        status = str(row["status"] or "PENDING").upper()
        confirmed = status in CONFIRMED_FILL_STATUSES
        any_fill = any_fill or (confirmed and filled > 0)
        complete = complete and confirmed and filled == planned
        all_terminal_no_fill = all_terminal_no_fill and status in TERMINAL_NO_FILL_STATUSES
    if complete:
        return "EXECUTED"
    if any_fill:
        return "PARTIAL"
    if all_terminal_no_fill:
        return "CANCELLED"
    return "PLANNED"


async def _finalize_investment(
    session: AsyncSession,
    batch: BasketOrderBatch,
    payload: Mapping[str, object],
    now: dt.datetime,
) -> None:
    investment = await session.scalar(
        select(BasketInvestment)
        .where(BasketInvestment.id == batch.investment_id)
        .with_for_update()
    )
    if investment is None:
        raise PlanJournalUnavailableError(f"investment {batch.investment_id} disappeared")
    curated = _mapping(payload["curated"], "curated")
    target_version_id = _required_int(curated, "target_version_id")
    if batch.kind == "REBALANCE":
        investment.version_applied_id = target_version_id
        state = await session.scalar(
            select(BasketUserRebalanceState).where(
                BasketUserRebalanceState.user_id == batch.user_id,
                BasketUserRebalanceState.version_id == target_version_id,
            )
        )
        if state is None:
            raise PlanJournalUnavailableError("rebalance state disappeared before journal fill")
        state.state = "APPLIED"
        state.decided_at = now
        actions = (
            (
                await session.execute(
                    select(BasketPendingAction).where(
                        BasketPendingAction.user_id == batch.user_id,
                        BasketPendingAction.type == "REBALANCE_AVAILABLE",
                        BasketPendingAction.resolved_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for action in actions:
            if action.payload.get("version_id") == target_version_id:
                action.resolved_at = now
    if batch.kind in {"BUY", "INVEST_MORE"}:
        investment.last_invested_at = now
    if batch.kind == "EXIT":
        investment.status = "EXITED"
        investment.exited_at = now


async def _retire_empty_initial_shell(
    session: AsyncSession, batch: BasketOrderBatch, now: dt.datetime
) -> None:
    if batch.kind != "BUY":
        return
    count = await session.scalar(
        select(func.count())
        .select_from(BasketInvestmentHolding)
        .where(
            BasketInvestmentHolding.investment_id == batch.investment_id,
            BasketInvestmentHolding.qty > 0,
        )
    )
    if int(count or 0) != 0:
        return
    investment = await session.scalar(
        select(BasketInvestment).where(BasketInvestment.id == batch.investment_id).with_for_update()
    )
    if investment is not None:
        investment.status = "EXITED"
        investment.exited_at = now


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise PlanJournalUnavailableError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _required_int(value: Mapping[str, object], key: str) -> int:
    raw = value.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise PlanJournalUnavailableError(f"desk plan field {key!r} must be an integer")
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise PlanJournalUnavailableError(f"desk plan field {key!r} must be an integer") from exc
    return parsed


def _required_str(value: Mapping[str, object], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw:
        raise PlanJournalUnavailableError(f"desk plan field {key!r} must be text")
    return raw


def _decimal(value: object, label: str) -> Decimal:
    if value is None or isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise PlanJournalUnavailableError(f"{label} must be numeric")
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise PlanJournalUnavailableError(f"{label} must be numeric") from exc
    if not parsed.is_finite():
        raise PlanJournalUnavailableError(f"{label} must be finite")
    return parsed


def _aware(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CuratedPlanInputError("plan time must be timezone-aware")
    return value.astimezone(dt.UTC)
