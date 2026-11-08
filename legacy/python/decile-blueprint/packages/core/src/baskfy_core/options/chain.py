"""The chain: quotes, ATM, strike step, the parity forward, liquidity, staleness (``04`` §2).

A quote here is what the collector stores per contract per minute (``op_chain_snapshot``): top of
book, five depth levels a side as Kite gives them, OI and the quote's timestamp. Every rule reads
the **conservative side** — a buy at the ask, a sell at the bid — and every depth test reads the
side the order would *take* (a buy lifts the asks, a sell hits the bids); getting that backwards
is the frozen lab's "filling at mid wearing a disguise".

Ported by re-implementation from the frozen lab's ``strategies/options.py`` (``OptionQuote``,
``_liquid``, ``top_depth_lots``; PACK.2) with every threshold moved into ``ChainConfig``.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from baskfy_core.options.config import ChainConfig, OiUnit, OptionType, Side
from baskfy_core.options.greeks import Greeks, solve, year_fraction

_TWO = Decimal(2)
_HUNDRED = Decimal(100)
_MIN_STRIKES = 2


@dataclass(frozen=True, slots=True)
class Level:
    """One depth level: a price and the quantity resting there (units, not lots)."""

    price: Decimal
    quantity: int


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """One contract's quote at one instant (``03`` §5)."""

    instrument_token: int
    expiry: dt.date
    strike: Decimal
    option_type: OptionType
    bid: Decimal | None
    ask: Decimal | None
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    oi: int
    ts: dt.datetime
    last: Decimal | None = None
    volume: int = 0

    @property
    def mid(self) -> Decimal | None:
        """``(bid + ask) / 2`` — ``None`` unless both sides are quoted and positive."""
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0:
            return None
        return (self.bid + self.ask) / _TWO

    @property
    def spread_pct(self) -> Decimal | None:
        """``(ask - bid) / mid * 100`` (``04`` §2.4)."""
        mid = self.mid
        if mid is None or self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / mid * _HUNDRED


def atm(spot: Decimal, step: Decimal) -> Decimal:
    """The strike nearest ``spot`` on a ``step`` grid; a tie goes to the **lower** strike."""
    if step <= 0:
        raise ValueError("strike step must be positive")
    below = (spot // step) * step
    above = below + step
    return below if spot - below <= above - spot else above


def strike_step(strikes: Iterable[Decimal], near: Decimal, window: int) -> Decimal | None:
    """The modal difference between adjacent listed strikes near ``near`` (``04`` §2.1).

    Read, not assumed: the ``2 * window + 1`` listed strikes nearest ``near`` are taken in order
    and the most common adjacent gap wins (a tie goes to the smaller gap, the finer grid). ``None``
    when fewer than two strikes are listed.
    """
    distinct = sorted(set(strikes))
    if len(distinct) < _MIN_STRIKES:
        return None
    nearest = sorted(distinct, key=lambda k: (abs(k - near), k))[: 2 * window + 1]
    ordered = sorted(nearest)
    gaps = Counter(b - a for a, b in pairwise(ordered))
    if not gaps:
        return None
    top = max(gaps.values())
    return min(gap for gap, count in gaps.items() if count == top)


def snapshot_strikes(
    listed: Iterable[Decimal], spot: Decimal, step: Decimal, count: int
) -> tuple[Decimal, ...]:
    """The listed strikes within ``count`` steps either side of ATM (``04`` §2.1)."""
    centre = atm(spot, step)
    low, high = centre - step * count, centre + step * count
    return tuple(sorted(k for k in set(listed) if low <= k <= high))


def parity_forward(
    quotes: Sequence[OptionQuote], spot: Decimal, step: Decimal, years: float, rate: float
) -> Decimal | None:
    """``F = K + (C_mid - P_mid) * e^{rT}`` from the ATM pair (``04`` §2.2).

    Falls back pair by pair, nearest strike to ATM first, until a strike has both mids; ``None``
    if none does. All ``quotes`` must be one expiry — mixing expiries is a caller bug and raises.
    """
    if len({q.expiry for q in quotes}) > 1:
        raise ValueError("parity_forward takes one expiry's quotes")
    calls = {q.strike: q.mid for q in quotes if q.option_type is OptionType.CE}
    puts = {q.strike: q.mid for q in quotes if q.option_type is OptionType.PE}
    centre = atm(spot, step)
    growth = (Decimal(repr(rate)) * Decimal(repr(years))).exp()
    for strike in sorted(set(calls) & set(puts), key=lambda k: (abs(k - centre), k)):
        call_mid, put_mid = calls[strike], puts[strike]
        if call_mid is not None and put_mid is not None:
            return strike + (call_mid - put_mid) * growth
    return None


def quote_greeks(  # noqa: PLR0913 - a quote, its forward, the clock and the config
    quote: OptionQuote,
    forward: Decimal,
    *,
    now: dt.datetime,
    tick: Decimal,
    config: ChainConfig,
    settle: dt.time,
) -> Greeks | None:
    """IV and greeks for ``quote`` on ``forward`` at ``now`` — or ``None`` where ``04`` §2.3
    refuses (no mid, below ``min_premium``, below intrinsic + a tick, no convergence)."""
    mid = quote.mid
    if mid is None:
        return None
    return solve(
        float(mid),
        float(forward),
        float(quote.strike),
        year_fraction(now, quote.expiry, settle),
        config.rate,
        quote.option_type,
        min_premium=float(config.min_premium),
        tick=float(tick),
        lower=config.iv_lower,
        upper=config.iv_upper,
        tolerance=config.iv_tolerance,
    )


class Illiquid(StrEnum):
    """Why a contract failed ``is_liquid`` — every reason, not only the first."""

    NO_QUOTE = "NO_QUOTE"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    DEPTH_TOO_THIN = "DEPTH_TOO_THIN"
    OI_TOO_LOW = "OI_TOO_LOW"


@dataclass(frozen=True, slots=True)
class Liquidity:
    ok: bool
    reasons: tuple[Illiquid, ...]


def depth_available(quote: OptionQuote, side: Side, levels: int) -> int:
    """Units resting on the side an order takes, within the first ``levels`` levels."""
    ladder = quote.asks if side is Side.BUY else quote.bids
    return sum(level.quantity for level in ladder[:levels] if level.quantity > 0)


def oi_in_units(quote: OptionQuote, lot_size: int, unit: OiUnit) -> int:
    """Kite's ``oi`` in units whatever it counts (the conversion follows OP0's read, OP1.3)."""
    return quote.oi * lot_size if unit is OiUnit.LOTS else quote.oi


def is_liquid(  # noqa: PLR0913 - each is an input 04 §2.4 names
    quote: OptionQuote,
    qty: int,
    side: Side,
    lot_size: int,
    config: ChainConfig,
    *,
    wing: bool = False,
) -> Liquidity:
    """``04`` §2.4: spread, depth on the side taken, and OI — or, for an O1 **wing**, depth only.

    ``qty`` is units (lots * lot size). A contract failing any test is never a leg.
    """
    reasons: list[Illiquid] = []
    spread = quote.spread_pct
    if spread is None:
        reasons.append(Illiquid.NO_QUOTE)
    elif not wing and spread > config.max_spread_pct:
        reasons.append(Illiquid.SPREAD_TOO_WIDE)
    if depth_available(quote, side, config.depth_levels) < qty:
        reasons.append(Illiquid.DEPTH_TOO_THIN)
    if not wing and oi_in_units(quote, lot_size, config.oi_unit) < config.min_oi_lots * lot_size:
        reasons.append(Illiquid.OI_TOO_LOW)
    return Liquidity(ok=not reasons, reasons=tuple(reasons))


def is_stale_quote(quote_ts: dt.datetime, now: dt.datetime, config: ChainConfig) -> bool:
    """Older than ``stale_quote_seconds`` (``04`` §2.5)."""
    return (now - quote_ts).total_seconds() > config.stale_quote_seconds


def is_stale_index(last_tick: dt.datetime | None, now: dt.datetime, config: ChainConfig) -> bool:
    """No index tick for ``stale_index_seconds`` — or none at all (``04`` §2.5)."""
    if last_tick is None:
        return True
    return (now - last_tick).total_seconds() > config.stale_index_seconds


def is_stale_scan(snapshot_ts: dt.datetime, now: dt.datetime, config: ChainConfig) -> bool:
    """A scan computed from a snapshot older than two minutes is ``stale=true`` (``04`` §2.5)."""
    return (now - snapshot_ts).total_seconds() > config.stale_scan_seconds
