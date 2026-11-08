"""F1: the index monthly iron condor's strikes, credit, max loss, liquidity and sequences
(``04`` §2), and its profit-take and loss-close predicates (``04`` §1).

Strikes (``sd = F·sigma·√T``, ``T`` = calendar days ÷ 365):

========== ================================================ =====
leg        strike                                           qty
========== ================================================ =====
long call  the smallest listed strike ≥ ``F + (k + w)·sd``  +q
short call the smallest listed strike ≥ ``F + k·sd``        -q
short put  the largest listed strike ≤ ``F - k·sd``         -q
long put   the largest listed strike ≤ ``F - (k + w)·sd``   +q
========== ================================================ =====

A wing equal to its short (or no listed strike) is ``REJECTED_STRUCTURE``. The credit
``C = Σ short mids - Σ long mids`` must be positive; the max loss per unit is
``max(call width, put width) - C``. **Entry** goes long put → long call → short put → short call;
**exit** goes short call → short put → long call → long put, so the book is covered at every
prefix (``covered.py`` proves it).

The cost to close is valued with each vertical clamped to ``[0, width]`` (``RESEARCH.md``
method: marks from non-simultaneous closes breach the no-arbitrage bound), so the loss close
**never** reads a loss beyond the wings' max loss.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from baskfy_core.fno.config import F1Config, PlanState
from baskfy_core.options.config import OptionType

_HUNDRED = Decimal(100)
_DAYS_PER_YEAR = 365


class LegRole(StrEnum):
    LONG_PUT = "LONG_PUT"
    LONG_CALL = "LONG_CALL"
    SHORT_PUT = "SHORT_PUT"
    SHORT_CALL = "SHORT_CALL"


#: ``fo_leg.entry_seq`` (``04`` §2): longs first.
ENTRY_SEQUENCE: tuple[LegRole, ...] = (
    LegRole.LONG_PUT,
    LegRole.LONG_CALL,
    LegRole.SHORT_PUT,
    LegRole.SHORT_CALL,
)
#: The exit order (``04`` §2): shorts first.
EXIT_SEQUENCE: tuple[LegRole, ...] = (
    LegRole.SHORT_CALL,
    LegRole.SHORT_PUT,
    LegRole.LONG_CALL,
    LegRole.LONG_PUT,
)


def option_type_of(role: LegRole) -> OptionType:
    return OptionType.CE if role in (LegRole.LONG_CALL, LegRole.SHORT_CALL) else OptionType.PE


def sign_of(role: LegRole) -> int:
    """+1 for a long leg, -1 for a short leg."""
    return 1 if role in (LegRole.LONG_CALL, LegRole.LONG_PUT) else -1


@dataclass(frozen=True, slots=True)
class CondorStrikes:
    long_call: Decimal
    short_call: Decimal
    short_put: Decimal
    long_put: Decimal

    def strike(self, role: LegRole) -> Decimal:
        return {
            LegRole.LONG_CALL: self.long_call,
            LegRole.SHORT_CALL: self.short_call,
            LegRole.SHORT_PUT: self.short_put,
            LegRole.LONG_PUT: self.long_put,
        }[role]

    @property
    def call_width(self) -> Decimal:
        return self.long_call - self.short_call

    @property
    def put_width(self) -> Decimal:
        return self.short_put - self.long_put

    @property
    def max_width(self) -> Decimal:
        return max(self.call_width, self.put_width)


@dataclass(frozen=True, slots=True)
class StrikeChoice:
    """The strikes, or the reason there are none (``state`` is then ``REJECTED_STRUCTURE``)."""

    strikes: CondorStrikes | None
    sd: Decimal
    reasons: tuple[str, ...]

    @property
    def state(self) -> PlanState | None:
        return None if self.strikes is not None else PlanState.REJECTED_STRUCTURE


def one_sd(forward: Decimal, iv: float, days_to_expiry: int) -> Decimal:
    """``sd = F · sigma · √T`` with ``T`` in calendar years (``04`` §2)."""
    if days_to_expiry <= 0 or iv <= 0:
        raise ValueError("sd needs a positive IV and days to expiry")
    root = Decimal(repr(iv * math.sqrt(days_to_expiry / _DAYS_PER_YEAR)))
    return forward * root


def _smallest_at_or_above(listed: Sequence[Decimal], target: Decimal) -> Decimal | None:
    above = [k for k in listed if k >= target]
    return min(above) if above else None


def _largest_at_or_below(listed: Sequence[Decimal], target: Decimal) -> Decimal | None:
    below = [k for k in listed if k <= target]
    return max(below) if below else None


def select_strikes(  # noqa: PLR0913 - the inputs 04 §2 names, by keyword
    *,
    forward: Decimal,
    iv: float,
    days_to_expiry: int,
    call_strikes: Sequence[Decimal],
    put_strikes: Sequence[Decimal],
    config: F1Config,
) -> StrikeChoice:
    """``04`` §2's table on the listed strikes of each type."""
    sd = one_sd(forward, iv, days_to_expiry)
    k, w = config.short_sigma, config.wing_sigma
    long_call = _smallest_at_or_above(call_strikes, forward + (k + w) * sd)
    short_call = _smallest_at_or_above(call_strikes, forward + k * sd)
    short_put = _largest_at_or_below(put_strikes, forward - k * sd)
    long_put = _largest_at_or_below(put_strikes, forward - (k + w) * sd)
    reasons: list[str] = []
    named = (
        ("long call", long_call),
        ("short call", short_call),
        ("short put", short_put),
        ("long put", long_put),
    )
    reasons.extend(f"no listed strike for the {name}" for name, value in named if value is None)
    if long_call is not None and short_call is not None and long_call == short_call:
        reasons.append(f"the call wing equals the short call ({short_call})")
    if long_put is not None and short_put is not None and long_put == short_put:
        reasons.append(f"the put wing equals the short put ({short_put})")
    if reasons or long_call is None or short_call is None or short_put is None or long_put is None:
        return StrikeChoice(None, sd, tuple(reasons))
    return StrikeChoice(CondorStrikes(long_call, short_call, short_put, long_put), sd, ())


def credit(mids: Mapping[LegRole, Decimal]) -> Decimal:
    """``C = Σ short mids - Σ long mids`` per unit (``04`` §2)."""
    return sum((-sign_of(role) * mids[role] for role in LegRole), Decimal(0))


def max_loss_per_unit(strikes: CondorStrikes, credit_per_unit: Decimal) -> Decimal:
    """``max(call width, put width) - C`` (``04`` §2)."""
    return strikes.max_width - credit_per_unit


@dataclass(frozen=True, slots=True)
class Structure:
    """A priced condor, or its rejection (``state`` set, ``reasons`` in words)."""

    strikes: CondorStrikes | None
    credit: Decimal
    max_loss_per_unit: Decimal
    state: PlanState | None
    reasons: tuple[str, ...]


def price_structure(strikes: CondorStrikes, mids: Mapping[LegRole, Decimal]) -> Structure:
    """Credit and max loss; a non-positive credit is ``REJECTED_STRUCTURE``."""
    c = credit(mids)
    loss = max_loss_per_unit(strikes, c)
    if c <= 0:
        return Structure(
            strikes, c, loss, PlanState.REJECTED_STRUCTURE, (f"credit {c} is not positive",)
        )
    if loss <= 0:
        return Structure(
            strikes, c, loss, PlanState.REJECTED_STRUCTURE, (f"max loss {loss} is not positive",)
        )
    return Structure(strikes, c, loss, None, ())


@dataclass(frozen=True, slots=True)
class LegQuote:
    """A live quote for one leg: best bid, best ask and open interest in lots."""

    bid: Decimal | None
    ask: Decimal | None
    oi_lots: int

    @property
    def two_sided(self) -> bool:
        return self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0

    @property
    def mid(self) -> Decimal | None:
        if self.bid is None or self.ask is None or not self.two_sided:
            return None
        return (self.bid + self.ask) / 2


def liquidity_refusals(
    strikes: CondorStrikes, quotes: Mapping[LegRole, LegQuote], config: F1Config
) -> tuple[str, ...]:
    """``04`` §2 liquidity, **by name**: each short has OI ≥ ``f1_min_short_oi_lots`` and a
    spread ≤ ``f1_max_spread_pct`` of its mid; each wing has a two-sided quote. Empty is OK;
    anything else is ``REJECTED_LIQUIDITY``."""
    found: list[str] = []
    for role in ENTRY_SEQUENCE:
        name = f"{role.value.lower().replace('_', ' ')} {strikes.strike(role)}"
        quote = quotes.get(role)
        if quote is None or not quote.two_sided:
            found.append(f"{name}: no two-sided quote")
            continue
        if sign_of(role) > 0:
            continue
        mid = quote.mid
        if quote.oi_lots < config.min_short_oi_lots:
            found.append(f"{name}: OI {quote.oi_lots} lots < {config.min_short_oi_lots}")
        if mid is not None and quote.bid is not None and quote.ask is not None:
            spread_pct = (quote.ask - quote.bid) / mid * _HUNDRED
            if spread_pct > config.max_spread_pct:
                found.append(
                    f"{name}: spread {spread_pct:.2f} % of mid > {config.max_spread_pct} %"
                )
    return tuple(found)


def cost_to_close(strikes: CondorStrikes, marks: Mapping[LegRole, Decimal]) -> Decimal:
    """What buying the structure back costs per unit, each vertical clamped to
    ``[0, width]`` — so it is never more than the wider width (the wings' max loss + C)."""
    calls = marks[LegRole.SHORT_CALL] - marks[LegRole.LONG_CALL]
    puts = marks[LegRole.SHORT_PUT] - marks[LegRole.LONG_PUT]
    zero = Decimal(0)
    return min(max(calls, zero), strikes.call_width) + min(max(puts, zero), strikes.put_width)


def profit_take_level(entry_credit: Decimal, config: F1Config) -> Decimal:
    """``(1 - pct) x C``: close when the cost to close is at or below it (``04`` §1)."""
    return (Decimal(1) - config.profit_take_pct / _HUNDRED) * entry_credit


def loss_close_level(entry_credit: Decimal, strikes: CondorStrikes, config: F1Config) -> Decimal:
    """``(1 + mult) x C``, never above the wider width: the loss close never widens past the
    wings' max loss (``04`` §1)."""
    return min((Decimal(1) + config.loss_close_mult) * entry_credit, strikes.max_width)


def profit_take_hit(close_cost: Decimal, entry_credit: Decimal, config: F1Config) -> bool:
    return close_cost <= profit_take_level(entry_credit, config)


def loss_close_hit(
    close_cost: Decimal, entry_credit: Decimal, strikes: CondorStrikes, config: F1Config
) -> bool:
    return close_cost >= loss_close_level(entry_credit, strikes, config)
