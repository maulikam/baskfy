"""AF 3.3 — every RS amount in a dividend purpose is summed."""

from __future__ import annotations

from decimal import Decimal

from baskfy_providers.nse import parse_corporate_action_purpose


def test_dividend_amounts_are_summed() -> None:
    assert parse_corporate_action_purpose("DIVIDEND - RS.2.50 + RS.1.00 PER SHARE") == (
        "dividend",
        None,
        None,
        Decimal("3.50"),
    )
