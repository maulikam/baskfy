"""Risk per trade → share count (docs/swing/04 §5).

    "The single most important lesson from his blow-ups was that risk per trade, not conviction,
     decides survival."

Money is ``Decimal`` (house rule 9). The share count is the floor of the risk-derived quantity,
then the smallest of four caps, and the cap that bound is *named* in the result so the plan can
say "120 shares, capped by the 20% position limit" rather than presenting a number as if it were
the risk-derived one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from baskfy_core.swing.config import SizingConfig

_PCT = Decimal(100)
_ZERO = Decimal(0)


class SizeCap(StrEnum):
    """Which limit decided the quantity."""

    RISK = "RISK"
    POSITION_PCT = "POSITION_PCT"
    TURNOVER = "TURNOVER"
    CASH = "CASH"


class SizeRefusal(StrEnum):
    """Why no position at all."""

    STOP_NOT_BELOW_ENTRY = "STOP_NOT_BELOW_ENTRY"
    STOP_TOO_WIDE = "STOP_TOO_WIDE"
    BELOW_MIN_TRADE_VALUE = "BELOW_MIN_TRADE_VALUE"
    NO_EQUITY = "NO_EQUITY"


@dataclass(frozen=True, slots=True)
class SizedPosition:
    """One sized entry. ``quantity == 0`` always carries a ``refusal``."""

    quantity: int
    entry: Decimal
    stop: Decimal
    risk_inr: Decimal
    position_value: Decimal
    position_pct: Decimal
    stop_distance_pct: Decimal
    cap: SizeCap | None
    refusal: SizeRefusal | None

    @property
    def r_value(self) -> Decimal:
        """One R in rupees: what a full stop-out loses."""
        return self.risk_inr


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= _ZERO:
        return _ZERO
    return (numerator / denominator * _PCT).quantize(Decimal("0.01"))


def _refuse(entry: Decimal, stop: Decimal, why: SizeRefusal) -> SizedPosition:
    return SizedPosition(
        quantity=0,
        entry=entry,
        stop=stop,
        risk_inr=_ZERO,
        position_value=_ZERO,
        position_pct=_ZERO,
        stop_distance_pct=_pct(entry - stop, entry) if entry > _ZERO else _ZERO,
        cap=None,
        refusal=why,
    )


def size_position(  # noqa: PLR0913 - one keyword per input the size depends on
    *,
    equity: Decimal,
    cash_available: Decimal,
    entry: Decimal,
    stop: Decimal,
    avg_turnover_inr: Decimal | None,
    config: SizingConfig,
    max_stop_distance_pct: Decimal,
) -> SizedPosition:
    """The share count for one entry.

    ``quantity = floor(equity x risk% / (entry - stop))``, then the minimum of that and the
    position-percent cap, the turnover cap (a position no larger than ``max_position_vs_turnover``
    of the name's average daily turnover, the desk's own ``MAX_POS_VS_DAY_VALUE`` rule) and the
    cash on hand. ``max_stop_distance_pct`` is the widest stop the method tolerates: a stop 14%
    away is not a swing-trade stop, and shrinking the size to fit it would only disguise that.
    """
    if equity <= _ZERO:
        return _refuse(entry, stop, SizeRefusal.NO_EQUITY)
    if stop >= entry or entry <= _ZERO:
        return _refuse(entry, stop, SizeRefusal.STOP_NOT_BELOW_ENTRY)
    distance = entry - stop
    distance_pct = _pct(distance, entry)
    if distance_pct > max_stop_distance_pct:
        return _refuse(entry, stop, SizeRefusal.STOP_TOO_WIDE)

    risk_budget = equity * Decimal(str(config.risk_per_trade_pct)) / _PCT
    by_risk = int((risk_budget / distance).to_integral_value(rounding=ROUND_DOWN))
    by_position = int(
        (equity * Decimal(str(config.max_position_pct)) / _PCT / entry).to_integral_value(
            rounding=ROUND_DOWN
        )
    )
    by_cash = int((cash_available / entry).to_integral_value(rounding=ROUND_DOWN))
    caps: list[tuple[int, SizeCap]] = [
        (by_risk, SizeCap.RISK),
        (by_position, SizeCap.POSITION_PCT),
        (by_cash, SizeCap.CASH),
    ]
    if avg_turnover_inr is not None and avg_turnover_inr > _ZERO:
        by_turnover = int(
            (
                avg_turnover_inr * Decimal(str(config.max_position_vs_turnover)) / entry
            ).to_integral_value(rounding=ROUND_DOWN)
        )
        caps.append((by_turnover, SizeCap.TURNOVER))

    quantity, cap = min(caps, key=lambda pair: pair[0])
    value = entry * quantity
    if quantity <= 0 or value < config.min_trade_value_inr:
        return _refuse(entry, stop, SizeRefusal.BELOW_MIN_TRADE_VALUE)
    return SizedPosition(
        quantity=quantity,
        entry=entry,
        stop=stop,
        risk_inr=(distance * quantity).quantize(Decimal("0.01")),
        position_value=value.quantize(Decimal("0.01")),
        position_pct=_pct(value, equity),
        stop_distance_pct=distance_pct,
        cap=cap,
        refusal=None,
    )


def implied_risk_pct(position: SizedPosition, equity: Decimal) -> Decimal:
    """What fraction of equity this position actually risks — below the budget when capped."""
    if equity <= _ZERO:
        return _ZERO
    return _pct(position.risk_inr, equity)


def r_multiple(*, entry: Decimal, stop: Decimal, exit_price: Decimal) -> Decimal:
    """How many initial risks the exit realised. ``-1`` is a full stop-out."""
    distance = entry - stop
    if distance <= _ZERO:
        raise ValueError(f"stop {stop} must be below entry {entry}")
    return ((exit_price - entry) / distance).quantize(Decimal("0.01"))
