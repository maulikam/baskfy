"""Holdings normalisation — M41."""

from __future__ import annotations

from decimal import Decimal

from baskfy_execution.broker_ports import normalize_holding, total_quantity


def test_normalize_holding_uses_decimal() -> None:
    row = normalize_holding(symbol="reliance", quantity="1", average_price="100.25")
    assert row.symbol == "RELIANCE"
    assert isinstance(row.average_price, Decimal)


def test_total_quantity_is_non_negotiable_two() -> None:
    row = normalize_holding(
        symbol="INFY",
        quantity=10,
        t1_quantity=2,
        collateral_quantity=3,
        average_price="1",
    )
    assert total_quantity(row) == Decimal("15")
