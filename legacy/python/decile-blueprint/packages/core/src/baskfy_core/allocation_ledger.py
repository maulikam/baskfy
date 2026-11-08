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
**The slices of a holding never add up to more than the holding.** Formally, for every physical
position, ``sum(allocated quantities) <= held quantity``, and the difference is Unallocated.

This replaced a stricter rule on 10 Sep 2026, and the old wording is worth keeping because it
explains the shape of everything below. It read:

    "A holding belongs to exactly one capital portfolio, or to Unallocated. Whole holdings only
    in v1. That single constraint is what makes sell attribution automatic and what makes the
    totals add up — and it is why partial-quantity allocation is Phase 3 rather than a
    nice-to-have: split a holding across two portfolios and a sell has no owner."

Maulik asked for Phase 3 directly: *"one stock can appear in multiple portfolios, so if stock a
bought 100 qty for shortterm 20 for long term 34 for some swing 36 for momentum"*. He is right
that this is what the product is for — a person does not buy ITC once, they buy it four times for
four reasons — and the old rule made the product unable to say so.

**What the old rule was protecting, and how each part is protected now:**

*Criterion 1, the totals add up.* Previously true because each holding had one owner. Now true
because the slices partition the holding: :func:`validate_against_holdings` refuses any set whose
slices exceed what is held, so parts + remainder = whole by construction rather than by luck.
Over-allocation is a **hard error**, not a warning, for the same reason a missing price is: a net
worth that is too high and still balances is the worst number this product can print.

*Criterion 4, sell attribution.* Previously free. Now free **only when the holding has exactly one
slice** — which is the old whole-holding case, so nothing that works today starts asking
questions. A sell out of a split holding raises :attr:`ReconciliationReason.SPLIT_HOLDING` with a
pro-rata suggestion attached, and attributes nothing until a human answers. That is Maulik's
choice, taken on 10 Sep 2026 over silent pro-rata: *"ask me, pre-filled pro-rata"*. Silent
pro-rata was rejected because selling the swing lot would quietly move all four portfolios'
returns and nothing would say so.

*Rounding.* A split has to be exact. :func:`pro_rata_split` apportions by largest remainder, so
the parts sum to the whole for any ratio; and :func:`scale_allocations` gives a corporate action's
residue to Unallocated rather than to whichever slice rounded up last.

A *holding* here is the physical position: `(instrument_id, broker_account_id)`. Not the
instrument. The same stock at two brokers is two holdings which the UI displays aggregated
(§6.7); the ledger keeps them apart because they can be allocated apart and sold apart.

MONITORING LENSES ARE STILL WHOLE-HOLDING, DELIBERATELY
-------------------------------------------------------
Only capital slices carry a quantity. A lens ("all defence stocks") answers a question about
*which names* you hold, not how many of them belong to it, and it enters no total — so giving it
a quantity would add a number nobody could use and one more thing to keep summing correctly.

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
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
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
    "allocated_quantity",
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
    "pro_rata_split",
    "scale_allocations",
    "slices_of",
    "unallocated_holdings",
    "unallocated_quantity",
    "validate_against_holdings",
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
    #: 10 Sep 2026. The holding is split across capital portfolios, so a sell has more than one
    #: possible owner and the ledger will not pick. The item carries `suggested_split` — a
    #: pro-rata pre-fill — so answering is one click when pro-rata is what happened.
    SPLIT_HOLDING = "SPLIT_HOLDING"

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
            ReconciliationReason.SPLIT_HOLDING: (
                "You hold this name in more than one portfolio — which of them did you sell from?"
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
    """**A quantity** of one holding counted against one capital portfolio.

    A monitoring view never appears here: membership of a lens is not an allocation, which is the
    whole reason lenses may overlap.

    ``portfolio_id`` is not nullable, and that is the change of 10 Sep 2026. It used to be
    ``int | None`` with ``None`` meaning Unallocated, which made sense when a holding had exactly
    one allocation. With slices it does not: Unallocated is now *the remainder*
    (``held - sum(slices)``), a quantity that is computed rather than stored, so a row asserting
    it could disagree with the arithmetic. :func:`unallocated_quantity` is the only way to ask.

    ``quantity`` is the number of shares in this slice, always positive. A zero slice is not "no
    shares here", it is a row that should not exist — and permitting it would let two callers
    disagree about whether a portfolio holds a name at all.
    """

    key: HoldingKey
    portfolio_id: int
    quantity: Decimal

    def __post_init__(self) -> None:
        if self.portfolio_id is None:
            raise ValueError(
                "an allocation names a capital portfolio; Unallocated is the remainder, not a "
                "row — ask unallocated_quantity() for it"
            )
        if self.quantity <= 0:
            raise ValueError(
                f"an allocation must be a positive quantity; got {self.quantity}. Remove the row "
                "instead of writing a zero one"
            )


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
    #: ``((portfolio_id, quantity), ...)`` the UI should pre-fill when the holding is split, from
    #: :func:`pro_rata_split`. Empty for a single-owner holding, where `suggested_portfolio_id`
    #: says everything. It is a SUGGESTION with the same force as the field above: the sell is
    #: not attributed, the figures stay frozen, and a human is still the one who decides.
    suggested_split: tuple[tuple[int, Decimal], ...] = ()

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


def _slice_index(allocations: Sequence[Allocation]) -> dict[HoldingKey, dict[int, Decimal]]:
    """``{holding -> {capital portfolio -> quantity}}``, and where a duplicate slice is caught.

    Every function that needs to know how a holding is divided builds this index, so the
    duplicate check is unavoidable rather than something the totals path happens to do. That
    matters more than it looks: an earlier shape validated only inside :func:`consolidated_value`,
    which meant :func:`portfolio_value` would happily return a number for an allocation set the
    consolidated figure refused — the parts and the whole disagreeing about whether the data was
    even legal.

    A repeated ``(holding, portfolio)`` pair is refused rather than summed. Two rows saying "40 of
    ITC in Momentum" almost always means one write happened twice, and adding them produces 80
    shares the user does not own — silently, and in a way that still balances against itself.

    It is also the difference between O(holdings x allocations) and O(holdings + allocations).
    §6.5 draws a row per portfolio, so the quadratic version was quadratic *per page render*.
    """
    index: dict[HoldingKey, dict[int, Decimal]] = {}
    for allocation in allocations:
        slices = index.setdefault(allocation.key, {})
        if allocation.portfolio_id in slices:
            raise ValueError(
                f"holding {allocation.key} has two allocations to portfolio "
                f"{allocation.portfolio_id} ({slices[allocation.portfolio_id]} and "
                f"{allocation.quantity}); a holding has at most one slice per portfolio, and "
                "summing them would credit shares nobody owns"
            )
        slices[allocation.portfolio_id] = allocation.quantity
    return index


def validate_allocations(
    allocations: Sequence[Allocation],
    portfolios: Mapping[int, Portfolio],
) -> None:
    """Refuse an allocation set that breaks §4.1 or §4.2. Raises, never repairs.

    What this checks is everything decidable **without knowing the holdings**:

    * the same ``(holding, portfolio)`` pair twice — caught by the index;
    * a slice in a monitoring view — lenses are not allocations, and permitting it would put an
      overlapping portfolio into the totals;
    * a slice in a portfolio that does not exist.

    What it deliberately does **not** check is over-allocation, because that is a fact about the
    holdings and this function is not given them. :func:`validate_against_holdings` is that check,
    and every valuation path calls both.

    Raising rather than dropping the bad row is deliberate. A ledger that silently discards an
    allocation still adds up, which is precisely how a wrong net worth would survive review.
    """
    for slices in _slice_index(allocations).values():
        for portfolio_id in slices:
            portfolio = portfolios.get(portfolio_id)
            if portfolio is None:
                raise ValueError(f"allocation names portfolio {portfolio_id}, which does not exist")
            if portfolio.kind is PortfolioKind.MONITORING:
                raise ValueError(
                    f"{portfolio.name!r} is a monitoring view, and a monitoring view holds no "
                    "allocation: it overlaps other portfolios by design and is excluded from "
                    "every total (spec section 4.1)"
                )


def validate_against_holdings(
    holdings: Sequence[Holding],
    allocations: Sequence[Allocation],
) -> None:
    """The Phase-3 invariant: no holding's slices may exceed what is held. Raises.

    This is the check that took over criterion 1's job when whole-holding allocation ended. It
    has to be separate from :func:`validate_allocations` because it needs the holdings, and a lot
    of callers legitimately have allocations in hand before they have quantities.

    Two failure shapes, and the message names the numbers because the user can act on them:

    * the slices sum to more than the position — the caller is about to print a net worth that is
      too high **and internally consistent**, which is the one thing this module exists to stop;
    * a slice against a holding that is not in the list at all — usually a position that was sold
      to zero while an allocation row survived it.

    A shortfall is not an error: that is Unallocated, and it is the normal state of a freshly
    connected broker account.
    """
    held = {holding.key: holding.quantity for holding in holdings}
    for key, slices in _slice_index(allocations).items():
        if key not in held:
            raise ValueError(
                f"allocations name holding {key}, which is not held at all; a slice of a position "
                "that no longer exists cannot be valued"
            )
        allocated = sum(slices.values(), Decimal("0"))
        if allocated > held[key]:
            raise ValueError(
                f"holding {key} is over-allocated: {allocated} shares are filed across "
                f"{len(slices)} portfolio(s) but only {held[key]} are held. Free "
                f"{allocated - held[key]} before this can be valued"
            )


def slices_of(allocations: Sequence[Allocation], key: HoldingKey) -> dict[int, Decimal]:
    """``{capital portfolio -> quantity}`` for one holding; empty when wholly unallocated.

    The single-holding convenience. Anything walking a list should build the index once instead.
    """
    return _slice_index(allocations).get(key, {})


def allocated_quantity(allocations: Sequence[Allocation], key: HoldingKey) -> Decimal:
    """How many of this holding's shares are filed somewhere. Zero when none are."""
    return sum(slices_of(allocations, key).values(), Decimal("0"))


def unallocated_quantity(holding: Holding, allocations: Sequence[Allocation]) -> Decimal:
    """``held - sum(slices)``, never negative. **The only way to ask about Unallocated.**

    Computed rather than stored, which is why :class:`Allocation` no longer has a nullable
    ``portfolio_id``: a stored row could disagree with the arithmetic, and then two surfaces would
    print two different remainders from the same data.

    Clamped at zero rather than returning a negative, because an over-allocated holding is a hard
    error that :func:`validate_against_holdings` raises — a caller that skipped the check should
    not be handed a negative quantity that silently reduces a total.
    """
    remainder = holding.quantity - allocated_quantity(allocations, holding.key)
    return remainder if remainder > 0 else Decimal("0")


def unallocated_holdings(
    holdings: Sequence[Holding], allocations: Sequence[Allocation]
) -> list[Holding]:
    """The **unfiled remainder** of every holding. §6.6 makes this the centerpiece, not a footer.

    Each returned holding carries the remaining quantity, not the full position: a 100-share ITC
    filed 20/34/36 comes back as 10. Holdings with nothing left over are absent entirely, so the
    section empties as the user sorts — which is the behaviour §6.6 is describing when it calls
    getting from 40 unallocated holdings to 4 named portfolios "activation".

    A holding with no allocation at all comes back whole, because absence is the default and a
    newly synced broker account lands here in its entirety.
    """
    index = _slice_index(allocations)
    remainders: list[Holding] = []
    for holding in holdings:
        filed = sum(index.get(holding.key, {}).values(), Decimal("0"))
        remaining = holding.quantity - filed
        if remaining > 0:
            remainders.append(replace(holding, quantity=remaining))
    return remainders


def pro_rata_split(quantity: Decimal, weights: Mapping[int, Decimal]) -> dict[int, Decimal]:
    """Divide ``quantity`` across ``weights`` so the parts sum to the whole **exactly**.

    Largest remainder, not naive rounding. Selling 30 of a 100-share holding filed 20/34/36/10
    gives 6 / 10.2 / 10.8 / 3 — which happens to land, but 30 of a 7/7/7 split does not, and the
    naive version loses or invents shares there. Every leftover unit of
    :data:`QUANTITY_PRECISION` goes to the largest fractional remainder, ties broken by portfolio
    id so the same input always produces the same answer.

    Exactness is not fussiness here. This function's output is a *suggestion* the user accepts in
    one click, and a suggestion that does not add up to what they sold is worse than none: they
    would accept it, and the ledger would then hold a position that disagrees with the broker's.
    """
    if quantity <= 0:
        raise ValueError(f"cannot split a non-positive quantity; got {quantity}")
    total_weight = sum(weights.values(), Decimal("0"))
    if total_weight <= 0:
        raise ValueError("cannot split across weights that sum to zero")

    exact = {pid: quantity * weight / total_weight for pid, weight in weights.items()}
    floors = {
        pid: value.quantize(QUANTITY_PRECISION, rounding=ROUND_DOWN) for pid, value in exact.items()
    }
    shortfall = quantity - sum(floors.values(), Decimal("0"))
    # Hand out the residue one unit at a time, biggest fractional part first. Sorting by
    # (-remainder, portfolio_id) makes it deterministic: the same pile always splits the same way,
    # which is what lets a test assert an answer rather than a tolerance.
    order = sorted(exact, key=lambda pid: (-(exact[pid] - floors[pid]), pid))
    units = int(shortfall / QUANTITY_PRECISION)
    for i in range(units):
        pid = order[i % len(order)]
        floors[pid] += QUANTITY_PRECISION
    return floors


def scale_allocations(
    allocations: Sequence[Allocation], key: HoldingKey, multiple: Decimal
) -> list[Allocation]:
    """Scale one holding's slices by a corporate action's multiple (§4.5).

    Rounds each slice **down** and lets the residue fall to Unallocated rather than giving it to
    whichever slice rounded up last. That direction is chosen deliberately: a slightly larger
    Unallocated is a visible prompt the user can act on, whereas a slightly larger slice is an
    invisible share credited to a portfolio that never earned it — and over enough splits, to the
    same portfolio every time.

    Slices of other holdings pass through untouched, so this can be applied to the whole set.
    """
    if multiple <= 0:
        raise ValueError(f"a corporate-action multiple must be positive; got {multiple}")
    scaled: list[Allocation] = []
    for allocation in allocations:
        if allocation.key != key:
            scaled.append(allocation)
            continue
        quantity = (allocation.quantity * multiple).quantize(
            QUANTITY_PRECISION, rounding=ROUND_DOWN
        )
        # A slice that rounds away entirely is dropped, not written as zero: `Allocation` refuses
        # a zero quantity, and a reverse split deep enough to erase a slice has genuinely erased
        # it. The shares it stood for are still in the holding, and land in Unallocated.
        if quantity > 0:
            scaled.append(replace(allocation, quantity=quantity))
    return scaled


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
    """The market value of one capital portfolio's **slices**, or of Unallocated when ``None``.

    Summed per holding *after* each slice is quantised, so this figure equals the sum of the rows
    a user can see on the holdings tab. Quantising the total instead would produce a headline
    that disagrees with its own breakdown by a paisa, and users notice exactly that.
    """
    index = _slice_index(allocations)
    total = Decimal("0")
    for holding in holdings:
        slices = index.get(holding.key, {})
        if portfolio_id is UNALLOCATED:
            remaining = holding.quantity - sum(slices.values(), Decimal("0"))
            if remaining > 0:
                total += holding_value(replace(holding, quantity=remaining), prices)
        elif portfolio_id in slices:
            total += holding_value(replace(holding, quantity=slices[portfolio_id]), prices)
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

    Validates first — **both** checks — so a table can never be drawn from an allocation set the
    consolidated total would reject. That includes over-allocation, which is why this takes the
    holdings it walks rather than trusting them.
    """
    validate_allocations(allocations, portfolios)
    validate_against_holdings(holdings, allocations)
    index = _slice_index(allocations)
    values: dict[int | None, Decimal] = {UNALLOCATED: Decimal("0")}
    for portfolio in portfolios.values():
        if portfolio.kind is PortfolioKind.CAPITAL:
            values[portfolio.portfolio_id] = Decimal("0")
    for holding in holdings:
        slices = index.get(holding.key, {})
        for portfolio_id, quantity in slices.items():
            sliced = replace(holding, quantity=quantity)
            values[portfolio_id] = values.get(portfolio_id, Decimal("0")) + holding_value(
                sliced, prices
            )
        remaining = holding.quantity - sum(slices.values(), Decimal("0"))
        if remaining > 0:
            values[UNALLOCATED] += holding_value(replace(holding, quantity=remaining), prices)
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

    **Criterion 1 is a property of this function**: it walks the holdings once and adds each one
    whole, so it cannot disagree with :func:`portfolio_values` — whose slices plus remainder sum
    to exactly the same thing, guaranteed by :func:`validate_against_holdings` refusing any set
    where they would not. That is stronger than asserting equality after the fact, and it is the
    property that had to be re-established when slices replaced whole-holding allocation: with
    over-allocation permitted, the parts would exceed this total while each half stayed
    internally consistent.

    Monitoring views contribute nothing and are not consulted — they hold no allocations at all,
    so there is no filter here to forget. ``portfolios`` is taken so the allocation set can be
    validated against it, which is the only reason it is a parameter.
    """
    validate_allocations(allocations, portfolios)
    validate_against_holdings(holdings, allocations)
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

    **One owner means one answer, and that stays free.** A holding filed entirely into a single
    capital portfolio is attributed silently, exactly as before slices existed — so nothing that
    works today starts asking questions tomorrow. Everything else becomes a question, and the type
    makes "guess quietly" unrepresentable.

    The four questions, in the order they can occur:

    * we have no record of the holding at all -> ``UNKNOWN_INFLOW``;
    * more was sold than we recorded -> ``QUANTITY_MISMATCH``, which usually means a trade we
      never saw, and attributing the excess to the portfolio we do know would corrupt its return
      series with volume it never held;
    * the holding is in no portfolio at all -> ``UNALLOCATED_HOLDING``;
    * the holding is **split** across capital portfolios -> ``SPLIT_HOLDING`` (10 Sep 2026), with
      a pro-rata pre-fill attached.

    On the last one: pro-rata is offered and never applied. Maulik chose that over silent
    pro-rata, and the reason is that the two are indistinguishable in the data and very different
    in fact — a person who sells 30 of a 100 filed 20/34/36/10 has almost always sold *one lot*,
    not a sixth of each. Applying the suggestion would move four portfolios' returns at once and
    print nothing to say it had happened.

    The unallocated remainder is not offered as a slice. Shares nobody has filed are not a
    portfolio, and pre-selecting them would answer the question the item is asking.
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

    slices = _slice_index(allocations).get(sell.key, {})
    sole = next(iter(slices)) if len(slices) == 1 else None
    if sell.quantity > held.quantity:
        return SellAttribution(
            item=ReconciliationItem(
                key=sell.key,
                quantity=sell.quantity,
                reason=ReconciliationReason.QUANTITY_MISMATCH,
                suggested_portfolio_id=sole,
                suggested_split=_suggest(sell.quantity, slices) if len(slices) > 1 else (),
            )
        )
    if not slices:
        return SellAttribution(
            item=ReconciliationItem(
                key=sell.key,
                quantity=sell.quantity,
                reason=ReconciliationReason.UNALLOCATED_HOLDING,
            )
        )
    if sole is not None:
        # The old whole-holding case, whether or not the slice covers the whole position. A sell
        # bigger than the slice but no bigger than the holding is still this portfolio's — the
        # rest of the position is Unallocated, and Unallocated cannot have sold anything.
        return SellAttribution(portfolio_id=sole)
    return SellAttribution(
        item=ReconciliationItem(
            key=sell.key,
            quantity=sell.quantity,
            reason=ReconciliationReason.SPLIT_HOLDING,
            suggested_split=_suggest(sell.quantity, slices),
        )
    )


def _suggest(quantity: Decimal, slices: Mapping[int, Decimal]) -> tuple[tuple[int, Decimal], ...]:
    """The pre-fill for a split sell: pro-rata, capped at what is actually sliced.

    Capped because ``QUANTITY_MISMATCH`` reaches here with a quantity larger than the position,
    and suggesting that someone sold more out of a portfolio than it ever held would be a
    pre-fill that cannot be accepted. In that case the suggestion drains the slices and leaves
    the excess for the human to explain, which is what the item is asking about anyway.
    """
    filed = sum(slices.values(), Decimal("0"))
    split = pro_rata_split(min(quantity, filed), slices)
    return tuple(sorted((pid, qty) for pid, qty in split.items() if qty > 0))


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
