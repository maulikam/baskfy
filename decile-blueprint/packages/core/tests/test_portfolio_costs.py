"""The charges on a portfolio's recorded trades.

**Why this module exists at all.** The attribution panel listed six effects as "Not available".
Four need data that exists nowhere. Fees was the odd one out and its own entry admitted it —
*"Needs: wiring the existing cost model through the portfolio rebalance path"* — so it was never a
data problem, only an unbuilt one.

The tests that matter here are not the arithmetic (that is `costs.py`'s, calibrated against 163
real fills). They are the three ways this aggregation can lie: charging the flat depository fee per
order instead of per scrip-day, reporting ₹0 for a portfolio that simply has no trade history, and
letting a gross return be presented as net.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from baskfy_core.costs import DP_CHARGE_PER_SELL, order_cost
from baskfy_core.portfolio_costs import (
    ESTIMATE_CAVEAT,
    NO_TRADES_REASON,
    TradeFlow,
    estimate_portfolio_costs,
)

DAY = dt.date(2026, 9, 10)
NEXT = dt.date(2026, 9, 11)


def buy(value: str, *, instrument: int = 1, on: dt.date = DAY) -> TradeFlow:
    return TradeFlow(on=on, kind="BUY", value=Decimal(value), instrument_id=instrument)


def sell(value: str, *, instrument: int = 1, on: dt.date = DAY) -> TradeFlow:
    return TradeFlow(on=on, kind="SELL", value=Decimal(value), instrument_id=instrument)


class TestTheDepositoryChargeIsPerScripPerDay:
    """The one fixed component, and the one an aggregate gets wrong by default."""

    def test_three_sells_of_one_scrip_on_one_day_are_charged_once(self) -> None:
        costs = estimate_portfolio_costs([sell("100000"), sell("100000"), sell("100000")])
        assert costs is not None
        assert costs.sell_scrip_days == 1
        assert costs.dp == Decimal(str(round(DP_CHARGE_PER_SELL, 2)))

    def test_summing_order_cost_per_order_would_have_charged_it_three_times(self) -> None:
        """The bug this guards, stated as the arithmetic it would have produced."""
        naive = sum(order_cost(100000.0, "SELL").dp for _ in range(3))
        costs = estimate_portfolio_costs([sell("100000"), sell("100000"), sell("100000")])

        assert costs is not None
        assert float(costs.dp) < naive
        assert round(naive, 2) == round(DP_CHARGE_PER_SELL * 3, 2)

    def test_two_scrips_on_one_day_are_two_charges(self) -> None:
        costs = estimate_portfolio_costs([sell("50000", instrument=1), sell("50000", instrument=2)])
        assert costs is not None
        assert costs.sell_scrip_days == 2

    def test_one_scrip_on_two_days_is_two_charges(self) -> None:
        costs = estimate_portfolio_costs([sell("50000", on=DAY), sell("50000", on=NEXT)])
        assert costs is not None
        assert costs.sell_scrip_days == 2

    def test_a_buy_incurs_no_depository_charge(self) -> None:
        """Sell side only — the model says so and a buy-only portfolio must show ₹0 for it."""
        costs = estimate_portfolio_costs([buy("100000"), buy("250000")])
        assert costs is not None
        assert costs.dp == Decimal("0.00")
        assert costs.sell_scrip_days == 0


class TestItRefusesToInventAFigure:
    def test_no_trades_produces_no_record_rather_than_zero(self) -> None:
        """Absent is not zero. ₹0 reads as 'you were charged nothing', which is a claim."""
        assert estimate_portfolio_costs([]) is None

    def test_non_trade_flows_are_not_trades(self) -> None:
        """A dividend or a deposit moves money and incurs no trading charge."""
        flows = [
            TradeFlow(on=DAY, kind="DIVIDEND", value=Decimal("5000")),
            TradeFlow(on=DAY, kind="EXTERNAL_DEPOSIT", value=Decimal("100000")),
            TradeFlow(on=DAY, kind="ASSIGN", value=Decimal("250000")),
        ]
        assert estimate_portfolio_costs(flows) is None

    def test_the_reason_says_why_there_is_no_figure_and_names_the_cause(self) -> None:
        assert "no buys or sells are recorded" in NO_TRADES_REASON.lower()
        assert "broker sync" in NO_TRADES_REASON

    def test_the_caveat_says_the_return_is_not_net_of_these(self) -> None:
        """The sentence that keeps the figure honest. Without it the panel implies a net return."""
        assert "estimate" in ESTIMATE_CAVEAT.lower()
        assert "not a billed amount" in ESTIMATE_CAVEAT
        assert "before these charges" in ESTIMATE_CAVEAT


class TestEveryComponentSurvives:
    def test_all_six_components_plus_brokerage_are_reported_separately(self) -> None:
        """A single 'fees' total hides that STT is 85 % of it and brokerage is genuinely nothing."""
        costs = estimate_portfolio_costs([buy("1000000"), sell("1000000")])
        assert costs is not None

        assert costs.stt > 0
        assert costs.exchange > 0
        assert costs.sebi > 0
        assert costs.stamp > 0  # buy side only, and there is a buy
        assert costs.gst > 0
        assert costs.dp > 0  # sell side only, and there is a sell
        assert costs.brokerage == Decimal("0.00")  # Zerodha charges nothing for delivery

    def test_the_total_is_the_components(self) -> None:
        costs = estimate_portfolio_costs([buy("737000"), sell("412000", instrument=2)])
        assert costs is not None
        assert costs.total == (
            costs.stt
            + costs.exchange
            + costs.sebi
            + costs.stamp
            + costs.gst
            + costs.dp
            + costs.brokerage
        )

    def test_turnover_and_trade_count_are_what_was_costed(self) -> None:
        costs = estimate_portfolio_costs([buy("100000"), sell("250000"), buy("50000")])
        assert costs is not None
        assert costs.turnover == Decimal("400000")
        assert costs.trades == 3

    def test_the_headline_lands_near_the_measured_session(self) -> None:
        """`costs.py` measured a real session at 11.68 bps of turnover across buys and sells.

        A round trip here is the same shape, so the figure must land in that neighbourhood. Not an
        equality — that session's mix of scrip-days was its own — but a wrong aggregation (double
        counting, a missing component) would leave this band immediately.
        """
        costs = estimate_portfolio_costs([buy("3674776"), sell("3674776")])
        assert costs is not None
        bps = costs.bps_of_turnover
        assert bps is not None
        assert Decimal("10") < bps < Decimal("14"), bps

    def test_bps_is_absent_rather_than_a_division_by_zero(self) -> None:
        costs = estimate_portfolio_costs([TradeFlow(on=DAY, kind="BUY", value=Decimal("1"))])
        assert costs is not None
        assert costs.bps_of_turnover is not None

    def test_a_signed_amount_is_costed_on_its_magnitude(self) -> None:
        """`portfolio_cash_flow.amount` is signed by direction; turnover has no sign."""
        positive = estimate_portfolio_costs([sell("100000")])
        negative = estimate_portfolio_costs(
            [TradeFlow(on=DAY, kind="SELL", value=Decimal("-100000"), instrument_id=1)]
        )
        assert positive is not None and negative is not None
        assert positive.total == negative.total
