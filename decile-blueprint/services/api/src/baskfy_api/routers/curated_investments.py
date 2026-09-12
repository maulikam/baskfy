"""Investment reads, mark-as-invested (T7.2 b), and the portfolio join. No order path.

``POST /cb/investments/mark`` records a book the user already traded at the broker.
``GET /cb/investments`` and ``GET /cb/investments/{id}`` read it. ``GET /cb/fees`` reads
the accrued (uncollected) ledger those marks write.

``PUT``/``DELETE /cb/investments/{id}/portfolio`` (tree 5, leaf B3) file that book entry under
one of the user's portfolios, and un-file it. Both are **bookkeeping only**: they move a foreign
key, they never reach a broker and they never touch a share. Nothing in this module can cause an
order — the desk's first non-negotiable — and a grep for the order path is part of the leaf's
acceptance.

**Why the link is optional in both directions.** ``cb_investment.portfolio_id`` is nullable
(migration 0019) and stays that way. An investment that is filed under nothing is a complete,
readable investment, not a broken one, so every read here reports the unlinked case as a first
class answer rather than hiding or defaulting it.

**Why deleting a portfolio cannot delete a book entry.** The column is ``ON DELETE SET NULL``.
Removing a grouping removes the grouping; the record of what the user owns survives it. That is
a schema fact (0019), asserted here by test rather than assumed.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Protocol

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.curated_investments import (
    HoldingIn,
    list_open_pending,
    load_investment_for_user,
    mark_invested,
    snapshot_dict,
)
from baskfy_api.curated_tenant import investments_for_user_stmt, scoped_sole_user_id
from baskfy_api.db import SessionDep
from baskfy_api.live_prices import live_prices_by_instrument
from baskfy_api.problems import Problem, ProblemType, not_found
from baskfy_core.curated_accounting import HoldingPosition
from baskfy_core.models import (
    BrokerAccount,
    CbBasket,
    CbFeeLedger,
    CbInvestment,
    CbInvestmentHolding,
    CbOrderBatch,
    CbPendingAction,
    Instrument,
    JsonObject,
    Portfolio,
)

router = APIRouter(tags=["curated-investments"])

UTC = dt.UTC


class HoldingBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    qty: Decimal = Field(gt=0)
    avg_price: Decimal = Field(ge=0)


class MarkBody(BaseModel):
    basket_slug: str = Field(min_length=1, max_length=64)
    amount: Decimal = Field(gt=0)
    confirmed: bool
    holdings: list[HoldingBody] = Field(min_length=1)
    desk_plan_id: str | None = None


class PortfolioLinkBody(BaseModel):
    """Which portfolio to file this investment under. One field, because that is the whole act."""

    portfolio_id: int = Field(gt=0)


class BrokerReconciliationOut(BaseModel):
    """How the investment's broker attribution sits against the portfolio's declared one.

    Both sides are reported side by side and **neither is rewritten**. A link is a filing
    decision; it is not evidence that the money moved, so it may not quietly restate where the
    shares are held or what the portfolio declares itself to be.

    ``conflict`` follows the same reading ``baskfy_core.portfolio_graph.BrokerRollup`` already
    uses, so the API and the roll-up cannot disagree about the same pair of rows:

    * portfolio declares **one** broker account and the investment is held at a different one
      → ``True``. The portfolio's declaration is an assertion the user made about everything
      underneath it, and this filing makes that assertion false.
    * portfolio declares **no** broker account (``broker_account_id IS NULL``, a roll-up that
      spans brokers) → ``False``, whatever the investment says. A container declared as spanning
      is allowed to hold one broker's money today; the declaration is about what the container is
      for, not about what happens to be inside it this morning.
    """

    #: ``cb_investment.broker_account_id`` — NOT NULL since migration 0018.
    investment_broker_account_id: int
    #: The catalog id (``zerodha``, …) of that account, when the row is still readable.
    investment_broker_id: str | None = None
    #: ``portfolio.broker_account_id`` — NULL means the portfolio declares itself as spanning.
    portfolio_broker_account_id: int | None = None
    portfolio_broker_id: str | None = None
    #: True exactly when ``portfolio_broker_account_id`` is NULL, so a reader never has to infer
    #: "spans brokers" from a null and get it confused with "not linked".
    portfolio_spans_brokers: bool
    conflict: bool
    #: Names both sides in prose when ``conflict``; ``None`` otherwise, because a message that is
    #: always present is a message nobody reads.
    detail: str | None = None


class InvestmentPortfolioLinkOut(BaseModel):
    """The state of one investment's portfolio filing, after a link or an unlink."""

    investment_id: int
    #: ``None`` after an unlink, and after a link that was never made.
    portfolio_id: int | None = None
    portfolio_name: str | None = None
    #: ``None`` when the investment is filed under no portfolio: there are not two attributions
    #: to reconcile, so reporting a reconciliation would be inventing one.
    broker: BrokerReconciliationOut | None = None


class BatchOut(BaseModel):
    id: int
    kind: str
    status: str
    desk_plan_id: str | None = None
    created_at: dt.datetime | None = None


class MarkOut(BaseModel):
    id: int
    status: str
    basket_slug: str
    batch: BatchOut


class SnapshotOut(BaseModel):
    money_put_in: Decimal
    current_investment: Decimal
    current_value: Decimal
    current_returns: Decimal
    current_returns_pct: Decimal
    realized_pnl: Decimal
    dividends: Decimal
    xirr: Decimal | None = None
    xirr_displayable: bool
    #: True when any holding is still marked at average cost because no live quote was available
    #: (audit 3.13). False when every held instrument was overlaid from Kite.
    marked_at_cost: bool = False


class PendingBriefOut(BaseModel):
    id: int
    type: str
    title: str
    body: str | None = None


class InvestmentHoldingOut(BaseModel):
    symbol: str
    qty: Decimal
    weight: Decimal | None = None
    returns_pct: Decimal | None = None


class InvestmentRowOut(BaseModel):
    id: int
    basket_slug: str
    basket_name: str
    status: str
    invested_at: dt.datetime | None = None
    last_invested_at: dt.datetime | None = None
    days_since_last_investment: int | None = None
    rebalance_pending: bool
    snapshot: SnapshotOut | None = None
    #: From ``cb_basket.source`` / ``visibility`` so the one-book view can sort a holding into
    #: manager / your-rule / by-hand without a second round-trip or a guess from the name.
    basket_source: str
    visibility: str
    #: ``None`` = filed under no portfolio. A legitimate, complete state — see the module
    #: docstring — never an error and never defaulted to some "inbox" portfolio.
    portfolio_id: int | None = None
    portfolio_name: str | None = None
    #: See :class:`BrokerReconciliationOut`. ``False`` for an unlinked investment, because there
    #: is no declaration for it to conflict with.
    broker_conflict: bool = False


class InvestmentListOut(BaseModel):
    items: list[InvestmentRowOut]
    total: int
    net_worth: Decimal | None = None
    pending_actions: list[PendingBriefOut]
    #: How many of ``items`` are filed under no portfolio. Reported rather than left to be
    #: counted client-side, so "money the user has not filed anywhere" is a number the product
    #: can show instead of a gap between two other numbers.
    unassigned_count: int = 0
    #: How many of ``items`` sit under a portfolio that declares a different broker account.
    #: Surfacing the count at list level is what stops a conflict from being visible only to
    #: whoever happened to be watching the response to the link call.
    broker_conflicts: int = 0


class InvestmentDetailOut(InvestmentRowOut):
    holdings: list[InvestmentHoldingOut]
    orders: list[BatchOut]


class FeeRowOut(BaseModel):
    id: int
    kind: str
    base_fee: Decimal
    gst: Decimal
    total: Decimal
    accrued_at: dt.datetime
    collected: bool
    basket_name: str | None = None


class FeeLedgerOut(BaseModel):
    items: list[FeeRowOut]
    accrued_total: Decimal | None = None
    collected_total: Decimal | None = None


_TITLE_FOR_TYPE: dict[str, str] = {
    "DRIFT": "Incorrect holdings — Fix now",
    "REBALANCE_AVAILABLE": "Rebalance update available",
    "SIP_DUE": "SIP due",
    "GENERIC": "Action needed",
}


def _payload_of(action: CbPendingAction) -> JsonObject:
    """``payload`` as a mapping, whatever JSONB actually holds.

    The column is typed as an object and every writer in this service writes one, but JSONB
    accepts an array or a scalar just as happily. A payload that is not an object carries no
    ``title`` and no ``body`` by definition, so it reads as empty — the list of pending actions
    still renders, with this row falling back to its type's title.
    """
    raw: object = action.payload
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    return {}


def _pending_brief(action: CbPendingAction) -> PendingBriefOut:
    payload = _payload_of(action)
    raw_title = payload.get("title")
    title = (
        raw_title.strip()
        if isinstance(raw_title, str) and raw_title.strip()
        else _TITLE_FOR_TYPE.get(action.type, "Action needed")
    )
    body: str | None = None
    for key in ("body", "body_md", "message", "summary"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            body = raw.strip()
            break
    return PendingBriefOut(id=int(action.id), type=action.type, title=title, body=body)


def _days_since(when: dt.datetime | None, *, now: dt.datetime) -> int | None:
    if when is None:
        return None
    start = when.date() if when.tzinfo else when.replace(tzinfo=UTC).date()
    return (now.date() - start).days


class _HoldingMark(Protocol):
    """Duck-typed holding used by :func:`_snapshot_for` (ORM row or test stand-in)."""

    instrument_id: int
    qty: Decimal
    avg_price: Decimal


def _snapshot_for(
    holdings: Sequence[_HoldingMark],
    buy_amounts: Sequence[Decimal],
    first_invested: dt.date,
    as_of: dt.date,
    *,
    live_prices: Mapping[int, Decimal] | None = None,
) -> SnapshotOut:
    """Mark holdings at live quotes when a Kite session has them; otherwise at cost (audit 3.13)."""
    positions = [
        HoldingPosition(instrument_id=int(h.instrument_id), qty=h.qty, avg_price=h.avg_price)
        for h in holdings
    ]
    prices = {int(h.instrument_id): h.avg_price for h in holdings}
    marked_at_cost = False
    overlay = live_prices or {}
    for instrument_id in list(prices):
        live = overlay.get(instrument_id)
        if live is not None and live > 0:
            prices[instrument_id] = live
        else:
            marked_at_cost = True
    if not prices:
        marked_at_cost = False
    raw = snapshot_dict(
        buy_amounts=buy_amounts,
        holdings=positions,
        prices=prices,
        first_invested=first_invested,
        as_of=as_of,
    )
    out = SnapshotOut.model_validate(raw)
    return out.model_copy(update={"marked_at_cost": marked_at_cost})


def _broker_conflict(*, investment_broker_account_id: int, portfolio: Portfolio | None) -> bool:
    """Does filing this investment here contradict what the portfolio declares about brokers?

    Pure, and deliberately asymmetric — see :class:`BrokerReconciliationOut` for the full
    reasoning. Only a portfolio that names exactly one broker account can be contradicted.
    """
    if portfolio is None:
        return False
    declared = portfolio.broker_account_id
    if declared is None:
        return False
    return int(declared) != investment_broker_account_id


@dataclass(frozen=True, slots=True)
class _RowContext:
    """Everything a row needs that is not on the investment or its basket.

    A frozen bundle rather than six keyword arguments: the row builder grew a portfolio when the
    two halves of the product were joined, and a function whose parameter list keeps growing is
    one whose callers keep drifting apart. Frozen so a caller cannot mutate the context of a row
    it has already built.
    """

    holdings: Sequence[CbInvestmentHolding]
    buy_amounts: Sequence[Decimal]
    rebalance_pending: bool
    #: The portfolio named by ``inv.portfolio_id``, already loaded, or ``None`` when unlinked.
    portfolio: Portfolio | None
    now: dt.datetime


def _row_out(
    inv: CbInvestment,
    basket: CbBasket,
    ctx: _RowContext,
    *,
    live_prices: Mapping[int, Decimal] | None = None,
) -> InvestmentRowOut:
    first = inv.created_at or ctx.now
    snap = _snapshot_for(
        ctx.holdings,
        ctx.buy_amounts,
        first.date(),
        ctx.now.date(),
        live_prices=live_prices,
    )
    return InvestmentRowOut(
        id=int(inv.id),
        basket_slug=basket.slug,
        basket_name=basket.name,
        status=inv.status,
        invested_at=inv.created_at,
        last_invested_at=inv.last_invested_at,
        days_since_last_investment=_days_since(inv.last_invested_at, now=ctx.now),
        rebalance_pending=ctx.rebalance_pending,
        snapshot=snap,
        basket_source=basket.source,
        visibility=basket.visibility,
        portfolio_id=int(inv.portfolio_id) if inv.portfolio_id is not None else None,
        portfolio_name=ctx.portfolio.name if ctx.portfolio is not None else None,
        broker_conflict=_broker_conflict(
            investment_broker_account_id=int(inv.broker_account_id), portfolio=ctx.portfolio
        ),
    )


async def _portfolio_for_user(
    session: AsyncSession, *, user_id: int, portfolio_id: int
) -> Portfolio:
    """One of *this* user's portfolios, or ``NOT_FOUND``.

    A portfolio belonging to somebody else answers exactly as a portfolio that does not exist:
    404, never 403. A 403 would confirm that the id names a real row, which is the fact a
    stranger is probing for.
    """
    row = await session.scalar(
        select(Portfolio).where(Portfolio.id == portfolio_id, Portfolio.user_id == user_id)
    )
    if row is None:
        raise not_found("portfolio", str(portfolio_id))
    return row


async def _portfolio_of(session: AsyncSession, inv: CbInvestment) -> Portfolio | None:
    """The portfolio an investment is filed under, or ``None`` when it is filed under nothing.

    Scoped by ``user_id`` even though only this module writes the column, and only ever with a
    portfolio it has already proved belongs to the caller. This read is what puts a portfolio's
    *name* into a response body, so it is the place a cross-tenant row would become visible, and
    a predicate that costs nothing is cheaper than the argument that it cannot happen.
    """
    if inv.portfolio_id is None:
        return None
    # Annotated rather than returned inline: `AsyncSession.scalar` is typed as returning `Any`,
    # and handing that straight back would launder the annotation this function advertises.
    found: Portfolio | None = await session.scalar(
        select(Portfolio).where(
            Portfolio.id == int(inv.portfolio_id), Portfolio.user_id == int(inv.user_id)
        )
    )
    return found


async def _link_out(
    session: AsyncSession, inv: CbInvestment, portfolio: Portfolio | None
) -> InvestmentPortfolioLinkOut:
    """Render the filing, with both broker attributions reported and neither one changed."""
    if portfolio is None:
        return InvestmentPortfolioLinkOut(investment_id=int(inv.id), portfolio_id=None)

    investment_account = int(inv.broker_account_id)
    declared = portfolio.broker_account_id
    declared_account = int(declared) if declared is not None else None
    wanted = {investment_account} | ({declared_account} if declared_account is not None else set())
    # Scoped to the owner: nothing cross-table forces `portfolio.broker_account_id` to name an
    # account of the same user, and this lookup is what turns an id into a broker's name in a
    # response. A foreign account therefore reads as "unknown broker" rather than being named.
    accounts = list(
        (
            await session.scalars(
                select(BrokerAccount).where(
                    BrokerAccount.id.in_(wanted), BrokerAccount.user_id == int(inv.user_id)
                )
            )
        ).all()
    )
    broker_id_for = {int(row.id): str(row.broker_id) for row in accounts}

    conflict = _broker_conflict(
        investment_broker_account_id=investment_account, portfolio=portfolio
    )
    detail: str | None = None
    # `conflict` already implies a declared account; the second clause is what tells the type
    # checker so, rather than a sentinel id that could one day collide with a real row.
    if conflict and declared_account is not None:
        detail = (
            f"Investment {int(inv.id)} is held at broker account {investment_account}"
            f" ({broker_id_for.get(investment_account, 'unknown broker')}), but portfolio"
            f" {int(portfolio.id)} ({portfolio.name!r}) declares broker account"
            f" {declared_account} ({broker_id_for.get(declared_account, 'unknown broker')})."
            " The filing was recorded and both attributions were left exactly as they are;"
            " neither side is rewritten to make the other agree."
        )
    return InvestmentPortfolioLinkOut(
        investment_id=int(inv.id),
        portfolio_id=int(portfolio.id),
        portfolio_name=portfolio.name,
        broker=BrokerReconciliationOut(
            investment_broker_account_id=investment_account,
            investment_broker_id=broker_id_for.get(investment_account),
            portfolio_broker_account_id=declared_account,
            portfolio_broker_id=(
                broker_id_for.get(declared_account) if declared_account is not None else None
            ),
            portfolio_spans_brokers=declared_account is None,
            conflict=conflict,
            detail=detail,
        ),
    )


@router.post("/cb/investments/mark", response_model=MarkOut, status_code=201)
async def mark_as_invested(
    body: MarkBody,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> MarkOut:
    """Record that the sole user invested this basket at their broker. Does not place an order."""
    investment = await mark_invested(
        session,
        principal_user_id=principal.user_id,
        basket_slug=body.basket_slug,
        amount=body.amount,
        confirmed=body.confirmed,
        holdings=[
            HoldingIn(symbol=h.symbol, qty=h.qty, avg_price=h.avg_price) for h in body.holdings
        ],
        desk_plan_id=body.desk_plan_id,
    )
    basket = await session.scalar(select(CbBasket).where(CbBasket.id == investment.basket_id))
    batch = await session.scalar(
        select(CbOrderBatch)
        .where(CbOrderBatch.investment_id == investment.id)
        .order_by(CbOrderBatch.id.desc())
        .limit(1)
    )
    if basket is None or batch is None:
        raise RuntimeError("mark_invested did not persist basket or batch")
    return MarkOut(
        id=int(investment.id),
        status=investment.status,
        basket_slug=basket.slug,
        batch=BatchOut(
            id=int(batch.id),
            kind=batch.kind,
            status=batch.status,
            desk_plan_id=batch.desk_plan_id,
            created_at=batch.created_at,
        ),
    )


def _filtered_stmt(
    user_id: int, *, portfolio_id: int | None, unassigned: bool
) -> Select[tuple[CbInvestment]]:
    """The user's investments, narrowed to one portfolio or to the unfiled ones.

    Kept beside the handler and not inside it so the two filters cannot drift apart from the
    mutual-exclusion check that guards them.
    """
    stmt = investments_for_user_stmt(user_id)
    if portfolio_id is not None:
        return stmt.where(CbInvestment.portfolio_id == portfolio_id)
    if unassigned:
        return stmt.where(CbInvestment.portfolio_id.is_(None))
    return stmt


@router.get("/cb/investments", response_model=InvestmentListOut)
async def list_investments(  # noqa: PLR0912 — list path gathers holdings/batches/live marks in one handler
    session: SessionDep,
    principal: AuthenticatedDep,
    portfolio_id: Annotated[
        int | None,
        Query(ge=1, description="Only investments filed under this portfolio."),
    ] = None,
    unassigned: Annotated[
        bool,
        Query(description="Only investments filed under no portfolio at all."),
    ] = False,
) -> InvestmentListOut:
    """The user's book. Unfiled investments are included by default and counted separately.

    Neither filter is the default: an investment that belongs to no portfolio is still the
    user's money, and a list that quietly dropped it would be the one bug this endpoint cannot
    be allowed to have.

    ``net_worth`` sums whatever the filter selected, so under ``portfolio_id`` it is that
    portfolio's value rather than the whole book's. That is what lets the portfolio view count
    an investment; a total that ignored the filter would be a different number wearing the same
    name.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    if portfolio_id is not None and unassigned:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            "portfolio_id and unassigned=true select different sets; send one or neither.",
        )
    if portfolio_id is not None:
        # Ownership is checked before the filter runs, so a foreign portfolio id answers 404
        # rather than an empty list — an empty list would say "yours, and empty", which is a
        # different and untrue statement.
        await _portfolio_for_user(session, user_id=user_id, portfolio_id=portfolio_id)
    now = dt.datetime.now(tz=UTC)
    rows = list(
        (
            await session.scalars(
                _filtered_stmt(user_id, portfolio_id=portfolio_id, unassigned=unassigned)
            )
        ).all()
    )
    linked_ids = {int(row.portfolio_id) for row in rows if row.portfolio_id is not None}
    portfolios: dict[int, Portfolio] = {}
    if linked_ids:
        portfolios = {
            int(row.id): row
            for row in (
                await session.scalars(
                    select(Portfolio).where(
                        Portfolio.id.in_(linked_ids), Portfolio.user_id == user_id
                    )
                )
            ).all()
        }
    items: list[InvestmentRowOut] = []
    pending = await list_open_pending(session, user_id)
    pending_briefs = [_pending_brief(a) for a in pending]
    rebalance_baskets: set[int] = set()
    for action in pending:
        if action.type != "REBALANCE_AVAILABLE":
            continue
        payload = action.payload if isinstance(action.payload, dict) else {}
        raw = payload.get("basket_id")
        if isinstance(raw, int):
            rebalance_baskets.add(raw)

    net = Decimal("0")
    any_value = False
    inv_ids = [int(inv.id) for inv in rows]
    holdings_by_inv: dict[int, list[CbInvestmentHolding]] = {iid: [] for iid in inv_ids}
    batches_by_inv: dict[int, list[CbOrderBatch]] = {iid: [] for iid in inv_ids}
    if inv_ids:
        for holding in (
            await session.scalars(
                select(CbInvestmentHolding).where(CbInvestmentHolding.investment_id.in_(inv_ids))
            )
        ).all():
            holdings_by_inv.setdefault(int(holding.investment_id), []).append(holding)
        for batch in (
            await session.scalars(
                select(CbOrderBatch).where(CbOrderBatch.investment_id.in_(inv_ids))
            )
        ).all():
            batches_by_inv.setdefault(int(batch.investment_id), []).append(batch)

    basket_ids = {int(inv.basket_id) for inv in rows}
    baskets: dict[int, CbBasket] = {}
    if basket_ids:
        baskets = {
            int(row.id): row
            for row in (
                await session.scalars(select(CbBasket).where(CbBasket.id.in_(basket_ids)))
            ).all()
        }

    instrument_ids = sorted(
        {
            int(h.instrument_id)
            for holdings in holdings_by_inv.values()
            for h in holdings
        }
    )
    live = await live_prices_by_instrument(session, instrument_ids) if instrument_ids else {}

    for inv in rows:
        basket = baskets.get(int(inv.basket_id))
        if basket is None:
            continue
        holding_rows = holdings_by_inv.get(int(inv.id), [])
        batches = batches_by_inv.get(int(inv.id), [])
        buy_amounts = [
            b.requested_amount
            for b in batches
            if b.kind in ("BUY", "INVEST_MORE", "SIP") and b.requested_amount is not None
        ]
        row = _row_out(
            inv,
            basket,
            _RowContext(
                holdings=holding_rows,
                buy_amounts=buy_amounts or [Decimal("0")],
                rebalance_pending=int(inv.basket_id) in rebalance_baskets,
                portfolio=(
                    portfolios.get(int(inv.portfolio_id)) if inv.portfolio_id is not None else None
                ),
                now=now,
            ),
            live_prices=live,
        )
        items.append(row)
        if row.snapshot is not None:
            net += row.snapshot.current_value
            any_value = True

    return InvestmentListOut(
        items=items,
        total=len(items),
        net_worth=net if any_value else None,
        pending_actions=pending_briefs,
        unassigned_count=sum(1 for row in items if row.portfolio_id is None),
        broker_conflicts=sum(1 for row in items if row.broker_conflict),
    )


@router.put(
    "/cb/investments/{investment_id}/portfolio",
    response_model=InvestmentPortfolioLinkOut,
    summary="File an investment under a portfolio",
)
async def link_investment_to_portfolio(
    investment_id: int,
    body: PortfolioLinkBody,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> InvestmentPortfolioLinkOut:
    """Record that this investment belongs under this portfolio. A filing, not a transaction.

    Idempotent: filing an investment where it already sits changes nothing and answers the same
    body. Re-filing it under a different portfolio of the same user is a move, and is allowed —
    the book entry is unchanged either way, only the folder it sits in.

    A portfolio that belongs to another user answers ``NOT_FOUND``, the same as one that does not
    exist. See :func:`_portfolio_for_user`.

    A broker mismatch is **reported and kept**, never merged: see
    :class:`BrokerReconciliationOut`. Refusing the link was the rejected alternative — the
    portfolio's declaration is a claim about a container, and the honest answer to a claim that
    stopped being true is to say so, not to forbid the user from recording where their money is.
    Silently rewriting either side's ``broker_account_id`` to agree would be worse still: it
    would destroy a fact about where shares are actually held in order to tidy a label.
    """
    investment = await load_investment_for_user(
        session, principal_user_id=principal.user_id, investment_id=investment_id
    )
    portfolio = await _portfolio_for_user(
        session, user_id=int(investment.user_id), portfolio_id=body.portfolio_id
    )
    investment.portfolio_id = int(portfolio.id)
    await session.flush()
    return await _link_out(session, investment, portfolio)


@router.delete(
    "/cb/investments/{investment_id}/portfolio",
    response_model=InvestmentPortfolioLinkOut,
    summary="Un-file an investment from its portfolio",
)
async def unlink_investment_from_portfolio(
    investment_id: int,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> InvestmentPortfolioLinkOut:
    """Remove the filing. **The investment itself is untouched and stays fully readable.**

    This is not a delete and it must never become one: the book is the user's record of what
    they own, and the worst outcome available on this route is losing it to tidy up a grouping.
    Only ``portfolio_id`` is cleared — holdings, order batches, fees and status all stand.

    Idempotent: un-filing an investment that is filed under nothing succeeds and reports
    ``portfolio_id: null``, rather than inventing a 404 for a state the caller asked for.
    """
    investment = await load_investment_for_user(
        session, principal_user_id=principal.user_id, investment_id=investment_id
    )
    investment.portfolio_id = None
    await session.flush()
    return await _link_out(session, investment, None)


@router.get("/cb/investments/{investment_id}", response_model=InvestmentDetailOut)
async def get_investment(
    investment_id: int,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> InvestmentDetailOut:
    inv = await load_investment_for_user(
        session, principal_user_id=principal.user_id, investment_id=investment_id
    )
    basket = await session.get(CbBasket, inv.basket_id)
    if basket is None:
        raise RuntimeError("investment references a missing basket")
    now = dt.datetime.now(tz=UTC)
    holding_rows = list(
        (
            await session.scalars(
                select(CbInvestmentHolding).where(CbInvestmentHolding.investment_id == inv.id)
            )
        ).all()
    )
    batches = list(
        (
            await session.scalars(
                select(CbOrderBatch)
                .where(CbOrderBatch.investment_id == inv.id)
                .order_by(CbOrderBatch.id.desc())
            )
        ).all()
    )
    buy_amounts = [
        b.requested_amount
        for b in batches
        if b.kind in ("BUY", "INVEST_MORE", "SIP") and b.requested_amount is not None
    ]
    pending = await list_open_pending(session, int(inv.user_id))
    rebalance_pending = any(
        a.type == "REBALANCE_AVAILABLE"
        and isinstance(a.payload, dict)
        and a.payload.get("basket_id") == inv.basket_id
        for a in pending
    )
    live = await live_prices_by_instrument(
        session, [int(h.instrument_id) for h in holding_rows]
    )
    row = _row_out(
        inv,
        basket,
        _RowContext(
            holdings=holding_rows,
            buy_amounts=buy_amounts or [Decimal("0")],
            rebalance_pending=rebalance_pending,
            portfolio=await _portfolio_of(session, inv),
            now=now,
        ),
        live_prices=live,
    )
    deployed = sum((h.qty * h.avg_price for h in holding_rows), Decimal("0"))
    symbols: dict[int, str] = {}
    if holding_rows:
        instruments = list(
            (
                await session.scalars(
                    select(Instrument).where(
                        Instrument.id.in_([int(h.instrument_id) for h in holding_rows])
                    )
                )
            ).all()
        )
        symbols = {int(i.id): str(i.symbol) for i in instruments}

    holdings_out: list[InvestmentHoldingOut] = []
    for h in holding_rows:
        weight = (h.qty * h.avg_price / deployed) if deployed else None
        holdings_out.append(
            InvestmentHoldingOut(
                symbol=symbols.get(int(h.instrument_id), str(h.instrument_id)),
                qty=h.qty,
                weight=weight,
                returns_pct=None,
            )
        )
    return InvestmentDetailOut(
        **row.model_dump(),
        holdings=holdings_out,
        orders=[
            BatchOut(
                id=int(b.id),
                kind=b.kind,
                status=b.status,
                desk_plan_id=b.desk_plan_id,
                created_at=b.created_at,
            )
            for b in batches
        ],
    )


@router.get("/cb/fees", response_model=FeeLedgerOut)
async def list_fees(
    session: SessionDep,
    principal: AuthenticatedDep,
) -> FeeLedgerOut:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    rows = list(
        (
            await session.scalars(
                select(CbFeeLedger)
                .where(CbFeeLedger.user_id == user_id)
                .order_by(CbFeeLedger.accrued_at.desc())
            )
        ).all()
    )
    items: list[FeeRowOut] = []
    accrued = Decimal("0")
    collected = Decimal("0")
    for row in rows:
        batch = await session.get(CbOrderBatch, row.batch_id)
        basket_name: str | None = None
        if batch is not None:
            inv = await session.get(CbInvestment, batch.investment_id)
            if inv is not None:
                basket = await session.get(CbBasket, inv.basket_id)
                if basket is not None:
                    basket_name = basket.name
        items.append(
            FeeRowOut(
                id=int(row.id),
                kind=row.kind,
                base_fee=row.base_fee,
                gst=row.gst,
                total=row.total,
                accrued_at=row.accrued_at,
                collected=bool(row.collected),
                basket_name=basket_name,
            )
        )
        accrued += row.total
        if row.collected:
            collected += row.total
    return FeeLedgerOut(
        items=items,
        accrued_total=accrued if items else None,
        collected_total=collected if items else None,
    )
