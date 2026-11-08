"""Mark-as-invested and investment reads — T7.2 (b) / Tree 8 leaf 1.1.1.

The user confirms they already acted at their broker. This module records the intended
ledger (``cb_investment`` + holdings), a ``PLANNED`` BUY batch, and an uncollected fee
row. It never reaches a broker. ``EXECUTED`` is leaf 1.1.2's job, from the desk
journal.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.broker_accounts import ensure_default_broker_account
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.problems import Problem, ProblemType, not_found
from baskfy_core.curated_accounting import (
    HoldingPosition,
    current_investment,
    current_returns_abs,
    current_returns_pct,
    current_value,
    money_put_in,
    platform_fee,
    xirr_displayable,
)
from baskfy_core.gst import money
from baskfy_core.models import (
    CbBasket,
    CbBasketVersion,
    CbFeeLedger,
    CbInvestment,
    CbInvestmentHolding,
    CbOrderBatch,
    CbPendingAction,
    Instrument,
)

UTC = dt.UTC


class HoldingIn:
    """One declared broker lot. Built by the router from the request body."""

    __slots__ = ("avg_price", "qty", "symbol")

    def __init__(self, *, symbol: str, qty: Decimal, avg_price: Decimal) -> None:
        self.symbol = symbol
        self.qty = qty
        self.avg_price = avg_price


async def _instruments_by_symbol(
    session: AsyncSession, symbols: Sequence[str]
) -> dict[str, Instrument]:
    rows = list(
        (
            await session.scalars(
                select(Instrument).where(
                    func.upper(Instrument.symbol).in_(symbols),
                    Instrument.is_active.is_(True),
                )
            )
        ).all()
    )
    return {str(row.symbol).upper(): row for row in rows}


# Keyword-only by design, so PLR0913's actual hazard — an unreadable positional call site —
# cannot occur. Six named inputs to one calculation; a parameter object would only rename them.
def snapshot_dict(  # noqa: PLR0913
    *,
    buy_amounts: Sequence[Decimal],
    holdings: Sequence[HoldingPosition],
    prices: dict[int, Decimal],
    first_invested: dt.date,
    as_of: dt.date,
    dividends: Decimal = Decimal("0"),
) -> dict[str, object]:
    """Investor math for one book, using declared marks when live prices are absent."""
    put_in = money_put_in(buy_amounts)
    invested = current_investment(put_in, Decimal("0"))
    value = current_value(holdings, prices)
    returns_abs = current_returns_abs(value, invested)
    returns_pct = current_returns_pct(value, invested)
    displayable = xirr_displayable(first_invested, as_of) if as_of >= first_invested else False
    return {
        "money_put_in": put_in,
        "current_investment": invested,
        "current_value": value,
        "current_returns": returns_abs,
        "current_returns_pct": returns_pct,
        "realized_pnl": money(Decimal("0")),
        "dividends": money(dividends),
        "xirr": None,
        "xirr_displayable": displayable,
    }


# Same as `snapshot_dict`: everything after `session` is keyword-only. `confirmed` is a
# deliberate explicit gate, not a parameter to be folded away.
async def mark_invested(  # noqa: PLR0913
    session: AsyncSession,
    *,
    principal_user_id: int | None,
    basket_slug: str,
    amount: Decimal,
    confirmed: bool,
    holdings: Sequence[HoldingIn],
    desk_plan_id: str | None,
) -> CbInvestment:
    """Persist an ACTIVE investment from a declared broker book. Status of the batch is PLANNED."""
    if not confirmed:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            "confirmed must be true — this records a broker action, it does not place one.",
        )
    if amount <= 0:
        raise Problem(ProblemType.INVALID_SCREEN_DEFINITION, "amount must be greater than zero")
    if not holdings:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            "declare at least one holding (symbol, qty, avg_price) of what you bought",
        )

    user_id = await scoped_sole_user_id(session, principal_user_id)
    slug = basket_slug.strip()
    basket = await session.scalar(
        select(CbBasket).where(CbBasket.slug == slug, CbBasket.archived_at.is_(None))
    )
    if basket is None:
        raise not_found("basket", slug)

    existing = await session.scalar(
        select(CbInvestment.id).where(
            CbInvestment.user_id == user_id,
            CbInvestment.basket_id == basket.id,
            CbInvestment.status == "ACTIVE",
        )
    )
    if existing is not None:
        raise Problem(
            ProblemType.STALE_DATA_VERSION,
            f"an ACTIVE investment already exists for {slug!r}",
            sent_data_version=int(existing),
            current_data_version=int(existing),
        )

    version = await session.scalar(
        select(CbBasketVersion)
        .where(CbBasketVersion.basket_id == basket.id)
        .order_by(CbBasketVersion.version_no.desc())
        .limit(1)
    )
    if version is None:
        raise Problem(ProblemType.INTERNAL_ERROR, f"basket {slug!r} has no version")

    symbols = [h.symbol.strip().upper() for h in holdings]
    if len(set(symbols)) != len(symbols):
        raise Problem(ProblemType.INVALID_SCREEN_DEFINITION, "duplicate symbols are not allowed")
    for lot in holdings:
        if lot.qty <= 0:
            raise Problem(ProblemType.INVALID_SCREEN_DEFINITION, "qty must be greater than zero")
        if lot.avg_price < 0:
            raise Problem(ProblemType.INVALID_SCREEN_DEFINITION, "avg_price cannot be negative")

    by_symbol = await _instruments_by_symbol(session, symbols)
    missing = [sym for sym in symbols if sym not in by_symbol]
    if missing:
        raise Problem(ProblemType.NOT_FOUND, f"unknown symbols: {', '.join(missing)}")

    now = dt.datetime.now(tz=UTC)
    broker_account_id = await ensure_default_broker_account(session, user_id)
    investment = CbInvestment(
        user_id=user_id,
        broker_account_id=broker_account_id,
        basket_id=int(basket.id),
        status="ACTIVE",
        version_applied_id=int(version.id),
        exited_at=None,
        last_invested_at=now,
    )
    session.add(investment)
    await session.flush()

    for lot in holdings:
        instrument = by_symbol[lot.symbol.strip().upper()]
        session.add(
            CbInvestmentHolding(
                investment_id=int(investment.id),
                instrument_id=int(instrument.id),
                qty=lot.qty,
                avg_price=lot.avg_price,
            )
        )

    batch = CbOrderBatch(
        user_id=user_id,
        broker_account_id=broker_account_id,
        investment_id=int(investment.id),
        kind="BUY",
        requested_amount=amount,
        desk_plan_id=desk_plan_id,
        status="PLANNED",
        fee_entry_id=None,
        executed_at=None,
    )
    session.add(batch)
    await session.flush()

    fee = platform_fee("BUY", amount)
    ledger = CbFeeLedger(
        user_id=user_id,
        batch_id=int(batch.id),
        kind="BUY",
        base_fee=fee.base_fee,
        gst=fee.gst,
        total=fee.total,
        collected=False,
        accrued_at=now,
    )
    session.add(ledger)
    await session.flush()
    batch.fee_entry_id = int(ledger.id)

    await session.commit()
    await session.refresh(investment)
    await session.refresh(batch)
    return investment


async def load_investment_for_user(
    session: AsyncSession, *, principal_user_id: int | None, investment_id: int
) -> CbInvestment:
    user_id = await scoped_sole_user_id(session, principal_user_id)
    row = await session.scalar(
        select(CbInvestment).where(
            CbInvestment.id == investment_id,
            CbInvestment.user_id == user_id,
        )
    )
    if row is None:
        raise not_found("investment", str(investment_id))
    return row


async def list_open_pending(session: AsyncSession, user_id: int) -> list[CbPendingAction]:
    rows = (
        await session.scalars(
            select(CbPendingAction)
            .where(
                CbPendingAction.user_id == user_id,
                CbPendingAction.dismissed_at.is_(None),
                CbPendingAction.resolved_at.is_(None),
            )
            .order_by(CbPendingAction.created_at.desc())
        )
    ).all()
    return list(rows)
