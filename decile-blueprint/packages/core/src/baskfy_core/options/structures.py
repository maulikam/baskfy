"""What every sleeve's candidate is made of: a priced chain, legs, and the round trip (OP4).

A scan's candidate and a plan must be the **same computation** (``04`` §10): the scan shows the
structure the plan builder would build from the same minute's quotes. So the pieces both need
live here, once:

* :class:`ChainView` — one expiry's quotes at one instant with the parity forward (``04`` §2.2) and
  Black-76 greeks per contract (``04`` §2.3), computed by ``chain``'s own functions. The stored
  greeks of ``op_chain_snapshot`` are the collector's record for Tier 3; a candidate recomputes
  them here from the same quotes, as the desk's plan builder will from its own (OP4.4).
* :class:`CandidateLeg` / :class:`Candidate` — the legs with their quotes, limit prices
  (``04`` §8.2 attempt 1: the quoted side improved by ``limit_improve_ticks``), and the priced
  structure: lots at the budget, risk per lot, max loss, the round trip and the cost share, or the
  one rejection code that stopped it.
* :func:`round_trip` — ``04`` §6.1's charges over the orders a plan implies: each leg entered at
  its attempt-1 limit and exited at the opposite side's attempt-1 limit on the same quotes (a
  long sold at ``bid - tick``, a short bought back at ``ask + tick``). Pricing both crossings at
  the conservative side is §6.2's slippage; OP6-OP8 pin these figures to the paisa (OP4.5).

``to_json`` renders a candidate for ``op_scan.candidates`` (JSONB): every price and rupee a string
at its stored precision (house rules 8 and 9 — never a float), greeks rounded to six places.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from baskfy_core.options.chain import OptionQuote, atm, parity_forward, quote_greeks, strike_step
from baskfy_core.options.config import (
    ChainConfig,
    CostRates,
    ExecutionConfig,
    Mode,
    OptionsCeilings,
    OptionType,
    Side,
    SizingConfig,
    SizingMode,
)
from baskfy_core.options.costs import CostFill, charges, cost_test, paise
from baskfy_core.options.execution import LegRole, next_attempt
from baskfy_core.options.greeks import Greeks, year_fraction
from baskfy_core.options.sizing import Sizing, size

_CENT = Decimal("0.01")
_MICRO = Decimal("0.000001")


class Structure(StrEnum):
    """``03`` §9's ``op_plan.structure``."""

    IRON_CONDOR = "IRON_CONDOR"
    LONG_OPTION = "LONG_OPTION"
    DEBIT_SPREAD = "DEBIT_SPREAD"


class Direction(StrEnum):
    UP = "UP"
    DOWN = "DOWN"


class Rejection(StrEnum):
    """Why a candidate is not a plan. ``04`` §3-§7's codes, plus OP4's two data refusals."""

    REJECTED_NO_SHORT_CALL = "REJECTED_NO_SHORT_CALL"
    REJECTED_NO_SHORT_PUT = "REJECTED_NO_SHORT_PUT"
    REJECTED_NO_WING = "REJECTED_NO_WING"
    REJECTED_CREDIT = "REJECTED_CREDIT"
    REJECTED_DELTA = "REJECTED_DELTA"
    REJECTED_ILLIQUID = "REJECTED_ILLIQUID"
    REJECTED_DEBIT = "REJECTED_DEBIT"
    REJECTED_COST = "REJECTED_COST"
    REJECTED_PREMIUM_CAP = "REJECTED_PREMIUM_CAP"
    REJECTED_NO_LOT_SIZE = "REJECTED_NO_LOT_SIZE"
    REJECTED_NO_SLEEVE_CAPITAL = "REJECTED_NO_SLEEVE_CAPITAL"
    REJECTED_BUDGET = "REJECTED_BUDGET"
    REJECTED_SLOT_TAKEN = "REJECTED_SLOT_TAKEN"
    REJECTED_PAUSED = "REJECTED_PAUSED"
    #: OP4.6: the contract the rule names is not in the minute's quotes (or has no tick size).
    REJECTED_NO_CONTRACT = "REJECTED_NO_CONTRACT"
    #: OP4.6: no quotes at all for the expiry the sleeve trades this minute.
    REJECTED_NO_CHAIN = "REJECTED_NO_CHAIN"


@dataclass(frozen=True, slots=True)
class PricedQuote:
    """A quote and its greeks on the expiry's forward — ``None`` where ``04`` §2.3 refuses."""

    quote: OptionQuote
    greeks: Greeks | None

    @property
    def strike(self) -> Decimal:
        return self.quote.strike

    @property
    def option_type(self) -> OptionType:
        return self.quote.option_type

    @property
    def delta(self) -> float | None:
        return None if self.greeks is None else self.greeks.delta


@dataclass(frozen=True, slots=True)
class ChainView:
    """One expiry's chain at one instant, priced (``04`` §2.1-§2.3)."""

    expiry: dt.date
    now: dt.datetime
    spot: Decimal
    step: Decimal
    atm: Decimal
    forward: Decimal | None
    tick: Decimal
    quotes: tuple[PricedQuote, ...]

    def get(self, strike: Decimal, option_type: OptionType) -> PricedQuote | None:
        return next(
            (q for q in self.quotes if q.strike == strike and q.option_type is option_type),
            None,
        )

    def of_type(self, option_type: OptionType) -> tuple[PricedQuote, ...]:
        return tuple(q for q in self.quotes if q.option_type is option_type)


def chain_view(  # noqa: PLR0913 - the quotes, the spot, the clock, the tick and the config
    quotes: Iterable[OptionQuote],
    *,
    expiry: dt.date,
    spot: Decimal,
    now: dt.datetime,
    tick: Decimal,
    config: ChainConfig,
    settle: dt.time,
    listed_strikes: Iterable[Decimal] | None = None,
) -> ChainView | None:
    """Price ``expiry``'s quotes at ``now``: the strike step (read from the master's listed
    strikes when given, else from the quotes), ATM, the parity forward, and greeks per contract.

    ``None`` when there is no quote for the expiry or no step can be read — a sleeve then rejects
    ``REJECTED_NO_CHAIN`` rather than guess.
    """
    same = tuple(q for q in quotes if q.expiry == expiry)
    if not same:
        return None
    strikes = tuple(listed_strikes) if listed_strikes is not None else tuple(q.strike for q in same)
    step = strike_step(strikes, spot, config.snapshot_strikes)
    if step is None:
        return None
    years = year_fraction(now, expiry, settle)
    forward = parity_forward(same, spot, step, years, config.rate) if years > 0 else None
    priced = tuple(
        PricedQuote(
            q,
            quote_greeks(q, forward, now=now, tick=tick, config=config, settle=settle)
            if forward is not None
            else None,
        )
        for q in sorted(same, key=lambda q: (q.strike, q.option_type.value))
    )
    return ChainView(
        expiry=expiry,
        now=now,
        spot=spot,
        step=step,
        atm=atm(spot, step),
        forward=forward,
        tick=tick,
        quotes=priced,
    )


@dataclass(frozen=True, slots=True)
class CandidateLeg:
    """One leg as a plan would send it: role, contract, quote, greeks and attempt-1 limit."""

    role: LegRole
    strike: Decimal
    option_type: OptionType
    expiry: dt.date
    instrument_token: int
    bid: Decimal | None
    ask: Decimal | None
    iv: float | None
    delta: float | None
    limit_price: Decimal

    @property
    def entry_side(self) -> Side:
        return self.role.entry_side


def leg(role: LegRole, priced: PricedQuote, tick: Decimal, config: ExecutionConfig) -> CandidateLeg:
    """A leg at ``04`` §8.2's attempt-1 limit (buy ``ask + tick``, sell ``bid - tick``)."""
    q = priced.quote
    if q.bid is None or q.ask is None:
        raise ValueError("a leg needs a two-sided quote")
    attempt = next_attempt(
        1, role.entry_side, q.bid, q.ask, tick, closing_reduces_risk=False, config=config
    )
    if attempt is None:
        raise ValueError("attempt 1 always exists")
    return CandidateLeg(
        role=role,
        strike=q.strike,
        option_type=q.option_type,
        expiry=q.expiry,
        instrument_token=q.instrument_token,
        bid=q.bid,
        ask=q.ask,
        iv=None if priced.greeks is None else priced.greeks.iv,
        delta=priced.delta,
        limit_price=attempt.price,
    )


def exit_price(candidate_leg: CandidateLeg, tick: Decimal, config: ExecutionConfig) -> Decimal:
    """The same quotes' attempt-1 close: a long sold at ``bid - tick``, a short bought at
    ``ask + tick``."""
    if candidate_leg.bid is None or candidate_leg.ask is None:
        raise ValueError("a leg needs a two-sided quote")
    attempt = next_attempt(
        1,
        candidate_leg.role.exit_side,
        candidate_leg.bid,
        candidate_leg.ask,
        tick,
        closing_reduces_risk=False,
        config=config,
    )
    if attempt is None:
        raise ValueError("attempt 1 always exists")
    return attempt.price


def round_trip(
    legs: Sequence[CandidateLeg],
    quantity: int,
    tick: Decimal,
    *,
    rates: CostRates,
    execution: ExecutionConfig,
) -> Decimal:
    """``04`` §6.1 over the plan's orders: every leg in at its limit and out at the same quotes'
    opposite-side limit — O1 eight orders, O2 two, O3 four (OP4.5)."""
    fills = [CostFill(lg.entry_side, lg.limit_price, quantity) for lg in legs]
    fills += [CostFill(lg.role.exit_side, exit_price(lg, tick, execution), quantity) for lg in legs]
    return charges(fills, rates).total


@dataclass(frozen=True, slots=True)
class Candidate:
    """A sleeve's priced structure — or the one code that stopped it, with what was known.

    ``points`` is the structure's premium per unit: the condor's credit, the long's planned entry
    ``E``, the spread's debit. ``lots == 0`` iff ``rejection`` is set by the sizing or earlier.
    """

    structure: Structure
    expiry: dt.date
    direction: Direction | None
    legs: tuple[CandidateLeg, ...]
    points: Decimal | None
    lot_size: int | None
    lots: int
    sizing_mode: SizingMode | None
    risk_per_lot_inr: Decimal | None
    r_inr: Decimal | None
    max_loss_inr: Decimal | None
    round_trip_inr: Decimal | None
    expected_gain_inr: Decimal | None
    cost_share: Decimal | None
    rejection: Rejection | None
    message: str
    half_size: bool = False
    warnings: tuple[str, ...] = ()
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def viable(self) -> bool:
        return self.rejection is None


def rejected(  # noqa: PLR0913 - what was known when the rule refused
    structure: Structure,
    expiry: dt.date,
    code: Rejection,
    message: str,
    *,
    direction: Direction | None = None,
    legs: tuple[CandidateLeg, ...] = (),
    points: Decimal | None = None,
    lot_size: int | None = None,
    extra: dict[str, str] | None = None,
) -> Candidate:
    return Candidate(
        structure=structure,
        expiry=expiry,
        direction=direction,
        legs=legs,
        points=points,
        lot_size=lot_size,
        lots=0,
        sizing_mode=None,
        risk_per_lot_inr=None,
        r_inr=None,
        max_loss_inr=None,
        round_trip_inr=None,
        expected_gain_inr=None,
        cost_share=None,
        rejection=code,
        message=message,
        extra=dict(extra or {}),
    )


@dataclass(frozen=True, slots=True)
class SleeveBook:
    """What sizing needs to know about the sleeve (``04`` §7): its mode, its capital and limits
    (``op_sleeve_config``), and how many real journal rows it has (§7.5). Capital defaults to
    ₹0, so a paper candidate is one lot (PACK.6)."""

    mode: Mode = Mode.PAPER
    sleeve_capital_inr: Decimal = Decimal(0)
    risk_per_trade_pct: Decimal | None = None
    max_lots: int | None = None
    real_journal_rows: int = 0


def size_for(  # noqa: PLR0913 - the book, the sleeve's defaults, the structure's risk, the rules
    book: SleeveBook,
    *,
    default_risk_pct: Decimal,
    default_max_lots: int,
    risk_per_lot_inr: Decimal,
    lot_size: int | None,
    config: SizingConfig,
    ceilings: OptionsCeilings,
) -> Sizing:
    """``sizing.size`` with the sleeve's own settings, falling back to ``04``'s defaults."""
    return size(
        mode=book.mode,
        sleeve_capital_inr=book.sleeve_capital_inr,
        risk_per_trade_pct=book.risk_per_trade_pct
        if book.risk_per_trade_pct is not None
        else default_risk_pct,
        max_lots=book.max_lots if book.max_lots is not None else default_max_lots,
        risk_per_lot_inr=risk_per_lot_inr,
        lot_size=lot_size,
        real_journal_rows=book.real_journal_rows,
        config=config,
        ceilings=ceilings,
    )


def cost_gate(
    round_trip_inr: Decimal, expected_gain_inr: Decimal, share_max: Decimal
) -> tuple[Decimal | None, bool]:
    """``04`` §6.4: the share (``None`` when the gain cannot pay) and whether it passes."""
    verdict = cost_test(round_trip_inr, expected_gain_inr, share_max)
    return verdict.cost_share, verdict.cost_share is not None and verdict.cost_share <= share_max


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(paise(value))


def _price(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _greek(value: float | None) -> str | None:
    if value is None:
        return None
    return str(Decimal(repr(value)).quantize(_MICRO, rounding=ROUND_HALF_UP))


def _share(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(Decimal("0.0001"), ROUND_HALF_UP))


def leg_json(candidate_leg: CandidateLeg) -> dict[str, object]:
    return {
        "role": candidate_leg.role.value,
        "strike": _price(candidate_leg.strike),
        "option_type": candidate_leg.option_type.value,
        "expiry": candidate_leg.expiry.isoformat(),
        "instrument_token": candidate_leg.instrument_token,
        "side": candidate_leg.entry_side.value,
        "bid": _price(candidate_leg.bid),
        "ask": _price(candidate_leg.ask),
        "iv": _greek(candidate_leg.iv),
        "delta": _greek(candidate_leg.delta),
        "limit_price": _price(candidate_leg.limit_price),
    }


def to_json(candidate: Candidate) -> dict[str, object]:
    """``op_scan.candidates``' element: strings for money and prices, never floats."""
    return {
        "structure": candidate.structure.value,
        "expiry": candidate.expiry.isoformat(),
        "direction": None if candidate.direction is None else candidate.direction.value,
        "legs": [leg_json(lg) for lg in candidate.legs],
        "points": _price(candidate.points),
        "lot_size": candidate.lot_size,
        "lots": candidate.lots,
        "sizing_mode": None if candidate.sizing_mode is None else candidate.sizing_mode.value,
        "half_size": candidate.half_size,
        "risk_per_lot_inr": _money(candidate.risk_per_lot_inr),
        "r_inr": _money(candidate.r_inr),
        "max_loss_inr": _money(candidate.max_loss_inr),
        "round_trip_inr": _money(candidate.round_trip_inr),
        "expected_gain_inr": _money(candidate.expected_gain_inr),
        "cost_share": _share(candidate.cost_share),
        "rejection": None if candidate.rejection is None else candidate.rejection.value,
        "message": candidate.message,
        "warnings": list(candidate.warnings),
        **{key: value for key, value in sorted(candidate.extra.items())},
    }
