"""Audit 3.13 — curated marks use live overlay when available, else marked_at_cost."""

from __future__ import annotations

import datetime as dt
import inspect
from dataclasses import dataclass
from decimal import Decimal

from typing import Sequence, cast

from baskfy_api.routers import curated_investments as mod
from baskfy_api.routers.curated_investments import _HoldingMark, _snapshot_for


@dataclass(frozen=True, slots=True)
class _Holding:
    instrument_id: int
    qty: Decimal
    avg_price: Decimal


def test_snapshot_is_marked_at_cost_when_no_live_quotes() -> None:
    holdings = [_Holding(1, Decimal("10"), Decimal("100"))]
    snap = _snapshot_for(
        cast(Sequence[_HoldingMark], holdings),
        [Decimal("1000")],
        first_invested=dt.date(2026, 1, 1),
        as_of=dt.date(2026, 9, 12),
        live_prices={},
    )
    assert snap.marked_at_cost is True
    assert snap.current_value == Decimal("1000")


def test_snapshot_clears_marked_at_cost_when_every_holding_has_a_live_quote() -> None:
    holdings = [_Holding(1, Decimal("10"), Decimal("100"))]
    snap = _snapshot_for(
        cast(Sequence[_HoldingMark], holdings),
        [Decimal("1000")],
        first_invested=dt.date(2026, 1, 1),
        as_of=dt.date(2026, 9, 12),
        live_prices={1: Decimal("120")},
    )
    assert snap.marked_at_cost is False
    assert snap.current_value == Decimal("1200")


def test_list_investments_source_does_not_n_plus_one_holdings() -> None:
    """Audit 4.15: holdings and batches load once per list, not once per investment."""
    source = inspect.getsource(mod.list_investments)
    assert "CbInvestmentHolding.investment_id.in_(inv_ids)" in source
    assert "CbOrderBatch.investment_id.in_(inv_ids)" in source
    assert "where(CbInvestmentHolding.investment_id == inv.id)" not in source
