"""Curated-basket investor accounting - docs/smallcase/04 sections 1 and 4 (SC4).

Fixtures assert the *spec*: fee boundaries straddle the 1.5% cap at 6666 / 7000;
XIRR matches a hand-computed ACT/365 rate to 4 decimal places; realized PnL covers a
partial exit at a loss. No I/O; nothing here places an order.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import cast

import pytest

from baskfy_core.curated_accounting import (
    BUY_FEE_CAP,
    FEE_RATE,
    GST_RATE,
    SIP_FEE_CAP,
    ZERO_FEE_KINDS,
    CashFlow,
    FeeKind,
    HoldingPosition,
    SellFill,
    current_investment,
    current_returns_abs,
    current_returns_pct,
    current_value,
    money_put_in,
    platform_fee,
    realized_pnl,
    xirr,
    xirr_displayable,
)
from baskfy_core.gst import money


def _d(value: str) -> Decimal:
    return Decimal(value)


# ---------------------------------------------------------------------------
# Fees - 04 section 1. Boundary amounts 6666 and 7000 straddle the 1.5% / 100 cap.
#
#   6_666 * 0.015 = 99.99  -> under cap -> base 99.99, gst 18.00, total 117.99
#   7_000 * 0.015 = 105.00 -> hits cap  -> base 100.00, gst 18.00, total 118.00
# ---------------------------------------------------------------------------


class TestPlatformFeeBoundaries:
    def test_buy_below_cap_at_6666(self) -> None:
        fee = platform_fee("BUY", _d("6666"))
        assert fee.base_fee == _d("99.99")
        assert fee.gst == _d("18.00")
        assert fee.total == _d("117.99")
        assert fee.kind == "BUY"

    def test_buy_at_cap_for_7000(self) -> None:
        fee = platform_fee("BUY", _d("7000"))
        assert fee.base_fee == BUY_FEE_CAP
        assert fee.base_fee == _d("100.00")
        assert fee.gst == _d("18.00")
        assert fee.total == _d("118.00")

    def test_invest_more_same_schedule_as_buy(self) -> None:
        low = platform_fee("INVEST_MORE", _d("6666"))
        high = platform_fee("INVEST_MORE", _d("7000"))
        assert low.total == _d("117.99")
        assert high.total == _d("118.00")

    def test_6666_and_7000_straddle_the_rate_cap(self) -> None:
        """The two fixture amounts sit on opposite sides of min(100, 1.5% * amt)."""
        under = _d("6666") * FEE_RATE
        over = _d("7000") * FEE_RATE
        assert under < BUY_FEE_CAP < over
        assert platform_fee("BUY", _d("6666")).base_fee == under
        assert platform_fee("BUY", _d("7000")).base_fee == BUY_FEE_CAP

    def test_sip_cap_is_ten_rupees(self) -> None:
        # 666 * 1.5% = 9.99 (under 10); 700 * 1.5% = 10.50 -> capped at 10.
        under = platform_fee("SIP", _d("666"))
        over = platform_fee("SIP", _d("700"))
        assert under.base_fee == _d("9.99")
        assert under.gst == _d("1.80")
        assert under.total == _d("11.79")
        assert over.base_fee == SIP_FEE_CAP
        assert over.gst == _d("1.80")
        assert over.total == _d("11.80")

    @pytest.mark.parametrize(
        "kind",
        ["REBALANCE", "EXIT", "PARTIAL_EXIT", "CUSTOMIZE"],
    )
    def test_zero_fee_kinds(self, kind: str) -> None:
        fee_kind = cast(FeeKind, kind)
        assert fee_kind in ZERO_FEE_KINDS
        fee = platform_fee(fee_kind, _d("50000"))
        assert fee.base_fee == _d("0.00")
        assert fee.gst == _d("0.00")
        assert fee.total == _d("0.00")

    def test_gst_is_eighteen_percent_of_base(self) -> None:
        fee = platform_fee("BUY", _d("7000"))
        assert fee.gst == money(fee.base_fee * GST_RATE)
        assert fee.gst == _d("18.00")

    def test_rejects_negative_amount(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            platform_fee("BUY", _d("-1"))


# ---------------------------------------------------------------------------
# Ledgers - 04 section 4
# ---------------------------------------------------------------------------


class TestLedgers:
    def test_money_put_in_sums_buy_side_only(self) -> None:
        assert money_put_in([_d("10000"), _d("2500"), _d("1000")]) == _d("13500.00")

    def test_current_investment_nets_exited_cost(self) -> None:
        # Put in 50_000; exit half the cost basis (20_000) -> current investment 30_000.
        assert current_investment(_d("50000"), _d("20000")) == _d("30000.00")

    def test_current_value_from_positions(self) -> None:
        holdings = [
            HoldingPosition(instrument_id=1, qty=_d("10"), avg_price=_d("100")),
            HoldingPosition(instrument_id=2, qty=_d("5"), avg_price=_d("200")),
        ]
        prices = {1: _d("110"), 2: _d("180")}
        # 10*110 + 5*180 = 1100 + 900 = 2000
        assert current_value(holdings, prices) == _d("2000.00")

    def test_current_returns(self) -> None:
        assert current_returns_abs(_d("12000"), _d("10000")) == _d("2000.00")
        assert current_returns_pct(_d("12000"), _d("10000")) == _d("0.20")


# ---------------------------------------------------------------------------
# Realized PnL - partial exit at a loss (SC4 AC)
# ---------------------------------------------------------------------------


class TestRealizedPnl:
    def test_partial_exit_at_a_loss(self) -> None:
        # Bought 100 @ 200; sold 40 @ 180 -> realized = 40 * (180 - 200) = -800.
        pnl = realized_pnl(
            [SellFill(qty=_d("40"), sell_price=_d("180"), avg_cost=_d("200"))]
        )
        assert pnl == _d("-800.00")

    def test_mixed_fills_net(self) -> None:
        pnl = realized_pnl(
            [
                SellFill(qty=_d("10"), sell_price=_d("120"), avg_cost=_d("100")),
                SellFill(qty=_d("5"), sell_price=_d("90"), avg_cost=_d("100")),
            ]
        )
        # +200 + (-50) = +150
        assert pnl == _d("150.00")


# ---------------------------------------------------------------------------
# XIRR - hand fixture to 4 decimal places (SC4 AC / leaf 1.8.5)
#
# Hand derivation (ACT/365):
#   CF0: -100_000 on 2021-01-01
#   CF1: +115_000 on 2022-01-01   (exactly 365 days -> year fraction 1)
#   Solve -100000 + 115000 / (1+r)^1 = 0  =>  r = 0.15 exactly
#   Quantized to 4 dp: 0.1500
# ---------------------------------------------------------------------------


class TestXirrHandFixture:
    def test_exact_one_year_fifteen_percent(self) -> None:
        rate = xirr(
            [
                CashFlow(on=dt.date(2021, 1, 1), amount=_d("-100000")),
                CashFlow(on=dt.date(2022, 1, 1), amount=_d("115000")),
            ]
        )
        assert rate == _d("0.1500")

    def test_irregular_flows_match_hand_four_dp(self) -> None:
        """Hand-solved Newton root for the three-flow series is 0.08260024... -> 0.0826.

        CF: -100000 (2022-01-15), -25000 (2022-06-15), +140000 (2023-07-20).
        Year fractions from t0: 0, 151/365, 551/365.
        """
        rate = xirr(
            [
                CashFlow(on=dt.date(2022, 1, 15), amount=_d("-100000")),
                CashFlow(on=dt.date(2022, 6, 15), amount=_d("-25000")),
                CashFlow(on=dt.date(2023, 7, 20), amount=_d("140000")),
            ]
        )
        assert rate == _d("0.0826")

    def test_displayable_only_after_365_days(self) -> None:
        # Non-leap: 2023-01-01 -> 2024-01-01 = 365 days -> still hidden; +1 day -> shown.
        assert xirr_displayable(dt.date(2023, 1, 1), dt.date(2024, 1, 1)) is False
        assert xirr_displayable(dt.date(2023, 1, 1), dt.date(2024, 1, 2)) is True
        # Leap span 2024-01-01 -> 2025-01-01 = 366 days > 365 -> shown.
        assert xirr_displayable(dt.date(2024, 1, 1), dt.date(2025, 1, 1)) is True

    def test_all_one_sign_returns_none(self) -> None:
        assert (
            xirr(
                [
                    CashFlow(on=dt.date(2021, 1, 1), amount=_d("-1000")),
                    CashFlow(on=dt.date(2022, 1, 1), amount=_d("-500")),
                ]
            )
            is None
        )
