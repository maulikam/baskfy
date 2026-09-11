"""Ten equal slots, and every cap named, in order (``docs/twt/04`` §6).

Money is ``Decimal`` (house rule 9). The share count is the floor of a target value that four caps
take turns lowering, **in the order ``04`` §6.2 gives them**, and *which* ones bound is part of the
answer — so a plan line can say "412 shares, capped by 1 % of the name's 20-session turnover"
rather than presenting a number as if it were the slot size.

The order is not decoration. With ``min`` alone it would only matter on a tie, but the caps answer
different questions and the note a person reads should name the *first* reason the line is not a
slot: a ceiling on the position, then a verdict about the name's liquidity, then the sleeve's own
cash. ``04`` §6.2 fixes it, and this module walks it.

There is no risk-based sizing here and that is the strategy, not an omission: TWT-1's stop is a flat
percentage of the entry, so risk-per-trade sizing and equal-weight sizing are the same arithmetic
with different constants, and the research sized it equal-weight.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from baskfy_core.twt.config import SizingConfig

_PCT = Decimal(100)
_ZERO = Decimal(0)
_PAISE = Decimal("0.01")


class SizeCap(StrEnum):
    """Which budget decided the quantity (``04`` §6.2), in the order they are applied."""

    #: ``equity / max_slots`` — the ordinary answer, and the only one that is not a cap.
    SLOT = "SLOT"
    #: ``max_position_pct`` [12.5 %] of equity, which binds only when equity has drifted.
    POSITION_PCT = "POSITION_PCT"
    #: 1 % of the name's 20-session average turnover. At ₹25 lakh over ten slots it binds until the
    #: name turns over ₹2.5 crore a day — which is the whole argument for the ₹5 crore floor
    #: (``04`` §3.5, DECISIONS-TW TW0.3).
    TURNOVER = "TURNOVER"
    #: The cash the sleeve actually has, after earlier lines of the same plan.
    CASH = "CASH"


class SizeRefusal(StrEnum):
    """Why no position at all. Each is a ``SkipReason`` on the plan (``04`` §10.1)."""

    NO_SLEEVE_CAPITAL = "NO_SLEEVE_CAPITAL"
    STOP_NOT_BELOW_ENTRY = "STOP_NOT_BELOW_ENTRY"
    BELOW_MIN_TRADE_VALUE = "BELOW_MIN_TRADE_VALUE"


@dataclass(frozen=True, slots=True)
class SizedEntry:
    """One sized market order. ``quantity == 0`` always carries a ``refusal``."""

    quantity: int
    entry_price: Decimal
    stop_price: Decimal
    value_inr: Decimal
    position_pct: Decimal
    risk_inr: Decimal
    #: The last cap that lowered the target — the one the quantity is actually made of.
    cap: SizeCap | None
    #: Every cap that lowered it, in the order ``04`` §6.2 applies them. A line whose turnover cap
    #: bound is **not** a skip; it is a smaller line with a note (``04`` §10.1), and this is the
    #: note.
    caps_applied: tuple[SizeCap, ...]
    refusal: SizeRefusal | None

    @property
    def placed(self) -> bool:
        return self.quantity > 0

    @property
    def turnover_capped(self) -> bool:
        return SizeCap.TURNOVER in self.caps_applied


def _pct_of(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= _ZERO:
        return _ZERO
    return (numerator / denominator * _PCT).quantize(_PAISE)


def _refuse(entry: Decimal, stop: Decimal, why: SizeRefusal) -> SizedEntry:
    return SizedEntry(
        quantity=0,
        entry_price=entry,
        stop_price=stop,
        value_inr=_ZERO,
        position_pct=_ZERO,
        risk_inr=_ZERO,
        cap=None,
        caps_applied=(),
        refusal=why,
    )


def _floor_div(budget: Decimal, price: Decimal) -> int:
    return int((budget / price).to_integral_value(rounding=ROUND_DOWN))


def size_entry(  # noqa: PLR0913 - one keyword per input the size depends on
    *,
    equity: Decimal,
    cash_available: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    turnover_avg_inr: Decimal | None,
    config: SizingConfig,
    slot_multiplier: Decimal = Decimal(1),
) -> SizedEntry:
    """The share count for one entry (``04`` §6.1-6.2).

    ``quantity = floor(target_value / entry_price)``, an integer number of shares, where
    ``target_value`` starts at ``equity / max_slots x slot_multiplier`` and is lowered, in order,
    by the per-position ceiling, the name's liquidity and the sleeve's cash. A value below
    ``min_trade_value_inr`` [₹10,000] is refused ``BELOW_MIN_TRADE_VALUE``: below it brokerage
    dominates the edge.

    ``entry_price`` is the price the caller expects to pay. The backtest passes the session's open
    **plus costs** (``04`` §5.3), because that is what the book spends; the plan passes the
    reference price it is previewing against. The stop is computed from the **open** either way and
    is passed in.

    ``slot_multiplier`` is ``04`` §6.4's first-live halving, applied **here** rather than to a
    quantity at send time: the line shown must be the line sent, and every cap and refusal below
    must see the real size. It is 1 for a ``DRY_RUN`` plan — half size is a live-money discipline,
    and a paper plan that is not the plan is not a rehearsal.
    """
    if equity <= _ZERO:
        return _refuse(entry_price, stop_price, SizeRefusal.NO_SLEEVE_CAPITAL)
    if entry_price <= _ZERO or stop_price >= entry_price or stop_price <= _ZERO:
        return _refuse(entry_price, stop_price, SizeRefusal.STOP_NOT_BELOW_ENTRY)

    target = equity / Decimal(config.max_slots) * slot_multiplier
    cap = SizeCap.SLOT
    applied: list[SizeCap] = []
    ceilings: list[tuple[SizeCap, Decimal]] = [
        (SizeCap.POSITION_PCT, equity * config.max_position_pct / _PCT)
    ]
    if turnover_avg_inr is not None:
        ceilings.append((SizeCap.TURNOVER, turnover_avg_inr * config.max_position_vs_turnover))
    ceilings.append((SizeCap.CASH, max(cash_available, _ZERO)))
    for name, ceiling in ceilings:
        if ceiling < target:
            target, cap = ceiling, name
            applied.append(name)

    quantity = _floor_div(target, entry_price)
    value = entry_price * quantity
    if value < config.min_trade_value_inr:
        return _refuse(entry_price, stop_price, SizeRefusal.BELOW_MIN_TRADE_VALUE)
    return SizedEntry(
        quantity=quantity,
        entry_price=entry_price,
        stop_price=stop_price,
        value_inr=value.quantize(_PAISE),
        position_pct=_pct_of(value, equity),
        risk_inr=((entry_price - stop_price) * quantity).quantize(_PAISE),
        cap=cap,
        caps_applied=tuple(applied),
        refusal=None,
    )


def first_live_multiplier(
    *, entries_left: int, execution_enabled: bool, config: SizingConfig
) -> Decimal:
    """``04`` §6.4: half a slot while the first ten live **entries** are being taken.

    It counts entries, not sessions. The swing book and VBT-1 count sessions because they enter
    most days; this book enters about eighteen times a year, and a five-session allowance would be
    spent by a quiet week. The counter is decremented once per **filled** entry by the session that
    filled it — never by a request, never by a plan that proposed a line nobody confirmed.

    A ``DRY_RUN`` plan is **full size**, which is why ``execution_enabled`` is an input: halving a
    paper line would rehearse a size the rules do not describe.
    """
    if execution_enabled and entries_left > 0:
        return config.risk_multiplier_first_live
    return Decimal(1)
