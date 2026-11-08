"""O3 — two expiry-day debit-spread setups on the expiring contract (``04`` §5; PACK.10).

* **O3-A, range break** (:func:`range_break`, ``04`` §5.1): the 09:15-10:14 morning range must be
  narrow (``o3a_range_max_pct``); the trigger is the first completed 5-minute bar closing in
  ``[10:19, 13:00]`` beyond the range by ``o3a_buffer_pct`` **with** Kaufman's ER from 09:15 to
  that bar's close at least ``o3a_er_min``. A break with a lower ER is recorded, not traded, and
  the watch goes on. Direction = the side broken.
* **O3-B, gap hold** (:func:`gap_hold`, ``04`` §5.2): an opening gap between ``o3b_gap_min_pct``
  and ``o3b_gap_max_pct`` that never trades back through half the gap from 09:15 to the 09:44 bar;
  it triggers at the plan time (09:45) in the gap's direction.

Both trade one ``DEBIT_SPREAD`` (:func:`build`): long ATM, short ``width_points`` further in the
move's direction, ``debit = long.ask - short.bid`` at most ``max_debit_frac * width``, both legs
liquid, today's expiry-day slot free (``04`` §8.6). One O3 trade a day; if both would fire, O3-B —
always the earlier — holds (``04`` §5.5; the scan applies it).

Exits (:func:`exit_decision`, ``04`` §5.3) mark the spread at ``V = long.bid - short.ask``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from baskfy_core.options.bars import (
    Bar,
    add_seconds,
    bar_at,
    closed,
    efficiency_ratio,
    er_points,
    expected_minutes,
    five_minute_bars,
    high_low,
    ist,
    minute_after,
    pct,
    window,
    window_settled,
)
from baskfy_core.options.chain import is_liquid
from baskfy_core.options.config import (
    ExpirySetupsConfig,
    OptionsCeilings,
    OptionsConfig,
    OptionType,
)
from baskfy_core.options.costs import expected_gain_o3
from baskfy_core.options.execution import LegRole, drop_on_stale, first_by_precedence
from baskfy_core.options.risk import trade_breached
from baskfy_core.options.sizing import risk_per_lot_debit_spread
from baskfy_core.options.structures import (
    Candidate,
    ChainView,
    Direction,
    Rejection,
    SleeveBook,
    Structure,
    cost_gate,
    leg,
    rejected,
    round_trip,
    size_for,
)

_HUNDRED = Decimal(100)
_ONE = Decimal(1)
_TWO = Decimal(2)


class SetupReason(StrEnum):
    """Why a setup does not arm today (``04`` §5.1-§5.2), plus the shared data refusals."""

    RANGE_TOO_WIDE = "RANGE_TOO_WIDE"
    GAP_TOO_SMALL = "GAP_TOO_SMALL"
    GAP_TOO_BIG = "GAP_TOO_BIG"
    HOLD_BROKEN = "HOLD_BROKEN"
    EVENT_DAY = "EVENT_DAY"
    INCOMPLETE_OBSERVATION = "INCOMPLETE_OBSERVATION"


@dataclass(frozen=True, slots=True)
class RangeBreakScan:
    """O3-A at ``now``."""

    final: bool
    reasons: tuple[SetupReason, ...]
    bars: int
    range_high: Decimal | None
    range_low: Decimal | None
    range_pct: Decimal | None
    level_up: Decimal | None
    level_down: Decimal | None
    trigger_time: dt.time | None
    trigger_close: Decimal | None
    trigger_er: Decimal | None
    direction: Direction | None
    #: Breaks the ER refused, as ``(close_time, close, er)``.
    low_er_breaks: tuple[tuple[dt.time, Decimal, Decimal], ...]
    window_closed: bool
    last_close: Decimal | None

    @property
    def armed(self) -> bool:
        return self.final and not self.reasons


def range_break(  # noqa: PLR0913 - the bars, the close, the clock, the day and the config
    bars: Iterable[Bar],
    prev_close: Decimal,
    *,
    now: dt.datetime,
    event_day: bool,
    config: ExpirySetupsConfig,
    session_open: dt.time,
    grace_seconds: int,
) -> RangeBreakScan:
    """``04`` §5.1 over NIFTY 50's closed bars."""
    if prev_close <= 0:
        raise ValueError("the previous close must be positive")
    seen = closed(bars, now)
    morning = window(seen, config.o3a_range_start, config.o3a_range_end)
    hl = high_low(morning)
    range_pct = pct(hl[0] - hl[1], prev_close) if hl is not None else None
    final = window_settled(morning, config.o3a_range_end, now, grace_seconds)
    reasons: list[SetupReason] = []
    if range_pct is not None and range_pct > config.o3a_range_max_pct:
        reasons.append(SetupReason.RANGE_TOO_WIDE)
    if event_day:
        reasons.append(SetupReason.EVENT_DAY)
    if final and len(morning) < config.o3a_range_bars:
        reasons.append(SetupReason.INCOMPLETE_OBSERVATION)
    up = hl[0] * (_ONE + config.o3a_buffer_pct / _HUNDRED) if hl is not None else None
    down = hl[1] * (_ONE - config.o3a_buffer_pct / _HUNDRED) if hl is not None else None
    trigger: tuple[dt.time, Decimal, Decimal, Direction] | None = None
    refused: list[tuple[dt.time, Decimal, Decimal]] = []
    if final and not reasons and up is not None and down is not None:
        path_from = window(seen, config.o3a_range_start, dt.time(23, 59))
        for bar in five_minute_bars(seen, session_open, config.bar_minutes):
            if bar.close is None:
                continue
            if not config.o3a_window_start <= bar.close_time <= config.o3a_window_end:
                continue
            if bar.close > up:
                side = Direction.UP
            elif bar.close < down:
                side = Direction.DOWN
            else:
                continue
            upto = window(path_from, config.o3a_range_start, bar.close_time)
            er = efficiency_ratio(er_points(upto))
            if er >= config.o3a_er_min:
                trigger = (bar.close_time, bar.close, er, side)
                break
            refused.append((bar.close_time, bar.close, er))
    done = ist(now).time() >= add_seconds(minute_after(config.o3a_window_end), grace_seconds)
    return RangeBreakScan(
        final=final,
        reasons=tuple(reasons),
        bars=len(morning),
        range_high=hl[0] if hl is not None else None,
        range_low=hl[1] if hl is not None else None,
        range_pct=range_pct,
        level_up=up,
        level_down=down,
        trigger_time=trigger[0] if trigger else None,
        trigger_close=trigger[1] if trigger else None,
        trigger_er=trigger[2] if trigger else None,
        direction=trigger[3] if trigger else None,
        low_er_breaks=tuple(refused),
        window_closed=trigger is None and done,
        last_close=seen[-1].close if seen else None,
    )


@dataclass(frozen=True, slots=True)
class GapHoldScan:
    """O3-B at ``now``."""

    final: bool
    reasons: tuple[SetupReason, ...]
    bars: int
    open_0915: Decimal | None
    gap_points: Decimal | None
    gap_pct: Decimal | None
    half_gap: Decimal | None
    direction: Direction | None
    held_so_far: bool | None
    triggered: bool
    window_closed: bool


def gap_hold(  # noqa: PLR0913 - the bars, the close, the clock, the day and the config
    bars: Iterable[Bar],
    prev_close: Decimal,
    *,
    now: dt.datetime,
    event_day: bool,
    config: ExpirySetupsConfig,
    session_open: dt.time,
    grace_seconds: int,
) -> GapHoldScan:
    """``04`` §5.2: the gap's size, then no bar through ``half_gap`` to the 09:44 bar inclusive;
    triggered at ``o3b_plan_time`` when the hold is complete and nothing refused it."""
    if prev_close <= 0:
        raise ValueError("the previous close must be positive")
    seen = closed(bars, now)
    hold = window(seen, session_open, config.o3b_hold_end)
    first = bar_at(hold, session_open)
    open_0915 = first.open if first is not None else None
    gap = open_0915 - prev_close if open_0915 is not None else None
    gap_pct = abs(pct(gap, prev_close)) if gap is not None else None
    half = prev_close + gap / _TWO if gap is not None else None
    direction = None
    if gap is not None and gap != 0:
        direction = Direction.UP if gap > 0 else Direction.DOWN
    held: bool | None = None
    if half is not None and direction is not None and hold:
        if direction is Direction.UP:
            held = all(b.low > half for b in hold)
        else:
            held = all(b.high < half for b in hold)
    expected = expected_minutes(session_open, config.o3b_hold_end)
    final = window_settled(hold, config.o3b_hold_end, now, grace_seconds)
    reasons: list[SetupReason] = []
    if gap_pct is not None and gap_pct < config.o3b_gap_min_pct:
        reasons.append(SetupReason.GAP_TOO_SMALL)
    if gap_pct is not None and gap_pct > config.o3b_gap_max_pct:
        reasons.append(SetupReason.GAP_TOO_BIG)
    if held is False:
        reasons.append(SetupReason.HOLD_BROKEN)
    if event_day:
        reasons.append(SetupReason.EVENT_DAY)
    if final and len(hold) < expected:
        reasons.append(SetupReason.INCOMPLETE_OBSERVATION)
    clock = ist(now).time()
    triggered = final and not reasons and clock >= config.o3b_plan_time
    return GapHoldScan(
        final=final,
        reasons=tuple(reasons),
        bars=len(hold),
        open_0915=open_0915,
        gap_points=gap,
        gap_pct=gap_pct,
        half_gap=half,
        direction=direction,
        held_so_far=held,
        triggered=triggered,
        window_closed=not triggered and clock > config.o3b_window_end,
    )


def strikes_for(
    view: ChainView, direction: Direction, width: Decimal
) -> tuple[Decimal, Decimal, OptionType]:
    """``04`` §5: long ``atm(spot)``, short ``width`` further in the move's direction."""
    if direction is Direction.UP:
        return view.atm, view.atm + width, OptionType.CE
    return view.atm, view.atm - width, OptionType.PE


def build(  # noqa: PLR0913, PLR0911 - every input 04 §5/§6/§7 names; one return per refusal
    view: ChainView | None,
    direction: Direction,
    *,
    lot_size: int | None,
    book: SleeveBook,
    config: ExpirySetupsConfig,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
    expiry: dt.date,
    slot_free: bool = True,
    paused: bool = False,
) -> Candidate:
    """The debit spread a plan would carry — or the first refusal: paused, slot, chain, lot size,
    contracts, debit (§5), sizing (§5.5/§7), liquidity at the quantity, cost (§6.4)."""
    structure = Structure.DEBIT_SPREAD
    if paused:
        return rejected(
            structure, expiry, Rejection.REJECTED_PAUSED, "the sleeve or book is paused",
            direction=direction,
        )  # fmt: skip
    if not slot_free:
        return rejected(
            structure, expiry, Rejection.REJECTED_SLOT_TAKEN, "another sleeve holds today's slot",
            direction=direction,
        )  # fmt: skip
    if view is None:
        return rejected(
            structure, expiry, Rejection.REJECTED_NO_CHAIN, "no chain for the expiry",
            direction=direction,
        )  # fmt: skip
    if lot_size is None or lot_size <= 0:
        return rejected(
            structure, expiry, Rejection.REJECTED_NO_LOT_SIZE, "the master has no lot size",
            direction=direction,
        )  # fmt: skip
    width = config.width_points
    k_long, k_short, option_type = strikes_for(view, direction, width)
    long_q, short_q = view.get(k_long, option_type), view.get(k_short, option_type)
    if (
        any(q is None or q.quote.bid is None or q.quote.ask is None for q in (long_q, short_q))
        or long_q is None
        or short_q is None
    ):
        return rejected(
            structure,
            expiry,
            Rejection.REJECTED_NO_CONTRACT,
            f"{k_long}/{k_short} {option_type.value} is not quoted two-sided",
            direction=direction,
            lot_size=lot_size,
        )
    long_role = LegRole.LONG_CALL if option_type is OptionType.CE else LegRole.LONG_PUT
    short_role = LegRole.SHORT_CALL if option_type is OptionType.CE else LegRole.SHORT_PUT
    legs = (
        leg(long_role, long_q, view.tick, options.execution),
        leg(short_role, short_q, view.tick, options.execution),
    )
    debit = (long_q.quote.ask or Decimal(0)) - (short_q.quote.bid or Decimal(0))
    cap = config.max_debit_frac * width
    extra = {"width_points": str(width), "max_debit_points": str(cap)}
    if debit <= 0 or debit > cap:
        return rejected(
            structure,
            expiry,
            Rejection.REJECTED_DEBIT,
            f"debit {debit} outside (0, {cap}] ({config.max_debit_frac} * {width})",
            direction=direction,
            legs=legs,
            points=debit,
            lot_size=lot_size,
            extra=extra,
        )
    per_lot = risk_per_lot_debit_spread(debit, lot_size, config.reserve_per_lot_inr)
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
            direction=direction,
            legs=legs,
            points=debit,
            lot_size=lot_size,
            extra=extra,
        )
    quantity = sizing.lots * lot_size
    thin = [
        lg.role.value
        for lg, q in zip(legs, (long_q, short_q), strict=True)
        if not is_liquid(q.quote, quantity, lg.entry_side, lot_size, options.chain).ok
    ]
    if thin:
        return rejected(
            structure,
            expiry,
            Rejection.REJECTED_ILLIQUID,
            f"not liquid for {quantity} units: {', '.join(thin)}",
            direction=direction,
            legs=legs,
            points=debit,
            lot_size=lot_size,
            extra=extra,
        )
    trip = round_trip(legs, quantity, view.tick, rates=options.costs, execution=options.execution)
    gain = expected_gain_o3(width, debit, quantity, config.target_frac_of_width)
    share, passes = cost_gate(trip, gain, config.cost_share_max)
    return Candidate(
        structure=structure,
        expiry=expiry,
        direction=direction,
        legs=legs,
        points=debit,
        lot_size=lot_size,
        lots=sizing.lots if passes else 0,
        sizing_mode=sizing.sizing_mode,
        risk_per_lot_inr=per_lot,
        r_inr=sizing.r_inr,
        max_loss_inr=debit * quantity,
        round_trip_inr=trip,
        expected_gain_inr=gain,
        cost_share=share,
        rejection=None if passes else Rejection.REJECTED_COST,
        message=sizing.message
        if passes
        else f"cost share {share} > {config.cost_share_max} (round trip ₹{trip})",
        half_size=sizing.half_size,
        extra=extra,
    )


# --- exits (04 §5.3) -----------------------------------------------------------------------------


class SpreadExit(StrEnum):
    HARD_EXIT = "HARD_EXIT"
    STOP = "STOP"
    INVALIDATED = "INVALIDATED"
    TARGET = "TARGET"
    MANUAL = "MANUAL"


PRECEDENCE: tuple[SpreadExit, ...] = (
    SpreadExit.HARD_EXIT,
    SpreadExit.STOP,
    SpreadExit.INVALIDATED,
    SpreadExit.TARGET,
    SpreadExit.MANUAL,
)


def close_value(long_bid: Decimal, short_ask: Decimal) -> Decimal:
    """``V = long.bid - short.ask`` — the conservative value of closing now, per unit."""
    return long_bid - short_ask


def range_break_invalidated(
    five_minute_close: Decimal | None, direction: Direction, high: Decimal, low: Decimal
) -> bool:
    """O3-A: a completed 5-minute close back inside the morning range."""
    if five_minute_close is None:
        return False
    return five_minute_close < high if direction is Direction.UP else five_minute_close > low


def gap_hold_invalidated(bar: Bar | None, direction: Direction, half_gap: Decimal) -> bool:
    """O3-B: any bar through ``half_gap`` (a low at or below it on an up-gap, a high at or above
    it on a down-gap)."""
    if bar is None:
        return False
    return bar.low <= half_gap if direction is Direction.UP else bar.high >= half_gap


def exit_decision(  # noqa: PLR0913 - every input 04 §5.3 and §9.2 name, by keyword
    *,
    value: Decimal,
    entry_debit: Decimal,
    width: Decimal,
    is_invalidated: bool,
    now: dt.datetime,
    stale: bool,
    marked_loss_inr: Decimal,
    risk_budget_inr: Decimal,
    config: ExpirySetupsConfig,
    options: OptionsConfig,
    manual: bool = False,
) -> SpreadExit | None:
    """One tick's exit for the spread: target at ``V ≥ target_frac_of_width * width``, stop at
    ``V ≤ stop_frac_of_debit * D``, invalidation, hard exit; ``HARD_EXIT > STOP > INVALIDATED >
    TARGET``; the target is not taken on a stale mark."""
    fired: list[SpreadExit] = []
    if ist(now).time() >= config.hard_exit_time:
        fired.append(SpreadExit.HARD_EXIT)
    if value <= config.stop_frac_of_debit * entry_debit or trade_breached(
        marked_loss_inr, risk_budget_inr, options.risk
    ):
        fired.append(SpreadExit.STOP)
    if is_invalidated:
        fired.append(SpreadExit.INVALIDATED)
    if value >= config.target_frac_of_width * width:
        fired.append(SpreadExit.TARGET)
    if manual:
        fired.append(SpreadExit.MANUAL)
    kept = drop_on_stale(fired, (SpreadExit.TARGET,), stale)
    return first_by_precedence(kept, PRECEDENCE)
