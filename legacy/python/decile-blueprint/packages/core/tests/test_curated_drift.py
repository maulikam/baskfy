"""Holdings drift detection - docs/smallcase/04 section 7 (SC4 / leaf 1.3.2).

Simulated direct sale raises a DRIFT action; fix re-bases with a synthetic EXIT.
Excess at the broker becomes a CUSTOMIZE history marker.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from baskfy_core.curated_accounting import current_investment, money_put_in
from baskfy_core.curated_drift import (
    CUSTOMIZE_KIND,
    DRIFT_ACTION_TYPE,
    BrokerHolding,
    LedgerHolding,
    detect_drift,
    detect_drift_maps,
    fix_drift,
    rebase_holdings_after_drift,
)


def _d(value: str) -> Decimal:
    return Decimal(value)


class TestDetectDrift:
    def test_direct_sale_shortfall_raises_drift_action(self) -> None:
        # Ledger still shows 100; broker (direct sale) shows 60 -> shortfall 40.
        detection = detect_drift(
            ledger=[
                LedgerHolding(instrument_id=1, qty=_d("100"), avg_price=_d("2500")),
                LedgerHolding(instrument_id=2, qty=_d("50"), avg_price=_d("100")),
            ],
            broker=[
                BrokerHolding(instrument_id=1, qty=_d("60")),
                BrokerHolding(instrument_id=2, qty=_d("50")),
            ],
        )
        assert detection.action_type == DRIFT_ACTION_TYPE
        assert len(detection.deltas) == 1
        delta = detection.deltas[0]
        assert delta.instrument_id == 1
        assert delta.shortfall == _d("40")
        assert delta.broker_qty == _d("60")
        assert delta.excess == _d("0")

    def test_matched_books_produce_no_action(self) -> None:
        detection = detect_drift(
            ledger=[LedgerHolding(instrument_id=1, qty=_d("10"), avg_price=_d("100"))],
            broker=[BrokerHolding(instrument_id=1, qty=_d("10"))],
        )
        assert detection.action_type is None
        assert detection.deltas == ()

    def test_excess_is_customize_not_drift(self) -> None:
        detection = detect_drift(
            ledger=[LedgerHolding(instrument_id=1, qty=_d("10"), avg_price=_d("100"))],
            broker=[BrokerHolding(instrument_id=1, qty=_d("15"))],
        )
        assert detection.action_type is None
        assert detection.customize_instrument_ids == (1,)
        assert detection.deltas[0].excess == _d("5")
        assert CUSTOMIZE_KIND == "CUSTOMIZE"


class TestFixDrift:
    def test_fix_rebases_with_synthetic_exit(self) -> None:
        ledger = [
            LedgerHolding(instrument_id=1, qty=_d("100"), avg_price=_d("2500")),
            LedgerHolding(instrument_id=2, qty=_d("50"), avg_price=_d("100")),
        ]
        broker = [
            BrokerHolding(instrument_id=1, qty=_d("60")),
            BrokerHolding(instrument_id=2, qty=_d("50")),
        ]
        result = fix_drift(ledger, broker)
        assert result.cleared_action is True
        assert len(result.synthetic_exits) == 1
        exit_lot = result.synthetic_exits[0]
        assert exit_lot.instrument_id == 1
        assert exit_lot.qty == _d("40")
        assert exit_lot.avg_cost == _d("2500")
        assert exit_lot.kind == "EXIT"

        by_id = {h.instrument_id: h for h in result.holdings}
        assert by_id[1].qty == _d("60")
        assert by_id[1].avg_price == _d("2500.00")
        assert by_id[2].qty == _d("50")

    def test_full_broker_exit_drops_holding(self) -> None:
        result = fix_drift(
            ledger=[LedgerHolding(instrument_id=1, qty=_d("10"), avg_price=_d("50"))],
            broker=[BrokerHolding(instrument_id=1, qty=_d("0"))],
        )
        assert result.holdings == ()
        assert result.synthetic_exits[0].qty == _d("10")

    def test_synthetic_exit_keeps_current_investment_honest(self) -> None:
        put_in = money_put_in([_d("10000")])
        result = fix_drift(
            ledger=[LedgerHolding(instrument_id=1, qty=_d("50"), avg_price=_d("100"))],
            broker=[BrokerHolding(instrument_id=1, qty=_d("30"))],
        )
        exited_basis = result.synthetic_exits[0].cost_basis
        assert exited_basis == _d("2000.00")
        assert current_investment(put_in, exited_basis) == _d("8000.00")


class TestMappingContract:
    """Leaf contract: detect_drift_maps / rebase_holdings_after_drift on {id: qty}."""

    def test_detect_drift_maps_shortfalls(self) -> None:
        shortfalls = detect_drift_maps(
            {1: _d("100"), 2: _d("50")},
            {1: _d("100"), 2: _d("30")},
        )
        assert len(shortfalls) == 1
        assert shortfalls[0].instrument_id == 2
        assert shortfalls[0].delta == _d("20")

    def test_rebase_holdings_after_drift_shortfall_and_excess(self) -> None:
        result = rebase_holdings_after_drift(
            intended={1: _d("100")},
            broker={1: _d("80"), 2: _d("5")},
            avg_prices={1: _d("50"), 2: _d("40")},
        )
        assert result.new_intended == {1: _d("80"), 2: _d("5")}
        assert result.synthetic_exits[0].qty == _d("20")
        assert result.synthetic_exits[0].cost_basis == _d("1000.00")
        assert len(result.customize_markers) == 1
        assert result.customize_markers[0].kind == "CUSTOMIZE"
        assert result.customize_markers[0].qty == _d("5")

    def test_rebase_requires_avg_price_on_shortfall(self) -> None:
        with pytest.raises(KeyError, match="avg_price"):
            rebase_holdings_after_drift(
                intended={1: _d("5")},
                broker={1: _d("1")},
                avg_prices={},
            )
