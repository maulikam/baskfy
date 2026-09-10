"""The nightly end-of-day NAV job — ``PORTFOLIO_REDESIGN.md`` §5.1, §4.3, §5.2.

§5.1, verbatim, is the whole specification of this module: *"v1 is end-of-day only, presented
honestly, like a fund NAV: a nightly job computes an official EOD value per portfolio and for the
consolidated total. The EOD NAV series powers everything: combined chart, per-day P&L,
contribution, drawdown."*

This is that job. It is the **I/O half** of the valuation: it reads holdings, prices, cash flows
and reconciliation items out of the database, hands them to the pure arithmetic in
``baskfy_core.allocation_ledger`` and ``baskfy_core.cash_ledger``, and writes one
``portfolio_nav_daily`` row per capital portfolio plus one consolidated row. Not one rupee is
added up here that the domain layer could add up instead — law 1 (``packages/core`` touches
nothing) is only worth anything if the I/O side actually calls into it rather than reimplementing
the sums where the database is handy.

WHY THE SERIES IS STORED RATHER THAN COMPUTED WHEN SOMEONE OPENS THE PAGE
-------------------------------------------------------------------------
Two reasons, and the second is the one that matters. The first is cost: §6.3 draws 1M/3M/1Y/3Y/All
ranges, and re-valuing a year of holdings per page load is work nobody needs done twice. The
second is truth. "Valued at close of {date}" (§6.1) is a claim *about that day*. If the number
were re-derived on read, then moving a holding into a different portfolio tomorrow would silently
rewrite every yesterday — the chart would change shape because of an organisational decision, not
because of the market. A stored row is a record; a derivation is an opinion that keeps changing.

WHAT "HONESTLY" COSTS, CONCRETELY
---------------------------------
Four refusals, each of which makes the job produce *less* than it easily could:

1. **A day the market did not print gets no rows at all.** Not zeroes, not a carried-forward
   copy of Friday stamped with Saturday's date. A gap in the series is readable as "no close
   that day"; a zero is readable as "you lost everything", and a chart draws it that way.
2. **A monitoring view never gets a row.** §4.1: a lens overlaps other portfolios by design and
   is excluded from every total. The exclusion here is structural — monitoring holdings are never
   loaded — rather than a filter applied at the end, because a filter is something a later edit
   can forget and a double-counted net worth is the worst number this product can print.
3. **An open reconciliation item freezes the holding it concerns** (§4.3). The holding drops out
   of the day's valuation and every row it would have touched is written with
   ``pending_reconciliation = true``. The row is still written — omitting it would leave the gap
   of (1), which reads as zero — but it says out loud that it is incomplete. *"Never silently
   corrupt a portfolio's return series."*
4. **A holding whose instrument has no price on or before the date is not valued at zero.** It is
   treated exactly like (3): dropped, and its portfolio flagged. ``holding_value`` in the domain
   layer raises on a missing price for precisely this reason — a zero produces a total that is
   wrong and still balances.

CRITERION 1 IS A PROPERTY OF THE WRITE, NOT AN ASSERTION AFTER IT
------------------------------------------------------------------
*"Sum of all capital portfolios + unallocated (stocks + cash) equals consolidated net worth, to
the paisa, at all times."* Nothing in this module adds the parts up and compares them to the
whole. Instead both come from the same two calls over the same two lists:

* stocks — :func:`baskfy_core.allocation_ledger.portfolio_values` and
  :func:`~baskfy_core.allocation_ledger.consolidated_value` walk the identical ``holdings``
  sequence, and every holding is allocated to exactly one capital portfolio or to Unallocated
  (criterion 2, enforced by the database's partial unique index and re-checked by
  ``validate_allocations``). Each holding is quantised to paise *before* it is summed, in both
  functions, so the headline and its own breakdown cannot disagree by a rounding step;
* cash — :func:`baskfy_core.cash_ledger.total_cash` is defined as the sum of the unallocated and
  per-portfolio sign tables, so ``ASSIGN``'s ``-1`` and ``+1`` cancel by construction. The
  identity ``sum(unallocated) + sum(per-portfolio) == total`` is arithmetic, not a coincidence.

They therefore cannot drift, which is a stronger guarantee than checking afterwards.
``test_portfolio_nav_job.py`` still asserts it, on prices chosen so that a ``float``
implementation would be off by a paisa — because the guarantee is only as good as the claim that
every rupee here is a ``Decimal`` (house rule 9).

WHAT ``net_flow`` IS FOR, AND WHY IT IS A DIFFERENT KIND OF FLOW ON THE CONSOLIDATED ROW
-----------------------------------------------------------------------------------------
A time-weighted return has to divide the day at each cash flow, otherwise money arriving looks
like performance. The flow that does that is *the money that entered or left the pot without
being a return* — and which flows those are depends on which pot you mean:

* **A capital portfolio's** pot is filled by ``ASSIGN`` and drained by ``RELEASE`` (§4.4). A
  ``BUY`` inside it is cash turning into stock and changes nothing about the pot's size, so it is
  not a flow. Neither is a ``SELL`` or a ``DIVIDEND``. This is exactly
  ``cash_ledger.XIRR_EVENT_KINDS`` and it is not a coincidence: the same two kinds divide the day
  for TWR and are the events for XIRR.
* **The consolidated pot** is the whole broker relationship, so ``ASSIGN`` moves money from one of
  its pockets to another and must *not* divide the day — counting it there would penalise the
  user's total return for organising their own holdings. What crosses the consolidated boundary is
  ``EXTERNAL_DEPOSIT`` and ``EXTERNAL_WITHDRAWAL``.

Writing ``ASSIGN``-minus-``RELEASE`` on the consolidated row would have matched the per-portfolio
rows and been wrong in the only way that matters, so the two rows carry different flows and this
paragraph exists so nobody "fixes" the inconsistency.

THE "SINCE GROUPED" MARKS (§5.2)
--------------------------------
§5.2 gives a holding group *"since grouped" return (from EOD marks at grouping date)* and then
forbids the obvious upgrade: *"Until then, do NOT display XIRR or since-purchase P&L for these."*
Both halves are written here, and they are written into two different columns on purpose:

* ``portfolio_holding.first_bought_on`` is set to ``added_on`` — the day this holding was grouped
  into this portfolio, which is the earliest day we have any mark for. That is the anchor date
  "since grouped" is measured from.
* ``portfolio_holding.history_source`` is deliberately **left at ``NONE``**. It records where a
  purchase fact came from, and a grouping date is not a purchase fact. Advancing it would be the
  lie that quietly unlocks the XIRR §5.2 forbids, on a date the user never bought anything.

So ``history_source = 'NONE'`` with ``first_bought_on`` set reads as *"grouped on this date;
purchase history still unknown"*, and only a CAS import (§5.3) may move it off ``NONE``. The
update only ever touches rows where ``first_bought_on IS NULL`` and ``history_source = 'NONE'``,
so it can never overwrite a real purchase date and re-running it is a no-op (house rule 7).

NOT AN ORDER PATH
-----------------
Nothing here names a side, a product, a venue or an order type, and nothing reaches a broker.
``test_portfolio_nav_job.py`` scans this module's source for that vocabulary, the same guard
``allocation_ledger`` and ``portfolio_units`` carry, so an edit that quietly crosses the line
fails a test rather than a review.

ON ``baskfy_core.portfolio_nav``
--------------------------------
That module (TWR, drawdown, since-grouped) is a sibling leaf and is **downstream** of this one:
it reads the series this job writes. There is deliberately no import of it here and no adapter
standing in for one — a NAV job that needed a return calculation to compute a NAV would have the
dependency backwards.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from sqlalchemy import Date, bindparam, delete, literal_column, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.allocation_ledger import (
    UNALLOCATED,
    Allocation,
    Holding,
    HoldingKey,
    PortfolioKind,
    PortfolioSource,
    consolidated_value,
    portfolio_values,
)
from baskfy_core.allocation_ledger import (
    Portfolio as LedgerPortfolio,
)
from baskfy_core.cash_ledger import (
    CashFlowKind,
    portfolio_cash_by_portfolio,
    total_cash,
    unallocated_cash_by_broker,
)
from baskfy_core.cash_ledger import (
    PortfolioCashFlow as LedgerCashFlow,
)
from baskfy_core.gst import money
from baskfy_core.models import OhlcvDaily, Portfolio, PortfolioHolding
from baskfy_core.models.accounts import (
    BrokerAccount,
    PortfolioNavDaily,
    ReconciliationItem,
)
from baskfy_core.models.accounts import (
    PortfolioCashFlow as CashFlowRow,
)
from baskfy_core.models.base import JsonObject

ZERO: Final = Decimal("0")

#: The unique index migration 0023 created, and the arbiter of the upsert below. Named rather
#: than inferred so that a schema change which drops it fails here loudly instead of silently
#: turning "re-run the date" into "append the date again" (house rule 7).
NAV_CONFLICT_COLUMNS: Final[tuple[str, str, str]] = ("user_id", "date", "portfolio_id")

#: ``reconciliation_item.state`` for a question nobody has answered yet. Only OPEN freezes.
RECONCILE_OPEN: Final = "OPEN"

#: ``portfolio_holding.history_source`` for "we have no purchase record". See the module
#: docstring: the "since grouped" mark is written *without* moving this off NONE, because a
#: grouping date is not a purchase date and §5.2 forbids the XIRR that a purchase date unlocks.
HISTORY_SOURCE_NONE: Final = "NONE"


@dataclass(frozen=True, slots=True)
class NavRow:
    """One ``portfolio_nav_daily`` row, before it is written.

    ``portfolio_id`` is ``None`` for the consolidated row — the same sentinel the table uses, and
    the same one ``allocation_ledger.UNALLOCATED`` uses for "no capital portfolio", because in
    both places it means "this is not about one portfolio".

    Frozen: a row is a claim about a day. Something that can be edited after the fact is a claim
    that can change without an event, which is how a series comes to disagree with its history.
    """

    user_id: int
    portfolio_id: int | None
    market_value: Decimal
    cash: Decimal
    net_flow: Decimal
    pending_reconciliation: bool

    @property
    def total_value(self) -> Decimal:
        """Stocks plus cash — this row's contribution to net worth (criterion 1)."""
        return money(self.market_value + self.cash)


@dataclass(frozen=True, slots=True)
class UserNav:
    """Everything one user's day resolved to, including the parts that get no row of their own.

    Unallocated has no ``portfolio_nav_daily`` row: it is not a portfolio, so giving it one would
    let it be renamed, deleted or given a benchmark (the same reasoning
    ``allocation_ledger.UNALLOCATED`` records). It is nevertheless part of consolidated net worth,
    so it is carried here — which is what lets a caller, and the acceptance test, check criterion
    1 without re-deriving anything: ``consolidated.total_value`` must equal the capital rows plus
    :attr:`unallocated_market_value` plus :attr:`unallocated_cash`.
    """

    user_id: int
    consolidated: NavRow
    portfolio_rows: tuple[NavRow, ...]
    unallocated_market_value: Decimal
    unallocated_cash: Decimal
    #: Holdings dropped from the day's valuation because a question about them is still open
    #: (§4.3) or because no price exists on or before the date. Both are counted so an operator
    #: can tell "one frozen holding" from "the price feed is down".
    frozen_holdings: int = 0
    unpriced_holdings: int = 0

    @property
    def all_rows(self) -> tuple[NavRow, ...]:
        """Every row this user's day actually writes, consolidated first.

        A view rather than a field: a stored copy of the same rows is a second thing to keep in
        step with the first, and the two would drift the moment a row was recomputed.
        """
        return (self.consolidated, *self.portfolio_rows)


@dataclass(slots=True)
class NavRunResult:
    """What the job did, in the shape a Celery result and an operator can both read."""

    as_of: dt.date
    users: list[UserNav] = field(default_factory=list)
    rows_written: int = 0
    rows_removed: int = 0
    marks_written: int = 0
    #: Set when the job wrote nothing on purpose. ``None`` means it ran.
    skipped_reason: str | None = None

    def as_json(self) -> JsonObject:
        """A JSON-serialisable summary. Money is stringified, never floated (house rule 9)."""
        return {
            "as_of": self.as_of.isoformat(),
            "users": len(self.users),
            "rows_written": self.rows_written,
            "rows_removed": self.rows_removed,
            "marks_written": self.marks_written,
            "pending_reconciliation": sum(
                1 for user in self.users for row in user.all_rows if row.pending_reconciliation
            ),
            "net_worth": {
                str(user.user_id): str(user.consolidated.total_value) for user in self.users
            },
            "skipped_reason": self.skipped_reason,
        }


async def run_portfolio_nav(session: AsyncSession, as_of: dt.date) -> NavRunResult:
    """Value every user's portfolios at the close of ``as_of`` and store the day (§5.1).

    Idempotent by construction (house rule 7): every row is an upsert keyed on
    ``(user_id, date, portfolio_id)``, and any row left over for a portfolio that no longer earns
    one — it became a monitoring view, or its holdings moved away — is deleted rather than left
    behind to be summed by tomorrow's chart. Running the same date twice produces the same table.

    Returns without writing anything when the market did not print on ``as_of``. That is not an
    error and does not raise: a Saturday, a holiday and a day the ingest has not yet landed are
    all "no official close exists", and inventing one is the failure this refusal exists to
    prevent.
    """
    result = NavRunResult(as_of=as_of)
    if not await _market_printed(session, as_of):
        result.skipped_reason = f"no bars on {as_of.isoformat()}; the market printed no close"
        return result

    for user_id in await _users_with_a_ledger(session):
        user_nav = await _value_user(session, as_of, user_id)
        if user_nav is None:
            # Nothing left to value, but there may be rows from a run when there was. Convergence
            # is the whole of house rule 7 here: an upsert cannot remove what should no longer
            # exist, and a stale row keeps being summed by every chart that reads the series.
            result.rows_removed += await _remove_every_row(session, as_of, user_id)
            continue
        result.users.append(user_nav)
        result.rows_written += await _write_rows(session, as_of, user_nav)
        result.rows_removed += await _remove_stale_rows(session, as_of, user_nav)
        result.marks_written += await _mark_since_grouped(session, as_of, user_nav)
    return result


# ---------------------------------------------------------------------------
# Reading — every query in the job lives below, so law 1 has a visible boundary
# ---------------------------------------------------------------------------


async def _market_printed(session: AsyncSession, as_of: dt.date) -> bool:
    """Did the exchange print *any* close on this date?

    One indexed existence check, and the gate for refusal (1) in the module docstring. Deliberately
    not ``trading_day``: a day the calendar calls open but for which no bar has been ingested yet
    is, for valuation purposes, a day with no close. The calendar says what *should* exist; this
    says what does. Valuing against a calendar entry would produce a NAV out of prices from
    whenever the last successful ingest happened to be.
    """
    found = await session.scalar(select(OhlcvDaily.instrument_id).where(OhlcvDaily.date == as_of))
    return found is not None


async def _users_with_a_ledger(session: AsyncSession) -> list[int]:
    """Every user who could have a NAV: one with a portfolio, or with a broker account.

    Both halves are needed and neither implies the other. A user who has connected a broker but
    organised nothing yet holds cash and unallocated stock, and §6.6 makes that the centerpiece of
    the page rather than a footnote — so they get a consolidated row. A user with portfolios but
    no broker account is the fixture case and the pre-sync case, and they get rows too.

    Sorted so a run is deterministic, which is what makes "re-running changes nothing" checkable
    by comparing whole tables rather than sets.
    """
    portfolio_users = select(Portfolio.user_id)
    broker_users = select(BrokerAccount.user_id)
    rows = await session.scalars(portfolio_users.union(broker_users).order_by(Portfolio.user_id))
    return [int(user_id) for user_id in rows.all()]


async def _load_portfolios(session: AsyncSession, user_id: int) -> dict[int, LedgerPortfolio]:
    """This user's portfolios as domain objects — both kinds, keyed by id.

    Monitoring views are loaded even though they get no row: ``validate_allocations`` needs them
    present to be able to *refuse* an allocation that names one (§4.1). Leaving them out would
    turn "a lens is not an allocation" into "we did not look", and the two produce very different
    behaviour on the day a bug writes such a row.

    ``started_on`` is read through ``literal_column`` because migration 0022 added the column and
    the ``Portfolio`` ORM class has not caught up — that file belongs to another leaf, and adding
    the attribute here would put two definitions of the same table in the tree. The column is in
    the database; this reads it without claiming ownership of the model.
    """
    started_on = literal_column("portfolio.started_on", Date())
    rows = await session.execute(
        select(
            Portfolio.id,
            Portfolio.name,
            Portfolio.kind,
            Portfolio.source,
            started_on,
        ).where(Portfolio.user_id == user_id)
    )
    portfolios: dict[int, LedgerPortfolio] = {}
    for portfolio_id, name, kind, source, start in rows.tuples():
        portfolios[int(portfolio_id)] = LedgerPortfolio(
            portfolio_id=int(portfolio_id),
            name=str(name),
            kind=PortfolioKind(kind),
            source=PortfolioSource(source),
            started_on=start,
        )
    return portfolios


async def _load_holdings(
    session: AsyncSession, capital_ids: Sequence[int]
) -> list[tuple[int, HoldingKey, Decimal]]:
    """``(portfolio_id, key, quantity)`` for every holding in a capital portfolio.

    Monitoring holdings are excluded by never being asked for — refusal (2). A NULL or
    non-positive quantity is dropped here rather than valued: ``Holding`` refuses a negative
    quantity outright, and a zero-quantity row is a position that has been fully sold, which
    contributes nothing and would only add a phantom line to the holdings tab.
    """
    if not capital_ids:
        return []
    rows = await session.execute(
        select(
            PortfolioHolding.portfolio_id,
            PortfolioHolding.instrument_id,
            PortfolioHolding.broker_account_id,
            PortfolioHolding.quantity,
        ).where(PortfolioHolding.portfolio_id.in_(capital_ids))
    )
    holdings: list[tuple[int, HoldingKey, Decimal]] = []
    for portfolio_id, instrument_id, broker_account_id, quantity in rows.tuples():
        if quantity is None or quantity <= ZERO:
            continue
        key = HoldingKey(instrument_id=int(instrument_id), broker_account_id=int(broker_account_id))
        holdings.append((int(portfolio_id), key, Decimal(quantity)))
    return holdings


async def _load_prices(
    session: AsyncSession, instrument_ids: Sequence[int], as_of: dt.date
) -> dict[int, Decimal]:
    """The latest exchange print on or before ``as_of``, per instrument.

    ``close_raw``, not ``close``: house rule 6 says the adjusted series is what factors read and
    the exchange print is what the user is shown, and a portfolio's market value is the most
    literal "what the user expects a real price" there is. Valuing at the adjusted close would
    make today's net worth move when a split from 2019 was reprocessed.

    *On or before*, not *on*: a stock that did not trade today is worth its last traded price,
    which is what every fund NAV in the country does. The refusal that keeps this honest is one
    level up — :func:`_market_printed` has already established that the market printed *something*
    on this date, so this can only carry a single illiquid name forward, never a whole weekend.

    ``DISTINCT ON`` does the whole thing in one indexed pass; the alternative, a correlated
    ``MAX(date)`` subquery per instrument, is the same answer once per holding.
    """
    if not instrument_ids:
        return {}
    rows = await session.execute(
        select(OhlcvDaily.instrument_id, OhlcvDaily.close_raw)
        .where(OhlcvDaily.instrument_id.in_(instrument_ids), OhlcvDaily.date <= as_of)
        .order_by(OhlcvDaily.instrument_id, OhlcvDaily.date.desc())
        .distinct(OhlcvDaily.instrument_id)
    )
    return {int(instrument_id): Decimal(close) for instrument_id, close in rows.tuples()}


async def _load_open_reconciliations(
    session: AsyncSession, user_id: int, as_of: dt.date
) -> set[HoldingKey]:
    """The holdings §4.3 freezes: an unanswered question, detected on or before this date.

    Bounded by ``detected_on`` so that re-running an old date is not retroactively poisoned by a
    question raised last week. A day is valued with what was known about it, which is the same
    point-in-time discipline house rule 5 makes non-negotiable everywhere else.

    RESOLVED and DISMISSED items do not freeze anything — a resolved item names the portfolio it
    was resolved to (migration 0022 enforces that with a CHECK), so the holding has an owner
    again and there is nothing left to guess.
    """
    rows = await session.execute(
        select(ReconciliationItem.instrument_id, ReconciliationItem.broker_account_id).where(
            ReconciliationItem.user_id == user_id,
            ReconciliationItem.state == RECONCILE_OPEN,
            ReconciliationItem.detected_on <= as_of,
        )
    )
    return {
        HoldingKey(instrument_id=int(instrument_id), broker_account_id=int(broker_account_id))
        for instrument_id, broker_account_id in rows.tuples()
    }


async def _load_flows(session: AsyncSession, user_id: int, as_of: dt.date) -> list[LedgerCashFlow]:
    """Every cash flow in this user's broker accounts, up to and including ``as_of``.

    Converted into ``cash_ledger.PortfolioCashFlow`` rather than summed here: the domain object's
    constructor is where §4.4's rules live (a positive amount whose direction comes from its kind,
    an external flow that names no portfolio, an internal one that must). Building it means a row
    that breaks those rules is refused with a sentence rather than quietly added up.

    The balances are derived from these flows and not read from ``broker_cash.balance``, on
    ``cash_ledger.unallocated_cash``'s own instruction: the column is a cache for a page that must
    not sum a lifetime of rows, this is the definition it caches, and when they disagree the flows
    are right. It is also the only version that can answer for a *past* date, which is exactly
    what a NAV series is made of.
    """
    rows = await session.execute(
        select(
            CashFlowRow.broker_account_id,
            CashFlowRow.portfolio_id,
            CashFlowRow.kind,
            CashFlowRow.amount,
            CashFlowRow.occurred_on,
            CashFlowRow.instrument_id,
            CashFlowRow.quantity,
        )
        .join(BrokerAccount, BrokerAccount.id == CashFlowRow.broker_account_id)
        .where(BrokerAccount.user_id == user_id, CashFlowRow.occurred_on <= as_of)
    )
    flows: list[LedgerCashFlow] = []
    for (
        broker_account_id,
        portfolio_id,
        kind,
        amount,
        occurred_on,
        instrument_id,
        qty,
    ) in rows.tuples():
        flows.append(
            LedgerCashFlow(
                broker_account_id=int(broker_account_id),
                kind=CashFlowKind(kind),
                amount=Decimal(amount),
                occurred_on=occurred_on,
                portfolio_id=None if portfolio_id is None else int(portfolio_id),
                instrument_id=None if instrument_id is None else int(instrument_id),
                quantity=None if qty is None else Decimal(qty),
            )
        )
    return flows


# ---------------------------------------------------------------------------
# Valuing one user — the domain layer does the arithmetic, this assembles its inputs
# ---------------------------------------------------------------------------


async def _value_user(session: AsyncSession, as_of: dt.date, user_id: int) -> UserNav | None:
    """Resolve one user's day into the rows it should have. ``None`` when they should have none.

    A user with no portfolio of *either* kind and no cash flow gets nothing rather than a
    consolidated row of zeroes. The zero row would be true and useless: it says "this user's net
    worth is nil" on every day since the account was created, and §6.6's empty state (*connect
    your broker*) is the honest surface for that, not a flat line at zero.

    A user whose only portfolio is a monitoring view is *not* that case, and gets a consolidated
    row of zero. They have organised something; §4.1 simply says none of it counts towards a
    total. Writing the row is what makes "a lens never enters a total" visible in the data rather
    than inferable from an absence, and it is also the row that has to exist for a portfolio which
    was capital yesterday to lose its own row today.
    """
    portfolios = await _load_portfolios(session, user_id)
    capital_ids = sorted(
        portfolio_id
        for portfolio_id, portfolio in portfolios.items()
        if portfolio.kind is PortfolioKind.CAPITAL
    )
    flows = await _load_flows(session, user_id, as_of)
    if not portfolios and not flows:
        return None
    holding_rows = await _load_holdings(session, capital_ids)

    _refuse_monitoring_flows(portfolios, flows)

    frozen_keys = await _load_open_reconciliations(session, user_id, as_of)
    prices = await _load_prices(
        session, sorted({key.instrument_id for _, key, _ in holding_rows}), as_of
    )

    holdings: list[Holding] = []
    allocations: list[Allocation] = []
    pending_portfolios: set[int] = set()
    frozen_count = 0
    unpriced_count = 0
    for portfolio_id, key, quantity in holding_rows:
        if key in frozen_keys:
            # Refusal (3): the holding is not valued and its portfolio says so. Attributing it
            # anyway is exactly the "guess" §4.3 forbids, and the guess is invisible afterwards.
            frozen_count += 1
            pending_portfolios.add(portfolio_id)
            continue
        if key.instrument_id not in prices:
            # Refusal (4): no print on or before this date. A zero here would be a total that is
            # wrong and still balances, which is the one failure criterion 1 cannot catch.
            unpriced_count += 1
            pending_portfolios.add(portfolio_id)
            continue
        holdings.append(Holding(key=key, quantity=quantity))
        allocations.append(Allocation(key=key, portfolio_id=portfolio_id, quantity=quantity))

    # Both calls walk the same list, which is why the parts and the whole cannot disagree.
    values = portfolio_values(holdings, allocations, portfolios, prices)
    consolidated_market_value = consolidated_value(holdings, allocations, portfolios, prices)

    portfolio_cash = portfolio_cash_by_portfolio(flows)
    unallocated_cash = sum(unallocated_cash_by_broker(flows).values(), start=ZERO)
    day_flows = [flow for flow in flows if flow.occurred_on == as_of]

    # Any open question makes the whole day's consolidated figure incomplete, including one about
    # a holding in no portfolio at all — which is the ``UNALLOCATED_HOLDING`` reason, and the most
    # common one there is.
    consolidated_pending = bool(frozen_keys) or bool(pending_portfolios)

    rows = tuple(
        NavRow(
            user_id=user_id,
            portfolio_id=portfolio_id,
            market_value=values.get(portfolio_id, ZERO),
            cash=portfolio_cash.get(portfolio_id, ZERO),
            net_flow=_internal_net_flow(day_flows, portfolio_id),
            pending_reconciliation=portfolio_id in pending_portfolios,
        )
        for portfolio_id in capital_ids
    )
    consolidated = NavRow(
        user_id=user_id,
        portfolio_id=UNALLOCATED,
        market_value=consolidated_market_value,
        cash=total_cash(flows),
        net_flow=_external_net_flow(day_flows),
        pending_reconciliation=consolidated_pending,
    )
    return UserNav(
        user_id=user_id,
        consolidated=consolidated,
        portfolio_rows=rows,
        unallocated_market_value=values.get(UNALLOCATED, ZERO),
        unallocated_cash=money(unallocated_cash),
        frozen_holdings=frozen_count,
        unpriced_holdings=unpriced_count,
    )


def _refuse_monitoring_flows(
    portfolios: Mapping[int, LedgerPortfolio], flows: Sequence[LedgerCashFlow]
) -> None:
    """Refuse a cash flow that names a monitoring view, the way the ledger refuses an allocation.

    §4.1 says a monitoring view is excluded from every total. A flow naming one would be counted
    by ``total_cash`` — which sums flows, not portfolios — and by no per-portfolio row, because a
    monitoring view gets none. Criterion 1 would then fail by exactly that amount, on a day nobody
    changed anything, and the arithmetic above would still look correct.

    Raising rather than skipping: dropping the flow would make the consolidated cash silently too
    low and still balance, and this is a writer bug, not a user mistake — the same reasoning
    ``validate_allocations`` records for refusing rather than repairing.
    """
    for flow in flows:
        if flow.portfolio_id is None:
            continue
        portfolio = portfolios.get(flow.portfolio_id)
        if portfolio is None:
            raise ValueError(
                f"cash flow names portfolio {flow.portfolio_id}, which does not belong to this "
                "user; a flow is attributed to the broker account it moved in, so this is a "
                "cross-tenant row and not a valuation problem"
            )
        if portfolio.kind is PortfolioKind.MONITORING:
            raise ValueError(
                f"{portfolio.name!r} is a monitoring view and holds no cash: it overlaps other "
                "portfolios by design and is excluded from every total (spec section 4.1). The "
                "flow would enter consolidated cash and no portfolio row, breaking acceptance "
                "criterion 1 by its own amount"
            )


def _internal_net_flow(day_flows: Sequence[LedgerCashFlow], portfolio_id: int) -> Decimal:
    """``ASSIGN`` minus ``RELEASE`` for one portfolio on the day (§4.4).

    ``BUY``, ``SELL`` and ``DIVIDEND`` are excluded because they do not change the size of the
    pot — they move value between the cash and the stock halves of the same portfolio. Counting
    them would make a rebalance look like a deposit, and a time-weighted return would then divide
    the day for a trade rather than for a contribution.
    """
    total = ZERO
    for flow in day_flows:
        if flow.portfolio_id != portfolio_id:
            continue
        if flow.kind is CashFlowKind.ASSIGN:
            total += flow.amount
        elif flow.kind is CashFlowKind.RELEASE:
            total -= flow.amount
    return money(total)


def _external_net_flow(day_flows: Sequence[LedgerCashFlow]) -> Decimal:
    """Deposits minus withdrawals for the day — the consolidated row's flow.

    See the module docstring: ``ASSIGN`` moves money between two pockets of the same consolidated
    pot and must not divide the day there, or a user's total return would be penalised for
    organising their own holdings.
    """
    total = ZERO
    for flow in day_flows:
        if flow.kind is CashFlowKind.EXTERNAL_DEPOSIT:
            total += flow.amount
        elif flow.kind is CashFlowKind.EXTERNAL_WITHDRAWAL:
            total -= flow.amount
    return money(total)


# ---------------------------------------------------------------------------
# Writing — upsert, prune, mark. All three idempotent (house rule 7)
# ---------------------------------------------------------------------------


async def _write_rows(session: AsyncSession, as_of: dt.date, user: UserNav) -> int:
    """Upsert the day's rows. Re-running produces the identical table, never a second copy.

    ``ON CONFLICT (user_id, date, portfolio_id)`` is arbitrated by migration 0023's
    ``NULLS NOT DISTINCT`` unique index, which is what lets the consolidated row — whose
    ``portfolio_id`` is NULL — collide with itself on a re-run instead of being appended. Under a
    plain unique index two NULLs are distinct and the second run would silently double the user's
    net worth in every chart that sums the series.

    Money is quantised by the domain layer before it reaches here (house rule 8, round at write
    time), so the numeric columns store exactly what was computed and the API, the UI and the CSV
    export cannot disagree about the last paisa.
    """
    rows: list[dict[str, object]] = [
        {
            "user_id": row.user_id,
            "portfolio_id": row.portfolio_id,
            "date": as_of,
            "market_value": row.market_value,
            "cash": row.cash,
            "net_flow": row.net_flow,
            "pending_reconciliation": row.pending_reconciliation,
        }
        for row in (user.consolidated, *user.portfolio_rows)
    ]
    statement = insert(PortfolioNavDaily).values(rows)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=list(NAV_CONFLICT_COLUMNS),
            set_={
                "market_value": statement.excluded.market_value,
                "cash": statement.excluded.cash,
                "net_flow": statement.excluded.net_flow,
                "pending_reconciliation": statement.excluded.pending_reconciliation,
            },
        )
    )
    return len(rows)


async def _remove_every_row(session: AsyncSession, as_of: dt.date, user_id: int) -> int:
    """Drop this date entirely for a user who no longer has anything to value.

    The other half of convergence. ``_remove_stale_rows`` prunes portfolios that lost their row
    while the user still has some; this handles the user who lost all of them — the consolidated
    row included, since there is no longer a net worth to state.
    """
    removed = await session.execute(
        delete(PortfolioNavDaily)
        .where(PortfolioNavDaily.user_id == user_id, PortfolioNavDaily.date == as_of)
        .returning(PortfolioNavDaily.user_id)
    )
    return len(removed.all())


async def _remove_stale_rows(session: AsyncSession, as_of: dt.date, user: UserNav) -> int:
    """Delete this date's rows for portfolios that no longer earn one.

    An upsert alone is not idempotence when the *set* of rows can shrink. Flip a portfolio from
    CAPITAL to MONITORING and yesterday's run left it a row; §4.1 says a lens never enters a
    total, so a chart that sums the day would keep counting it. Re-running the date has to
    converge on the correct table, not merely refresh the rows that are still correct.

    The consolidated row is never a candidate — it always exists for a user being valued — so the
    predicate excludes NULL explicitly rather than relying on ``NOT IN`` semantics, which would
    have answered NULL and deleted nothing anyway.
    """
    kept = [row.portfolio_id for row in user.portfolio_rows]
    condition = [
        PortfolioNavDaily.user_id == user.user_id,
        PortfolioNavDaily.date == as_of,
        PortfolioNavDaily.portfolio_id.is_not(None),
    ]
    if kept:
        condition.append(PortfolioNavDaily.portfolio_id.not_in(kept))
    # ``RETURNING`` rather than ``rowcount``: the count is wanted for the run summary, and a
    # returning clause makes it a typed column rather than a driver attribute whose type nobody
    # can check. It costs nothing here — the rows are almost always none.
    removed = await session.execute(
        delete(PortfolioNavDaily).where(*condition).returning(PortfolioNavDaily.portfolio_id)
    )
    return len(removed.all())


#: The "since grouped" backfill (§5.2). Raw SQL because migration 0022 added
#: ``first_bought_on``/``history_source`` and the ``PortfolioHolding`` ORM class has not caught up
#: — that model belongs to another leaf, and declaring the columns here would put two definitions
#: of one table in the tree. The predicate is the whole safety argument: it touches only rows that
#: have no date and no purchase record, so it can never overwrite a CAS import and running it
#: twice updates nothing the second time (house rule 7).
_MARK_SINCE_GROUPED = text(
    """
    UPDATE portfolio_holding
       SET first_bought_on = added_on
     WHERE portfolio_id IN :portfolio_ids
       AND first_bought_on IS NULL
       AND history_source = :history_source
       AND added_on <= :as_of
 RETURNING portfolio_id, instrument_id, broker_account_id
    """
).bindparams(bindparam("portfolio_ids", expanding=True))


async def _mark_since_grouped(session: AsyncSession, as_of: dt.date, user: UserNav) -> int:
    """Stamp the grouping date onto holdings that have no purchase history (§5.2).

    ``added_on`` is the day the holding entered this portfolio, which is the earliest day any EOD
    mark exists for it — so it is the anchor "since grouped" is measured from, and the honest
    answer to "since when?" for a holding group whose buy prices nobody has imported yet.

    ``added_on <= as_of`` keeps a backfill honest: valuing 2026-01-05 must not stamp a holding
    that was grouped in March. The mark is a fact about the past, and a job re-run over a past
    date should not import the present into it (house rule 5).

    ``history_source`` is left at ``NONE`` on purpose — see the module docstring. The pair
    (``first_bought_on`` set, ``history_source = 'NONE'``) is the encoding of "grouped then,
    bought we-don't-know-when", which is exactly the state §5.2 says must not display XIRR.
    """
    portfolio_ids = [row.portfolio_id for row in user.portfolio_rows]
    if not portfolio_ids:
        return 0
    marked = await session.execute(
        _MARK_SINCE_GROUPED,
        {
            "portfolio_ids": portfolio_ids,
            "history_source": HISTORY_SOURCE_NONE,
            "as_of": as_of,
        },
    )
    return len(marked.all())
