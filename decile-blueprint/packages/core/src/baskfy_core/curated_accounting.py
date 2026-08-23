"""Investor accounting for curated baskets — docs/smallcase/04 §§1 and 4 (SC4).

Pure Decimal math: cash flows and holdings in, fee rows / ledgers / XIRR out.
No database, no network, no clock, no OrderGateway.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from baskfy_core.gst import money

__all__ = [
    "BUY_FEE_CAP",
    "FEE_RATE",
    "GST_RATE",
    "SIP_FEE_CAP",
    "XIRR_DISPLAY_MIN_DAYS",
    "ZERO_FEE_KINDS",
    "CashFlow",
    "FeeBreakdown",
    "FeeKind",
    "HoldingPosition",
    "InvestorSnapshot",
    "SellFill",
    "current_investment",
    "current_returns_abs",
    "current_returns_pct",
    "current_value",
    "money_put_in",
    "platform_fee",
    "realized_pnl",
    "xirr",
    "xirr_displayable",
]

FeeKind = Literal[
    "BUY",
    "INVEST_MORE",
    "SIP",
    "REBALANCE",
    "EXIT",
    "PARTIAL_EXIT",
    "CUSTOMIZE",
]

#: docs/smallcase/04 §1 — buy / invest-more cap.
BUY_FEE_CAP: Final = Decimal("100")

#: docs/smallcase/04 §1 — SIP instalment cap.
SIP_FEE_CAP: Final = Decimal("10")

#: Platform fee rate before the cap.
FEE_RATE: Final = Decimal("0.015")

#: GST on the platform base fee.
GST_RATE: Final = Decimal("0.18")

#: Kinds that accrue zero platform fee (broker charges are out of scope).
ZERO_FEE_KINDS: Final[frozenset[FeeKind]] = frozenset(
    {"REBALANCE", "EXIT", "PARTIAL_EXIT", "CUSTOMIZE"}
)

#: ACT/365 year basis for money-weighted return.
_DAYS_PER_YEAR: Final = Decimal("365")

#: XIRR is shown only after the first investment is older than this many days (04 section 4).
XIRR_DISPLAY_MIN_DAYS: Final = 365

#: Minimum cash-flow count for an XIRR root to exist.
_MIN_XIRR_FLOWS: Final = 2

#: XIRR display quantisation (module AC: 4 decimal places on the annual rate).
_XIRR_QUANT: Final = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class FeeBreakdown:
    """One ``cb_fee_ledger`` row's computed columns (``collected`` stays false this run)."""

    kind: FeeKind
    base_fee: Decimal
    gst: Decimal
    total: Decimal


@dataclass(frozen=True, slots=True)
class CashFlow:
    """One signed cash movement. Buys are negative; sells and dividends are positive."""

    on: dt.date
    amount: Decimal


@dataclass(frozen=True, slots=True)
class HoldingPosition:
    instrument_id: int
    qty: Decimal
    avg_price: Decimal


@dataclass(frozen=True, slots=True)
class SellFill:
    """One exited quantity at an execution price against the lot's average cost."""

    qty: Decimal
    sell_price: Decimal
    avg_cost: Decimal


@dataclass(frozen=True, slots=True)
class InvestorSnapshot:
    """docs/smallcase/04 §4 investor math for one investment."""

    money_put_in: Decimal
    current_investment: Decimal
    current_value: Decimal
    current_returns: Decimal
    current_returns_pct: Decimal
    realized_pnl: Decimal
    dividends: Decimal


def platform_fee(kind: FeeKind, amount: Decimal) -> FeeBreakdown:
    """Compute the platform fee for one executed batch - docs/smallcase/04 section 1.

    Buy / Invest-more::

        base = min(100, 1.5% * amount); gst = 18% * base; total = base + gst

    SIP::

        base = min(10, 1.5% * amount) + 18% GST

    Rebalance, exit, partial exit, customize -> zero.
    All money columns round half-up to 2 dp via :func:`baskfy_core.gst.money`.
    """
    if amount < 0:
        raise ValueError("fee amount cannot be negative")
    if kind in ZERO_FEE_KINDS:
        zero = money(Decimal("0"))
        return FeeBreakdown(kind=kind, base_fee=zero, gst=zero, total=zero)

    if kind in ("BUY", "INVEST_MORE"):
        cap = BUY_FEE_CAP
    elif kind == "SIP":
        cap = SIP_FEE_CAP
    else:
        raise ValueError(f"unknown fee kind: {kind}")

    uncapped = money(amount * FEE_RATE)
    base = money(min(cap, uncapped))
    gst = money(base * GST_RATE)
    total = money(base + gst)
    return FeeBreakdown(kind=kind, base_fee=base, gst=gst, total=total)


def money_put_in(buy_side_amounts: Sequence[Decimal]) -> Decimal:
    """Sum of buy-side cash (buy / invest-more / SIP), net of nothing - 04 section 4."""
    total = Decimal("0")
    for amount in buy_side_amounts:
        if amount < 0:
            raise ValueError("buy-side amount cannot be negative")
        total += amount
    return money(total)


def current_investment(
    money_put_in_amount: Decimal,
    exited_cost_basis: Decimal,
) -> Decimal:
    """Money put in minus cost basis of exited quantity - 04 section 4."""
    if exited_cost_basis < 0:
        raise ValueError("exited cost basis cannot be negative")
    return money(money_put_in_amount - exited_cost_basis)


def current_value(
    holdings: Sequence[HoldingPosition] | Mapping[int, Decimal],
    prices: Mapping[int, Decimal],
) -> Decimal:
    """Holdings * last price - 04 section 4."""
    total = Decimal("0")
    if isinstance(holdings, Mapping):
        items = holdings.items()
        for instrument_id, qty in items:
            if qty < 0:
                raise ValueError("holding qty cannot be negative")
            price = prices[instrument_id]
            if price < 0:
                raise ValueError("price cannot be negative")
            total += qty * price
    else:
        for position in holdings:
            if position.qty < 0:
                raise ValueError("holding qty cannot be negative")
            price = prices[position.instrument_id]
            if price < 0:
                raise ValueError("price cannot be negative")
            total += position.qty * price
    return money(total)


def current_returns_abs(value: Decimal, investment: Decimal) -> Decimal:
    """Current returns in INR (value - current investment)."""
    return money(value - investment)


def current_returns_pct(value: Decimal, investment: Decimal) -> Decimal:
    """Current returns as a fraction of current investment (2 dp). Zero investment -> 0."""
    if investment == 0:
        return money(Decimal("0"))
    return money((value - investment) / investment)


def realized_pnl(fills: Sequence[SellFill]) -> Decimal:
    """Realized INR from sells at execution price vs avg cost - can be negative (04 section 4)."""
    total = Decimal("0")
    for fill in fills:
        if fill.qty < 0:
            raise ValueError("sell qty cannot be negative")
        if fill.sell_price < 0 or fill.avg_cost < 0:
            raise ValueError("prices cannot be negative")
        total += fill.qty * (fill.sell_price - fill.avg_cost)
    return money(total)


def xirr_displayable(first_date: dt.date, as_of: dt.date) -> bool:
    """XIRR is shown only when the first investment is more than 365 days old - 04 section 4."""
    if as_of < first_date:
        raise ValueError("as_of cannot precede first_date")
    return (as_of - first_date).days > XIRR_DISPLAY_MIN_DAYS


def xirr(  # noqa: PLR0911, PLR0912, PLR0915 - Newton then bisection; splitting hides the solver
    flows: Sequence[CashFlow],
    *,
    guess: Decimal = Decimal("0.1"),
) -> Decimal | None:
    """Money-weighted annual return (ACT/365) as a Decimal.

    Sign convention matches the desk and smallcase: invest negative, withdraw / dividend /
    terminal value positive. Returns ``None`` when no root exists (all one sign, or fewer
    than two flows). The returned rate is quantized to 4 decimal places.
    """
    if len(flows) < _MIN_XIRR_FLOWS:
        return None
    ordered = sorted(flows, key=lambda f: (f.on, f.amount))
    amounts = [f.amount for f in ordered]
    if not (any(a > 0 for a in amounts) and any(a < 0 for a in amounts)):
        return None

    t0 = ordered[0].on
    years = [Decimal((f.on - t0).days) / _DAYS_PER_YEAR for f in ordered]

    def npv(rate: Decimal) -> Decimal:
        total = Decimal("0")
        one = Decimal("1") + rate
        if one <= 0:
            # Domain of (1+r)^t - treat as non-finite for the solver.
            return Decimal("1") if amounts[0] > 0 else Decimal("-1")
        for amount, year in zip(amounts, years, strict=True):
            total += amount / (one**year)
        return total

    def d_npv(rate: Decimal) -> Decimal:
        total = Decimal("0")
        one = Decimal("1") + rate
        if one <= 0:
            return Decimal("0")
        for amount, year in zip(amounts, years, strict=True):
            total += -amount * year / (one ** (year + Decimal("1")))
        return total

    rate = guess
    tol = Decimal("1e-12")
    for _ in range(100):
        if rate <= Decimal("-1"):
            break
        f = npv(rate)
        if abs(f) < tol:
            return rate.quantize(_XIRR_QUANT)
        d = d_npv(rate)
        if d == 0:
            break
        nxt = rate - f / d
        if nxt <= Decimal("-1"):
            break
        if abs(nxt - rate) < tol:
            return nxt.quantize(_XIRR_QUANT)
        rate = nxt
    else:
        if abs(npv(rate)) < Decimal("1e-6"):
            return rate.quantize(_XIRR_QUANT)

    # Bisection fallback over a scanned bracket.
    lo = Decimal("-0.9999")
    f_lo = npv(lo)
    bracket: tuple[Decimal, Decimal] | None = None
    # Coarse grid then fine high end - same shape as the desk solver.
    grid: list[Decimal] = []
    step = Decimal("0.01")
    cursor = Decimal("-0.99")
    while cursor <= Decimal("1"):
        grid.append(cursor)
        cursor += step
    cursor = Decimal("1")
    while cursor <= Decimal("100"):
        grid.append(cursor)
        cursor += Decimal("0.5")

    for hi in grid:
        if hi <= lo:
            continue
        f_hi = npv(hi)
        if f_lo * f_hi < 0:
            bracket = (lo, hi)
            break
        lo, f_lo = hi, f_hi

    if bracket is None:
        return None

    lo, hi = bracket
    for _ in range(200):
        mid = (lo + hi) / Decimal("2")
        f_mid = npv(mid)
        if abs(f_mid) < tol or (hi - lo) < tol:
            return mid.quantize(_XIRR_QUANT)
        if npv(lo) * f_mid < 0:
            hi = mid
        else:
            lo = mid
    return ((lo + hi) / Decimal("2")).quantize(_XIRR_QUANT)
