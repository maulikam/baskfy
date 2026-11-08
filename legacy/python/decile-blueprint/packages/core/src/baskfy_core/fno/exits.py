"""Exits and pauses (``04`` §1, §7, §10).

**F1**: ``LATE_EXIT`` when the hard-exit date has passed (a rule violation, exited at the next
open), the loss close, the profit take, and the hard exit at 15:00 on ``E - n``. The cost to
close is valued by ``condor.cost_to_close`` (clamped, so never beyond max loss).

**F2**: initial stop ``entry - stop_atr x ATR14`` (ATR at the signal close); the trail
``max(stop, highest close since entry - stop_atr x ATR14 at entry)``, **never lowered**; the GTT
triggers at ``max(stop, stop_from_vol(entry, ann_vol))`` — for a long the higher price is the
tighter stop; the time exit at 15:00 on the ``f2_max_sessions``-th session of the hold (the
entry session is the first, as ``res_f2.py`` counted it); the roll at 15:00 on ``E - n``.

``stop_from_vol`` is the desk's formula, re-implemented pure (law 1 forbids importing the desk):
``baskfy_core.score.stop_from_vol`` — ``pct = clip(ann_vol ÷ √52 x STOP_VOL_MULT, STOP_MIN,
STOP_MAX)``, ``round(price x (1 - pct), 1)`` — called by ``kite-momentum-rebalancer/app/
scoring.py`` with ``app/config.py``'s ``0.08, 0.12, 2.2``. ``test_fno_exits.py`` checks the two
agree.

**Pauses** (``04`` §7) stop new entries only; open structures run to their own exits.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from baskfy_core.fno.condor import CondorStrikes, loss_close_hit, profit_take_hit
from baskfy_core.fno.config import CommonConfig, F1Config, F2Config, StopVolConfig

_WEEKS_PER_YEAR = 52


class ExitReason(StrEnum):
    LATE_EXIT = "LATE_EXIT"
    LOSS_CLOSE = "LOSS_CLOSE"
    PROFIT_TAKE = "PROFIT_TAKE"
    HARD_EXIT = "HARD_EXIT"
    STOP = "STOP"
    TIME_EXIT = "TIME_EXIT"
    ROLL = "ROLL"
    #: F2's future without its resting GTT: an alert, a paper violation, and the exit at the next
    #: check (``04`` §10; FO7).
    NAKED_FUTURE = "NAKED_FUTURE"
    #: F3's (``04`` §11, M.5), in precedence; ``HARD_EXIT`` above is shared.
    LEVEL_BREAK = "LEVEL_BREAK"
    LOSS_CUT = "LOSS_CUT"
    DECAY_TARGET = "DECAY_TARGET"


@dataclass(frozen=True, slots=True)
class ExitDecision:
    reason: ExitReason
    message: str


def f1_exit(  # noqa: PLR0913 - every input 04 §1 names, by keyword
    *,
    now: dt.datetime,
    hard_exit_date: dt.date,
    close_cost: Decimal | None,
    entry_credit: Decimal,
    strikes: CondorStrikes,
    f1: F1Config,
    common: CommonConfig,
) -> ExitDecision | None:
    """The F1 exit due at ``now``, or ``None``. ``close_cost`` is ``None`` without marks."""
    today = now.date()
    if today > hard_exit_date:
        return ExitDecision(
            ExitReason.LATE_EXIT,
            f"hard-exit date {hard_exit_date.isoformat()} passed while the desk was down",
        )
    if close_cost is not None:
        if loss_close_hit(close_cost, entry_credit, strikes, f1):
            return ExitDecision(
                ExitReason.LOSS_CLOSE,
                f"cost to close {close_cost} ≥ (1 + {f1.loss_close_mult}) x credit {entry_credit}",
            )
        if profit_take_hit(close_cost, entry_credit, f1):
            return ExitDecision(
                ExitReason.PROFIT_TAKE,
                f"cost to close {close_cost} ≤ {100 - f1.profit_take_pct} % of {entry_credit}",
            )
    if today == hard_exit_date and now.time() >= common.hard_exit_time:
        return ExitDecision(ExitReason.HARD_EXIT, "15:00 on E - n; never into expiry day")
    return None


def stop_from_vol(price: Decimal, ann_vol: float, config: StopVolConfig) -> Decimal:
    """The desk's vol-scaled stop, 8-12 % below ``price`` (see the module docstring)."""
    raw = ann_vol / math.sqrt(_WEEKS_PER_YEAR) * config.vol_mult
    pct = min(max(raw, config.stop_min), config.stop_max)
    return Decimal(repr(round(float(price) * (1 - pct), 1)))


def f2_initial_stop(entry: Decimal, atr_at_signal: Decimal, f2: F2Config) -> Decimal:
    """``entry - f2_stop_atr x ATR14`` (``04`` §10)."""
    return entry - f2.stop_atr * atr_at_signal


def f2_trail(stop: Decimal, highest_close: Decimal, atr_at_entry: Decimal, f2: F2Config) -> Decimal:
    """Each close: ``max(stop, highest close - mult x ATR at entry)``. Never lowered; with
    ``f2_trail`` off the stop stays where it is."""
    if not f2.trail:
        return stop
    return max(stop, highest_close - f2.stop_atr * atr_at_entry)


def gtt_trigger(stop: Decimal, entry: Decimal, ann_vol: float, config: StopVolConfig) -> Decimal:
    """``max(stop, stop_from_vol(entry, ann_vol))`` — the tighter of the two for a long."""
    return max(stop, stop_from_vol(entry, ann_vol, config))


def f2_time_exit_session(
    sessions: Sequence[dt.date], entry_session: dt.date, f2: F2Config
) -> dt.date | None:
    """The ``f2_max_sessions``-th session of the hold, the entry session counted as the first."""
    try:
        i = list(sessions).index(entry_session)
    except ValueError:
        return None
    j = i + f2.max_sessions - 1
    return sessions[j] if j < len(sessions) else None


def f2_exit(  # noqa: PLR0913 - every input 04 §10 names, by keyword
    *,
    now: dt.datetime,
    price: Decimal,
    stop: Decimal,
    time_exit_date: dt.date | None,
    roll_date: dt.date | None,
    common: CommonConfig,
) -> ExitDecision | None:
    """The F2 action due at ``now``: the stop first, then the time exit, then the roll."""
    if price <= stop:
        return ExitDecision(ExitReason.STOP, f"price {price} ≤ stop {stop}")
    at_close = now.time() >= common.hard_exit_time
    if time_exit_date is not None and (
        now.date() > time_exit_date or (now.date() == time_exit_date and at_close)
    ):
        return ExitDecision(ExitReason.TIME_EXIT, f"session limit reached {time_exit_date}")
    if roll_date is not None and now.date() == roll_date and at_close:
        return ExitDecision(ExitReason.ROLL, "15:00 on E - n: sell the held month, buy the next")
    return None


def f1_paused(closed_r: Iterable[Decimal], f1: F1Config) -> bool:
    """``04`` §7: the last ``pause_consecutive`` (3) closed F1 trades on one underlying were
    each ≤ ``pause_loss_r`` (-0.6R). ``closed_r`` is in close order, oldest first."""
    rows = list(closed_r)
    tail = rows[-f1.pause_consecutive :]
    return len(tail) == f1.pause_consecutive and all(r <= f1.pause_loss_r for r in tail)


def f2_month_paused(month_closed_r: Iterable[Decimal], f2: F2Config) -> bool:
    """``04`` §7: the calendar month's closed F2 trades sum to ≤ ``month_pause_r`` (-6R)."""
    return sum(month_closed_r, Decimal(0)) <= f2.month_pause_r


def book_paused(month_realised_inr: Decimal, monthly_pause_inr: Decimal) -> bool:
    """``04`` §7: the month's realised FO loss has reached ``monthly_pause_inr``."""
    return month_realised_inr <= -monthly_pause_inr
