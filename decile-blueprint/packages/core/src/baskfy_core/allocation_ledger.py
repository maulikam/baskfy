"""The allocation ledger — `PORTFOLIO_REDESIGN.md` §4 and §5.2, as pure arithmetic.

The spec's one-sentence summary is that Baskfy's core is *an allocation ledger that reconciles
real broker holdings into user-defined, independently measurable portfolio groups*, and that the
page is thin once the ledger exists. This module is that ledger's rules. It is the first thing
§10 asks for and it is deliberately the least glamorous.

**Prices, dates and holdings arrive as arguments.** Nothing here reaches a broker, a database, a
clock or a file (law 1). A ledger that could read the time would be a ledger you could not test
at a chosen instant, and every rule below is a rule *about a moment*.

WHAT THIS IS NOT
----------------
Not a valuation service and not a sync. It never fetches a quote, never decides that a sell
happened, and never writes a row. It answers, given a picture someone else assembled: does this
picture balance, what may it be called, and what does this event do to it.

Not an order path either. Nothing here names a side, a product, a venue or an order type, and
`test_allocation_ledger.py` scans the source for that vocabulary so a later edit that quietly
crosses the line fails a test rather than a review (the same guard `portfolio_units` carries).

THE ONE RULE EVERYTHING ELSE HANGS OFF
--------------------------------------
A holding belongs to **exactly one** capital portfolio, or to Unallocated (§4.1, §4.2). Whole
holdings only in v1. That single constraint is what makes sell attribution automatic (§4.3) and
what makes the totals add up (criterion 1) — and it is why partial-quantity allocation is Phase 3
rather than a nice-to-have: split a holding across two portfolios and a sell has no owner.

A *holding* here is the physical position: `(instrument_id, broker_account_id)`. Not the
instrument. The same stock at two brokers is two holdings which the UI displays aggregated
(§6.7); the ledger keeps them apart because they can be allocated apart and sold apart.

MONITORING VIEWS ARE OUTSIDE THE ARITHMETIC
-------------------------------------------
A monitoring view is a lens ("all defence stocks"), it overlaps freely, and it is excluded from
every total (§4.1). That exclusion is enforced by :func:`consolidated_value` refusing a
monitoring portfolio outright rather than by a filter someone can forget to apply, because a
silently double-counted net worth is the worst number this product can print.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final

from baskfy_core.gst import money

__all__ = [
    "QUANTITY_PRECISION",
    "UNALLOCATED",
    "Allocation",
    "CorporateAction",
    "CorporateActionKind",
    "DetectedSell",
    "Holding",
    "HoldingKey",
    "MetricKind",
    "Portfolio",
    "PortfolioKind",
    "PortfolioSource",
    "ReconciliationItem",
    "ReconciliationReason",
    "ReturnFigure",
    "SellAttribution",
    "allocation_of",
    "apply_corporate_action",
    "attribute_sell",
    "consolidated_value",
    "cost_basis",
    "displayable_figures",
    "headline_metric",
    "holding_value",
    "model_figure",
    "portfolio_value",
    "portfolio_values",
    "unallocated_holdings",
    "validate_allocations",
]

#: Quantities carry four decimals. A bonus or split ratio need not divide evenly, and the
#: remainder has to live somewhere visible rather than being lost to an int cast.
QUANTITY_PRECISION: Final = Decimal("0.0001")

#: The sentinel for "this holding is in no capital portfolio". Not a portfolio id — Unallocated is
#: not a portfolio and giving it one would let it be renamed, deleted or given a benchmark.
UNALLOCATED: Final = None


class PortfolioKind(StrEnum):
    """§4.1. The two kinds behave differently in *arithmetic*, not merely in styling."""

    #: Sums into consolidated net worth. A holding belongs to at most one of these.
    CAPITAL = "CAPITAL"
    #: An overlapping lens. Never enters a total.
    MONITORING = "MONITORING"


class PortfolioSource(StrEnum):
    """§3. Shown as a badge everywhere, and it decides the headline metric (§5.2)."""

    SUBSCRIBED = "SUBSCRIBED"
    MY_SCREEN = "MY_SCREEN"
    MY_STRATEGY = "MY_STRATEGY"
    HOLDING_GROUP = "HOLDING_GROUP"


class MetricKind(StrEnum):
    """What a return number actually *is*. Criterion 3: never an unlabelled column."""

    TWR_SINCE_SUBSCRIBED = "TWR_SINCE_SUBSCRIBED"
    TWR_SINCE_GO_LIVE = "TWR_SINCE_GO_LIVE"
    TWR_SINCE_CREATED = "TWR_SINCE_CREATED"
    SINCE_GROUPED = "SINCE_GROUPED"
    #: The money-weighted return a holding group earns once a CAS import supplies its purchase
    #: history (§5.2, §5.3). Distinct from ``SINCE_GROUPED`` on purpose: that one is measured
    #: from the day we first saw the shares, this one from the day they were bought, and printing
    #: the second under the first's label is exactly the unlabelled column criterion 3 forbids.
    XIRR_SINCE_PURCHASE = "XIRR_SINCE_PURCHASE"
    #: The consolidated cash-flow-adjusted figure §5.2 requires at the top level, shown beside
    #: TWR as two labelled numbers rather than blended into one.
    XIRR_CONSOLIDATED = "XIRR_CONSOLIDATED"

    @property
    def label(self) -> str:
        """The words a user reads. §5.2's column is labelled, always."""
        return {
            MetricKind.TWR_SINCE_SUBSCRIBED: "TWR since you subscribed",
            MetricKind.TWR_SINCE_GO_LIVE: "TWR since go-live",
            MetricKind.TWR_SINCE_CREATED: "TWR since created",
            MetricKind.SINCE_GROUPED: "Since grouped",
            MetricKind.XIRR_SINCE_PURCHASE: "XIRR since purchase",
            MetricKind.XIRR_CONSOLIDATED: "XIRR",
        }[self]


class ReconciliationReason(StrEnum):
    """§4.3. Why a change could not be attributed on its own."""

    UNALLOCATED_HOLDING = "UNALLOCATED_HOLDING"
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    UNKNOWN_INFLOW = "UNKNOWN_INFLOW"

    @property
    def question(self) -> str:
        """The sentence the inbox asks. §4.3 wants a question, not an error code."""
        return {
            ReconciliationReason.UNALLOCATED_HOLDING: (
                "This holding was not in any portfolio — which portfolio should it count against?"
            ),
            ReconciliationReason.QUANTITY_MISMATCH: (
                "The quantity sold is more than we had recorded — which portfolio?"
            ),
            ReconciliationReason.UNKNOWN_INFLOW: (
                "We saw shares arrive that we cannot account for — which portfolio?"
            ),
        }[self]


class CorporateActionKind(StrEnum):
    """§4.5. Both change quantity and average price; neither is a P&L event."""

    SPLIT = "SPLIT"
    BONUS = "BONUS"


@dataclass(frozen=True, slots=True)
class HoldingKey:
    """The physical position: one instrument in one broker account.

    Frozen and hashable because it is the ledger's key everywhere — the thing that is allocated,
    sold and adjusted. `instrument_id` alone would merge two brokers' positions into one and make
    "sell 100 at Zerodha" unattributable.
    """

    instrument_id: int
    broker_account_id: int


@dataclass(frozen=True, slots=True)
class Holding:
    """A quantity of one instrument in one broker account, at a stated average price.

    ``avg_price`` is ``None`` when the buy history is not known — a holding synced from a broker
    that does not publish it, before a CAS import (§5.3). That is not a zero, and the difference
    matters: §5.2 forbids showing since-purchase P&L for such a holding.
    """

    key: HoldingKey
    quantity: Decimal
    avg_price: Decimal | None = None

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise ValueError(f"quantity cannot be negative; got {self.quantity}")
        if self.avg_price is not None and self.avg_price < 0:
            raise ValueError(f"avg_price cannot be negative; got {self.avg_price}")


@dataclass(frozen=True, slots=True)
class Portfolio:
    """§3 and §4.1. Kind decides the arithmetic; source decides the metric."""

    portfolio_id: int
    name: str
    kind: PortfolioKind
    source: PortfolioSource
    #: When this grouping began. It is the start date every metric in §5.2 is measured from, and
    #: the date the "since grouped" mark is taken at for a holding group.
    started_on: dt.date


@dataclass(frozen=True, slots=True)
class Allocation:
    """Which capital portfolio a holding counts against, or Unallocated.

    ``portfolio_id`` is ``None`` for Unallocated. A monitoring view never appears here: membership
    of a lens is not an allocation, which is the whole reason lenses may overlap.
    """

    key: HoldingKey
    portfolio_id: int | None


@dataclass(frozen=True, slots=True)
class DetectedSell:
    """What a sync noticed: this position got smaller. It cannot know why (§4.3)."""

    key: HoldingKey
    quantity: Decimal

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"a sell must reduce a position; got {self.quantity}")


@dataclass(frozen=True, slots=True)
class ReconciliationItem:
    """An unanswered question about a detected change.

    Its existence is what stops a guess. §4.3: an unresolved item freezes that holding's
    contribution to performance rather than attributing it to a plausible portfolio.
    """

    key: HoldingKey
    quantity: Decimal
    reason: ReconciliationReason
    #: The portfolio the UI should pre-select. A suggestion, never an attribution.
    suggested_portfolio_id: int | None = None

    @property
    def question(self) -> str:
        return self.reason.question


@dataclass(frozen=True, slots=True)
class SellAttribution:
    """The result of :func:`attribute_sell` — exactly one of two outcomes, never both.

    Criterion 4 has no third case, so this type has no third field. ``portfolio_id`` set means it
    was attributed silently; ``item`` set means a human must answer first.
    """

    portfolio_id: int | None = None
    item: ReconciliationItem | None = None

    def __post_init__(self) -> None:
        if (self.portfolio_id is None) == (self.item is None):
            raise ValueError(
                "a sell is either attributed to a portfolio or raises exactly one "
                "reconciliation item; it is never both and never neither"
            )

    @property
    def attributed(self) -> bool:
        return self.item is None


@dataclass(frozen=True, slots=True)
class CorporateAction:
    """§4.5. ``from_ratio``/``to_ratio`` follow docs/04's convention: a 1:10 split is 1 -> 10.

    A 4:1 bonus (four new shares for every one held) is expressed as the resulting multiple:
    ``from_ratio=1, to_ratio=5`` — one share becomes five. The module does not guess which
    convention a caller meant; it multiplies quantity by ``to_ratio / from_ratio``.
    """

    key: HoldingKey
    kind: CorporateActionKind
    from_ratio: Decimal
    to_ratio: Decimal

    def __post_init__(self) -> None:
        if self.from_ratio <= 0 or self.to_ratio <= 0:
            raise ValueError("corporate-action ratios must both be positive")


@dataclass(frozen=True, slots=True)
class ReturnFigure:
    """A return number that cannot be shown without saying what it is.

    Criterion 3 asks every displayed return to carry its kind and start date. Criterion 5 asks
    that model and actual are never one figure. Both are enforced here rather than by convention:
    the value is inseparable from its label, and :attr:`is_model` is part of the type, so a
    caller adding a model figure to an actual one is adding two ``ReturnFigure`` objects — which
    this class does not support. There is deliberately no ``__add__``.

    ``value`` is ``None`` for "we cannot compute this honestly", and ``unavailable_reason`` says
    why in words a user can read. A holding group without transaction history is the case that
    matters (§5.2): it shows "since grouped" and refuses XIRR, rather than printing a plausible
    number derived from an average price nobody supplied.
    """

    kind: MetricKind
    since: dt.date
    value: Decimal | None = None
    #: True for a publisher's model track record; False for what this user actually experienced.
    is_model: bool = False
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if self.value is None and not self.unavailable_reason:
            raise ValueError(
                "a figure with no value must say why, or a caller will render an empty cell "
                "with no way for the user to know whether it is zero or unknown"
            )
        if self.value is not None and self.unavailable_reason:
            raise ValueError("a figure cannot both have a value and be unavailable")

    @property
    def label(self) -> str:
        """What the column header says. Model figures are never labelled as the user's own."""
        return f"Model {self.kind.label.lower()}" if self.is_model else self.kind.label

    @property
    def displayable(self) -> bool:
        return self.value is not None


# ---------------------------------------------------------------------------
# Allocation — criterion 2
# ---------------------------------------------------------------------------


def _allocation_index(allocations: Sequence[Allocation]) -> dict[HoldingKey, int | None]:
    """``{holding -> capital portfolio}``, and the place criterion 2 is actually enforced.

    Every function that needs to know where a holding sits builds this index, so the duplicate
    check is unavoidable rather than something the totals path happens to do. That matters more
    than it looks: an earlier shape validated only inside :func:`consolidated_value`, which meant
    :func:`portfolio_value` would happily return a number for an allocation set the consolidated
    figure refused — the parts and the whole disagreeing about whether the data was even legal.

    It is also the difference between O(holdings x allocations) and O(holdings + allocations).
    §6.5 draws a row per portfolio, so the quadratic version was quadratic *per page render*.
    """
    index: dict[HoldingKey, int | None] = {}
    for allocation in allocations:
        if allocation.portfolio_id is UNALLOCATED:
            continue
        previous = index.get(allocation.key)
        if previous is not None:
            raise ValueError(
                f"holding {allocation.key} is allocated to both portfolio {previous} and "
                f"portfolio {allocation.portfolio_id}; a holding belongs to exactly one capital "
                "portfolio (spec section 4.2, acceptance criterion 2)"
            )
        index[allocation.key] = allocation.portfolio_id
    return index


def validate_allocations(
    allocations: Sequence[Allocation],
    portfolios: Mapping[int, Portfolio],
) -> None:
    """Refuse an allocation set that breaks §4.1 or §4.2. Raises, never repairs.

    Three ways to break it, and all three are a caller bug rather than a user mistake:

    * the same holding allocated twice — criterion 2's headline case, caught by the index;
    * a holding allocated to a monitoring view — lenses are not allocations, and permitting it
      would put an overlapping portfolio into the totals;
    * a holding allocated to a portfolio that does not exist.

    Raising rather than dropping the bad row is deliberate. A ledger that silently discards an
    allocation still adds up, which is precisely how a wrong net worth would survive review.
    """
    for portfolio_id in _allocation_index(allocations).values():
        portfolio = portfolios.get(portfolio_id) if portfolio_id is not None else None
        if portfolio is None:
            raise ValueError(f"allocation names portfolio {portfolio_id}, which does not exist")
        if portfolio.kind is PortfolioKind.MONITORING:
            raise ValueError(
                f"{portfolio.name!r} is a monitoring view, and a monitoring view holds no "
                "allocation: it overlaps other portfolios by design and is excluded from every "
                "total (spec section 4.1)"
            )


def allocation_of(allocations: Sequence[Allocation], key: HoldingKey) -> int | None:
    """The capital portfolio a holding counts against, or ``None`` for Unallocated.

    The single-holding convenience. Anything walking a list should build the index once instead.
    """
    return _allocation_index(allocations).get(key, UNALLOCATED)


def unallocated_holdings(
    holdings: Sequence[Holding], allocations: Sequence[Allocation]
) -> list[Holding]:
    """Everything in no capital portfolio. §6.6 makes this the centerpiece, not a footer.

    A holding with no allocation row at all is unallocated — absence is the default, so a newly
    synced broker account lands entirely here and the product has something to help sort.
    """
    index = _allocation_index(allocations)
    return [h for h in holdings if index.get(h.key, UNALLOCATED) is UNALLOCATED]


# ---------------------------------------------------------------------------
# Valuation — criterion 1
# ---------------------------------------------------------------------------


def holding_value(holding: Holding, prices: Mapping[int, Decimal]) -> Decimal:
    """Quantity times the instrument's price, quantised to paise.

    A missing price raises. The alternative — treating it as zero — produces a net worth that is
    quietly too low and still balances, which is the failure criterion 1 exists to catch.
    """
    price = prices.get(holding.key.instrument_id)
    if price is None:
        raise KeyError(
            f"no price for instrument {holding.key.instrument_id}; a missing price must be "
            "resolved by the caller, because valuing it at zero produces a total that is wrong "
            "and still adds up"
        )
    return money(holding.quantity * price)


def portfolio_value(
    portfolio_id: int | None,
    holdings: Sequence[Holding],
    allocations: Sequence[Allocation],
    prices: Mapping[int, Decimal],
) -> Decimal:
    """The market value allocated to one capital portfolio, or to Unallocated when ``None``.

    Summed per holding *after* each is quantised, so this figure equals the sum of the rows a
    user can see on the holdings tab. Quantising the total instead would produce a headline that
    disagrees with its own breakdown by a paisa, and users notice exactly that.
    """
    index = _allocation_index(allocations)
    total = Decimal("0")
    for holding in holdings:
        if index.get(holding.key, UNALLOCATED) == portfolio_id:
            total += holding_value(holding, prices)
    return money(total)


def portfolio_values(
    holdings: Sequence[Holding],
    allocations: Sequence[Allocation],
    portfolios: Mapping[int, Portfolio],
    prices: Mapping[int, Decimal],
) -> dict[int | None, Decimal]:
    """Every capital portfolio's value plus Unallocated's, in one pass over the holdings.

    This is what §6.5's table actually needs — a row per portfolio — and calling
    :func:`portfolio_value` once per row would re-walk every holding for every row. Portfolios
    with nothing allocated are present with a zero rather than absent, so a caller rendering the
    table does not have to decide whether a missing key means zero or means the portfolio is
    gone.

    Validates first, so a table can never be drawn from an allocation set the consolidated total
    would reject.
    """
    validate_allocations(allocations, portfolios)
    index = _allocation_index(allocations)
    values: dict[int | None, Decimal] = {UNALLOCATED: Decimal("0")}
    for portfolio in portfolios.values():
        if portfolio.kind is PortfolioKind.CAPITAL:
            values[portfolio.portfolio_id] = Decimal("0")
    for holding in holdings:
        target = index.get(holding.key, UNALLOCATED)
        values[target] = values.get(target, Decimal("0")) + holding_value(holding, prices)
    return {key: money(value) for key, value in values.items()}


def consolidated_value(
    holdings: Sequence[Holding],
    allocations: Sequence[Allocation],
    portfolios: Mapping[int, Portfolio],
    prices: Mapping[int, Decimal],
    *,
    cash: Decimal = Decimal("0"),
) -> Decimal:
    """Consolidated net worth: every capital portfolio, plus Unallocated, plus cash.

    **Criterion 1 is a property of this function**: because a holding has at most one allocation
    (enforced first, by :func:`validate_allocations`) and every holding is either allocated or
    unallocated, summing the parts and summing the whole are the same walk over the same list.
    They cannot drift, which is stronger than asserting equality after the fact.

    Monitoring views contribute nothing and are not consulted — they hold no allocations at all,
    so there is no filter here to forget. ``portfolios`` is taken so the allocation set can be
    validated against it, which is the only reason it is a parameter.
    """
    validate_allocations(allocations, portfolios)
    total = cash
    for holding in holdings:
        total += holding_value(holding, prices)
    return money(total)


# ---------------------------------------------------------------------------
# Sell attribution — criterion 4
# ---------------------------------------------------------------------------


def attribute_sell(
    sell: DetectedSell,
    holdings: Sequence[Holding],
    allocations: Sequence[Allocation],
) -> SellAttribution:
    """Decide which portfolio a detected sell belongs to, or ask (§4.3).

    Whole-holding allocation is what makes the common case free: the holding has one capital
    portfolio, so the sell has one owner and is applied silently. Everything else becomes a
    question, and the type makes "guess quietly" unrepresentable.

    The three questions, in the order they can occur:

    * the holding is not in any portfolio -> ``UNALLOCATED_HOLDING``;
    * more was sold than we recorded -> ``QUANTITY_MISMATCH``, which usually means a trade we
      never saw, and attributing the excess to the portfolio we do know would corrupt its return
      series with volume it never held;
    * we have no record of the holding at all -> ``UNKNOWN_INFLOW``.
    """
    held = next((h for h in holdings if h.key == sell.key), None)
    if held is None:
        return SellAttribution(
            item=ReconciliationItem(
                key=sell.key,
                quantity=sell.quantity,
                reason=ReconciliationReason.UNKNOWN_INFLOW,
            )
        )

    portfolio_id = _allocation_index(allocations).get(sell.key, UNALLOCATED)
    if sell.quantity > held.quantity:
        return SellAttribution(
            item=ReconciliationItem(
                key=sell.key,
                quantity=sell.quantity,
                reason=ReconciliationReason.QUANTITY_MISMATCH,
                suggested_portfolio_id=portfolio_id,
            )
        )
    if portfolio_id is UNALLOCATED:
        return SellAttribution(
            item=ReconciliationItem(
                key=sell.key,
                quantity=sell.quantity,
                reason=ReconciliationReason.UNALLOCATED_HOLDING,
            )
        )
    return SellAttribution(portfolio_id=portfolio_id)


# ---------------------------------------------------------------------------
# Corporate actions — criterion 6
# ---------------------------------------------------------------------------


def apply_corporate_action(holding: Holding, action: CorporateAction) -> Holding:
    """Adjust quantity and average price so that **cost basis is exactly unchanged**.

    Criterion 6: a split or bonus produces zero P&L. The way to guarantee that is not to compute
    a new average price and hope it rounds kindly — it is to hold ``quantity * avg_price``
    constant by construction, dividing the *original* cost by the *new* quantity.

    Worked, because the rounding is the whole difficulty: 100 shares at 333.33 is a cost of
    33,333.00. A 1:3 split gives 300 shares. Scaling the price by 3 gives 111.11, and
    300 * 111.11 = 33,333.00 — but a ratio that does not divide evenly would not land, and the
    residue would surface as P&L on a day the user did nothing. Recomputing from the preserved
    cost keeps the identity exact for any ratio; the test asserts it on 1:3, which is the case
    that catches the naive version.

    An unknown ``avg_price`` stays unknown. Inventing one here would silently unlock a
    since-purchase P&L that §5.2 forbids for exactly that holding.
    """
    multiple = action.to_ratio / action.from_ratio
    new_quantity = (holding.quantity * multiple).quantize(
        QUANTITY_PRECISION, rounding=ROUND_HALF_UP
    )
    if new_quantity <= 0:
        raise ValueError(
            f"corporate action would reduce {holding.key} to zero quantity; "
            "a split or bonus never removes a position"
        )
    if holding.avg_price is None:
        return replace(holding, quantity=new_quantity)

    original_cost = holding.quantity * holding.avg_price
    return replace(
        holding,
        quantity=new_quantity,
        avg_price=original_cost / new_quantity,
    )


def cost_basis(holding: Holding) -> Decimal | None:
    """``quantity * avg_price``, or ``None`` when the buy price is unknown.

    The quantity that :func:`apply_corporate_action` holds invariant, and therefore the thing a
    test asserts against rather than comparing two average prices for approximate equality.
    """
    if holding.avg_price is None:
        return None
    return holding.quantity * holding.avg_price


# ---------------------------------------------------------------------------
# Metric selection — §5.2, criteria 3 and 5
# ---------------------------------------------------------------------------

#: §5.2's table, as data. One source, one headline metric, no unlabelled column anywhere.
_HEADLINE_BY_SOURCE: Final[Mapping[PortfolioSource, MetricKind]] = {
    PortfolioSource.SUBSCRIBED: MetricKind.TWR_SINCE_SUBSCRIBED,
    PortfolioSource.MY_STRATEGY: MetricKind.TWR_SINCE_GO_LIVE,
    PortfolioSource.MY_SCREEN: MetricKind.TWR_SINCE_CREATED,
    PortfolioSource.HOLDING_GROUP: MetricKind.SINCE_GROUPED,
}


def headline_metric(
    portfolio: Portfolio,
    value: Decimal | None,
    *,
    has_transaction_history: bool = False,
) -> ReturnFigure:
    """The one return number a portfolio row shows, with its label and start date (§5.2).

    ``has_transaction_history`` is the CAS-import switch (§5.3) and only a holding group consults
    it. Before an import, a holding group has EOD marks from its grouping date and nothing
    earlier, so "since grouped" is the *only* honest number — and if the caller cannot even supply
    that, the figure comes back unavailable with the reason, never as a zero.

    The returned figure is always the user's actual experience: ``is_model`` is False here and
    there is no parameter to change it. A publisher's track record is built by
    :func:`model_figure` and the two are separate objects, which is how criterion 5 is enforced
    structurally rather than by remembering.
    """
    kind = _HEADLINE_BY_SOURCE[portfolio.source]
    if value is None:
        return ReturnFigure(
            kind=kind,
            since=portfolio.started_on,
            unavailable_reason=(
                "No valuation for this date yet"
                if has_transaction_history or portfolio.source is not PortfolioSource.HOLDING_GROUP
                else "Import your CAS to see returns from your purchase dates"
            ),
        )
    return ReturnFigure(kind=kind, since=portfolio.started_on, value=value)


def model_figure(portfolio: Portfolio, value: Decimal | None) -> ReturnFigure:
    """The publisher's own track record, labelled as theirs and never blended (§5.2).

    Only a subscribed portfolio has one — asking for a model figure on a portfolio the user built
    themselves is a caller bug, not an empty state, because there is no publisher whose record it
    could be.
    """
    if portfolio.source is not PortfolioSource.SUBSCRIBED:
        raise ValueError(
            f"{portfolio.name!r} has source {portfolio.source}; only a subscribed portfolio has a "
            "publisher model, and blending a model figure into a user's own returns is forbidden "
            "(acceptance criterion 5)"
        )
    if value is None:
        return ReturnFigure(
            kind=MetricKind.TWR_SINCE_SUBSCRIBED,
            since=portfolio.started_on,
            is_model=True,
            unavailable_reason="Publisher has not reported for this period",
        )
    return ReturnFigure(
        kind=MetricKind.TWR_SINCE_SUBSCRIBED,
        since=portfolio.started_on,
        value=value,
        is_model=True,
    )


def displayable_figures(figures: Iterable[ReturnFigure]) -> list[ReturnFigure]:
    """The subset a row can actually render. Order preserved; nothing is combined."""
    return [figure for figure in figures if figure.displayable]
