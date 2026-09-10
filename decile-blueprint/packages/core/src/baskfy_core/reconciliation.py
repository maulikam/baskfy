"""The reconciliation inbox — `PORTFOLIO_REDESIGN.md` §4.3, §4.5 and §6.4, as pure arithmetic.

`allocation_ledger` answers one event at a time: *this* sell either attributes or raises exactly
one question (§4.3, criterion 4); *this* holding survives a split with its cost basis intact
(§4.5, criterion 6). Both answers are correct and neither is enough on its own, because the two
sentences the spec actually cares about are about a **set**:

    "Unresolved reconciliation items freeze that holding's contribution to performance (show as
     'pending reconciliation') rather than guessing. Never silently corrupt a portfolio's return
     series." — §4.3

    "...update quantity + average price across the affected holding wherever it appears (its
     capital portfolio and all monitoring views) atomically." — §4.5

A question that nobody is holding cannot freeze anything, and an adjustment applied to one row
out of four is the corruption §4.5 exists to prevent. So this module is the layer above: the
open questions as a collection, what their existence forbids, what answering one implies, and
the fan-out that makes "atomically" a property of a value rather than a hope about a transaction.

**It imports the ledger and re-implements none of it.** :func:`fan_out_corporate_action` calls
:func:`~baskfy_core.allocation_ledger.apply_corporate_action` once per row rather than carrying
its own idea of what a split does to an average price, and :func:`entry_for` takes the ledger's
:class:`~baskfy_core.allocation_ledger.SellAttribution` rather than re-deciding whether a sell
was attributable. Two modules that both know the rule are two modules that will eventually
disagree, and the one users would meet is whichever ran last.

**Prices, dates and holdings arrive as arguments** (law 1). Nothing here reaches a broker, a
database, a clock or a file. "Is this price stale?" is answered against an ``as_of`` the caller
supplies, because a module that could read the time could not be tested at a chosen instant —
and every rule below is a rule about a moment.

WHAT FREEZING MEANS, AND WHY IT IS NOT A FILTER
-----------------------------------------------
Freezing is not "drop the holding from the total". Dropping it would produce a smaller number
that still balances, which is the same failure mode `holding_value` refuses a missing price for.
It is "this figure cannot be stated honestly yet, and the surface must say so": §6.5's Status
column reads *Pending reconciliation*, and migration 0022 carries the same fact onto every NAV
row as ``pending_reconciliation`` so a day whose value we could not trust says so rather than
vanishing — a gap in a chart reads as a zero.

:func:`freeze_report` therefore returns *which* portfolios are affected rather than a filtered
list of holdings. The blast radius is deliberately narrow: one open question about one physical
position marks that position's capital portfolio and the monitoring views it appears in, and
nothing else. A user with four portfolios and one unanswered sell keeps three honest headline
numbers. Marking everything pending would be safe in the trivial sense and useless in practice,
and a product whose figures are usually greyed out teaches people to ignore the grey.

WHY A DISMISSAL UNFREEZES
-------------------------
The lifecycle is OPEN -> RESOLVED | DISMISSED, fixed by migration 0022's check constraint. Only
OPEN freezes. That looks like a hole — dismissing makes the warning go away — and it is not:
dismissing is a human *answering* "there is nothing here to attribute" (a sync artefact, a
broker restatement, shares that were never ours). The thing §4.3 forbids is **us** guessing. A
recorded human decision is the opposite of a guess, which is why DISMISSED is a terminal state
with a date on it and not a delete.

WHY RESOLVING RETURNS A VALUE INSTEAD OF APPLYING ONE
-----------------------------------------------------
:func:`resolve` hands back the :class:`~baskfy_core.allocation_ledger.Allocation` the answer
implies and writes nothing. Law 1 makes that mandatory, but it would be the right shape anyway:
the allocation and the state change have to land in one database transaction with the NAV rows
they invalidate, and a function that had already "applied" half of it would leave the caller
holding a partial commit. A value can be validated, logged, shown to the user for confirmation
and then written once.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Final

from baskfy_core.allocation_ledger import (
    UNALLOCATED,
    Allocation,
    CorporateAction,
    Holding,
    HoldingKey,
    Portfolio,
    PortfolioKind,
    ReconciliationItem,
    SellAttribution,
    apply_corporate_action,
    cost_basis,
    slices_of,
    unallocated_holdings,
)
from baskfy_core.gst import money

__all__ = [
    "PENDING_STATUS_LABEL",
    "AttentionInputs",
    "AttentionItem",
    "AttentionKind",
    "CorporateActionFanOut",
    "FreezeReport",
    "InboxEntry",
    "LedgerPosition",
    "ReconciliationState",
    "ResolutionOutcome",
    "attention_items",
    "dismiss",
    "entry_for",
    "fan_out_corporate_action",
    "freeze_report",
    "open_entries",
    "positions_from_allocations",
    "resolve",
]

#: The words §6.5's Status column shows for a frozen row. Defined once here because the API, the
#: table and the inspector drawer must not each invent their own phrasing — house rule 8's
#: argument about storage precision applies to the words as well as the numbers.
PENDING_STATUS_LABEL: Final = "Pending reconciliation"

#: How many calendar days of price staleness are unremarkable before §6.4 says something.
#:
#: Four, because §5.1 is end-of-day: a Friday close is the newest honest price all weekend, and a
#: Monday holiday makes Tuesday morning four days out from it with nothing wrong. Importing
#: `trading_calendar` to answer this exactly was the obvious alternative and is rejected — core
#: would then need a market's holiday list to answer a *display* question, and the caller running
#: the nightly job already knows the real gap and can pass it.
DEFAULT_STALE_AFTER_DAYS: Final = 4


class ReconciliationState(StrEnum):
    """The inbox lifecycle, fixed by migration 0022's ``reconciliation_item_state_known``.

    Three states and no more. There is deliberately no ``SNOOZED`` and no ``IN_PROGRESS``: both
    would be a fourth thing that freezes performance indefinitely while looking like progress,
    and §4.3's whole point is that the freeze should be uncomfortable enough to get answered.
    """

    #: The question is unanswered. This is the only state that freezes anything.
    OPEN = "OPEN"
    #: A human named the portfolio. Terminal, and it must name one — see :class:`InboxEntry`.
    RESOLVED = "RESOLVED"
    #: A human said there is nothing to attribute. Terminal, and it names no portfolio.
    DISMISSED = "DISMISSED"

    @property
    def freezes(self) -> bool:
        """Whether an entry in this state withholds its holding from performance (§4.3)."""
        return self is ReconciliationState.OPEN


@dataclass(frozen=True, slots=True)
class InboxEntry:
    """One reconciliation item with the lifecycle the inbox puts around it.

    The ledger's :class:`~baskfy_core.allocation_ledger.ReconciliationItem` is the *question* —
    which holding, how much, why we could not attribute it. It has no state because at the moment
    it is created there is nothing to have a state about. This is the row: the question plus what
    has happened to it, mirroring ``reconciliation_item`` in migration 0022 field for field.

    The invariant is that migration's third check constraint, restated here so it holds before a
    database is anywhere near the code::

        (state = 'RESOLVED' AND resolved_portfolio_id IS NOT NULL) OR
        (state <> 'RESOLVED' AND resolved_portfolio_id IS NULL)

    Without it, "resolved" could mean "somebody clicked something" and a return series would move
    on a decision nobody recorded — which is criterion 4's failure wearing a different hat.

    ``resolved_on`` is deliberately *optional* on a terminal entry rather than required. Being
    stricter than the schema was the alternative and it is the wrong kind of strict: migration
    0022 leaves ``resolved_at`` nullable, so a legitimately stored row can have none, and a core
    type that refused to represent a row the database happily holds would fail at load time on
    data that is not actually wrong. It is refused on an OPEN entry, where it is not a gap but a
    contradiction.
    """

    item_id: int
    item: ReconciliationItem
    #: The day sync raised the question. Supplied, never read from a clock (law 1).
    detected_on: dt.date
    state: ReconciliationState = ReconciliationState.OPEN
    resolved_portfolio_id: int | None = None
    resolved_on: dt.date | None = None

    def __post_init__(self) -> None:
        names_portfolio = self.resolved_portfolio_id is not None
        if (self.state is ReconciliationState.RESOLVED) != names_portfolio:
            raise ValueError(
                f"reconciliation item {self.item_id} is {self.state} and "
                f"{'names' if names_portfolio else 'names no'} portfolio; a resolved item must "
                "name the portfolio it resolved to, and an unresolved one cannot name any — "
                "otherwise 'resolved' means only that somebody clicked something, and a return "
                "series moves on a decision nobody recorded (spec section 4.3, migration 0022)"
            )
        if self.state is ReconciliationState.OPEN and self.resolved_on is not None:
            raise ValueError(
                f"reconciliation item {self.item_id} is OPEN but carries a resolution date; an "
                "open question has not been answered on any day"
            )

    @property
    def key(self) -> HoldingKey:
        """The physical position the question is about."""
        return self.item.key

    @property
    def quantity(self) -> Decimal:
        return self.item.quantity

    @property
    def question(self) -> str:
        """The sentence §4.3 wants the inbox to ask. Owned by the ledger's reason enum."""
        return self.item.question

    @property
    def freezes(self) -> bool:
        return self.state.freezes


@dataclass(frozen=True, slots=True)
class LedgerPosition:
    """One appearance of a physical holding inside one portfolio — a ``portfolio_holding`` row.

    This is the shape §4.5's word *atomically* needs and the ledger deliberately does not have.
    A :class:`~baskfy_core.allocation_ledger.Holding` is the position itself: 320 HDFC Bank at
    Zerodha, one of them in the world. But the schema stores it once per portfolio it appears
    in — 0021's partial unique index allows exactly one CAPITAL row per physical holding and
    places no limit at all on MONITORING rows, which is §4.1 exactly — so the same 320 shares are
    a row in "Long term" and a row in "All defence stocks" and a row in "Bought in 2026".

    A split that updates one of those rows and not the others does not produce a wrong number in
    one place; it produces two portfolios that disagree about how many shares exist, and the
    disagreement surfaces as P&L on a day the user did nothing. Hence
    :func:`fan_out_corporate_action` takes the whole set and returns the whole set.

    ``portfolio_id`` is ``None`` for the Unallocated bucket, matching the ledger's
    :data:`~baskfy_core.allocation_ledger.UNALLOCATED` sentinel: Unallocated is not a portfolio,
    and giving it an id would let it be renamed or deleted.
    """

    portfolio_id: int | None
    holding: Holding

    @property
    def key(self) -> HoldingKey:
        return self.holding.key


@dataclass(frozen=True, slots=True)
class FreezeReport:
    """What the open questions forbid: §4.3's freeze, as data a surface can render.

    Three separate facts, because three different surfaces need different ones and collapsing
    them would make each caller re-derive the others:

    * :attr:`frozen` — the positions whose contribution to performance is withheld. §7's holdings
      tab greys these rows and shows :data:`PENDING_STATUS_LABEL` instead of a return.
    * :attr:`pending_portfolio_ids` — every portfolio, capital **or monitoring**, holding at
      least one frozen position. §6.5's Status column reads from this.
    * :attr:`unallocated_pending` — whether the Unallocated bucket itself is affected, which
      §6.6 makes the centerpiece of the page rather than a footer.

    Monitoring views are included even though they are excluded from every total (§4.1). They are
    excluded from *arithmetic*; they still print a return in §6.5's muted tab, and a lens over a
    position we cannot value honestly is exactly as dishonest as a capital portfolio over it.
    """

    frozen: frozenset[HoldingKey]
    pending_portfolio_ids: frozenset[int]
    unallocated_pending: bool

    @property
    def consolidated_pending(self) -> bool:
        """Whether the consolidated figure (§6.2's hero metrics) must be marked pending.

        A single frozen position anywhere, because criterion 1 makes the consolidated number
        the sum of every part: one part we cannot state honestly makes the whole one we cannot
        state honestly. This is the one place the blast radius is deliberately total.
        """
        return bool(self.frozen)

    def is_frozen(self, key: HoldingKey) -> bool:
        """Whether this position's contribution to performance is withheld (§4.3)."""
        return key in self.frozen

    def is_pending(self, portfolio_id: int | None) -> bool:
        """Whether this portfolio's figures must show :data:`PENDING_STATUS_LABEL`.

        ``None`` asks about Unallocated, matching every other portfolio-keyed call in the ledger.
        """
        if portfolio_id is UNALLOCATED:
            return self.unallocated_pending
        return portfolio_id in self.pending_portfolio_ids


@dataclass(frozen=True, slots=True)
class ResolutionOutcome:
    """What answering one question implies — computed, not applied.

    Two values that must land together: the entry in its new terminal state, and the allocation
    row the answer implies. Returning them as one object rather than two calls is what stops a
    caller from writing the state change and losing the allocation, which would leave an item
    marked RESOLVED whose holding is still in no portfolio — an unfrozen holding with nowhere to
    contribute, and therefore a quietly wrong total instead of a loudly pending one.

    :attr:`implied_allocation` is ``None`` for a dismissal: "there is nothing to attribute" is an
    answer that changes no allocation, and manufacturing one would be the guess §4.3 forbids.
    """

    entry: InboxEntry
    implied_allocation: Allocation | None

    @property
    def unfreezes(self) -> bool:
        """Whether this outcome releases the holding back into performance. Always, by design:
        both terminal states are answers, and only OPEN withholds."""
        return not self.entry.freezes


@dataclass(frozen=True, slots=True)
class CorporateActionFanOut:
    """§4.5's atomic update, as a value: every row the action touches, or an exception.

    "Atomically" is a promise about a transaction, and a pure function cannot make it. What it
    can do is make the partial state unrepresentable: this object is either constructed with the
    complete new picture or it is not constructed at all, so there is no shape in which a caller
    holds three updated rows and one stale one. The caller's transaction then writes
    :attr:`positions` in one statement.

    :attr:`cost_basis_before` and :attr:`cost_basis_after` are carried rather than merely
    checked, because criterion 6 ("a split/bonus changes quantity and average price but produces
    zero P&L") is the kind of claim that should be inspectable at the call site and in a log, not
    only inside an assertion that already ran.
    """

    action: CorporateAction
    #: The complete position set after the action — untouched rows included, so the caller
    #: writes one picture rather than merging two.
    positions: tuple[LedgerPosition, ...]
    #: Only the rows the action changed. Every portfolio the holding appears in is represented.
    updated: tuple[LedgerPosition, ...]
    #: Summed across :attr:`updated`. ``None`` when no row knew its purchase price (§5.3), which
    #: is not a zero: there is no cost basis to preserve, so none is claimed.
    cost_basis_before: Decimal | None
    cost_basis_after: Decimal | None

    @property
    def touched_portfolio_ids(self) -> tuple[int | None, ...]:
        """Every portfolio the fan-out reached, in the order the rows were given."""
        return tuple(position.portfolio_id for position in self.updated)


class AttentionKind(StrEnum):
    """§6.4's v1 ribbon, enumerated. LATER items are absent rather than present and disabled.

    The member *is* the deep link: §6.4 says each item links to its resolution flow, and the
    route is the surface's business. Core naming a URL would be core knowing about the web app.
    """

    BROKER_CONNECTION_EXPIRED = "BROKER_CONNECTION_EXPIRED"
    RECONCILIATION_PENDING = "RECONCILIATION_PENDING"
    STALE_PRICE_DATA = "STALE_PRICE_DATA"
    HOLDINGS_UNALLOCATED = "HOLDINGS_UNALLOCATED"
    REBALANCE_AVAILABLE = "REBALANCE_AVAILABLE"


#: The ribbon's order, and it is an argument rather than a preference.
#:
#: An expired connection comes first because while sync is dead every other item on the ribbon is
#: potentially stale advice — including the reconciliation count. Reconciliation is next: it is
#: the only item that is actively withholding numbers from the user (§4.3). Stale prices follow,
#: because they make the numbers old rather than absent. Unallocated holdings and an available
#: rebalance are work the user chooses to do, not something wrong, and §6.6 already gives
#: unallocated holdings a whole section of the page — the ribbon only needs to point at it.
_ATTENTION_ORDER: Final[tuple[AttentionKind, ...]] = (
    AttentionKind.BROKER_CONNECTION_EXPIRED,
    AttentionKind.RECONCILIATION_PENDING,
    AttentionKind.STALE_PRICE_DATA,
    AttentionKind.HOLDINGS_UNALLOCATED,
    AttentionKind.REBALANCE_AVAILABLE,
)


def _plural(count: int, singular: str, plural: str) -> str:
    """``1 holding`` / ``3 holdings``. A ribbon that says "1 holdings" reads as a bug report."""
    return f"{count} {singular if count == 1 else plural}"


@dataclass(frozen=True, slots=True)
class AttentionItem:
    """One row of §6.4's ribbon.

    One shape for all five kinds, not five classes. A ribbon renderer draws {icon, sentence,
    link} per row and nothing else; five types would force it to branch five ways to produce one
    row, and the branch is where a sixth kind gets forgotten.

    :attr:`count` means "how many subjects", which for four of the five kinds is
    ``len(subject_ids)``. :data:`AttentionKind.STALE_PRICE_DATA` has no subjects — it is about
    the market-data layer, not about anything the user owns — so its count is the number of days
    the prices are behind, and :attr:`since` carries the date the sentence needs.

    The sentences avoid §8's renamed vocabulary entirely: no box, book, sleeve, nest, divide,
    file under, spans brokers, your rule or run by hand. Criterion 7 is a property of every
    string this product shows, and a string defined in core is one the UI cannot fix later.
    """

    kind: AttentionKind
    count: int
    subject_ids: tuple[int, ...] = ()
    #: The date :data:`AttentionKind.STALE_PRICE_DATA`'s sentence quotes. ``None`` elsewhere, and
    #: also ``None`` when there is no price data at all — a different sentence, not a blank one.
    since: dt.date | None = None

    @property
    def message(self) -> str:
        """The sentence the ribbon shows. One definition, so every surface says it identically."""
        if self.kind is AttentionKind.BROKER_CONNECTION_EXPIRED:
            noun = _plural(self.count, "broker connection", "broker connections")
            verb = "has" if self.count == 1 else "have"
            return f"{noun} {verb} expired — reconnect to resume syncing"
        if self.kind is AttentionKind.RECONCILIATION_PENDING:
            noun = _plural(self.count, "holding is", "holdings are")
            return f"{noun} pending reconciliation — returns are on hold until you answer"
        if self.kind is AttentionKind.STALE_PRICE_DATA:
            if self.since is None:
                return "No closing prices yet — values cannot be shown"
            return f"Prices are still at the close of {self.since.isoformat()}"
        if self.kind is AttentionKind.HOLDINGS_UNALLOCATED:
            noun = _plural(self.count, "holding is", "holdings are")
            return f"{noun} not in any portfolio yet"
        return f"Rebalance available on {_plural(self.count, 'portfolio', 'portfolios')}"


@dataclass(frozen=True, slots=True)
class AttentionInputs:
    """Everything §6.4 needs to decide the ribbon, gathered into one value.

    A parameter list rather than a dataclass was the first shape and it reached nine arguments,
    which is both a lint failure and a genuine warning: nine positional facts at a call site is
    where "which of these two date arguments is which" bugs live. Gathering them names every one
    at construction, and makes the ribbon's whole input something a test can build once.

    Three of the five items are *derived* here rather than passed — the unallocated count from
    the ledger's own :func:`~baskfy_core.allocation_ledger.unallocated_holdings`, the pending
    count from the entries, the stale verdict from two dates. Passing them pre-counted was the
    alternative, and it would let a caller's arithmetic disagree with the ledger's about what
    "unallocated" means, which is precisely the disagreement §6.6's centerpiece cannot afford.
    The two items core genuinely cannot know — a broker session's expiry, whether a publisher has
    issued a rebalance — arrive as ids.
    """

    #: The day the ribbon is being drawn for. An argument, never a clock (law 1).
    as_of: dt.date
    holdings: Sequence[Holding] = ()
    allocations: Sequence[Allocation] = ()
    entries: Sequence[InboxEntry] = ()
    #: Broker accounts whose connection needs re-authorising. Core cannot know this: it is a
    #: token's expiry, which lives in `broker_connections` and is I/O by nature.
    expired_broker_account_ids: Sequence[int] = ()
    #: Portfolios with a rebalance the user has not taken up — §6.4 names subscribed portfolios,
    #: and a strategy-driven one presents identically, so the kind is not narrowed here.
    rebalance_due_portfolio_ids: Sequence[int] = ()
    #: The close the current prices are from (§5.1's "Valued at close of {date}"). ``None`` means
    #: there are no prices at all, which is a louder sentence rather than a missing row.
    prices_as_of: dt.date | None = None
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS
    #: Set when the caller has no market-data layer in play at all and staleness is not a
    #: question worth asking — a first run before any close has been recorded, for instance.
    check_prices: bool = True


# ---------------------------------------------------------------------------
# The inbox — §4.3
# ---------------------------------------------------------------------------


def entry_for(
    attribution: SellAttribution,
    item_id: int,
    detected_on: dt.date,
) -> InboxEntry | None:
    """Turn the ledger's verdict on one detected sell into an inbox row, or ``None``.

    ``None`` when the sell attributed silently, which is the common case that whole-holding
    allocation exists to produce (§4.2 -> §4.3): there is no question, so there is no row, so
    nothing freezes. The inbox is not a log of everything sync saw; it is the list of things
    sync could not decide, and padding it with resolved-on-arrival rows would bury the five that
    matter under five hundred that do not.

    The bridge is a function rather than a constructor on :class:`InboxEntry` so that the ledger
    stays unaware of the inbox. `allocation_ledger` decides *whether* there is a question; this
    module decides what happens to one. Reversing that would make the ledger import a lifecycle
    it has no use for.
    """
    if attribution.item is None:
        return None
    return InboxEntry(item_id=item_id, item=attribution.item, detected_on=detected_on)


def open_entries(entries: Sequence[InboxEntry]) -> list[InboxEntry]:
    """The unanswered questions, in the order given. The only ones that freeze anything."""
    return [entry for entry in entries if entry.freezes]


def freeze_report(
    entries: Sequence[InboxEntry],
    positions: Sequence[LedgerPosition],
) -> FreezeReport:
    """§4.3's freeze: which positions are withheld, and whose figures must say so.

    The rule in one line: an OPEN entry freezes its physical position, and a frozen position
    marks every portfolio it appears in — its one capital portfolio and each monitoring view.

    A frozen position with **no** row at all is the ``UNKNOWN_INFLOW`` case (shares arrived that
    we cannot account for), and it marks Unallocated pending. That is the honest answer rather
    than a no-op: the shares physically exist in the account, so they are somewhere in the
    consolidated total by criterion 1, and the only bucket that can own something nobody has
    assigned is Unallocated. Treating it as affecting nothing would leave §6.6's centerpiece
    quietly wrong while every portfolio row looked fine.

    Note what is *not* consulted: ``suggested_portfolio_id``. The ledger is explicit that the
    suggestion is "a suggestion, never an attribution", and letting it mark a portfolio pending
    would be attributing the item to it by the back door — a portfolio whose returns went on hold
    because of a sell that may have had nothing to do with it.
    """
    frozen = frozenset(entry.key for entry in entries if entry.freezes)
    pending: set[int] = set()
    unallocated_pending = False
    seen: set[HoldingKey] = set()
    for position in positions:
        if position.key not in frozen:
            continue
        seen.add(position.key)
        if position.portfolio_id is UNALLOCATED:
            unallocated_pending = True
        else:
            pending.add(position.portfolio_id)
    if frozen - seen:
        unallocated_pending = True
    return FreezeReport(
        frozen=frozen,
        pending_portfolio_ids=frozenset(pending),
        unallocated_pending=unallocated_pending,
    )


def resolve(
    entry: InboxEntry,
    portfolio_id: int,
    portfolios: Mapping[int, Portfolio],
    resolved_on: dt.date,
) -> ResolutionOutcome:
    """Answer one question by naming a portfolio, and return what that implies (§4.3).

    Three refusals, and each is a caller bug rather than a user mistake:

    * the entry is already terminal — an answered question is not re-answerable, because the
      allocation and the NAV rows its first answer produced have already been written. Changing
      one's mind is a new event with its own date, which is why 0022 keeps ``resolved_at`` and
      not a mutable answer;
    * the named portfolio does not exist;
    * the named portfolio is a monitoring view. Lenses hold no allocation at all (§4.1), so
      "resolve it to All defence stocks" has no meaning — the shares would still be in no capital
      portfolio and the total would still be short. The ledger refuses the same thing at
      :func:`~baskfy_core.allocation_ledger.validate_allocations`; refusing it here as well means
      the bad allocation is never even constructed.

    The implied allocation is the same for all three reasons — the holding now counts against the
    named portfolio — and that uniformity is not a simplification. It is what whole-holding
    allocation buys: with no partial quantities (§4.2), every answer to "which portfolio?" is
    fully expressed by one allocation row. Phase 3's partial allocation is exactly where this
    return type would have to grow, and it is scoped out for that reason.
    """
    if not entry.freezes:
        raise ValueError(
            f"reconciliation item {entry.item_id} is already {entry.state}; an answered question "
            "is not re-answerable, because the allocation and the valuations its answer produced "
            "have been written — record a new item instead"
        )
    portfolio = portfolios.get(portfolio_id)
    if portfolio is None:
        raise ValueError(
            f"cannot resolve reconciliation item {entry.item_id} to portfolio {portfolio_id}, "
            "which does not exist"
        )
    if portfolio.kind is PortfolioKind.MONITORING:
        raise ValueError(
            f"{portfolio.name!r} is a monitoring view, and a monitoring view holds no "
            "allocation: it overlaps other portfolios by design and is excluded from every "
            "total (spec section 4.1). Resolving to it would leave the holding in no capital "
            "portfolio and the consolidated total short"
        )
    return ResolutionOutcome(
        entry=replace(
            entry,
            state=ReconciliationState.RESOLVED,
            resolved_portfolio_id=portfolio_id,
            resolved_on=resolved_on,
        ),
        implied_allocation=Allocation(
            key=entry.key, portfolio_id=portfolio_id, quantity=entry.quantity
        ),
    )


def dismiss(entry: InboxEntry, dismissed_on: dt.date) -> ResolutionOutcome:
    """Record "there is nothing here to attribute", which unfreezes and changes no allocation.

    A sync artefact, a broker restatement, shares that were never this user's. The holding goes
    back to contributing on whatever allocation it already had, and no allocation row is implied
    — inventing one would be the guess §4.3 forbids, just with a human's click as cover.

    Terminal, with a date, and never a delete: §7's Activity tab lists reconciliation history,
    and a question that was asked and waved away is part of why a return series looks the way it
    does. Deleting the row would make that unexplainable a year later.
    """
    if not entry.freezes:
        raise ValueError(
            f"reconciliation item {entry.item_id} is already {entry.state}; it cannot be "
            "dismissed twice"
        )
    return ResolutionOutcome(
        entry=replace(entry, state=ReconciliationState.DISMISSED, resolved_on=dismissed_on),
        implied_allocation=None,
    )


# ---------------------------------------------------------------------------
# Corporate actions — §4.5, criterion 6
# ---------------------------------------------------------------------------


def positions_from_allocations(
    holdings: Sequence[Holding],
    allocations: Sequence[Allocation],
    views: Mapping[HoldingKey, Sequence[int]] | None = None,
) -> list[LedgerPosition]:
    """Build the position set from the ledger's own types: one capital row per holding, plus
    each monitoring view it appears in.

    A convenience, and it is written the slow way on purpose. It asks the ledger
    (:func:`~baskfy_core.allocation_ledger.slices_of`) how each holding is divided rather than
    building its own index, which makes it O(holdings x allocations). The fast version means a
    second copy of the duplicate check living in a second module, and the copy is the one that
    goes stale — a set of allocations the ledger would refuse must not be able to produce a
    position set this module happily accepts. A user's holdings number in the tens; the
    correctness is worth more than the walk.

    **One capital row per SLICE since 10 Sep 2026**, not one per holding. A 100-share ITC filed
    20/34/36 produces three capital positions carrying 20, 34 and 36 shares, plus a fourth
    carrying the unallocated 10. Reconciliation compares the ledger against the broker, and a
    ledger that reported one 100-share row per holding would agree with the broker while hiding
    every fact the split was created to record.

    A monitoring view still sees the WHOLE holding: a lens answers "which names", not "how many"
    (§4.1), and it enters no total, so there is nothing here to double-count.

    ``views`` maps a holding to the monitoring portfolios it appears in. Absent means none, which
    is the right default: a lens is something the user built deliberately.
    """
    lenses = views or {}
    positions: list[LedgerPosition] = []
    for holding in holdings:
        slices = slices_of(allocations, holding.key)
        for portfolio_id, quantity in sorted(slices.items()):
            positions.append(
                LedgerPosition(
                    portfolio_id=portfolio_id, holding=replace(holding, quantity=quantity)
                )
            )
        remaining = holding.quantity - sum(slices.values(), Decimal("0"))
        if remaining > 0 or not slices:
            positions.append(
                LedgerPosition(portfolio_id=None, holding=replace(holding, quantity=remaining))
            )
        for view_id in lenses.get(holding.key, ()):
            positions.append(LedgerPosition(portfolio_id=view_id, holding=holding))
    return positions


def _total_cost_basis(positions: Sequence[LedgerPosition]) -> Decimal | None:
    """Summed cost basis over the rows that know one, or ``None`` when no row does.

    ``None`` is not zero. A holding synced from a broker that does not publish purchase history
    (§5.3, before a CAS import) has no cost basis to preserve, and claiming it preserved a zero
    would be a claim about a number that does not exist.
    """
    known = [cost_basis(position.holding) for position in positions]
    present = [value for value in known if value is not None]
    if not present:
        return None
    return sum(present, Decimal("0"))


def fan_out_corporate_action(
    action: CorporateAction,
    positions: Sequence[LedgerPosition],
) -> CorporateActionFanOut:
    """§4.5: apply one split or bonus to the affected holding **everywhere it appears**.

    :func:`~baskfy_core.allocation_ledger.apply_corporate_action` already does the hard part for
    one row, and it is called once per row here rather than reimplemented — it is the only place
    that knows cost basis is preserved by dividing the *original* cost by the *new* quantity
    instead of scaling the price and hoping the remainder rounds kindly.

    What this adds is the set. Four refusals, all before anything is returned, so the caller
    either gets the whole new picture or gets an exception and writes nothing:

    * **nothing matches the action.** A silent no-op is the worst outcome available: the split
      appears to have been processed, the quantity never changes, and the position is wrong from
      that day forward with no record of why.
    * **the rows disagree about quantity.** They are views of one physical position; if the
      capital row says 320 and a lens says 300, the data was already broken and multiplying both
      by 3 would encode the break into a number three times larger.
    * **the rows disagree about average price.** Same argument, and this one decides P&L.
    * **cost basis moved.** Criterion 6 in one line — "a split/bonus changes quantity and average
      price but produces zero P&L" — checked across the whole fan-out rather than per row,
      because a fan-out that preserved each row and lost a paisa in the sum would satisfy the
      per-row test and still print P&L on a day nothing happened.

    The comparison is at paise, the precision every figure in this product is stated and stored
    at. Full-precision equality was the stricter alternative and it is unachievable rather than
    admirable: a ratio whose division does not terminate — 1:7, say — leaves a residue in the
    28th significant digit no matter how the arithmetic is arranged, and refusing a 1:7 bonus on
    those grounds would be a module with an opinion about which corporate actions are allowed to
    happen. Zero P&L to the paisa is the promise the spec makes and the promise the user can see.

    An open reconciliation item on the holding does **not** block the fan-out, and that is
    deliberate. A split is a fact published by an exchange, not an attribution: it is true
    regardless of which portfolio a disputed sell came from, and postponing it would leave the
    frozen position at a stale quantity that gets steadily more wrong while the question waits.
    """
    touched = [position for position in positions if position.key == action.key]
    if not touched:
        raise ValueError(
            f"corporate action names holding {action.key}, which appears in no portfolio; "
            "applying it to nothing would look like success and leave the position at a "
            "pre-action quantity from that day onward (spec section 4.5)"
        )

    quantities = {position.holding.quantity for position in touched}
    if len(quantities) > 1:
        raise ValueError(
            f"holding {action.key} is recorded at {len(quantities)} different quantities across "
            f"the portfolios it appears in ({sorted(quantities)}); these are views of one "
            "physical position and must already agree before a corporate action multiplies them"
        )
    prices = {position.holding.avg_price for position in touched}
    if len(prices) > 1:
        raise ValueError(
            f"holding {action.key} is recorded at {len(prices)} different average prices across "
            "the portfolios it appears in; these are views of one physical position, and an "
            "average price that differs by portfolio is a P&L disagreement already"
        )

    new_positions: list[LedgerPosition] = []
    updated: list[LedgerPosition] = []
    for position in positions:
        if position.key != action.key:
            new_positions.append(position)
            continue
        adjusted = replace(position, holding=apply_corporate_action(position.holding, action))
        new_positions.append(adjusted)
        updated.append(adjusted)

    before = _total_cost_basis(touched)
    after = _total_cost_basis(updated)
    if before is not None and after is not None and money(before) != money(after):
        raise ValueError(
            f"corporate action on {action.key} moved total cost basis from {money(before)} to "
            f"{money(after)}; a split or bonus changes quantity and average price but produces "
            "zero P&L (acceptance criterion 6)"
        )
    return CorporateActionFanOut(
        action=action,
        positions=tuple(new_positions),
        updated=tuple(updated),
        cost_basis_before=before,
        cost_basis_after=after,
    )


# ---------------------------------------------------------------------------
# Needs-attention ribbon — §6.4
# ---------------------------------------------------------------------------


def _stale_price_item(inputs: AttentionInputs) -> AttentionItem | None:
    """§6.4's stale-price row, or ``None`` when the prices are as fresh as EOD gets.

    ``prices_as_of`` in the future is treated as fresh rather than refused. Core has no clock to
    argue with, the caller supplied both dates, and a ribbon is not the place to litigate a
    timezone.
    """
    if not inputs.check_prices:
        return None
    if inputs.prices_as_of is None:
        return AttentionItem(kind=AttentionKind.STALE_PRICE_DATA, count=0)
    days_behind = (inputs.as_of - inputs.prices_as_of).days
    if days_behind <= inputs.stale_after_days:
        return None
    return AttentionItem(
        kind=AttentionKind.STALE_PRICE_DATA,
        count=days_behind,
        since=inputs.prices_as_of,
    )


def attention_items(inputs: AttentionInputs) -> list[AttentionItem]:
    """§6.4's v1 ribbon, derived as pure data. Empty when nothing needs attention.

    Empty is the point of the return type. A ribbon that always has something in it is furniture,
    and furniture does not get read — so a row exists only when there is something the user can
    actually do, and the five kinds are the five §6.4 names for v1. The LATER items (drift
    threshold, corporate-action review, overlap warnings) are absent from :class:`AttentionKind`
    entirely rather than present and always suppressed: a kind that cannot fire is a kind a
    reader has to check the code to rule out.

    Order comes from :data:`_ATTENTION_ORDER` and not from the order the checks are written in,
    so that adding a check cannot silently reshuffle the ribbon.
    """
    found: dict[AttentionKind, AttentionItem] = {}

    expired = tuple(inputs.expired_broker_account_ids)
    if expired:
        found[AttentionKind.BROKER_CONNECTION_EXPIRED] = AttentionItem(
            kind=AttentionKind.BROKER_CONNECTION_EXPIRED,
            count=len(expired),
            subject_ids=expired,
        )

    # Counted per frozen *holding*, not per open row: two questions about one position are one
    # position whose returns are on hold, and saying "2 holdings" would overstate the damage.
    frozen = freeze_report(
        inputs.entries, positions_from_allocations(inputs.holdings, inputs.allocations)
    )
    if frozen.frozen:
        found[AttentionKind.RECONCILIATION_PENDING] = AttentionItem(
            kind=AttentionKind.RECONCILIATION_PENDING,
            count=len(frozen.frozen),
        )

    stale = _stale_price_item(inputs)
    if stale is not None:
        found[AttentionKind.STALE_PRICE_DATA] = stale

    loose = unallocated_holdings(list(inputs.holdings), list(inputs.allocations))
    if loose:
        found[AttentionKind.HOLDINGS_UNALLOCATED] = AttentionItem(
            kind=AttentionKind.HOLDINGS_UNALLOCATED,
            count=len(loose),
        )

    due = tuple(inputs.rebalance_due_portfolio_ids)
    if due:
        found[AttentionKind.REBALANCE_AVAILABLE] = AttentionItem(
            kind=AttentionKind.REBALANCE_AVAILABLE,
            count=len(due),
            subject_ids=due,
        )

    return [found[kind] for kind in _ATTENTION_ORDER if kind in found]
