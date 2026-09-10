"""Ten equal slots, and the cap that bound is named (``docs/vbt/04`` §5).

Money is ``Decimal`` (house rule 9). The share count is the floor of the smallest of four
budgets, and **which one bound is part of the answer**, so a plan line can say "412 shares,
capped by 1% of the name's 20-day turnover" rather than presenting a number as if it were the
slot size.

There is no risk-based sizing here and that is the strategy, not an omission: VBT-1's stop is a
flat percentage of the entry, so risk-per-trade sizing and equal-weight sizing are the same
arithmetic with different constants, and the research sized it equal-weight.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from baskfy_core.vbt.config import SizingConfig

_PCT = Decimal(100)
_ZERO = Decimal(0)


class SizeCap(StrEnum):
    """Which budget decided the quantity."""

    #: Equity / ``max_slots`` — the ordinary answer.
    SLOT = "SLOT"
    #: ``max_position_pct`` of equity, which binds only when equity has drifted from the slot.
    POSITION_PCT = "POSITION_PCT"
    #: The cash the sleeve actually has, after earlier lines of the same plan.
    CASH = "CASH"
    #: 1% of the name's 20-day average turnover. Binds nowhere at ₹10 lakh; will at ₹1 crore.
    TURNOVER = "TURNOVER"


class SizeRefusal(StrEnum):
    """Why no position at all. Each is a ``SkipReason`` on the plan (``04`` §9.1)."""

    NO_SLEEVE_CAPITAL = "NO_SLEEVE_CAPITAL"
    STOP_NOT_BELOW_ENTRY = "STOP_NOT_BELOW_ENTRY"
    BELOW_MIN_TRADE_VALUE = "BELOW_MIN_TRADE_VALUE"
    TURNOVER_CAP = "TURNOVER_CAP"


@dataclass(frozen=True, slots=True)
class SizedEntry:
    """One sized limit order. ``quantity == 0`` always carries a ``refusal``."""

    quantity: int
    limit_price: Decimal
    stop_price: Decimal
    value_inr: Decimal
    position_pct: Decimal
    risk_inr: Decimal
    cap: SizeCap | None
    refusal: SizeRefusal | None

    @property
    def placed(self) -> bool:
        return self.quantity > 0


def _pct_of(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= _ZERO:
        return _ZERO
    return (numerator / denominator * _PCT).quantize(Decimal("0.01"))


def _refuse(limit: Decimal, stop: Decimal, why: SizeRefusal) -> SizedEntry:
    return SizedEntry(
        quantity=0,
        limit_price=limit,
        stop_price=stop,
        value_inr=_ZERO,
        position_pct=_ZERO,
        risk_inr=_ZERO,
        cap=None,
        refusal=why,
    )


def _floor_div(budget: Decimal, price: Decimal) -> int:
    return int((budget / price).to_integral_value(rounding=ROUND_DOWN))


def size_entry(  # noqa: PLR0913 - one keyword per input the size depends on
    *,
    equity: Decimal,
    cash_available: Decimal,
    limit_price: Decimal,
    stop_price: Decimal,
    turnover_avg_inr: Decimal | None,
    config: SizingConfig,
    slot_multiplier: Decimal = Decimal(1),
) -> SizedEntry:
    """The share count for one entry.

    ``quantity = floor(min(slot, position cap, cash, turnover cap) / limit_price)``.

    ``slot_multiplier`` is ``04`` §5.4's first-live halving, applied **here** rather than to a
    quantity at send time: the line shown must be the line sent, and every cap and refusal below
    must see the real size. It is 1 for a paper plan — the paper record rehearses the rules at
    the size the rules describe.
    """
    if equity <= _ZERO:
        return _refuse(limit_price, stop_price, SizeRefusal.NO_SLEEVE_CAPITAL)
    if limit_price <= _ZERO or stop_price >= limit_price:
        return _refuse(limit_price, stop_price, SizeRefusal.STOP_NOT_BELOW_ENTRY)

    by_slot = equity / Decimal(config.max_slots) * slot_multiplier
    by_position = equity * Decimal(str(config.max_position_pct)) / _PCT
    budgets: list[tuple[Decimal, SizeCap]] = [
        (by_slot, SizeCap.SLOT),
        (by_position, SizeCap.POSITION_PCT),
        (max(cash_available, _ZERO), SizeCap.CASH),
    ]
    if turnover_avg_inr is not None:
        budgets.append(
            (
                turnover_avg_inr * Decimal(str(config.max_position_vs_turnover)),
                SizeCap.TURNOVER,
            )
        )
    budget, cap = min(budgets, key=lambda pair: pair[0])
    quantity = _floor_div(budget, limit_price)
    value = limit_price * quantity
    if value < Decimal(str(config.min_trade_value_inr)):
        # Which refusal depends on *what* made it too small: a turnover ceiling that cannot fund
        # ₹10,000 of the name is a liquidity verdict about the name, and the page should say so
        # rather than blaming the sleeve's cash.
        why = (
            SizeRefusal.TURNOVER_CAP
            if cap is SizeCap.TURNOVER
            else SizeRefusal.BELOW_MIN_TRADE_VALUE
        )
        return _refuse(limit_price, stop_price, why)
    return SizedEntry(
        quantity=quantity,
        limit_price=limit_price,
        stop_price=stop_price,
        value_inr=value.quantize(Decimal("0.01")),
        position_pct=_pct_of(value, equity),
        risk_inr=((limit_price - stop_price) * quantity).quantize(Decimal("0.01")),
        cap=cap,
        refusal=None,
    )


def first_live_multiplier(
    *, sessions_left: int, execution_enabled: bool, config: SizingConfig
) -> Decimal:
    """``04`` §5.4: half a slot while the countdown runs **and** a confirm would be real.

    A paper plan is full size on purpose. Halving it would rehearse a size the rules do not
    describe, and the twenty DRY_RUN sessions of ``02`` §3.1 exist to rehearse the rules.
    """
    if execution_enabled and sessions_left > 0:
        return Decimal(str(config.risk_multiplier_first_live))
    return Decimal(1)
