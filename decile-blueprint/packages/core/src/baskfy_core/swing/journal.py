"""R-multiples and the statistics progressive exposure reads (docs/swing/04 §10).

    "His win rate on breakouts is only 25-35%, but average winners are 3-5x the size of average
     losers, and a few outlier trades per year make the whole year."

So the journal is kept in R, not rupees: a trade that risked ₹2,000 and made ₹6,000 is +3R
whatever the account size was that month. Expectancy in R is the one number that says whether
the method is working *for this trader*, which is what the exposure ladder needs.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

_ZERO = Decimal(0)
_TWO_DP = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    symbol: str
    setup: str
    entry_date: dt.date
    exit_date: dt.date
    entry: Decimal
    initial_stop: Decimal
    #: Average exit across partials, share-weighted.
    exit_avg: Decimal
    quantity: int

    @property
    def r_multiple(self) -> Decimal:
        one_r = self.entry - self.initial_stop
        if one_r <= _ZERO:
            raise ValueError(f"{self.symbol}: initial stop {self.initial_stop} not below entry")
        return ((self.exit_avg - self.entry) / one_r).quantize(_TWO_DP)

    @property
    def pnl_inr(self) -> Decimal:
        return ((self.exit_avg - self.entry) * self.quantity).quantize(_TWO_DP)

    @property
    def holding_days(self) -> int:
        return (self.exit_date - self.entry_date).days


@dataclass(frozen=True, slots=True)
class JournalStats:
    trades: int
    win_rate_pct: Decimal
    avg_win_r: Decimal
    avg_loss_r: Decimal
    expectancy_r: Decimal
    profit_factor: Decimal | None
    net_r: Decimal
    largest_win_r: Decimal
    largest_loss_r: Decimal
    current_loss_streak: int


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        return _ZERO
    return (sum(values, _ZERO) / len(values)).quantize(_TWO_DP)


def summarize(trades: Sequence[ClosedTrade]) -> JournalStats:
    """The book's statistics in R. Empty input is a zero row, not an error."""
    rs = [t.r_multiple for t in trades]
    wins = [r for r in rs if r > _ZERO]
    losses = [r for r in rs if r <= _ZERO]
    gross_win = sum(wins, _ZERO)
    gross_loss = -sum(losses, _ZERO)
    streak = 0
    for r in reversed(rs):
        if r < _ZERO:
            streak += 1
        else:
            break
    count = len(rs)
    return JournalStats(
        trades=count,
        win_rate_pct=(Decimal(len(wins)) / count * 100).quantize(_TWO_DP) if count else _ZERO,
        avg_win_r=_mean(wins),
        avg_loss_r=_mean(losses),
        expectancy_r=_mean(rs),
        profit_factor=(gross_win / gross_loss).quantize(_TWO_DP) if gross_loss > _ZERO else None,
        net_r=sum(rs, _ZERO).quantize(_TWO_DP),
        largest_win_r=max(rs, default=_ZERO),
        largest_loss_r=min(rs, default=_ZERO),
        current_loss_streak=streak,
    )


def exit_average(fills: Sequence[tuple[int, Decimal]]) -> Decimal:
    """Share-weighted average of ``(quantity, price)`` exit fills."""
    total = sum(q for q, _ in fills)
    if total <= 0:
        raise ValueError("no exit quantity")
    return (sum((Decimal(q) * p for q, p in fills), _ZERO) / total).quantize(_TWO_DP)
