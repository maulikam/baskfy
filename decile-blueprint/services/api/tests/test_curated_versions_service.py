"""API curated-version helpers — docs/smallcase/04 §5 (SC3 leaf 1.2.1).

Unit tests (no DB): weight/qty maps, preview wiring, mutation refuse.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from baskfy_api.curated_versions import (
    holdings_to_qty_map,
    preview_holdings_diff,
    refuse_version_row_mutation,
    weights_from_constituents,
)
from baskfy_core.curated_baskets import VersionImmutableError
from baskfy_core.gst import money


def _d(value: str) -> Decimal:
    return Decimal(value)


class TestKeyMaps:
    def test_weights_from_dicts(self) -> None:
        weights = weights_from_constituents(
            [
                {"instrument_id": 1, "weight": "0.5000"},
                {"instrument_id": 2, "weight": Decimal("0.5000")},
            ]
        )
        assert weights == {1: _d("0.5000"), 2: _d("0.5000")}

    def test_holdings_qty_map(self) -> None:
        qty = holdings_to_qty_map(
            [
                {"instrument_id": 1, "qty": "10.9"},
                {"instrument_id": 2, "qty": "0"},
            ]
        )
        assert qty == {1: _d("10.9")}


class TestPreviewHoldingsDiff:
    def test_fixture_matches_04_section5(self) -> None:
        diff = preview_holdings_diff(
            holdings={1: _d("10"), 2: _d("5")},
            target_weights={1: _d("0.2500"), 2: _d("0.2500"), 3: _d("0.5000")},
            prices={1: _d("100"), 2: _d("200"), 3: _d("100")},
        )
        assert diff.portfolio_value == _d("2000.00")
        by_id = {line.instrument_id: line for line in diff.lines}
        assert by_id[1].side == "SELL" and by_id[1].qty == _d("5")
        assert by_id[2].side == "SELL" and by_id[2].qty == _d("2")
        assert by_id[3].side == "BUY" and by_id[3].qty == _d("10")
        assert diff.residual_cash == money(_d("-100"))
        assert diff.top_up == _d("100.00")

    def test_refuse_mutation(self) -> None:
        with pytest.raises(VersionImmutableError, match="append-only"):
            refuse_version_row_mutation()
