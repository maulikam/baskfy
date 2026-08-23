"""Celery Beat dividend derivation — docs/smallcase/04 §4, leaf 3.4 / 2.3.

Loads ACTIVE investment holdings + cash corporate actions, runs
``baskfy_core.curated_dividends.derive_dividends``, and upserts ``cb_dividend`` rows.
Idempotent per ``(investment_id, instrument_id, ex_date)``. No broker path.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.curated_dividends import DIVIDEND_SOURCE, HoldingWindow, derive_dividends
from baskfy_core.models import (
    CbDividend,
    CbInvestment,
    CbInvestmentHolding,
    CorporateAction,
)
from baskfy_core.models.base import JsonObject


async def run_curated_dividends(
    session: AsyncSession,
    as_of: dt.date,
) -> JsonObject:
    """Derive and persist cash dividends for ACTIVE investments as of *as_of*.

    Holdings history is approximated from the current ledger: each positive qty is treated
    as held from ``investment.created_at.date()`` through ``as_of`` (closed window). Full lot
    history is a later writer; this job never invents qty from CA factors.
    """
    investments = list(
        (await session.scalars(select(CbInvestment).where(CbInvestment.status == "ACTIVE"))).all()
    )
    if not investments:
        return {
            "as_of": as_of.isoformat(),
            "investments": 0,
            "derived": 0,
            "inserted": 0,
            "skipped_existing": 0,
        }

    inv_ids = [int(row.id) for row in investments]
    holdings = list(
        (
            await session.scalars(
                select(CbInvestmentHolding).where(CbInvestmentHolding.investment_id.in_(inv_ids))
            )
        ).all()
    )
    by_inv: dict[int, list[CbInvestmentHolding]] = defaultdict(list)
    instrument_ids: set[int] = set()
    for hold in holdings:
        if hold.qty <= 0:
            continue
        by_inv[int(hold.investment_id)].append(hold)
        instrument_ids.add(int(hold.instrument_id))

    cash_actions: dict[int, list[tuple[dt.date, Decimal]]] = defaultdict(list)
    if instrument_ids:
        action_rows = list(
            (
                await session.scalars(
                    select(CorporateAction).where(
                        CorporateAction.instrument_id.in_(instrument_ids),
                        CorporateAction.action_type == "dividend",
                        CorporateAction.ex_date <= as_of,
                        CorporateAction.amount.is_not(None),
                    )
                )
            ).all()
        )
        for action in action_rows:
            amount = action.amount
            if amount is None or amount <= 0:
                continue
            cash_actions[int(action.instrument_id)].append((action.ex_date, Decimal(amount)))

    existing = list(
        (
            await session.scalars(select(CbDividend).where(CbDividend.investment_id.in_(inv_ids)))
        ).all()
    )
    existing_keys = {
        (int(row.investment_id), int(row.instrument_id), row.ex_date) for row in existing
    }

    derived = 0
    inserted = 0
    skipped_existing = 0
    inv_by_id = {int(row.id): row for row in investments}

    for investment_id, rows in by_inv.items():
        investment = inv_by_id[investment_id]
        from_date = investment.created_at.date() if investment.created_at else as_of
        if from_date > as_of:
            continue

        holdings_history: dict[int, list[HoldingWindow]] = {}
        for hold in rows:
            iid = int(hold.instrument_id)
            holdings_history.setdefault(iid, []).append(
                HoldingWindow(from_date=from_date, to_date=as_of, qty=Decimal(hold.qty))
            )

        relevant_actions = {
            iid: actions for iid in holdings_history if (actions := cash_actions.get(iid))
        }
        if not relevant_actions:
            continue

        dividends = derive_dividends(holdings_history, relevant_actions)
        derived += len(dividends)
        for row in dividends:
            key = (investment_id, row.instrument_id, row.ex_date)
            if key in existing_keys:
                skipped_existing += 1
                continue
            session.add(
                CbDividend(
                    investment_id=investment_id,
                    instrument_id=row.instrument_id,
                    ex_date=row.ex_date,
                    amount_per_share=row.amount_per_share,
                    qty_held=row.qty_held,
                    total=row.total,
                    source=DIVIDEND_SOURCE,
                )
            )
            existing_keys.add(key)
            inserted += 1

    await session.flush()
    return {
        "as_of": as_of.isoformat(),
        "investments": len(investments),
        "derived": derived,
        "inserted": inserted,
        "skipped_existing": skipped_existing,
    }
