"""O2 — directional buying on a with-trend opening-range break (``04`` §4; PACK.5).

The trend reads **NIFTY 50** on purpose: the sleeve buys NIFTY options, so the tape it asks about
is NIFTY 50's own 20-day EMA — not the swing gate's MidSmall 400, which answers a question about a
book of mid- and small-caps (kickoff note; CLAUDE.md "the decision wins").

* :func:`day_filters` — ``04`` §4.1 at 09:30: trend, gap, the 15-minute opening range, India VIX's
  previous close, the event day and completeness; every reason reported.
* :func:`find_trigger` — ``04`` §4.2: the first completed 5-minute bar in the entry window whose
  close breaks the opening range by ``buffer_pct`` **in the trend's direction**. Counter-trend
  breaks are recorded and never traded; one trigger per day.
* :func:`build` — ``04`` §4.3-§4.6: one step ITM on the nearest non-expiring weekly
  (``calendar.expiry_for_o2``), ``|delta|`` in band, liquid, sized from ``E * stop_frac`` per lot,
  the premium cap, the cost test.
* :func:`exit_decision` — ``04`` §4.5: stop, invalidation, target, time stop, hard exit; one
  decision by ``HARD_EXIT > STOP > INVALIDATED > TARGET > TIME_STOP``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from baskfy_core.options.bars import (
    Bar,
    add_seconds,
    bar_at,
    closed,
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
    DirectionalConfig,
    OptionsCeilings,
    OptionsConfig,
    OptionType,
)
from baskfy_core.options.costs import expected_gain_o2
from baskfy_core.options.execution import LegRole, drop_on_stale, first_by_precedence
from baskfy_core.options.risk import trade_breached
from baskfy_core.options.sizing import gap_through_long, premium_cap_ok, risk_per_lot_long
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


class DayReason(StrEnum):
    """``04`` §4.1's skip reasons, in table order, plus OP4.8's two missing-data refusals."""

    TREND_FLAT = "TREND_FLAT"
    TREND_UNKNOWN = "TREND_UNKNOWN"
    GAP_TOO_BIG = "GAP_TOO_BIG"
    RANGE_TOO_WIDE = "RANGE_TOO_WIDE"
    VIX_TOO_HIGH = "VIX_TOO_HIGH"
    VIX_UNKNOWN = "VIX_UNKNOWN"
    EVENT_DAY = "EVENT_DAY"
    INCOMPLETE_OBSERVATION = "INCOMPLETE_OBSERVATION"


def ema(closes: Sequence[Decimal], days: int) -> Decimal | None:
    """The ``days``-day EMA of ``closes`` (oldest first) at the last close.

    ``alpha = 2 / (days + 1)``, seeded with the simple mean of the first ``days`` closes, then
    recursive — ``None`` with fewer than ``days`` closes (OP4.8). Feed it ample history: the seed's
    weight decays by ``(1 - alpha)`` a session.
    """
    if days <= 0:
        raise ValueError("an EMA needs a positive span")
    if len(closes) < days:
        return None
    alpha = _TWO / Decimal(days + 1)
    value = sum(closes[:days], Decimal(0)) / Decimal(days)
    for close in closes[days:]:
        value = alpha * close + (_ONE - alpha) * value
    return value


def trend(daily_closes: Sequence[Decimal], days: int) -> tuple[Direction | None, Decimal | None]:
    """``04`` §4.1: ``UP`` if the previous close is above its EMA, ``DOWN`` if below, ``None``
    when equal (``TREND_FLAT``) or unknown (the EMA is ``None`` — ``TREND_UNKNOWN``)."""
    average = ema(daily_closes, days)
    if average is None:
        return None, None
    prev = daily_closes[-1]
    if prev > average:
        return Direction.UP, average
    if prev < average:
        return Direction.DOWN, average
    return None, average


@dataclass(frozen=True, slots=True)
class DayFilters:
    """``04`` §4.1's numbers and verdict. ``trade`` iff final with no reason."""

    final: bool
    reasons: tuple[DayReason, ...]
    trend: Direction | None
    ema: Decimal | None
    prev_close: Decimal | None
    open_0915: Decimal | None
    gap_pct: Decimal | None
    or_high: Decimal | None
    or_low: Decimal | None
    or_pct: Decimal | None
    vix: Decimal | None
    bars: int

    @property
    def trade(self) -> bool:
        return self.final and not self.reasons


def day_filters(  # noqa: PLR0913 - the bars, the history, the VIX, the clock, the day, the config
    bars: Iterable[Bar],
    daily_closes: Sequence[Decimal],
    vix_prev_close: Decimal | None,
    *,
    now: dt.datetime,
    event_day: bool,
    config: DirectionalConfig,
    grace_seconds: int,
) -> DayFilters:
    """``04`` §4.1 over NIFTY 50's closed bars and its daily closes through the previous session."""
    seen = closed(bars, now)
    opening = window(seen, config.opening_range_start, config.opening_range_end)
    direction, average = trend(daily_closes, config.trend_ema_days)
    prev = daily_closes[-1] if daily_closes else None
    first = bar_at(opening, config.opening_range_start)
    open_0915 = first.open if first is not None else None
    gap = abs(pct(open_0915 - prev, prev)) if open_0915 is not None and prev else None
    hl = high_low(opening)
    or_pct = pct(hl[0] - hl[1], prev) if hl is not None and prev else None
    final = window_settled(opening, config.opening_range_end, now, grace_seconds)
    reasons: list[DayReason] = []
    if average is None:
        reasons.append(DayReason.TREND_UNKNOWN)
    elif direction is None:
        reasons.append(DayReason.TREND_FLAT)
    if gap is not None and gap > config.gap_max_pct:
        reasons.append(DayReason.GAP_TOO_BIG)
    if or_pct is not None and or_pct > config.or_max_pct:
        reasons.append(DayReason.RANGE_TOO_WIDE)
    if vix_prev_close is None:
        reasons.append(DayReason.VIX_UNKNOWN)
    elif vix_prev_close > config.vix_max:
        reasons.append(DayReason.VIX_TOO_HIGH)
    if event_day:
        reasons.append(DayReason.EVENT_DAY)
    if final and len(opening) < config.opening_range_bars:
        reasons.append(DayReason.INCOMPLETE_OBSERVATION)
    return DayFilters(
        final=final,
        reasons=tuple(reasons),
        trend=direction,
        ema=average,
        prev_close=prev,
        open_0915=open_0915,
        gap_pct=gap,
        or_high=hl[0] if hl is not None else None,
        or_low=hl[1] if hl is not None else None,
        or_pct=or_pct,
        vix=vix_prev_close,
        bars=len(opening),
    )


@dataclass(frozen=True, slots=True)
class Break:
    """A completed 5-minute bar that closed beyond a level."""

    close_time: dt.time
    close: Decimal
    direction: Direction


@dataclass(frozen=True, slots=True)
class TriggerScan:
    """``04`` §4.2 at ``now``: the trigger (if any), the counter-trend breaks seen, the levels."""

    level_up: Decimal
    level_down: Decimal
    trigger: Break | None
    counter_breaks: tuple[Break, ...]
    window_closed: bool
    last_close: Decimal | None


def levels(or_high: Decimal, or_low: Decimal, buffer_pct: Decimal) -> tuple[Decimal, Decimal]:
    """The break levels: ``or_high * (1 + buffer)`` and ``or_low * (1 - buffer)``."""
    return (
        or_high * (_ONE + buffer_pct / _HUNDRED),
        or_low * (_ONE - buffer_pct / _HUNDRED),
    )


def find_trigger(  # noqa: PLR0913 - the bars, the range, the trend, the clock and the config
    bars: Iterable[Bar],
    *,
    or_high: Decimal,
    or_low: Decimal,
    direction: Direction,
    now: dt.datetime,
    config: DirectionalConfig,
    session_open: dt.time,
    grace_seconds: int,
) -> TriggerScan:
    """The first completed 5-minute bar closing in ``[entry_window_start, entry_window_end]``
    beyond the with-trend level; counter-trend breaks before it are recorded, never traded."""
    seen = closed(bars, now)
    up, down = levels(or_high, or_low, config.buffer_pct)
    counter: list[Break] = []
    trigger: Break | None = None
    for bar in five_minute_bars(seen, session_open, config.bar_minutes):
        if bar.close is None:
            continue
        if not config.entry_window_start <= bar.close_time <= config.entry_window_end:
            continue
        broke_up, broke_down = bar.close > up, bar.close < down
        with_trend = broke_up if direction is Direction.UP else broke_down
        against = broke_down if direction is Direction.UP else broke_up
        if with_trend:
            trigger = Break(bar.close_time, bar.close, direction)
            break
        if against:
            opposite = Direction.DOWN if direction is Direction.UP else Direction.UP
            counter.append(Break(bar.close_time, bar.close, opposite))
    done = ist(now).time() >= add_seconds(minute_after(config.entry_window_end), grace_seconds)
    return TriggerScan(
        level_up=up,
        level_down=down,
        trigger=trigger,
        counter_breaks=tuple(counter),
        window_closed=trigger is None and done,
        last_close=seen[-1].close if seen else None,
    )


def contract_for(
    view: ChainView, direction: Direction, itm_steps: int
) -> tuple[Decimal, OptionType]:
    """``04`` §4.3: a CE ``itm_steps`` below ATM on an up-trend, a PE above it on a down-trend."""
    if direction is Direction.UP:
        return view.atm - view.step * itm_steps, OptionType.CE
    return view.atm + view.step * itm_steps, OptionType.PE


def build(  # noqa: PLR0913, PLR0911 - every input 04 §4/§6/§7 names; one return per refusal
    view: ChainView | None,
    direction: Direction,
    *,
    lot_size: int | None,
    book: SleeveBook,
    config: DirectionalConfig,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
    expiry: dt.date,
    paused: bool = False,
) -> Candidate:
    """The long a plan would carry — or the first refusal: paused, chain, lot size, contract,
    delta (§4.3), sizing (§4.6/§7), premium cap (§4.6), liquidity at the quantity, cost (§6.4)."""
    structure = Structure.LONG_OPTION
    if paused:
        return rejected(
            structure, expiry, Rejection.REJECTED_PAUSED, "the sleeve or book is paused",
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
    strike, option_type = contract_for(view, direction, config.itm_steps)
    priced = view.get(strike, option_type)
    if priced is None or priced.quote.bid is None or priced.quote.ask is None:
        return rejected(
            structure,
            expiry,
            Rejection.REJECTED_NO_CONTRACT,
            f"{strike} {option_type.value} is not quoted two-sided",
            direction=direction,
            lot_size=lot_size,
        )
    role = LegRole.LONG_CALL if option_type is OptionType.CE else LegRole.LONG_PUT
    the_leg = leg(role, priced, view.tick, options.execution)
    entry = the_leg.limit_price
    delta = priced.delta
    legs = (the_leg,)
    if delta is None or not config.delta_min <= abs(Decimal(repr(delta))) <= config.delta_max:
        return rejected(
            structure,
            expiry,
            Rejection.REJECTED_DELTA,
            f"|delta| {'unknown' if delta is None else f'{abs(delta):.4f}'} outside "
            f"[{config.delta_min}, {config.delta_max}]",
            direction=direction,
            legs=legs,
            points=entry,
            lot_size=lot_size,
        )
    per_lot = risk_per_lot_long(entry, config.stop_frac, lot_size, config.reserve_per_lot_inr)
    sizing = size_for(
        book,
        default_risk_pct=config.risk_per_trade_pct,
        default_max_lots=config.max_lots,
        risk_per_lot_inr=per_lot,
        lot_size=lot_size,
        config=options.sizing,
        ceilings=ceilings,
    )
    extra = {"gap_through_per_lot_inr": str(gap_through_long(entry, lot_size))}
    if sizing.rejection is not None:
        return rejected(
            structure,
            expiry,
            Rejection(sizing.rejection.value),
            sizing.message,
            direction=direction,
            legs=legs,
            points=entry,
            lot_size=lot_size,
            extra=extra,
        )
    if not premium_cap_ok(
        sizing.lots, entry, lot_size, book.sleeve_capital_inr, config.premium_cap_pct
    ):
        return rejected(
            structure,
            expiry,
            Rejection.REJECTED_PREMIUM_CAP,
            f"{sizing.lots} lots * ₹{entry} * {lot_size} > {config.premium_cap_pct} % of capital",
            direction=direction,
            legs=legs,
            points=entry,
            lot_size=lot_size,
            extra=extra,
        )
    quantity = sizing.lots * lot_size
    if not is_liquid(priced.quote, quantity, the_leg.entry_side, lot_size, options.chain).ok:
        return rejected(
            structure,
            expiry,
            Rejection.REJECTED_ILLIQUID,
            f"{strike} {option_type.value} is not liquid for {quantity} units",
            direction=direction,
            legs=legs,
            points=entry,
            lot_size=lot_size,
            extra=extra,
        )
    trip = round_trip(legs, quantity, view.tick, rates=options.costs, execution=options.execution)
    gain = expected_gain_o2(entry, quantity, config.target_frac)
    share, passes = cost_gate(trip, gain, config.cost_share_max)
    extra |= {"gap_through_inr": str(gap_through_long(entry, lot_size) * sizing.lots)}
    return Candidate(
        structure=structure,
        expiry=expiry,
        direction=direction,
        legs=legs,
        points=entry,
        lot_size=lot_size,
        lots=sizing.lots if passes else 0,
        sizing_mode=sizing.sizing_mode,
        risk_per_lot_inr=per_lot,
        r_inr=sizing.r_inr,
        max_loss_inr=entry * config.stop_frac * quantity,
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


# --- exits (04 §4.5) -----------------------------------------------------------------------------


class LongExit(StrEnum):
    HARD_EXIT = "HARD_EXIT"
    STOP = "STOP"
    INVALIDATED = "INVALIDATED"
    TARGET = "TARGET"
    TIME_STOP = "TIME_STOP"
    MANUAL = "MANUAL"


PRECEDENCE: tuple[LongExit, ...] = (
    LongExit.HARD_EXIT,
    LongExit.STOP,
    LongExit.INVALIDATED,
    LongExit.TARGET,
    LongExit.TIME_STOP,
    LongExit.MANUAL,
)


def invalidated(
    five_minute_close: Decimal | None, direction: Direction, or_high: Decimal, or_low: Decimal
) -> bool:
    """A completed 5-minute index close back inside the opening range: ``< or_high`` for a call,
    ``> or_low`` for a put (``04`` §4.5)."""
    if five_minute_close is None:
        return False
    if direction is Direction.UP:
        return five_minute_close < or_high
    return five_minute_close > or_low


def exit_decision(  # noqa: PLR0913 - every input 04 §4.5 and §9.2 name, by keyword
    *,
    entry: Decimal,
    bid: Decimal,
    entered_at: dt.datetime,
    now: dt.datetime,
    is_invalidated: bool,
    stale: bool,
    marked_loss_inr: Decimal,
    risk_budget_inr: Decimal,
    config: DirectionalConfig,
    options: OptionsConfig,
    manual: bool = False,
) -> LongExit | None:
    """One tick's exit for the long (mark = the option's bid, ``E`` = the entry fill)."""
    fired: list[LongExit] = []
    if ist(now).time() >= config.hard_exit_time:
        fired.append(LongExit.HARD_EXIT)
    if bid <= entry * (_ONE - config.stop_frac) or trade_breached(
        marked_loss_inr, risk_budget_inr, options.risk
    ):
        fired.append(LongExit.STOP)
    if is_invalidated:
        fired.append(LongExit.INVALIDATED)
    if bid >= entry * (_ONE + config.target_frac):
        fired.append(LongExit.TARGET)
    held = ist(now) - ist(entered_at)
    if held >= dt.timedelta(minutes=config.time_stop_minutes) and bid < entry * (
        _ONE + config.time_stop_min_gain
    ):
        fired.append(LongExit.TIME_STOP)
    if manual:
        fired.append(LongExit.MANUAL)
    kept = drop_on_stale(fired, (LongExit.TARGET,), stale)
    return first_by_precedence(kept, PRECEDENCE)
