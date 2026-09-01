"""Broker holdings reach a portfolio — M75.

`POST /brokers/{id}/sync-holdings` read the account and returned the rows without writing them
down, so `portfolio_holding` was reachable only from a CSV import, a manual replace and
reconciliation. Maulik connected Zerodha on 1 Sep 2026, the token was valid, Kite answered 200 —
and the Portfolio page still read "Holdings not synced yet", because nothing joined the two.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from baskfy_execution.broker_ports import HoldingRow
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.broker_holdings import HoldingsResult, HoldingsSource
from baskfy_api.broker_holdings_sync import sync_holdings_into_portfolio

AS_OF = dt.date(2026, 9, 1)


def _row(symbol: str, *, qty: str = "10", t1: str = "0", collateral: str = "0") -> HoldingRow:
    return HoldingRow(
        symbol=symbol,
        exchange="NSE",
        quantity=Decimal(qty),
        t1_quantity=Decimal(t1),
        collateral_quantity=Decimal(collateral),
        average_price=Decimal("100.00"),
    )


class TestOnlyALiveReadIsEverWritten:
    """The guard that matters most: invented numbers must never sit behind a real rupee total."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["fixture", "empty", "unwired"])
    async def test_a_non_live_source_writes_nothing(self, source: HoldingsSource) -> None:
        rows = (_row("RELIANCE"),) if source == "fixture" else ()
        result = HoldingsResult(rows=rows, source=source)
        sync = await sync_holdings_into_portfolio(
            # A real, bindless session rather than a `None` the signature forbids. The guard
            # returns before any I/O, and typing it honestly is what house rule 3 asks for.
            AsyncSession(),
            result,
            user_id=1,
            broker_account_id=1,
            broker_name="Zerodha",
            as_of=AS_OF,
        )
        assert sync.persisted is False
        assert sync.written == 0
        assert sync.portfolio_id is None
        assert source in sync.reason

    @pytest.mark.asyncio
    async def test_a_degraded_live_read_writes_nothing(self) -> None:
        """`degraded` means the broker was unreachable and fixtures stood in for it."""
        result = HoldingsResult(rows=(_row("RELIANCE"),), source="fixture", degraded=True)
        sync = await sync_holdings_into_portfolio(
            AsyncSession(),
            result,
            user_id=1,
            broker_account_id=1,
            broker_name="Zerodha",
            as_of=AS_OF,
        )
        assert sync.persisted is False
        assert "degraded" in sync.reason
