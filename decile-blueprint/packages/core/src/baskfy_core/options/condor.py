"""O1 — the hedged condor on an expiry day, monthly and weekly (``04`` §3; condor §2, §4, §7).

Condor ``04`` is authoritative for this sleeve; ``04`` §3 ports it with two differences: every
field lives in one of two :class:`CondorConfig` instances (``condor_monthly`` for O1-M,
``condor_weekly`` for O1-W — the functions here take either, and never know which), and delta is
Black-76 on the parity forward (``04`` §2.3), not Black-Scholes on spot.

Three pieces, each the one the plan builder (OP6) and the desk (OP9) will call:

* :func:`observe` — the gate (condor §2): gap, observation range, containment in the opening
  range, Kaufman's ER, the event day and completeness, **every reason reported** in condor's order
  (§2.7). While the 09:15-09:59 window is still filling, the same function gives the live numbers
  the scan shows as ``OBSERVING``; the verdict is final once the window is settled (OP4.3).
* :func:`build` — the structure (condor §4): the short call is the liquid CE whose ``|delta|`` is in
  ``[delta_min, delta_max]`` **and** whose strike is above ``or_high + strike_buffer_points`` —
  delta ∧ range, neither relaxed to find the other (§4.1) — nearest ``delta_target``; the put
  symmetric; wings ``wing_width_points`` further out, depth-only liquidity (§4.3); the credit at
  the conservative side (§4.4); sized from risk per lot (``04`` §7), priced for costs (``04``
  §6.4).
* :func:`exit_decision` — the exits (condor §7): profit at ``D ≤ profit_take_frac * C``, stop at
  ``D ≥ stop_frac * C``, a touch of either short strike, the hard exit, and ``04`` §9.2's budget
  breach, one decision by ``HARD_EXIT > STRIKE_TOUCH > STOP > PROFIT``; profit is never taken on a
  stale mark (§7.7).

A condor is **never naked**: its legs always include both wings, so ``execution.entry_sequence``
accepts it and sends both longs before either short (tested as a property).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from baskfy_core.options.bars import (
    Bar,
    bar_at,
    closed,
    efficiency_ratio,
    er_points,
    high_low,
    ist,
    pct,
    window,
    window_settled,
)
from baskfy_core.options.chain import is_liquid
from baskfy_core.options.config import (
    CondorConfig,
    OptionsCeilings,
    OptionsConfig,
    OptionType,
    Side,
)
from baskfy_core.options.costs import expected_gain_o1, paise
from baskfy_core.options.execution import LegRole, drop_on_stale, first_by_precedence
from baskfy_core.options.risk import trade_breached
from baskfy_core.options.sizing import risk_per_lot_condor
from baskfy_core.options.structures import (
    Candidate,
    CandidateLeg,
    ChainView,
    PricedQuote,
    Rejection,
    SleeveBook,
    Structure,
    cost_gate,
    leg,
    rejected,
    round_trip,
    size_for,
)


class GateReason(StrEnum):
    """Condor ``04`` §2.1-§2.6, in the order they are reported (§2.7)."""

    GAP_TOO_BIG = "GAP_TOO_BIG"
    RANGE_TOO_WIDE = "RANGE_TOO_WIDE"
    NOT_CONTAINED = "NOT_CONTAINED"
    ER_TOO_HIGH = "ER_TOO_HIGH"
    EVENT_DAY = "EVENT_DAY"
    INCOMPLETE_OBSERVATION = "INCOMPLETE_OBSERVATION"


@dataclass(frozen=True, slots=True)
class GateNumbers:
    """What the gate measured — live while observing, final at the verdict."""

    prev_close: Decimal
    bars: int
    open_0915: Decimal | None
    gap_pct: Decimal | None
    obs_high: Decimal | None
    obs_low: Decimal | None
    range_pct: Decimal | None
    or_high: Decimal | None
    or_low: Decimal | None
    last_close: Decimal | None
    contained: bool | None
    er: Decimal | None


@dataclass(frozen=True, slots=True)
class DayVerdict:
    """Condor §2's ``DayVerdict``. ``trade`` iff ``final`` and ``reasons`` is empty."""

    final: bool
    reasons: tuple[GateReason, ...]
    numbers: GateNumbers

    @property
    def trade(self) -> bool:
        return self.final and not self.reasons


def observe(  # noqa: PLR0913 - the bars, the close, the clock, the day and the config
    bars: Iterable[Bar],
    prev_close: Decimal,
    *,
    now: dt.datetime,
    event_day: bool,
    config: CondorConfig,
    grace_seconds: int,
) -> DayVerdict:
    """Condor ``04`` §2 over the closed bars of ``[observation_start, observation_end]``.

    Gap needs the 09:15 bar; range reads whatever has closed (a range only grows, so an excess is
    already final); containment and ER need the whole window (``min_bars``) and are not judged on
    a partial one — the verdict then carries ``INCOMPLETE_OBSERVATION`` instead (OP4.3).
    """
    if prev_close <= 0:
        raise ValueError("the previous close must be positive")
    seen = closed(bars, now)
    obs = window(seen, config.observation_start, config.observation_end)
    opening = window(seen, config.observation_start, config.opening_range_end)
    first = bar_at(obs, config.observation_start)
    open_0915 = first.open if first is not None else None
    gap = abs(pct(open_0915 - prev_close, prev_close)) if open_0915 is not None else None
    hl = high_low(obs)
    obs_range = pct(hl[0] - hl[1], prev_close) if hl is not None else None
    or_hl = high_low(opening)
    opening_done = bar_at(opening, config.opening_range_end) is not None
    last = obs[-1].close if obs else None
    contained = (
        or_hl[1] <= last <= or_hl[0]
        if or_hl is not None and opening_done and last is not None
        else None
    )
    er = efficiency_ratio(er_points(obs)) if len(obs) > 1 else None
    complete = len(obs) >= config.min_bars and bar_at(obs, config.observation_end) is not None
    final = window_settled(obs, config.observation_end, now, grace_seconds)
    reasons: list[GateReason] = []
    if gap is not None and gap > config.gap_max_pct:
        reasons.append(GateReason.GAP_TOO_BIG)
    if obs_range is not None and obs_range > config.range_max_pct:
        reasons.append(GateReason.RANGE_TOO_WIDE)
    if complete and contained is False:
        reasons.append(GateReason.NOT_CONTAINED)
    if complete and er is not None and er > config.er_max:
        reasons.append(GateReason.ER_TOO_HIGH)
    if event_day:
        reasons.append(GateReason.EVENT_DAY)
    if final and not complete:
        reasons.append(GateReason.INCOMPLETE_OBSERVATION)
    numbers = GateNumbers(
        prev_close=prev_close,
        bars=len(obs),
        open_0915=open_0915,
        gap_pct=gap,
        obs_high=hl[0] if hl is not None else None,
        obs_low=hl[1] if hl is not None else None,
        range_pct=obs_range,
        or_high=or_hl[0] if or_hl is not None else None,
        or_low=or_hl[1] if or_hl is not None else None,
        last_close=last,
        contained=contained,
        er=er,
    )
    return DayVerdict(final=final, reasons=tuple(reasons), numbers=numbers)


def _abs_delta(priced: PricedQuote) -> Decimal | None:
    delta = priced.delta
    return None if delta is None else abs(Decimal(repr(delta)))


def select_short(  # noqa: PLR0913 - the chain, the side, the bound, the size and the configs
    view: ChainView,
    option_type: OptionType,
    bound: Decimal,
    quantity: int,
    lot_size: int,
    *,
    config: CondorConfig,
    options: OptionsConfig,
) -> PricedQuote | None:
    """Condor §4.1-§4.2: the liquid short whose ``|delta|`` is in band **and** whose strike is
    beyond the opening range (a call above ``or_high + buffer``, a put below ``or_low - buffer``),
    nearest ``delta_target``; on a tie, the one further from the money (OP4.7)."""
    eligible: list[tuple[Decimal, Decimal, PricedQuote]] = []
    for priced in view.of_type(option_type):
        delta = _abs_delta(priced)
        if delta is None or not config.delta_min <= delta <= config.delta_max:
            continue
        beyond = (
            priced.strike > bound + config.strike_buffer_points
            if option_type is OptionType.CE
            else priced.strike < bound - config.strike_buffer_points
        )
        if not beyond:
            continue
        if not is_liquid(priced.quote, quantity, Side.SELL, lot_size, options.chain).ok:
            continue
        further = -priced.strike if option_type is OptionType.CE else priced.strike
        eligible.append((abs(delta - config.delta_target), further, priced))
    if not eligible:
        return None
    return min(eligible, key=lambda item: (item[0], item[1]))[2]


def _wing(  # noqa: PLR0913, PLR0917 - the chain, the short, the width, the size, the rules
    view: ChainView, short: PricedQuote, width: Decimal, quantity: int, lot_size: int,
    options: OptionsConfig,
) -> PricedQuote | None:  # fmt: skip
    """Condor §4.3: the wing exists, is two-sided, and has the depth to buy ``quantity``."""
    strike = short.strike + width if short.option_type is OptionType.CE else short.strike - width
    wing = view.get(strike, short.option_type)
    if wing is None or wing.quote.bid is None or wing.quote.ask is None:
        return None
    ok = is_liquid(wing.quote, quantity, Side.BUY, lot_size, options.chain, wing=True).ok
    return wing if ok else None


def credit_points(
    short_call: PricedQuote, short_put: PricedQuote, long_call: PricedQuote, long_put: PricedQuote
) -> Decimal:
    """Condor §4.4: shorts at bid, wings at ask — the conservative side."""
    for q in (short_call, short_put, long_call, long_put):
        if q.quote.bid is None or q.quote.ask is None:
            raise ValueError("a condor leg needs a two-sided quote")
    return (
        (short_call.quote.bid or Decimal(0))
        + (short_put.quote.bid or Decimal(0))
        - (long_call.quote.ask or Decimal(0))
        - (long_put.quote.ask or Decimal(0))
    )


def build(  # noqa: PLR0913, PLR0911 - every input 04 §3/§6/§7 names; one return per refusal
    view: ChainView | None,
    *,
    or_high: Decimal,
    or_low: Decimal,
    lot_size: int | None,
    book: SleeveBook,
    config: CondorConfig,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
    slot_free: bool = True,
    paused: bool = False,
    expiry: dt.date,
) -> Candidate:
    """The condor a plan would carry, priced from ``view`` — or the first refusal, in order:
    paused (``04`` §9.4), slot (§3.3), chain, lot size, short call, short put, wings, credit
    floor, sizing (§7), liquidity at the sized quantity, cost (§6.4)."""
    structure = Structure.IRON_CONDOR
    if paused:
        return rejected(
            structure, expiry, Rejection.REJECTED_PAUSED, "the sleeve or book is paused"
        )
    if not slot_free:
        return rejected(
            structure, expiry, Rejection.REJECTED_SLOT_TAKEN, "another sleeve holds today's slot"
        )
    if view is None:
        return rejected(structure, expiry, Rejection.REJECTED_NO_CHAIN, "no chain for the expiry")
    if lot_size is None or lot_size <= 0:
        return rejected(
            structure, expiry, Rejection.REJECTED_NO_LOT_SIZE, "the master has no lot size"
        )
    one_lot = lot_size
    short_call = select_short(
        view, OptionType.CE, or_high, one_lot, lot_size, config=config, options=options
    )
    if short_call is None:
        return rejected(
            structure,
            expiry,
            Rejection.REJECTED_NO_SHORT_CALL,
            f"no liquid CE with |delta| in [{config.delta_min}, {config.delta_max}] above "
            f"{or_high + config.strike_buffer_points}",
            lot_size=lot_size,
        )
    short_put = select_short(
        view, OptionType.PE, or_low, one_lot, lot_size, config=config, options=options
    )
    if short_put is None:
        return rejected(
            structure,
            expiry,
            Rejection.REJECTED_NO_SHORT_PUT,
            f"no liquid PE with |delta| in [{config.delta_min}, {config.delta_max}] below "
            f"{or_low - config.strike_buffer_points}",
            lot_size=lot_size,
        )
    width = config.wing_width_points
    long_call = _wing(view, short_call, width, one_lot, lot_size, options)
    long_put = _wing(view, short_put, width, one_lot, lot_size, options)
    if long_call is None or long_put is None:
        return rejected(
            structure,
            expiry,
            Rejection.REJECTED_NO_WING,
            f"a wing {width} points out is missing, one-sided or too thin",
            lot_size=lot_size,
        )
    tick = view.tick
    legs = (
        leg(LegRole.LONG_PUT, long_put, tick, options.execution),
        leg(LegRole.LONG_CALL, long_call, tick, options.execution),
        leg(LegRole.SHORT_PUT, short_put, tick, options.execution),
        leg(LegRole.SHORT_CALL, short_call, tick, options.execution),
    )
    credit = credit_points(short_call, short_put, long_call, long_put)
    floor = config.credit_floor_frac * width
    extra = {
        "credit_floor_points": str(floor),
        "credit_target_points": str(config.credit_target_frac * width),
        "wing_width_points": str(width),
    }
    if credit < floor:
        return rejected(
            structure,
            expiry,
            Rejection.REJECTED_CREDIT,
            f"credit {credit} < floor {floor} ({config.credit_floor_frac} * {width})",
            legs=legs,
            points=credit,
            lot_size=lot_size,
            extra=extra,
        )
    per_lot = risk_per_lot_condor(width, credit, lot_size, config.reserve_per_lot_inr)
    sizing = size_for(
        book,
        default_risk_pct=config.risk_per_trade_pct,
        default_max_lots=config.max_lots,
        risk_per_lot_inr=per_lot,
        lot_size=lot_size,
        config=options.sizing,
        ceilings=ceilings,
    )
    if sizing.rejection is not None:
        return rejected(
            structure,
            expiry,
            Rejection(sizing.rejection.value),
            sizing.message,
            legs=legs,
            points=credit,
            lot_size=lot_size,
            extra=extra,
        )
    quantity = sizing.lots * lot_size
    thin = [
        lg.role.value
        for lg, q in zip(legs, (long_put, long_call, short_put, short_call), strict=True)
        if not is_liquid(
            q.quote,
            quantity,
            lg.entry_side,
            lot_size,
            options.chain,
            wing=lg.role.is_long,
        ).ok
    ]
    if thin:
        return rejected(
            structure,
            expiry,
            Rejection.REJECTED_ILLIQUID,
            f"not liquid for {quantity} units: {', '.join(thin)}",
            legs=legs,
            points=credit,
            lot_size=lot_size,
            extra=extra,
        )
    trip = round_trip(legs, quantity, tick, rates=options.costs, execution=options.execution)
    credit_inr = credit * quantity
    gain = expected_gain_o1(credit_inr, config.profit_take_frac)
    share, passes = cost_gate(trip, gain, config.cost_share_max)
    warnings: tuple[str, ...] = ()
    if trip / sizing.lots > config.reserve_per_lot_inr:
        warnings = ("RESERVE_EXCEEDED",)
    extra |= {"credit_inr": str(paise(credit_inr))}
    return Candidate(
        structure=structure,
        expiry=expiry,
        direction=None,
        legs=legs,
        points=credit,
        lot_size=lot_size,
        lots=sizing.lots if passes else 0,
        sizing_mode=sizing.sizing_mode,
        risk_per_lot_inr=per_lot,
        r_inr=sizing.r_inr,
        max_loss_inr=(width - credit) * quantity,
        round_trip_inr=trip,
        expected_gain_inr=gain,
        cost_share=share,
        rejection=None if passes else Rejection.REJECTED_COST,
        message=sizing.message
        if passes
        else f"cost share {share} > {config.cost_share_max} (round trip ₹{trip})",
        half_size=sizing.half_size,
        warnings=warnings,
        extra=extra,
    )


# --- exits (condor 04 §7) ------------------------------------------------------------------------


class CondorExit(StrEnum):
    HARD_EXIT = "HARD_EXIT"
    STRIKE_TOUCH = "STRIKE_TOUCH"
    STOP = "STOP"
    PROFIT = "PROFIT"
    MANUAL = "MANUAL"


#: Condor §7.6; MANUAL is the operator's button and outranks nothing automatic.
PRECEDENCE: tuple[CondorExit, ...] = (
    CondorExit.HARD_EXIT,
    CondorExit.STRIKE_TOUCH,
    CondorExit.STOP,
    CondorExit.PROFIT,
    CondorExit.MANUAL,
)


def cost_to_close(legs: Sequence[CandidateLeg]) -> Decimal:
    """Condor §7's ``D``: shorts at ask, wings at bid — the conservative close, per unit."""
    total = Decimal(0)
    for lg in legs:
        if lg.bid is None or lg.ask is None:
            raise ValueError("a mark needs a two-sided quote")
        total += -lg.bid if lg.role.is_long else lg.ask
    return total


def exit_decision(  # noqa: PLR0913 - every input condor §7 and 04 §9.2 name, by keyword
    *,
    entry_credit: Decimal,
    cost_now: Decimal,
    spot: Decimal,
    short_call_strike: Decimal,
    short_put_strike: Decimal,
    now: dt.datetime,
    stale: bool,
    marked_loss_inr: Decimal,
    risk_budget_inr: Decimal,
    config: CondorConfig,
    options: OptionsConfig,
    manual: bool = False,
) -> CondorExit | None:
    """One tick's exit, or ``None``. ``entry_credit`` is C from fills (§4.8), ``cost_now`` is D.

    ``04`` §9.2's budget breach closes as ``STOP`` whatever D/C says; a stale mark drops only
    ``PROFIT`` (§7.7). Feed loss is ``execution.feed_lost``'s, raised by the desk as
    ``HARD_EXIT``/``FEED_LOST``.
    """
    fired: list[CondorExit] = []
    if ist(now).time() >= config.hard_exit_time:
        fired.append(CondorExit.HARD_EXIT)
    if spot >= short_call_strike or spot <= short_put_strike:
        fired.append(CondorExit.STRIKE_TOUCH)
    if cost_now >= config.stop_frac * entry_credit or trade_breached(
        marked_loss_inr, risk_budget_inr, options.risk
    ):
        fired.append(CondorExit.STOP)
    if cost_now <= config.profit_take_frac * entry_credit:
        fired.append(CondorExit.PROFIT)
    if manual:
        fired.append(CondorExit.MANUAL)
    kept = drop_on_stale(fired, (CondorExit.PROFIT,), stale)
    return first_by_precedence(kept, PRECEDENCE)
