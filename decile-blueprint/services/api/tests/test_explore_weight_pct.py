"""Explore constituent weight_pct — audit 0.3.

Storage is a fraction of 1.0 (`0.0500`); the page must render percent (`5.00`), never
`0.0500%`. This helper is where house rule 8 rounds once.
"""

from __future__ import annotations

from decimal import Decimal

from baskfy_api.routers.explore import _weight_pct


def test_weight_pct_is_percent_not_fraction() -> None:
    assert _weight_pct(Decimal("0.0500")) == Decimal("5.00")
    assert _weight_pct(Decimal("0.0667")) == Decimal("6.67")
    # The old bug: labelling the fraction as % would look like 0.05%.
    assert _weight_pct(Decimal("0.0500")) >= Decimal(1)
