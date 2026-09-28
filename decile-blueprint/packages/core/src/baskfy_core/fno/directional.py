"""F3, the directional index credit spread — every rule of ``04`` §11, pure (Maulik, M.5).

Bars in, decisions out. The module reads no clock, no database and no network: the desk and the
worker hand it daily bars (``fo_index_daily``), minute bars (``op_index_minute``), the listed
expiries and strikes, and ``now``.

The method, in the order the trade is built:

1. **Levels** from the daily bars — pivot highs and lows over ``f3_pivot_lookback`` sessions;
   support is the highest pivot low below the close, resistance the lowest pivot high above it;
   the weekly range is the last ``f3_weekly_sessions`` sessions' high and low.
2. **Direction** — UP when the close is above its trend average and above support, DOWN when
   below both, NONE otherwise; then the **75-minute confirm**: the last completed 75-minute close
   against the average of the last ``f3_confirm_bars`` closes. Disagreement is NONE.
3. **The key level** — support for UP, resistance for DOWN. Broken when the index trades beyond
   it by ``f3_level_buffer_pct``: read from a print, never a close ("not after one more candle").
4. **The spread** — UP sells a put ``f3_distance_pct`` below the weekly low, DOWN a call that far
   above the weekly high, rounded to the strike step away from the index; the wing ``f3_wing_pct``
   of the index further out. Wing first, short second (``02`` §2.2's ordering is the executor's).
5. **Expiry** — the nearest listed expiry with at least the sleeve's minimum sessions left.
6. **Size** — the entry's max loss as ``f3_entry_share_pct`` of capital, under ``04`` §3's risk
   budget and the per-trade ceiling; ``fo_max_lots`` caps it; zero lots is ``REJECTED_SIZE``.
7. **Add** — the next session or later, only if the mark has decayed ``f3_add_working_pct`` of
   the credit, the direction and level still hold, and the full share is not reached.
8. **Exit**, in precedence — ``LEVEL_BREAK``, ``LOSS_CUT``, ``DECAY_TARGET``, ``HARD_EXIT``.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from enum import StrEnum

from baskfy_core.fno.calendar import sessions_between
from baskfy_core.fno.config import CommonConfig, F3Config, FnoCeilings, PlanState
from baskfy_core.fno.sizing import FoSizing, size
from baskfy_core.options.config import Mode, OptionType

_HUNDRED = Decimal(100)
_CENT = Decimal("0.01")
#: The five 75-minute bars of an NSE session, by their opening minute (``04`` §11).
BAR_75_OPENS: tuple[dt.time, ...] = (
    dt.time(9, 15),
    dt.time(10, 30),
    dt.time(11, 45),
    dt.time(13, 0),
    dt.time(14, 15),
)
_BAR_75_MINUTES = 75
_SESSION_OPEN_MINUTE = 9 * 60 + 15
_SESSION_CLOSE_MINUTE = 15 * 60 + 30


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


class Direction(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    NONE = "NONE"


class F3ExitReason(StrEnum):
    """``fo_position.closed_reason`` for F3 (``03`` §9), in precedence order."""

    LEVEL_BREAK = "LEVEL_BREAK"
    LOSS_CUT = "LOSS_CUT"
    DECAY_TARGET = "DECAY_TARGET"
    HARD_EXIT = "HARD_EXIT"


# ================================================================================================
# Bars
# ================================================================================================


@dataclass(frozen=True, slots=True)
class DailyBar:
    """One row of ``fo_index_daily``."""

    date: dt.date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True, slots=True)
class MinuteBar:
    """One row of ``op_index_minute``; ``ts`` is the minute's start, **in IST**."""

    ts: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True, slots=True)
class Bar75:
    """A completed 75-minute bar (``04`` §11): its opening minute and its OHLC."""

    start: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def _slot_of(ts: dt.datetime) -> int | None:
    minute = ts.hour * 60 + ts.minute
    if not _SESSION_OPEN_MINUTE <= minute < _SESSION_CLOSE_MINUTE:
        return None
    return (minute - _SESSION_OPEN_MINUTE) // _BAR_75_MINUTES


def seventy_five_minute_bars(minutes: Iterable[MinuteBar]) -> tuple[Bar75, ...]:
    """Aggregate minute bars into the session's five 75-minute bars, oldest first.

    A bar is **complete** only when its closing minute (09:15 + 75 n - 1, e.g. 10:29 for the
    first) has been seen; a bar still forming, or one whose last minute never arrived, is left
    out, so a confirm never reads a bar that has not closed. Minutes outside 09:15-15:30 are
    ignored.
    """
    grouped: defaultdict[tuple[dt.date, int], list[MinuteBar]] = defaultdict(list)
    for bar in minutes:
        slot = _slot_of(bar.ts)
        if slot is None:
            continue
        grouped[(bar.ts.date(), slot)].append(bar)
    out: list[Bar75] = []
    for (_day, slot), rows in sorted(grouped.items()):
        rows.sort(key=lambda b: b.ts)
        start_minute = _SESSION_OPEN_MINUTE + slot * _BAR_75_MINUTES
        last_minute = start_minute + _BAR_75_MINUTES - 1
        if rows[-1].ts.hour * 60 + rows[-1].ts.minute != last_minute:
            continue
        start = rows[0].ts.replace(
            hour=start_minute // 60, minute=start_minute % 60, second=0, microsecond=0
        )
        out.append(
            Bar75(
                start=start,
                open=rows[0].open,
                high=max(b.high for b in rows),
                low=min(b.low for b in rows),
                close=rows[-1].close,
            )
        )
    return tuple(out)


# ================================================================================================
# Levels and direction
# ================================================================================================


@dataclass(frozen=True, slots=True)
class Pivot:
    date: dt.date
    price: Decimal


def pivots(bars: Sequence[DailyBar], width: int) -> tuple[tuple[Pivot, ...], tuple[Pivot, ...]]:
    """Pivot highs and lows: a high with ``width`` lower highs on each side (strictly), a low with
    ``width`` higher lows on each side. The last ``width`` bars cannot yet be pivots."""
    highs: list[Pivot] = []
    lows: list[Pivot] = []
    for i in range(width, len(bars) - width):
        before = bars[i - width : i]
        after = bars[i + 1 : i + 1 + width]
        bar = bars[i]
        if all(bar.high > b.high for b in (*before, *after)):
            highs.append(Pivot(bar.date, bar.high))
        if all(bar.low < b.low for b in (*before, *after)):
            lows.append(Pivot(bar.date, bar.low))
    return tuple(highs), tuple(lows)


@dataclass(frozen=True, slots=True)
class Levels:
    """What the daily chart says at a close (``04`` §11 "Levels")."""

    as_of: dt.date
    close: Decimal
    support: Decimal | None
    resistance: Decimal | None
    weekly_high: Decimal
    weekly_low: Decimal
    trend_avg: Decimal
    pivot_highs: tuple[Pivot, ...]
    pivot_lows: tuple[Pivot, ...]


def bars_needed(f3: F3Config) -> int:
    """The daily history the levels need: the pivot lookback, and at least the trend window."""
    return max(f3.pivot_lookback, f3.trend_sessions, f3.weekly_sessions)


def levels(bars: Sequence[DailyBar], f3: F3Config) -> Levels | None:
    """The levels at the last bar's close, or ``None`` when the history is too short.

    ``bars`` are consecutive sessions, oldest first. Only the last ``f3_pivot_lookback`` bars
    are read for pivots; a pivot needs ``f3_pivot_width`` bars after it, so the newest extremes
    are not levels yet (a level is something the market has already turned at).
    """
    if len(bars) < bars_needed(f3):
        return None
    window = bars[-f3.pivot_lookback :]
    highs, lows = pivots(window, f3.pivot_width)
    last = bars[-1]
    below = [p.price for p in lows if p.price < last.close]
    above = [p.price for p in highs if p.price > last.close]
    weekly = bars[-f3.weekly_sessions :]
    trend = bars[-f3.trend_sessions :]
    return Levels(
        as_of=last.date,
        close=last.close,
        support=max(below) if below else None,
        resistance=min(above) if above else None,
        weekly_high=max(b.high for b in weekly),
        weekly_low=min(b.low for b in weekly),
        trend_avg=_money(sum((b.close for b in trend), Decimal(0)) / len(trend)),
        pivot_highs=highs,
        pivot_lows=lows,
    )


def daily_direction(lv: Levels) -> Direction:
    """UP above the trend average and above support; DOWN below both; otherwise NONE."""
    if lv.support is not None and lv.close > lv.trend_avg and lv.close > lv.support:
        return Direction.UP
    if lv.resistance is not None and lv.close < lv.trend_avg and lv.close < lv.resistance:
        return Direction.DOWN
    return Direction.NONE


@dataclass(frozen=True, slots=True)
class Confirm:
    """The 75-minute verdict: the bar it read, its close and the average it was judged against."""

    agrees: bool
    bar_start: dt.datetime | None
    last_close: Decimal | None
    average: Decimal | None
    message: str


def confirm_75(bars: Sequence[Bar75], daily: Direction, f3: F3Config) -> Confirm:
    """``04`` §11: the last completed 75-minute close above (UP) or below (DOWN) the average of
    the last ``f3_confirm_bars`` closes. Too few bars, or a NONE daily read, never agrees."""
    if daily is Direction.NONE:
        return Confirm(False, None, None, None, "the daily chart gives no direction")
    if len(bars) < f3.confirm_bars:
        return Confirm(
            False, None, None, None, f"{len(bars)} 75-minute bars < {f3.confirm_bars} needed"
        )
    recent = bars[-f3.confirm_bars :]
    average = _money(sum((b.close for b in recent), Decimal(0)) / len(recent))
    last = recent[-1]
    agrees = last.close > average if daily is Direction.UP else last.close < average
    verb = "above" if daily is Direction.UP else "below"
    return Confirm(
        agrees,
        last.start,
        last.close,
        average,
        f"75-minute close {last.close} {'is' if agrees else 'is not'} {verb} its "
        f"{f3.confirm_bars}-bar average {average}",
    )


def direction_for(lv: Levels, bars: Sequence[Bar75], f3: F3Config) -> tuple[Direction, Confirm]:
    """The daily read, confirmed; NONE when the two disagree."""
    daily = daily_direction(lv)
    verdict = confirm_75(bars, daily, f3)
    return (daily if verdict.agrees else Direction.NONE), verdict


def key_level(direction: Direction, lv: Levels) -> Decimal | None:
    if direction is Direction.UP:
        return lv.support
    if direction is Direction.DOWN:
        return lv.resistance
    return None


def intraday_aligned(direction: Direction, last_price: Decimal, session_open: Decimal) -> bool:
    """The intraday check at plan time: the index above the session's open for UP, below for
    DOWN. A print at the open is neither."""
    if direction is Direction.UP:
        return last_price > session_open
    if direction is Direction.DOWN:
        return last_price < session_open
    return False


def level_broken(direction: Direction, level: Decimal, last_price: Decimal, f3: F3Config) -> bool:
    """The index beyond the level by ``f3_level_buffer_pct`` — from a print, not a close."""
    buffer = level * f3.level_buffer_pct / _HUNDRED
    if direction is Direction.UP:
        return last_price < level - buffer
    if direction is Direction.DOWN:
        return last_price > level + buffer
    return False


# ================================================================================================
# The spread
# ================================================================================================


@dataclass(frozen=True, slots=True)
class SpreadStrikes:
    option_type: OptionType
    short: Decimal
    wing: Decimal

    @property
    def width(self) -> Decimal:
        return abs(self.short - self.wing)


def _to_step(value: Decimal, step: Decimal, *, up: bool) -> Decimal:
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    return (value / step).to_integral_value(rounding=rounding) * step


def spread_strikes(direction: Direction, lv: Levels, step: Decimal, f3: F3Config) -> SpreadStrikes:
    """UP: a put ``f3_distance_pct`` below the weekly low, rounded down; DOWN: a call that far
    above the weekly high, rounded up. The wing ``f3_wing_pct`` of the close further out, rounded
    away too, and never on the short's strike."""
    if direction is Direction.NONE:
        raise ValueError("no spread without a direction")
    if step <= 0:
        raise ValueError("strike step must be positive")
    distance = f3.distance_pct / _HUNDRED
    wing_gap = lv.close * f3.wing_pct / _HUNDRED
    if direction is Direction.UP:
        short = _to_step(lv.weekly_low * (1 - distance), step, up=False)
        wing = min(_to_step(short - wing_gap, step, up=False), short - step)
        return SpreadStrikes(OptionType.PE, short, wing)
    short = _to_step(lv.weekly_high * (1 + distance), step, up=True)
    wing = max(_to_step(short + wing_gap, step, up=True), short + step)
    return SpreadStrikes(OptionType.CE, short, wing)


def choose_expiry(
    expiries: Iterable[dt.date],
    sessions: Sequence[dt.date],
    entry: dt.date,
    min_sessions: int,
) -> dt.date | None:
    """The nearest listed expiry with at least ``min_sessions`` sessions after ``entry``.

    F3N hands the weekly list with ``f3_min_sessions_weekly``; F3B the monthly list with
    ``f3_min_sessions_monthly``. ``None`` when nothing listed qualifies.
    """
    for expiry in sorted(set(expiries)):
        if expiry < entry:
            continue
        left = sessions_between(sessions, entry, expiry)
        if left is not None and left >= min_sessions:
            return expiry
    return None


def credit_per_unit(short_price: Decimal, wing_price: Decimal) -> Decimal:
    """What the spread collects per unit: the short's price less the wing's."""
    return short_price - wing_price


def max_loss_per_unit(strikes: SpreadStrikes, credit: Decimal) -> Decimal:
    """The width less the credit; the exchange's spread margin is the same number."""
    return strikes.width - credit


@dataclass(frozen=True, slots=True)
class SpreadProposal:
    """The scan's or the monitor's proposal for one underlying. ``state`` is ``None`` when
    clean, else the first refusal in the order structure → cost → size."""

    state: PlanState | None
    reasons: tuple[str, ...]
    strikes: SpreadStrikes
    short_price: Decimal
    wing_price: Decimal
    credit: Decimal | None
    max_loss_per_unit: Decimal | None
    max_loss_per_lot_inr: Decimal | None
    sizing: FoSizing | None


def size_entry(  # noqa: PLR0913 - every input 04 §11 names, by keyword
    *,
    mode: Mode,
    capital_inr: Decimal,
    max_loss_per_unit: Decimal,
    lot_size: int | None,
    f3: F3Config,
    common: CommonConfig,
    ceilings: FnoCeilings,
) -> FoSizing:
    """``04`` §11 "Sizing": the budget is the smaller of the entry share and §3's risk budget
    (so the ₹ ceiling and ``fo_max_lots`` still bind), spent on max loss per lot."""
    return size(
        mode=mode,
        capital_inr=capital_inr,
        risk_pct=min(f3.entry_share_pct, f3.risk_per_trade_pct),
        risk_per_unit=max_loss_per_unit,
        lot_size=lot_size,
        common=common,
        ceilings=ceilings,
    )


def propose_spread(  # noqa: PLR0913 - every input 04 §11 names, by keyword
    *,
    mode: Mode,
    direction: Direction,
    lv: Levels,
    step: Decimal,
    prices: Mapping[tuple[Decimal, OptionType], Decimal],
    lot_size: int | None,
    capital_inr: Decimal,
    f3: F3Config,
    common: CommonConfig,
    ceilings: FnoCeilings,
) -> SpreadProposal:
    """Strikes, credit, max loss and lots from the listed prices (settles at night, mids in the
    window). A leg without a price is ``REJECTED_LIQUIDITY``; a credit that is not positive or a
    short below ``f3_short_premium_min_inr`` is ``REJECTED_COST``; no lots is ``REJECTED_SIZE``."""
    strikes = spread_strikes(direction, lv, step, f3)
    short_price = prices.get((strikes.short, strikes.option_type))
    wing_price = prices.get((strikes.wing, strikes.option_type))
    reasons: list[str] = []
    if short_price is None or short_price <= 0:
        reasons.append(f"short {strikes.option_type} {strikes.short}: no price")
    if wing_price is None or wing_price <= 0:
        reasons.append(f"wing {strikes.option_type} {strikes.wing}: no price")
    if reasons:
        return SpreadProposal(
            PlanState.REJECTED_LIQUIDITY,
            tuple(reasons),
            strikes,
            short_price or Decimal(0),
            wing_price or Decimal(0),
            None,
            None,
            None,
            None,
        )
    assert short_price is not None and wing_price is not None
    credit = credit_per_unit(short_price, wing_price)
    if short_price < f3.short_premium_min_inr:
        reasons.append(
            f"short premium {short_price} < ₹{f3.short_premium_min_inr}: not worth the round trip"
        )
    if credit <= 0:
        reasons.append(f"credit {credit} is not positive")
    if reasons:
        return SpreadProposal(
            PlanState.REJECTED_COST,
            tuple(reasons),
            strikes,
            short_price,
            wing_price,
            credit,
            None,
            None,
            None,
        )
    loss_unit = max_loss_per_unit(strikes, credit)
    sizing = size_entry(
        mode=mode,
        capital_inr=capital_inr,
        max_loss_per_unit=loss_unit,
        lot_size=lot_size,
        f3=f3,
        common=common,
        ceilings=ceilings,
    )
    per_lot = loss_unit * lot_size if lot_size else None
    return SpreadProposal(
        sizing.state,
        (sizing.message,) if sizing.state else (),
        strikes,
        short_price,
        wing_price,
        credit,
        loss_unit,
        per_lot,
        sizing,
    )


# ================================================================================================
# Add and exit
# ================================================================================================


@dataclass(frozen=True, slots=True)
class OpenSpread:
    """What the monitor knows of an open F3 position."""

    direction: Direction
    level: Decimal
    strikes: SpreadStrikes
    expiry: dt.date
    entry_session: dt.date
    entry_credit: Decimal
    lots: int
    lot_size: int
    max_loss_per_lot_inr: Decimal


@dataclass(frozen=True, slots=True)
class AddDecision:
    lots: int
    message: str

    @property
    def allowed(self) -> bool:
        return self.lots > 0


def add_decision(  # noqa: PLR0913, PLR0911 - every input 04 §11 names; one return per refusal
    *,
    position: OpenSpread,
    today: dt.date,
    direction_now: Direction,
    level_intact: bool,
    mark: Decimal | None,
    capital_inr: Decimal,
    f3: F3Config,
    common: CommonConfig,
) -> AddDecision:
    """``04`` §11 "Add": the next session or later; the mark decayed ``f3_add_working_pct`` of the
    credit; the direction and level unchanged; the same lots again, stopping at the full share.
    Paper at ₹0 allows one add of one lot, so the pyramid is exercised on paper too."""
    if today <= position.entry_session:
        return AddDecision(0, "an add waits for the next session")
    if direction_now is not position.direction:
        return AddDecision(0, f"the direction now reads {direction_now}, not {position.direction}")
    if not level_intact:
        return AddDecision(0, "the level is broken; this is an exit, not an add")
    if mark is None:
        return AddDecision(0, "no mark for the spread")
    working_at = position.entry_credit * (1 - f3.add_working_pct / _HUNDRED)
    if mark > working_at:
        return AddDecision(
            0,
            f"mark {mark} has not decayed to {_money(working_at)} "
            f"({f3.add_working_pct} % of the credit)",
        )
    cap = min(common.max_lots, common.max_lots_ceiling)
    if capital_inr <= 0:
        wanted = 1 if position.lots < 2 else 0  # noqa: PLR2004 - paper: one entry, one add
        if wanted == 0 or position.lots + wanted > cap:
            return AddDecision(0, "paper at ₹0: one add of one lot, already taken")
        return AddDecision(1, "paper at ₹0: one add of one lot")
    full = capital_inr * f3.full_share_pct / _HUNDRED
    room = full - position.lots * position.max_loss_per_lot_inr
    affordable = int((room / position.max_loss_per_lot_inr).to_integral_value(rounding=ROUND_FLOOR))
    lots = min(position.lots, affordable, cap - position.lots)
    if lots <= 0:
        return AddDecision(
            0, f"the full share ₹{_money(full)} is reached with {position.lots} lots open"
        )
    return AddDecision(lots, f"add {lots} lots; room ₹{_money(room)} under the full share")


@dataclass(frozen=True, slots=True)
class F3Exit:
    reason: F3ExitReason
    message: str


def decay_target_mark(entry_credit: Decimal, f3: F3Config) -> Decimal:
    return _money(entry_credit * (1 - f3.decay_target_pct / _HUNDRED))


def loss_cut_mark(entry_credit: Decimal, f3: F3Config) -> Decimal:
    return _money(entry_credit * f3.loss_cut_mult)


def f3_exit(  # noqa: PLR0913 - every input 04 §11 names, by keyword
    *,
    now: dt.datetime,
    position: OpenSpread,
    last_price: Decimal | None,
    mark: Decimal | None,
    f3: F3Config,
    common: CommonConfig,
) -> F3Exit | None:
    """The exit due at ``now``, in ``04`` §11's precedence, or ``None`` to hold.

    ``last_price`` is the index's latest print (the level check), ``mark`` the spread's cost to
    close per unit (the cut and the target); either may be ``None`` without a quote. The hard
    exit is on the expiry day at ``fo_hard_exit_time``, and a position found past its expiry is
    a hard exit too (the desk was down).
    """
    if last_price is not None and level_broken(position.direction, position.level, last_price, f3):
        return F3Exit(
            F3ExitReason.LEVEL_BREAK,
            f"index {last_price} beyond the level {position.level} by more than "
            f"{f3.level_buffer_pct} %",
        )
    if mark is not None:
        cut = loss_cut_mark(position.entry_credit, f3)
        if mark >= cut:
            return F3Exit(
                F3ExitReason.LOSS_CUT,
                f"mark {mark} ≥ {f3.loss_cut_mult} x credit {position.entry_credit} ({cut})",
            )
        target = decay_target_mark(position.entry_credit, f3)
        if mark <= target:
            return F3Exit(
                F3ExitReason.DECAY_TARGET,
                f"mark {mark} ≤ {target}: {f3.decay_target_pct} % of the credit decayed",
            )
    today = now.date()
    if today > position.expiry or (
        today == position.expiry and now.time() >= common.hard_exit_time
    ):
        return F3Exit(
            F3ExitReason.HARD_EXIT,
            f"expiry {position.expiry.isoformat()} at {common.hard_exit_time.strftime('%H:%M')}",
        )
    return None


def pnl_inr(entry_credit: Decimal, mark: Decimal, quantity: int) -> Decimal:
    """Credit received less the cost to close, times the units."""
    return _money((entry_credit - mark) * quantity)
