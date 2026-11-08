"""The cash ledger — `PORTFOLIO_REDESIGN.md` §4.4, as pure arithmetic.

`allocation_ledger` answers *which portfolio owns this holding*. This module answers the other
half of the same question: *whose money is this, and which movements of it are a return*. §10
puts the two side by side in the spine — "allocation ledger (whole-holding, §4.2) → Unallocated
bucket + cash ledger (§4.4)" — because a net worth built from holdings alone is missing the cash
and criterion 1 asks for both, to the paisa.

**Dates and amounts arrive as arguments.** Nothing here reaches a broker, a database, a clock or
a file (law 1). Every balance below is a statement about a moment, so the moment is a parameter;
a ledger that could read the time is a ledger nobody can test at a chosen instant.

WHAT THIS IS NOT
----------------
Not a sync, not a funds API, not a payment path. It never learns that a deposit happened, never
writes a row, and never moves a rupee anywhere: it takes the list of movements somebody else
recorded and derives what is true afterwards. The migration that fixes the seven kinds
(``0022_portfolio_redesign``) is the schema half of this module; this is the arithmetic half, and
the two agree on the kinds by construction because the names are copied verbatim.

Not an order path either. ``BUY`` and ``SELL`` here are *ledger* kinds — the cash side of a trade
that has already happened — not sides of an order anyone could place. Nothing here names a
product, a venue, an order type or a broker call, and ``test_cash_ledger.py`` scans this source
for that vocabulary so that an edit which quietly crosses the line fails a test rather than a
review. It is the same guard ``allocation_ledger`` and ``portfolio_units`` carry, and it matters
more here precisely because the kind names look like order sides.

THE ONE DISTINCTION THIS MODULE EXISTS TO PRESERVE
--------------------------------------------------
§4.4, in three sentences that are the whole design:

* One **Unallocated cash** bucket per broker account. External deposits and withdrawals land
  there, and they are *not* portfolio events — the user funding their demat account has said
  nothing about which strategy the money is for.
* **Assigning** cash to a capital portfolio is an internal flow, and it **is** the XIRR cash-flow
  event. That is the moment the user committed money to *this* portfolio, and it is the only
  moment a per-portfolio money-weighted return can honestly be measured from.
* **Buying** a stock inside a portfolio is an internal cash→stock transfer and is **not** an XIRR
  event at all. The money never left the portfolio; it changed shape.

Getting the third one wrong is the failure mode worth spelling out, because it is the intuitive
implementation and it is silently, catastrophically wrong. Treat a buy as a cash flow and a
portfolio that assigns ₹1,00,000 once and then churns through ten rebalances reports an XIRR
computed from eleven "contributions" of money the user never contributed. The number moves when
the user trades and not when the user invests, which is the exact inverse of what a money-weighted
return means. Worse, it is plausible: it has the right sign, roughly the right magnitude, and
nobody catches it by looking. So the rule is structural rather than remembered —
:attr:`CashFlowKind.is_xirr_event` is true for exactly two kinds, :func:`xirr_events` consults
nothing else, and a test asserts that adding a buy does not move the answer.

WHY AMOUNTS ARE POSITIVE AND THE SIGN COMES FROM THE KIND
---------------------------------------------------------
A signed amount plus a kind gives two sources of truth for one direction, and they can disagree:
an ``EXTERNAL_WITHDRAWAL`` of ``-500`` and one of ``+500`` both parse, both look deliberate, and
one of them is a bug that no constraint in migration 0022 can catch (the CHECK constraints there
police the kind and the portfolio, not the sign). So the kind decides the direction — once, in
:data:`_UNALLOCATED_SIGN` and :data:`_PORTFOLIO_SIGN` — and the amount is a magnitude. The
rejected alternative was signed amounts with a per-kind sign *assertion*; it is the same table
written as a guard instead of as the answer, and it leaves the caller having to know the
convention anyway.

The two sign tables are also where cash conservation comes from rather than being asserted after
the fact. ``ASSIGN`` is ``-1`` to Unallocated and ``+1`` to the portfolio; add the two columns and
it is zero, which is what "internal" means. :func:`total_cash` sums exactly that combination, so
the identity *unallocated + portfolio cash = deposits, less withdrawals, less buys, plus sells,
plus dividends* cannot drift — it is the same walk over the same list.

WHY SUB-PAISE AMOUNTS ARE REFUSED RATHER THAN ROUNDED
-----------------------------------------------------
``portfolio_cash_flow.amount`` is ``numeric(18, 2)``. An amount finer than a paisa cannot survive
a round trip through the table, so accepting it here would let a balance exist in memory that the
database can never reproduce — and the difference would surface later as a reconciliation nobody
can explain. Quantising it silently is worse: it makes the ledger and the row disagree by design.
House rule 8 says round at write time, and the honest reading of that here is that a flow arrives
already rounded or does not arrive at all. Derived balances are still passed through
:func:`baskfy_core.gst.money`, so a total always carries the exponent the API, the UI and the CSV
export share.

WHAT THIS MODULE DELIBERATELY DOES NOT ENFORCE
----------------------------------------------
An ``ASSIGN`` larger than the broker's derived Unallocated balance is not refused. It looks like
an obvious guard and it would be wrong: a user connects a broker in the middle of that account's
life, so the flow list this module sees is a *suffix* of the truth, and the derived balance starts
from an arbitrary point. Refusing the assignment would make the product unusable for exactly the
user §6.6 is written for — the one arriving with forty holdings and a history we never saw.
Whether an overdraft is real is a question for the sync layer, which knows the broker's own
reported balance; a pure function that has only the flows cannot tell an overdraft from a gap.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final

from baskfy_core.allocation_ledger import QUANTITY_PRECISION, UNALLOCATED
from baskfy_core.curated_accounting import CashFlow, xirr
from baskfy_core.gst import money

__all__ = [
    "CASH_ONLY_KINDS",
    "EXTERNAL_KINDS",
    "STOCK_KINDS",
    "XIRR_EVENT_KINDS",
    "CashFlowKind",
    "PortfolioCashFlow",
    "portfolio_cash",
    "portfolio_cash_by_portfolio",
    "portfolio_xirr",
    "total_cash",
    "unallocated_cash",
    "unallocated_cash_by_broker",
    "xirr_events",
]

ZERO: Final = Decimal("0")
ONE: Final = Decimal("1")
MINUS_ONE: Final = Decimal("-1")


class CashFlowKind(StrEnum):
    """The seven kinds, copied verbatim from migration 0022's ``_FLOW_KINDS``.

    Verbatim is the point: the migration writes them into a CHECK constraint, so a kind this enum
    spells differently is a row the database refuses at insert time — a failure that would show up
    in production rather than in this package's tests. There is no eighth kind and adding one is a
    migration, not an edit here.
    """

    #: Money arriving in the broker account from outside. Lands in Unallocated; no portfolio.
    EXTERNAL_DEPOSIT = "EXTERNAL_DEPOSIT"
    #: Money leaving the broker account for the user's bank. Also no portfolio.
    EXTERNAL_WITHDRAWAL = "EXTERNAL_WITHDRAWAL"
    #: Unallocated -> a capital portfolio. **The** XIRR cash-flow event (§4.4).
    ASSIGN = "ASSIGN"
    #: A capital portfolio -> Unallocated. The other XIRR event, in the other direction.
    RELEASE = "RELEASE"
    #: Cash -> stock inside one portfolio. Deliberately not an XIRR event.
    BUY = "BUY"
    #: Stock -> cash inside one portfolio. Deliberately not an XIRR event either.
    SELL = "SELL"
    #: A dividend credited to the portfolio holding the stock. Return, not contribution.
    DIVIDEND = "DIVIDEND"

    @property
    def is_external(self) -> bool:
        """True when this flow crosses the boundary of the whole broker account.

        External kinds carry no portfolio; every other kind must name one. That is migration
        0022's ``portfolio_cash_flow_external_has_no_portfolio`` constraint, and this property is
        the single place the split is spelled out so the check and the arithmetic cannot drift.
        """
        return self in EXTERNAL_KINDS

    @property
    def is_xirr_event(self) -> bool:
        """True only for ``ASSIGN`` and ``RELEASE`` — §4.4's whole point.

        Per-portfolio XIRR is computed from nothing else. A buy, a sell and a dividend all change
        what the portfolio is *worth*, and a money-weighted return already accounts for that
        through the closing value; counting them as flows as well would count them twice and make
        the return move when the user trades rather than when the user invests.
        """
        return self in XIRR_EVENT_KINDS


#: The kinds with no portfolio. Migration 0022's CHECK constraint, as a set.
EXTERNAL_KINDS: Final[frozenset[CashFlowKind]] = frozenset(
    {CashFlowKind.EXTERNAL_DEPOSIT, CashFlowKind.EXTERNAL_WITHDRAWAL}
)

#: The only two kinds a per-portfolio XIRR is allowed to see (§4.4).
XIRR_EVENT_KINDS: Final[frozenset[CashFlowKind]] = frozenset(
    {CashFlowKind.ASSIGN, CashFlowKind.RELEASE}
)

#: Kinds that move cash against a named instrument, and therefore must name one.
STOCK_KINDS: Final[frozenset[CashFlowKind]] = frozenset({CashFlowKind.BUY, CashFlowKind.SELL})

#: Kinds that move cash and nothing else. An instrument on one of these is a caller bug: there is
#: no stock involved in a deposit or in an assignment, and a stray instrument id would make the
#: activity feed (§7) claim a trade that never happened.
CASH_ONLY_KINDS: Final[frozenset[CashFlowKind]] = EXTERNAL_KINDS | XIRR_EVENT_KINDS

#: What each kind does to the broker account's **Unallocated** bucket (§4.4).
#:
#: ``BUY``, ``SELL`` and ``DIVIDEND`` are zero here and that is the interesting entry, not an
#: omission: they all happen *inside* a portfolio, so they never touch the bucket that holds the
#: money not yet committed to one.
_UNALLOCATED_SIGN: Final[Mapping[CashFlowKind, Decimal]] = {
    CashFlowKind.EXTERNAL_DEPOSIT: ONE,
    CashFlowKind.EXTERNAL_WITHDRAWAL: MINUS_ONE,
    CashFlowKind.ASSIGN: MINUS_ONE,
    CashFlowKind.RELEASE: ONE,
    CashFlowKind.BUY: ZERO,
    CashFlowKind.SELL: ZERO,
    CashFlowKind.DIVIDEND: ZERO,
}

#: What each kind does to the cash held **inside** a portfolio.
#:
#: A dividend is ``+1`` because the cash genuinely arrives in the portfolio that owns the share.
#: It is not an XIRR event, which is the same statement from the other side: the user did not
#: contribute that money, the investment earned it, and a money-weighted return should read it as
#: performance rather than as a deposit.
_PORTFOLIO_SIGN: Final[Mapping[CashFlowKind, Decimal]] = {
    CashFlowKind.EXTERNAL_DEPOSIT: ZERO,
    CashFlowKind.EXTERNAL_WITHDRAWAL: ZERO,
    CashFlowKind.ASSIGN: ONE,
    CashFlowKind.RELEASE: MINUS_ONE,
    CashFlowKind.BUY: MINUS_ONE,
    CashFlowKind.SELL: ONE,
    CashFlowKind.DIVIDEND: ONE,
}


@dataclass(frozen=True, slots=True)
class PortfolioCashFlow:
    """One row of ``portfolio_cash_flow``, with §4.4's rules enforced in the constructor.

    Frozen because a ledger entry is a record of something that happened; a flow you can edit in
    place is a balance that changes without an event, which is how a return series comes to
    disagree with its own history.

    ``amount`` is always positive — the direction lives in the kind (see the module docstring).
    ``portfolio_id`` is ``None`` for the external kinds and required for every other kind, which
    is migration 0022's ``portfolio_cash_flow_external_has_no_portfolio`` CHECK expressed where a
    caller meets it first. The database would refuse the row either way; refusing it here means
    the caller learns *which* rule they broke, in a sentence, instead of reading a constraint name
    out of a driver error.
    """

    broker_account_id: int
    kind: CashFlowKind
    amount: Decimal
    occurred_on: dt.date
    portfolio_id: int | None = None
    instrument_id: int | None = None
    quantity: Decimal | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if self.amount <= ZERO:
            raise ValueError(
                f"a cash flow moves a positive amount and its direction comes from its kind; "
                f"got {self.amount} for {self.kind}"
            )
        if money(self.amount) != self.amount:
            raise ValueError(
                f"{self.amount} is finer than a paisa, and portfolio_cash_flow.amount stores two "
                "decimal places; round it at write time rather than letting a balance exist that "
                "the ledger cannot reproduce"
            )
        if self.kind.is_external and self.portfolio_id is not UNALLOCATED:
            raise ValueError(
                f"{self.kind} crosses the boundary of the whole broker account and belongs to no "
                f"portfolio, but it names portfolio {self.portfolio_id}; money arriving from "
                "outside lands in Unallocated, and assigning it is a separate event (spec "
                "section 4.4)"
            )
        if not self.kind.is_external and self.portfolio_id is UNALLOCATED:
            raise ValueError(
                f"{self.kind} is an internal flow and must name the portfolio it happened in; "
                "without one it can be neither valued nor attributed, and a per-portfolio XIRR "
                "computed around it would silently omit a contribution (spec section 4.4)"
            )
        self._check_instrument()

    def _check_instrument(self) -> None:
        """Instrument and quantity belong to a trade and to nothing else.

        Split out from :meth:`__post_init__` so the two rules a reader comes here for — the sign
        and the portfolio — are not buried under the ones they rarely need.
        """
        if self.kind in STOCK_KINDS:
            if self.instrument_id is None:
                raise ValueError(f"{self.kind} moves cash against a stock and must name it")
            if self.quantity is None or self.quantity <= ZERO:
                raise ValueError(f"{self.kind} must carry the quantity traded; got {self.quantity}")
            if self.quantity.quantize(QUANTITY_PRECISION, rounding=ROUND_HALF_UP) != self.quantity:
                raise ValueError(
                    f"{self.quantity} is finer than the four decimals a quantity stores "
                    "(allocation_ledger.QUANTITY_PRECISION)"
                )
        else:
            if self.kind in CASH_ONLY_KINDS and self.instrument_id is not None:
                raise ValueError(
                    f"{self.kind} moves cash and nothing else, but it names instrument "
                    f"{self.instrument_id}; an activity feed would read that as a trade that "
                    "never happened"
                )
            if self.quantity is not None:
                raise ValueError(
                    f"{self.kind} moves no shares, so it carries no quantity; got {self.quantity}"
                )


# ---------------------------------------------------------------------------
# Balances — §4.4's Unallocated bucket, and the cash inside a portfolio
# ---------------------------------------------------------------------------


def unallocated_cash(flows: Sequence[PortfolioCashFlow], broker_account_id: int) -> Decimal:
    """The Unallocated cash bucket for one broker account (§4.4).

    Derived from the flows rather than stored, even though ``broker_cash.balance`` exists: the
    materialised column is a cache for a page that must not sum a lifetime of rows, and this is
    the definition it is a cache *of*. When they disagree, this one is right.

    Deposits raise it, withdrawals and assignments lower it, releases raise it again, and the
    three inside-a-portfolio kinds leave it alone. §6.6 makes this number the centerpiece of the
    Overview page, so it is the number a user is most likely to check against their broker.
    """
    total = ZERO
    for flow in flows:
        if flow.broker_account_id == broker_account_id:
            total += _UNALLOCATED_SIGN[flow.kind] * flow.amount
    return money(total)


def unallocated_cash_by_broker(flows: Sequence[PortfolioCashFlow]) -> dict[int, Decimal]:
    """Every broker account's Unallocated bucket, in one pass.

    §6.1 shows sync status per broker and §6.6 shows unallocated cash beside it, so the page needs
    a row per account; calling :func:`unallocated_cash` once per row would re-walk the whole flow
    list per row. An account that appears only in inside-a-portfolio flows still gets a key, with
    a zero — a bucket we have seen and that is currently empty is not the same as a broker we know
    nothing about, and a caller rendering the section should not have to guess which it has.
    """
    totals: dict[int, Decimal] = {}
    for flow in flows:
        running = totals.get(flow.broker_account_id, ZERO)
        totals[flow.broker_account_id] = running + _UNALLOCATED_SIGN[flow.kind] * flow.amount
    return {broker_account_id: money(total) for broker_account_id, total in totals.items()}


def portfolio_cash(flows: Sequence[PortfolioCashFlow], portfolio_id: int | None) -> Decimal:
    """The cash sitting inside one capital portfolio — assigned but not yet spent.

    This is the second half of criterion 1: a portfolio's contribution to net worth is the value
    of its holdings *plus* this. Leave it out and the parts no longer sum to the whole on any day
    a rebalance has sold something that has not been re-bought.

    ``None`` is refused rather than treated as Unallocated. It is tempting to mirror
    :func:`baskfy_core.allocation_ledger.portfolio_value`, which accepts ``None`` for the
    unallocated holdings — but Unallocated cash is per *broker account*, not per user, so there is
    no single number to return here. Answering with a plausible one would be the more dangerous
    kind of wrong.
    """
    if portfolio_id is UNALLOCATED:
        raise ValueError(
            "Unallocated is not a portfolio and its cash is held per broker account; "
            "call unallocated_cash(flows, broker_account_id) instead (spec section 4.4)"
        )
    total = ZERO
    for flow in flows:
        if flow.portfolio_id == portfolio_id:
            total += _PORTFOLIO_SIGN[flow.kind] * flow.amount
    return money(total)


def portfolio_cash_by_portfolio(flows: Sequence[PortfolioCashFlow]) -> dict[int, Decimal]:
    """Every portfolio's internal cash, in one pass. §6.5 draws a row per portfolio."""
    totals: dict[int, Decimal] = {}
    for flow in flows:
        if flow.portfolio_id is None:
            continue
        running = totals.get(flow.portfolio_id, ZERO)
        totals[flow.portfolio_id] = running + _PORTFOLIO_SIGN[flow.kind] * flow.amount
    return {portfolio_id: money(total) for portfolio_id, total in totals.items()}


def total_cash(flows: Sequence[PortfolioCashFlow]) -> Decimal:
    """Every rupee of cash the user holds, wherever it currently sits.

    Deliberately computed by adding the two sign tables rather than from a third table of its own.
    Every rupee is either in a broker's Unallocated bucket or inside a portfolio, and an
    assignment moves it between the two — so ``ASSIGN``'s two entries are ``-1`` and ``+1`` and
    cancel here, which is what "internal" means, expressed as arithmetic instead of as a comment.
    A third table would be a third place for the rule to be wrong.

    The identity that falls out, and that a test asserts::

        sum(unallocated_cash_by_broker) + sum(portfolio_cash_by_portfolio) == total_cash

    Buys and sells do *not* cancel: they cross between cash and stock, so cash is genuinely lower
    after a buy. The other half of that rupee is in the holdings, which is
    ``allocation_ledger``'s to value.
    """
    total = ZERO
    for flow in flows:
        total += (_UNALLOCATED_SIGN[flow.kind] + _PORTFOLIO_SIGN[flow.kind]) * flow.amount
    return money(total)


# ---------------------------------------------------------------------------
# Per-portfolio XIRR — §4.4's reason for existing
# ---------------------------------------------------------------------------


def xirr_events(flows: Sequence[PortfolioCashFlow], portfolio_id: int) -> list[CashFlow]:
    """The cash flows a per-portfolio XIRR may see, and no others (§4.4).

    Filtered by :attr:`CashFlowKind.is_xirr_event`, so exactly ``ASSIGN`` and ``RELEASE`` survive.
    Buys, sells and dividends are dropped — not because they are unimportant, but because their
    effect is already in the closing value that :func:`portfolio_xirr` appends. Counting them
    here as well would count them twice.

    Signs follow ``curated_accounting``'s convention, which is the desk's and smallcase's: money
    the investor puts in is negative, money that comes back is positive. So an assignment is
    negative and a release is positive — the mirror image of :data:`_PORTFOLIO_SIGN`, because
    that table is written from the portfolio's point of view and a cash-flow series is written
    from the investor's.

    Returned as ``curated_accounting.CashFlow`` rather than as a shape of our own, because these
    go straight into ``curated_accounting.xirr`` and this product has exactly one XIRR solver.
    """
    events: list[CashFlow] = []
    for flow in flows:
        if flow.portfolio_id != portfolio_id or not flow.kind.is_xirr_event:
            continue
        events.append(
            CashFlow(on=flow.occurred_on, amount=money(-_PORTFOLIO_SIGN[flow.kind] * flow.amount))
        )
    return events


def portfolio_xirr(
    flows: Sequence[PortfolioCashFlow],
    portfolio_id: int,
    *,
    as_of: dt.date,
    closing_value: Decimal,
) -> Decimal | None:
    """One capital portfolio's money-weighted return, from internal flows and a closing value.

    ``closing_value`` is everything the portfolio is worth on ``as_of`` — the market value of its
    holdings *plus* :func:`portfolio_cash`. It is a parameter rather than something computed here
    because valuing holdings needs prices, and prices need I/O (law 1). The caller that has the
    EOD marks (§5.1) already has both halves.

    Returns ``None`` when the portfolio has never been assigned cash. That is the honest answer:
    a money-weighted return with no contribution to weight is not a small number, it is not a
    number. ``None`` also comes back from the solver when no root exists, and both cases reach
    ``headline_metric(..., value=None)`` in ``allocation_ledger``, which turns them into a labelled
    "unavailable" figure rather than a zero (criterion 3).

    ``as_of`` earlier than the last assignment is refused rather than answered. Such a series
    describes a portfolio being valued before its own history finished, and the solver would
    return a confident number for it.

    Note what is *not* applied here: ``curated_accounting.xirr_displayable``'s 365-day gate. That
    rule belongs to the curated-basket product, and §5.2 states no equivalent for user portfolios;
    whether a three-week-old XIRR is fit to show is a decision for the surface showing it, next to
    the label it is required to carry. Computing a number and choosing to display it are different
    questions, and this module only answers the first.
    """
    if closing_value < ZERO:
        raise ValueError(f"a portfolio's closing value cannot be negative; got {closing_value}")
    events = xirr_events(flows, portfolio_id)
    if not events:
        return None
    last_event = max(event.on for event in events)
    if as_of < last_event:
        raise ValueError(
            f"as_of {as_of} precedes the last cash-flow event {last_event}; a portfolio cannot be "
            "valued before its own history is complete"
        )
    return xirr([*events, CashFlow(on=as_of, amount=money(closing_value))])
