"""What an FO round trip costs, itemised, in ``Decimal`` (``04`` §3, §10).

**F1**: the options pack's rates (``docs/options/04`` §6, OP0.1) on all eight orders through
``baskfy_core.options.costs.charges`` — no exercise STT, because nothing is held to expiry — plus
a crossing cost per leg per side of ``max(slippage_min, slippage_pct of premium)``. The plan
shows the round trip in ₹ and as a share of the credit; above ``f1_max_cost_share`` (25 %) the
plan is ``REJECTED_COST``. Exit premiums are the caller's estimate (by default the entry mids).

**F2**: ``04`` §10's futures charges per order — STT 0.05 % on the sale, exchange 0.00173 % a
side, stamp 0.002 % on the buy, ₹20 an order, GST 18 % on brokerage + exchange — plus
slippage 0.03 % a side; a roll is one sale and one buy, charged the same way.

Every component is rounded to the paisa, as a contract note rounds it (house rule 8).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from baskfy_core.fno.condor import LegRole, sign_of
from baskfy_core.fno.config import F1Config, FutureCostRates, PlanState
from baskfy_core.options.config import CostRates, Side
from baskfy_core.options.costs import ChargeBreakdown, CostFill, charges, paise

_HUNDRED = Decimal(100)


def crossing_cost(premium: Decimal, config: F1Config) -> Decimal:
    """Slippage per unit per crossing: ``max(min ₹, pct of premium)`` (``RESEARCH.md``)."""
    return max(config.slippage_min_inr, premium * config.slippage_pct / _HUNDRED)


def condor_fills(
    entry: Mapping[LegRole, Decimal], exit_: Mapping[LegRole, Decimal], quantity: int
) -> tuple[CostFill, ...]:
    """The eight orders: longs bought and shorts sold at entry, the reverse at exit."""
    fills: list[CostFill] = []
    for role in LegRole:
        opening = Side.BUY if sign_of(role) > 0 else Side.SELL
        closing = Side.SELL if opening is Side.BUY else Side.BUY
        fills.append(CostFill(opening, entry[role], quantity))
        fills.append(CostFill(closing, exit_[role], quantity))
    return tuple(fills)


@dataclass(frozen=True, slots=True)
class CondorCost:
    charges: ChargeBreakdown
    slippage_inr: Decimal
    round_trip_inr: Decimal
    credit_inr: Decimal
    #: Round trip ÷ credit, as a percent; ``None`` when the credit is not positive.
    cost_share_pct: Decimal | None
    state: PlanState | None


def condor_round_trip(
    *,
    entry: Mapping[LegRole, Decimal],
    exit_: Mapping[LegRole, Decimal] | None,
    quantity: int,
    rates: CostRates,
    config: F1Config,
) -> CondorCost:
    """``04`` §3: the round trip in ₹ and as a share of the credit; ``REJECTED_COST`` above
    ``f1_max_cost_share``."""
    closing = entry if exit_ is None else exit_
    breakdown = charges(condor_fills(entry, closing, quantity), rates)
    slip_units = sum(
        (crossing_cost(entry[r], config) + crossing_cost(closing[r], config) for r in LegRole),
        Decimal(0),
    )
    slippage = paise(slip_units * quantity)
    round_trip = breakdown.total + slippage
    credit_inr = paise(sum((-sign_of(r) * entry[r] for r in LegRole), Decimal(0)) * quantity)
    if credit_inr <= 0:
        return CondorCost(
            breakdown, slippage, round_trip, credit_inr, None, PlanState.REJECTED_COST
        )
    share = (round_trip / credit_inr * _HUNDRED).quantize(Decimal("0.01"))
    state = PlanState.REJECTED_COST if share > config.max_cost_share_pct else None
    return CondorCost(breakdown, slippage, round_trip, credit_inr, share, state)


@dataclass(frozen=True, slots=True)
class FutureCharges:
    orders: int
    brokerage: Decimal
    stt: Decimal
    exchange_txn: Decimal
    stamp: Decimal
    gst: Decimal
    slippage: Decimal

    @property
    def total(self) -> Decimal:
        return self.brokerage + self.stt + self.exchange_txn + self.stamp + self.gst + self.slippage


def future_charges(
    buys_inr: Decimal, sells_inr: Decimal, orders: int, rates: FutureCostRates
) -> FutureCharges:
    """``04`` §10 over a set of futures orders with the given buy and sell turnover."""
    turnover = buys_inr + sells_inr
    brokerage = paise(rates.brokerage_per_order_inr * orders)
    exchange = paise(turnover * rates.exchange_txn_pct / _HUNDRED)
    return FutureCharges(
        orders=orders,
        brokerage=brokerage,
        stt=paise(sells_inr * rates.stt_sell_pct / _HUNDRED),
        exchange_txn=exchange,
        stamp=paise(buys_inr * rates.stamp_buy_pct / _HUNDRED),
        gst=paise((brokerage + exchange) * rates.gst_pct / _HUNDRED),
        slippage=paise(turnover * rates.slippage_pct / _HUNDRED),
    )


def future_round_trip(
    entry: Decimal, exit_: Decimal, quantity: int, rates: FutureCostRates
) -> FutureCharges:
    """A long future bought at ``entry`` and sold at ``exit_``: two orders."""
    return future_charges(entry * quantity, exit_ * quantity, 2, rates)


def future_roll(
    sell_old: Decimal, buy_new: Decimal, quantity: int, rates: FutureCostRates
) -> FutureCharges:
    """F2's calendar roll: the held contract sold and the next bought (``04`` §10)."""
    return future_charges(buy_new * quantity, sell_old * quantity, 2, rates)
