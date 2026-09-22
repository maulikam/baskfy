"""What a round trip costs, itemised, in ``Decimal`` (``04`` §6).

Every conclusion about an option strategy's expectancy is a difference between two numbers of
similar size — gross edge and cost — so the cost is itemised per order and per charge, with every
rate a dated field of ``CostRates`` (verified by OP0, ``DECISIONS-OP`` OP0.1), never a blended
percentage. For a small four-leg condor the flat ₹20 brokerage dominates, which is exactly the
finding a blended rate would hide.

Ported by re-implementation from the frozen lab's ``strategies/options_costs.py`` (``option_costs``,
``exercise_stt``; PACK.2): floats become ``Decimal``, the stale IPFT rate is replaced by OP0's, and
each component is rounded to the paisa as a contract note rounds it (house rule 8).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from baskfy_core.options.config import CostRates, Side

_PAISA = Decimal("0.01")
_HUNDRED = Decimal(100)
_CRORE = Decimal(10_000_000)


def paise(value: Decimal) -> Decimal:
    """Round a rupee amount to the paisa, half up."""
    return value.quantize(_PAISA, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class CostFill:
    """One executed (or planned) order: its side, its price per unit and its units."""

    side: Side
    price: Decimal
    quantity: int

    @property
    def turnover(self) -> Decimal:
        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class ChargeBreakdown:
    """The statutory charges and brokerage over a set of orders, each rounded to the paisa."""

    orders: int
    brokerage: Decimal
    stt: Decimal
    exchange_txn: Decimal
    sebi: Decimal
    ipft: Decimal
    stamp: Decimal
    gst: Decimal

    @property
    def total(self) -> Decimal:
        return (
            self.brokerage
            + self.stt
            + self.exchange_txn
            + self.sebi
            + self.ipft
            + self.stamp
            + self.gst
        )


def charges(fills: Iterable[CostFill], rates: CostRates) -> ChargeBreakdown:
    """``04`` §6.1: brokerage per executed order, STT on the sell side's premium, exchange
    transaction and SEBI and IPFT on premium turnover both sides, stamp on the buy side, GST on
    brokerage + exchange + SEBI + IPFT."""
    orders = list(fills)
    sells = sum((f.turnover for f in orders if f.side is Side.SELL), Decimal(0))
    buys = sum((f.turnover for f in orders if f.side is Side.BUY), Decimal(0))
    turnover = sells + buys
    brokerage = paise(rates.brokerage_per_order_inr * len(orders))
    stt = paise(sells * rates.stt_sell_premium_pct / _HUNDRED)
    exchange = paise(turnover * rates.exchange_txn_pct / _HUNDRED)
    sebi = paise(turnover * rates.sebi_per_crore_inr / _CRORE)
    ipft = paise(turnover * rates.ipft_per_crore_inr / _CRORE)
    stamp = paise(buys * rates.stamp_buy_pct / _HUNDRED)
    gst = paise((brokerage + exchange + sebi + ipft) * rates.gst_pct / _HUNDRED)
    return ChargeBreakdown(
        orders=len(orders),
        brokerage=brokerage,
        stt=stt,
        exchange_txn=exchange,
        sebi=sebi,
        ipft=ipft,
        stamp=stamp,
        gst=gst,
    )


def exercise_stt(intrinsic_points: Decimal, quantity: int, rates: CostRates) -> Decimal:
    """STT on an ITM long left to settle: ``stt_exercise_intrinsic_pct`` of intrinsic * qty
    (``04`` §6.3). Levied on intrinsic, not notional; zero for an OTM leg."""
    return paise(
        max(intrinsic_points, Decimal(0)) * quantity * rates.stt_exercise_intrinsic_pct / _HUNDRED
    )


@dataclass(frozen=True, slots=True)
class HeldLong:
    """A long leg still open when a session ends: strike, type and units."""

    strike: Decimal
    is_call: bool
    quantity: int


def settlement_stt(
    held: Iterable[HeldLong],
    settlement_price: Decimal,
    hard_exit_time: dt.time,
    settle: dt.time,
    rates: CostRates,
) -> Decimal:
    """The expiry-day STT trap (``04`` §6.3), as the journal must carry it.

    A hard exit before settlement closes every leg, so nothing is exercised and the charge is
    zero — that is what makes the trap unreachable. If ``hard_exit_time`` were ever set at or past
    ``settle`` (15:30), every ITM long still held pays STT on its intrinsic value, and this is the
    number the journal would add rather than the contract note surprising anyone.
    """
    if hard_exit_time < settle:
        return Decimal("0.00")
    total = Decimal(0)
    for leg in held:
        value = settlement_price - leg.strike if leg.is_call else leg.strike - settlement_price
        total += exercise_stt(value, leg.quantity, rates)
    return paise(total)


def synthetic_half_spread(premium: Decimal, rates: CostRates) -> Decimal:
    """Tier 2's crossing cost per unit: ``synthetic_half_spread_pct`` of premium, minimum
    ``synthetic_half_spread_min_inr`` (``04`` §6.2)."""
    return max(
        premium * rates.synthetic_half_spread_pct / _HUNDRED, rates.synthetic_half_spread_min_inr
    )


def expected_gain_o1(credit_inr: Decimal, profit_take_frac: Decimal) -> Decimal:
    """O1: ``(1 - profit_take_frac) * credit_inr`` (``04`` §6.4, condor §5.3)."""
    return (Decimal(1) - profit_take_frac) * credit_inr


def expected_gain_o2(entry: Decimal, qty: int, target_frac: Decimal) -> Decimal:
    """O2: ``target_frac * E * qty`` (``04`` §6.4)."""
    return target_frac * entry * qty


def expected_gain_o3(
    width: Decimal, debit: Decimal, qty: int, target_frac_of_width: Decimal
) -> Decimal:
    """O3: ``(target_frac_of_width * width - debit) * qty`` (``04`` §6.4)."""
    return (target_frac_of_width * width - debit) * qty


class CostVerdict(StrEnum):
    OK = "OK"
    REJECTED_COST = "REJECTED_COST"


@dataclass(frozen=True, slots=True)
class CostTest:
    verdict: CostVerdict
    cost_share: Decimal | None


def cost_test(round_trip_inr: Decimal, expected_gain_inr: Decimal, share_max: Decimal) -> CostTest:
    """``cost_share = round trip / expected gain ≤ cost_share_max`` (``04`` §6.4).

    A non-positive expected gain cannot pay any cost, so it is ``REJECTED_COST`` with no share.
    """
    if expected_gain_inr <= 0:
        return CostTest(CostVerdict.REJECTED_COST, None)
    share = round_trip_inr / expected_gain_inr
    verdict = CostVerdict.OK if share <= share_max else CostVerdict.REJECTED_COST
    return CostTest(verdict, share)


def rates_review_due(rates: CostRates, today: dt.date) -> bool:
    """``OPTIONS_COST_RATES_REVIEWED_ON``'s warning: more than ``review_after_days`` since the
    rates were last checked against their primary sources."""
    return (today - rates.reviewed_on).days > rates.review_after_days
