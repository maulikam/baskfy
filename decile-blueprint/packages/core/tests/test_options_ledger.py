"""OP11 — a closed session's journal figures (``04`` §12): from fills, in ₹ and R, with costs and
MAE/MFE. The literals are the arithmetic of the fills written out."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from baskfy_core.options.bars import IST
from baskfy_core.options.config import CostRates, Side
from baskfy_core.options.costs import CostFill, charges
from baskfy_core.options.ledger import LegFill, journal_figures, pnl_points
from baskfy_core.options.structures import Structure

RATES = CostRates()
OPENED = dt.datetime(2026, 10, 27, 10, 5, 50, tzinfo=IST)
CLOSED = dt.datetime(2026, 10, 27, 11, 14, 50, tzinfo=IST)

#: A condor: wings bought at 5.10, shorts sold at 20.00 (credit 29.80), closed with the shorts
#: bought back at 5.20 and the wings sold at 0.50 (cost 9.40): a 20.40 gain per unit, 65 units.
CONDOR = [
    LegFill(Side.BUY, Decimal("5.10"), 65, closing=False),
    LegFill(Side.BUY, Decimal("5.10"), 65, closing=False),
    LegFill(Side.SELL, Decimal("20.00"), 65, closing=False),
    LegFill(Side.SELL, Decimal("20.00"), 65, closing=False),
    LegFill(Side.BUY, Decimal("5.20"), 65, closing=True),
    LegFill(Side.BUY, Decimal("5.20"), 65, closing=True),
    LegFill(Side.SELL, Decimal("0.50"), 65, closing=True),
    LegFill(Side.SELL, Decimal("0.50"), 65, closing=True),
]


class TestTheRow:
    def test_gross_is_the_signed_cash_of_every_fill(self) -> None:
        f = journal_figures(
            CONDOR, r_inr=Decimal("2500"), quantity=65, opened_at=OPENED, closed_at=CLOSED,
            peak_points=None, trough_points=None, rates=RATES,
        )  # fmt: skip
        assert f.entry_inr == Decimal("1937.00")  # (2 x 20.00 - 2 x 5.10) x 65 = 29.80 x 65
        assert f.exit_inr == Decimal("611.00")  # (2 x 5.20 - 2 x 0.50) x 65 = 9.40 x 65
        assert f.gross_pnl_inr == Decimal("1326.00")  # 20.40 x 65

    def test_costs_are_04_6_over_all_eight_orders(self) -> None:
        f = journal_figures(
            CONDOR, r_inr=Decimal("2500"), quantity=65, opened_at=OPENED, closed_at=CLOSED,
            peak_points=None, trough_points=None, rates=RATES,
        )  # fmt: skip
        expected = charges([CostFill(x.side, x.price, x.quantity) for x in CONDOR], RATES)
        assert f.costs == expected and f.costs.orders == 8
        assert f.net_pnl_inr == f.gross_pnl_inr - expected.total

    def test_r_and_the_minutes(self) -> None:
        f = journal_figures(
            CONDOR, r_inr=Decimal("2500"), quantity=65, opened_at=OPENED, closed_at=CLOSED,
            peak_points=Decimal("20.40"), trough_points=Decimal("-3.00"), rates=RATES,
        )  # fmt: skip
        assert f.r_multiple == (f.net_pnl_inr / Decimal("2500")).quantize(Decimal("0.01"))
        assert f.minutes_held == 69
        assert f.mfe_r == Decimal("0.53")  # 20.40 x 65 / 2500 = 0.5304
        assert f.mae_r == Decimal("-0.08")  # -3.00 x 65 / 2500 = -0.078

    def test_a_zero_r_is_a_sizing_bug(self) -> None:
        with pytest.raises(ValueError, match="R must be positive"):
            journal_figures(
                CONDOR, r_inr=Decimal(0), quantity=65, opened_at=OPENED, closed_at=CLOSED,
                peak_points=None, trough_points=None, rates=RATES,
            )  # fmt: skip


class TestThePnlSign:
    def test_a_gain_is_positive_for_every_structure(self) -> None:
        # A condor gains as the cost to close falls below the credit.
        assert pnl_points(Structure.IRON_CONDOR, Decimal("29.80"), Decimal("9.40")) == Decimal(
            "20.40"
        )
        # A long or a spread gains as its close value rises above what was paid.
        assert pnl_points(Structure.LONG_OPTION, Decimal("200"), Decimal("260")) == Decimal("60")
        assert pnl_points(Structure.DEBIT_SPREAD, Decimal("30"), Decimal("15")) == Decimal("-15")
