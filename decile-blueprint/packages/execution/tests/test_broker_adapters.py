"""Multi-broker adapters and holdings normalisation — M41."""

from __future__ import annotations

from decimal import Decimal

import pytest

from baskfy_core.broker_connections import BROKERS
from baskfy_execution.adapters import ADAPTERS, get_adapter, total_quantity
from baskfy_execution.broker_ports import normalize_holding


class TestAdapters:
    def test_every_catalog_broker_has_an_adapter_row(self) -> None:
        for broker in BROKERS:
            assert broker.id in ADAPTERS

    def test_only_zerodha_has_an_authorize_url_today(self) -> None:
        wired = [a for a in ADAPTERS.values() if a.authorize_base]
        assert len(wired) == 1
        assert wired[0].broker_id == "zerodha"

    def test_zerodha_start_builds_kite_login(self) -> None:
        adapter = get_adapter("zerodha")
        assert adapter is not None
        started = adapter.start(
            api_key="testkey",
            redirect_uri="https://baskfy.com/brokers/callback",
            state="abc",
        )
        assert started.authorize_url.startswith("https://kite.zerodha.com/connect/login?")
        assert "api_key=testkey" in started.authorize_url
        assert started.state == "abc"

    def test_holdings_mapper_sums_the_desk_way(self) -> None:
        adapter = get_adapter("zerodha")
        assert adapter is not None
        rows = adapter.holdings_from_kite_payload(
            [
                {
                    "tradingsymbol": "INFY",
                    "exchange": "NSE",
                    "quantity": 10,
                    "t1_quantity": 2,
                    "collateral_quantity": 3,
                    "average_price": "1500.50",
                    "last_price": "1510",
                    "product": "CNC",
                }
            ]
        )
        assert len(rows) == 1
        assert total_quantity(rows[0]) == Decimal("15")
        assert rows[0].average_price == Decimal("1500.50")

    def test_normalize_holding_uses_decimal(self) -> None:
        row = normalize_holding(symbol="reliance", quantity="1", average_price="100.25")
        assert row.symbol == "RELIANCE"
        assert isinstance(row.average_price, Decimal)


@pytest.mark.parametrize("empty", [None, ""])
def test_as_decimal_defaults(empty: object) -> None:
    adapter = get_adapter("zerodha")
    assert adapter is not None
    rows = adapter.holdings_from_kite_payload(
        [{"tradingsymbol": "TCS", "quantity": empty, "average_price": empty}]
    )
    assert rows[0].quantity == Decimal("0")
    assert rows[0].average_price == Decimal("0")
