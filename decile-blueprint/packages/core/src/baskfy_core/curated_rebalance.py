"""Exact, pure plan arithmetic and NSE session eligibility for curated baskets.

The product layer creates *intent* only.  These functions know nothing about repositories,
journals, brokers, HTTP, or execution.  Every price and quantity is explicit, every financial
value is a :class:`~decimal.Decimal`, and every time decision receives both the instant and the
stored trading sessions from its caller.

Plan arithmetic deliberately uses whole stock shares.  ``cb_investment_holding.qty`` has four
decimal places so the schema can remain instrument-agnostic, but the STOCK-only SC run must never
silently truncate a fractional holding into a sell order.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from zoneinfo import ZoneInfo

from baskfy_core.curated_metrics import (
    WeightedPrice,
    minimum_investment_amount,
    whole_share_allocation,
)

EXACT_TOTAL_WEIGHT = Decimal("1.0000")
WEIGHT_QUANTUM = Decimal("0.0001")
NSE_ZONE_NAME = "Asia/Kolkata"
NSE_ZONE = ZoneInfo(NSE_ZONE_NAME)
NSE_OPEN = dt.time(9, 15)
NSE_CLOSE = dt.time(15, 30)


class CuratedRebalanceError(ValueError):
    """An input cannot produce a safe and truthful product plan."""


class CuratedPlanKind(StrEnum):
    """The five SC3 operations, named exactly like ``cb_order_batch.kind``."""

    BUY = "BUY"
    INVEST_MORE = "INVEST_MORE"
    REBALANCE = "REBALANCE"
    EXIT = "EXIT"
    PARTIAL_EXIT = "PARTIAL_EXIT"


class OrderSide(StrEnum):
    """A desk-compatible child-order direction."""

    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class PriceQuote:
    """A positive price observed at an explicit aware instant."""

    instrument_id: int
    price: Decimal
    observed_at: dt.datetime


@dataclass(frozen=True, slots=True)
class TargetAllocation:
    """One instrument's exact four-decimal target weight."""

    instrument_id: int
    weight: Decimal


@dataclass(frozen=True, slots=True)
class IntendedHolding:
    """One intended whole-share holding from the product ledger."""

    instrument_id: int
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class QuoteFreshness:
    """The explicit reference instant and maximum admissible quote age."""

    pricing_at: dt.datetime
    maximum_quote_age: dt.timedelta


@dataclass(frozen=True, slots=True)
class PlanLine:
    """A whole-share child order shaped for the desk journal."""

    instrument_id: int
    side: OrderSide
    quantity: int
    price: Decimal
    notional: Decimal


@dataclass(frozen=True, slots=True)
class ResultingHolding:
    """The intended position after every line fills; never an optimistic mutation."""

    instrument_id: int
    quantity: int


@dataclass(frozen=True, slots=True)
class PlanPreview:
    """An immutable, cash-reconciled product plan preview.

    ``residual_cash`` is cash outside the resulting holdings after the proposed fills.
    ``top_up_amount`` is additional cash still needed before the plan is executable.
    ``unallocated_amount`` is used by amount-based partial exits: whole shares can release less
    than requested, and that exact shortfall must be visible rather than rounded away.
    """

    kind: CuratedPlanKind
    lines: tuple[PlanLine, ...]
    resulting_holdings: tuple[ResultingHolding, ...]
    requested_amount: Decimal | None
    minimum_amount: Decimal | None
    current_value: Decimal
    sell_proceeds: Decimal
    buy_cost: Decimal
    residual_cash: Decimal
    top_up_amount: Decimal
    unallocated_amount: Decimal
    executable: bool


@dataclass(frozen=True, slots=True)
class MarketSessionDecision:
    """Whether an explicit instant is inside NSE hours and, if closed, the next opening."""

    is_open: bool
    observed_at_ist: dt.datetime
    next_open_at: dt.datetime | None


def _finite_decimal(value: Decimal, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise CuratedRebalanceError(f"{field} must be a finite Decimal")
    return value


def _positive_decimal(value: Decimal, field: str) -> Decimal:
    result = _finite_decimal(value, field)
    if result <= 0:
        raise CuratedRebalanceError(f"{field} must be greater than zero")
    return result


def _nonnegative_decimal(value: Decimal, field: str) -> Decimal:
    result = _finite_decimal(value, field)
    if result < 0:
        raise CuratedRebalanceError(f"{field} must be non-negative")
    return result


def _positive_instrument_id(value: int) -> int:
    if type(value) is not int or value <= 0:
        raise CuratedRebalanceError("instrument_id must be a positive integer")
    return value


def _aware_instant(value: dt.datetime, field: str) -> dt.datetime:
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CuratedRebalanceError(f"{field} must be a timezone-aware datetime")
    return value


def _calculation_precision(values: Iterable[Decimal], *, extra_integer_digits: int = 0) -> int:
    """A context large enough for exact arithmetic over schema-scale Decimal inputs."""
    rows = tuple(values)
    digit_count = sum(max(1, len(value.as_tuple().digits)) for value in rows)
    return max(64, digit_count + extra_integer_digits + len(rows) + 8)


def _decimal_sum(values: Iterable[Decimal]) -> Decimal:
    rows = tuple(values)
    with localcontext() as context:
        context.prec = _calculation_precision(rows)
        return sum(rows, start=Decimal(0))


def _notional(price: Decimal, quantity: int) -> Decimal:
    with localcontext() as context:
        context.prec = _calculation_precision((price,), extra_integer_digits=len(str(quantity)))
        return price * quantity


def _floor_ratio(numerator_factors: Sequence[Decimal | int], denominator: Decimal) -> int:
    """Floor a product divided by a positive Decimal without context rounding."""
    top = 1
    bottom = 1
    for factor in numerator_factors:
        value = Decimal(factor) if type(factor) is int else factor
        factor_top, factor_bottom = value.as_integer_ratio()
        top *= factor_top
        bottom *= factor_bottom
    denominator_top, denominator_bottom = denominator.as_integer_ratio()
    return (top * denominator_bottom) // (bottom * denominator_top)


def _validated_targets(targets: Sequence[TargetAllocation]) -> tuple[TargetAllocation, ...]:
    rows = tuple(targets)
    if not rows:
        raise CuratedRebalanceError("at least one target allocation is required")
    seen: set[int] = set()
    total_weight = Decimal(0)
    for row in rows:
        if not isinstance(row, TargetAllocation):
            raise CuratedRebalanceError("every target must be a TargetAllocation")
        instrument_id = _positive_instrument_id(row.instrument_id)
        if instrument_id in seen:
            raise CuratedRebalanceError(f"duplicate target instrument_id: {instrument_id}")
        seen.add(instrument_id)
        weight = _positive_decimal(row.weight, "target weight")
        if weight > 1:
            raise CuratedRebalanceError("target weight must be at most one")
        if weight.quantize(WEIGHT_QUANTUM) != weight:
            raise CuratedRebalanceError("target weight must have at most four decimal places")
        total_weight += weight
    if total_weight != EXACT_TOTAL_WEIGHT:
        raise CuratedRebalanceError(
            f"target weights must total exactly {EXACT_TOTAL_WEIGHT}; got {total_weight}"
        )
    return tuple(sorted(rows, key=lambda row: row.instrument_id))


def _validated_holdings(holdings: Sequence[IntendedHolding]) -> tuple[IntendedHolding, ...]:
    rows = tuple(holdings)
    if not rows:
        raise CuratedRebalanceError("at least one intended holding is required")
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, IntendedHolding):
            raise CuratedRebalanceError("every holding must be an IntendedHolding")
        instrument_id = _positive_instrument_id(row.instrument_id)
        if instrument_id in seen:
            raise CuratedRebalanceError(f"duplicate holding instrument_id: {instrument_id}")
        seen.add(instrument_id)
        quantity = _positive_decimal(row.quantity, "holding quantity")
        if quantity != quantity.to_integral_value():
            raise CuratedRebalanceError(
                f"holding quantity for instrument_id {instrument_id} must be whole shares"
            )
    return tuple(sorted(rows, key=lambda row: row.instrument_id))


def _validated_quote_map(
    quotes: Sequence[PriceQuote],
    required_instrument_ids: frozenset[int],
    quote_freshness: QuoteFreshness,
) -> Mapping[int, PriceQuote]:
    if not isinstance(quote_freshness, QuoteFreshness):
        raise CuratedRebalanceError("quote_freshness must be a QuoteFreshness")
    instant = _aware_instant(quote_freshness.pricing_at, "pricing_at")
    maximum_quote_age = quote_freshness.maximum_quote_age
    if not isinstance(maximum_quote_age, dt.timedelta) or maximum_quote_age < dt.timedelta(0):
        raise CuratedRebalanceError("maximum_quote_age must be a non-negative timedelta")
    rows = tuple(quotes)
    seen: dict[int, PriceQuote] = {}
    for row in rows:
        if not isinstance(row, PriceQuote):
            raise CuratedRebalanceError("every quote must be a PriceQuote")
        instrument_id = _positive_instrument_id(row.instrument_id)
        if instrument_id in seen:
            raise CuratedRebalanceError(f"duplicate quote instrument_id: {instrument_id}")
        _positive_decimal(row.price, "quote price")
        observed_at = _aware_instant(row.observed_at, "quote observed_at")
        age = instant.astimezone(dt.UTC) - observed_at.astimezone(dt.UTC)
        if age < dt.timedelta(0):
            raise CuratedRebalanceError(f"future quote for instrument_id {instrument_id}")
        if age > maximum_quote_age:
            raise CuratedRebalanceError(f"stale quote for instrument_id {instrument_id}")
        seen[instrument_id] = row
    observed_ids = frozenset(seen)
    missing = sorted(required_instrument_ids - observed_ids)
    if missing:
        raise CuratedRebalanceError(f"missing quotes for instrument_ids: {missing}")
    unexpected = sorted(observed_ids - required_instrument_ids)
    if unexpected:
        raise CuratedRebalanceError(f"unexpected quotes for instrument_ids: {unexpected}")
    return seen


def _weighted_prices(
    targets: Sequence[TargetAllocation], quote_map: Mapping[int, PriceQuote]
) -> tuple[WeightedPrice, ...]:
    return tuple(
        WeightedPrice(row.instrument_id, row.weight, quote_map[row.instrument_id].price)
        for row in targets
    )


def _investment_plan(
    kind: CuratedPlanKind,
    requested_amount: Decimal,
    targets: Sequence[TargetAllocation],
    quotes: Sequence[PriceQuote],
    quote_freshness: QuoteFreshness,
) -> PlanPreview:
    amount = _positive_decimal(requested_amount, "requested_amount")
    target_rows = _validated_targets(targets)
    target_ids = frozenset(row.instrument_id for row in target_rows)
    quote_map = _validated_quote_map(
        quotes,
        target_ids,
        quote_freshness,
    )
    weighted_prices = _weighted_prices(target_rows, quote_map)
    minimum = minimum_investment_amount(weighted_prices)
    if amount < minimum:
        return PlanPreview(
            kind=kind,
            lines=(),
            resulting_holdings=(),
            requested_amount=amount,
            minimum_amount=minimum,
            current_value=Decimal(0),
            sell_proceeds=Decimal(0),
            buy_cost=Decimal(0),
            residual_cash=amount,
            top_up_amount=minimum - amount,
            unallocated_amount=Decimal(0),
            executable=False,
        )

    allocation = whole_share_allocation(amount, weighted_prices)
    lines = tuple(
        PlanLine(
            instrument_id=row.instrument_id,
            side=OrderSide.BUY,
            quantity=row.shares,
            price=row.raw_price,
            notional=_notional(row.raw_price, row.shares),
        )
        for row in allocation
    )
    buy_cost = _decimal_sum(row.notional for row in lines)
    return PlanPreview(
        kind=kind,
        lines=lines,
        resulting_holdings=tuple(
            ResultingHolding(row.instrument_id, row.shares) for row in allocation
        ),
        requested_amount=amount,
        minimum_amount=minimum,
        current_value=Decimal(0),
        sell_proceeds=Decimal(0),
        buy_cost=buy_cost,
        residual_cash=amount - buy_cost,
        top_up_amount=Decimal(0),
        unallocated_amount=Decimal(0),
        executable=True,
    )


def plan_invest(
    requested_amount: Decimal,
    targets: Sequence[TargetAllocation],
    quotes: Sequence[PriceQuote],
    quote_freshness: QuoteFreshness,
) -> PlanPreview:
    """Allocate an initial investment or disclose the exact minimum top-up required."""
    return _investment_plan(
        CuratedPlanKind.BUY,
        requested_amount,
        targets,
        quotes,
        quote_freshness,
    )


def plan_invest_more(
    requested_amount: Decimal,
    targets: Sequence[TargetAllocation],
    quotes: Sequence[PriceQuote],
    quote_freshness: QuoteFreshness,
) -> PlanPreview:
    """Allocate a new contribution to the current target version independently."""
    return _investment_plan(
        CuratedPlanKind.INVEST_MORE,
        requested_amount,
        targets,
        quotes,
        quote_freshness,
    )


def plan_apply_rebalance(
    holdings: Sequence[IntendedHolding],
    targets: Sequence[TargetAllocation],
    quotes: Sequence[PriceQuote],
    *,
    cash_contribution: Decimal,
    quote_freshness: QuoteFreshness,
) -> PlanPreview:
    """Diff current intended shares—not a prior version—against new target shares.

    The marked value of current intended holdings plus ``cash_contribution`` is allocated across
    the new version.  When the marked holding value is below the new version's minimum, the
    preview stays non-executable until the exact missing contribution is supplied.
    """
    holding_rows = _validated_holdings(holdings)
    target_rows = _validated_targets(targets)
    contribution = _nonnegative_decimal(cash_contribution, "cash_contribution")
    required_ids = frozenset(
        {row.instrument_id for row in holding_rows} | {row.instrument_id for row in target_rows}
    )
    quote_map = _validated_quote_map(
        quotes,
        required_ids,
        quote_freshness,
    )
    current_quantities = {row.instrument_id: int(row.quantity) for row in holding_rows}
    current_value = _decimal_sum(
        _notional(quote_map[row.instrument_id].price, int(row.quantity)) for row in holding_rows
    )
    weighted_prices = _weighted_prices(target_rows, quote_map)
    minimum = minimum_investment_amount(weighted_prices)
    required_contribution = max(Decimal(0), minimum - current_value)
    if contribution < required_contribution:
        return PlanPreview(
            kind=CuratedPlanKind.REBALANCE,
            lines=(),
            resulting_holdings=tuple(
                ResultingHolding(row.instrument_id, int(row.quantity)) for row in holding_rows
            ),
            requested_amount=contribution,
            minimum_amount=minimum,
            current_value=current_value,
            sell_proceeds=Decimal(0),
            buy_cost=Decimal(0),
            residual_cash=contribution,
            top_up_amount=required_contribution - contribution,
            unallocated_amount=Decimal(0),
            executable=False,
        )

    capital = current_value + contribution
    target_quantities = {
        row.instrument_id: row.shares for row in whole_share_allocation(capital, weighted_prices)
    }
    lines: list[PlanLine] = []
    for instrument_id in sorted(required_ids):
        current_quantity = current_quantities.get(instrument_id, 0)
        target_quantity = target_quantities.get(instrument_id, 0)
        delta = target_quantity - current_quantity
        if delta == 0:
            continue
        price = quote_map[instrument_id].price
        side = OrderSide.BUY if delta > 0 else OrderSide.SELL
        quantity = abs(delta)
        lines.append(
            PlanLine(
                instrument_id=instrument_id,
                side=side,
                quantity=quantity,
                price=price,
                notional=_notional(price, quantity),
            )
        )
    sell_proceeds = _decimal_sum(row.notional for row in lines if row.side is OrderSide.SELL)
    buy_cost = _decimal_sum(row.notional for row in lines if row.side is OrderSide.BUY)
    residual_cash = sell_proceeds + contribution - buy_cost
    if residual_cash < 0:
        raise CuratedRebalanceError("rebalance plan violates cash conservation")
    return PlanPreview(
        kind=CuratedPlanKind.REBALANCE,
        lines=tuple(lines),
        resulting_holdings=tuple(
            ResultingHolding(instrument_id, target_quantities[instrument_id])
            for instrument_id in sorted(target_quantities)
        ),
        requested_amount=contribution,
        minimum_amount=minimum,
        current_value=current_value,
        sell_proceeds=sell_proceeds,
        buy_cost=buy_cost,
        residual_cash=residual_cash,
        top_up_amount=Decimal(0),
        unallocated_amount=Decimal(0),
        executable=True,
    )


def plan_full_exit(
    holdings: Sequence[IntendedHolding],
    quotes: Sequence[PriceQuote],
    quote_freshness: QuoteFreshness,
) -> PlanPreview:
    """Sell every intended whole share, preserving the exact proceeds."""
    holding_rows = _validated_holdings(holdings)
    required_ids = frozenset(row.instrument_id for row in holding_rows)
    quote_map = _validated_quote_map(
        quotes,
        required_ids,
        quote_freshness,
    )
    lines = tuple(
        PlanLine(
            instrument_id=row.instrument_id,
            side=OrderSide.SELL,
            quantity=int(row.quantity),
            price=quote_map[row.instrument_id].price,
            notional=_notional(quote_map[row.instrument_id].price, int(row.quantity)),
        )
        for row in holding_rows
    )
    proceeds = _decimal_sum(row.notional for row in lines)
    return PlanPreview(
        kind=CuratedPlanKind.EXIT,
        lines=lines,
        resulting_holdings=(),
        requested_amount=None,
        minimum_amount=None,
        current_value=proceeds,
        sell_proceeds=proceeds,
        buy_cost=Decimal(0),
        residual_cash=proceeds,
        top_up_amount=Decimal(0),
        unallocated_amount=Decimal(0),
        executable=True,
    )


def plan_partial_exit(
    holdings: Sequence[IntendedHolding],
    quotes: Sequence[PriceQuote],
    requested_amount: Decimal,
    quote_freshness: QuoteFreshness,
) -> PlanPreview:
    """Release up to an amount by selling each intended position proportionally.

    Each ideal quantity is independently floored, making the result deterministic and ensuring
    proceeds never exceed the requested amount.  A request too small to sell one whole share is
    rejected instead of emitting an empty or misleading desk plan.
    """
    amount = _positive_decimal(requested_amount, "requested_amount")
    holding_rows = _validated_holdings(holdings)
    required_ids = frozenset(row.instrument_id for row in holding_rows)
    quote_map = _validated_quote_map(
        quotes,
        required_ids,
        quote_freshness,
    )
    current_value = _decimal_sum(
        _notional(quote_map[row.instrument_id].price, int(row.quantity)) for row in holding_rows
    )
    if amount >= current_value:
        raise CuratedRebalanceError(
            "partial-exit requested_amount must be below current value; use full exit"
        )

    lines: list[PlanLine] = []
    resulting: list[ResultingHolding] = []
    for row in holding_rows:
        owned = int(row.quantity)
        sell_quantity = _floor_ratio((owned, amount), current_value)
        if sell_quantity > owned:
            raise CuratedRebalanceError("partial-exit quantity exceeds intended holding")
        retained = owned - sell_quantity
        if sell_quantity:
            price = quote_map[row.instrument_id].price
            lines.append(
                PlanLine(
                    instrument_id=row.instrument_id,
                    side=OrderSide.SELL,
                    quantity=sell_quantity,
                    price=price,
                    notional=_notional(price, sell_quantity),
                )
            )
        if retained:
            resulting.append(ResultingHolding(row.instrument_id, retained))
    if not lines:
        raise CuratedRebalanceError(
            "partial-exit requested_amount is too small to sell one whole share"
        )
    proceeds = _decimal_sum(row.notional for row in lines)
    if proceeds > amount:
        raise CuratedRebalanceError("partial-exit proceeds exceed requested amount")
    return PlanPreview(
        kind=CuratedPlanKind.PARTIAL_EXIT,
        lines=tuple(lines),
        resulting_holdings=tuple(resulting),
        requested_amount=amount,
        minimum_amount=None,
        current_value=current_value,
        sell_proceeds=proceeds,
        buy_cost=Decimal(0),
        residual_cash=proceeds,
        top_up_amount=Decimal(0),
        unallocated_amount=amount - proceeds,
        executable=True,
    )


def _validated_sessions(trading_sessions: Sequence[dt.date]) -> tuple[dt.date, ...]:
    sessions = tuple(trading_sessions)
    if not sessions:
        raise CuratedRebalanceError("at least one stored trading session is required")
    previous: dt.date | None = None
    for session in sessions:
        if type(session) is not dt.date:
            raise CuratedRebalanceError("trading sessions must be dates, not datetimes")
        if previous is not None and session <= previous:
            raise CuratedRebalanceError("trading sessions must be unique and strictly increasing")
        previous = session
    return sessions


def market_session_at(
    instant: dt.datetime, trading_sessions: Sequence[dt.date]
) -> MarketSessionDecision:
    """Classify an aware instant using only the caller's stored NSE trading sessions.

    09:15 IST is open; 15:30 IST is closed.  A pre-open request on a trading date points to that
    date's opening.  At/after close, on a holiday, or on a weekend, the next strictly later stored
    session is returned.  Missing future calendar coverage fails closed.
    """
    aware = _aware_instant(instant, "instant")
    sessions = _validated_sessions(trading_sessions)
    observed = aware.astimezone(NSE_ZONE)
    local_date = observed.date()
    local_time = observed.timetz().replace(tzinfo=None)
    session_set = frozenset(sessions)
    if local_date in session_set and NSE_OPEN <= local_time < NSE_CLOSE:
        return MarketSessionDecision(True, observed, None)

    next_session: dt.date | None
    if local_date in session_set and local_time < NSE_OPEN:
        next_session = local_date
    else:
        next_session = next((session for session in sessions if session > local_date), None)
    if next_session is None:
        raise CuratedRebalanceError("stored trading calendar has no future NSE session")
    next_open = dt.datetime.combine(next_session, NSE_OPEN, tzinfo=NSE_ZONE)
    return MarketSessionDecision(False, observed, next_open)
