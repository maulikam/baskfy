"""The opening range and the live trigger (docs/swing/04 §7).

His entry is the opening-range high (ORH) break: the first 1-, 5- or 60-minute candle's high,
bought when price takes it out, stop at the candle's low or the low of the day. On NSE the
session opens at 09:15 IST and a 5-minute range closes at 09:20.

Pure. The monitor (desk process) feeds this module minute candles or quote snapshots and gets
back a verdict; it never decides anything itself. The circuit filter is the NSE twist: a name
locked at its upper band has no seller, so a "break" there is not a fill and the verdict says
so instead of pretending.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from baskfy_core.swing.config import OpeningRangeConfig

_PCT = Decimal(100)
_ZERO = Decimal(0)
#: Minutes in a full NSE cash session, 09:15-15:30.
SESSION_MINUTES: Final = 375


@dataclass(frozen=True, slots=True)
class Candle:
    """One intraday bar. ``start`` is the candle's opening timestamp, exchange-local."""

    start: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True, slots=True)
class OpeningRange:
    high: Decimal
    low: Decimal
    window_minutes: int
    #: False while the window has not closed yet — a range is not a range until it is.
    complete: bool
    candles: int


class TriggerState(StrEnum):
    TRIGGERED = "TRIGGERED"
    WAITING = "WAITING"
    RANGE_INCOMPLETE = "RANGE_INCOMPLETE"
    BELOW_PIVOT = "BELOW_PIVOT"
    LOCKED_UPPER_CIRCUIT = "LOCKED_UPPER_CIRCUIT"
    SESSION_OVER = "SESSION_OVER"


@dataclass(frozen=True, slots=True)
class TriggerVerdict:
    state: TriggerState
    entry: Decimal | None
    stop: Decimal | None
    range_high: Decimal | None
    range_low: Decimal | None


def _session_open(day: dt.date, config: OpeningRangeConfig) -> dt.datetime:
    hour, minute = config.session_open
    return dt.datetime.combine(day, dt.time(hour, minute))


def opening_range(
    candles: Sequence[Candle], *, day: dt.date, window_minutes: int, config: OpeningRangeConfig
) -> OpeningRange:
    """The high/low of the first ``window_minutes`` from the session open.

    Candles outside ``[open, open + window)`` are ignored, so the caller may pass the whole
    morning. ``complete`` is True once a candle at or after the window's end exists, which is
    the only evidence that the window closed — a clock is not consulted (law 1).
    """
    if window_minutes not in config.windows_minutes:
        raise ValueError(
            f"window {window_minutes} is not one of {config.windows_minutes} (docs/swing/04 §7)"
        )
    start = _session_open(day, config)
    end = start + dt.timedelta(minutes=window_minutes)
    inside = [c for c in candles if start <= c.start < end]
    complete = any(c.start >= end for c in candles)
    if not inside:
        return OpeningRange(_ZERO, _ZERO, window_minutes, complete=False, candles=0)
    return OpeningRange(
        high=max(c.high for c in inside),
        low=min(c.low for c in inside),
        window_minutes=window_minutes,
        complete=complete,
        candles=len(inside),
    )


def evaluate_trigger(  # noqa: PLR0913 - one keyword per input the verdict depends on
    *,
    last_price: Decimal,
    opening: OpeningRange,
    pivot_high: Decimal | None,
    low_of_day: Decimal,
    upper_circuit: Decimal | None,
    at: dt.datetime,
    config: OpeningRangeConfig,
) -> TriggerVerdict:
    """Has the ORH break happened, and if so at what entry and stop?

    * The range must be complete.
    * ``last_price`` must exceed the range high by ``break_buffer_pct``.
    * For a FLAG the price must also be above the daily pivot (an ORH break inside the base is
      not a breakout); pass ``pivot_high=None`` for an EP, whose pivot is the gap itself.
    * A last price at or above the upper circuit is a lock, not a trigger.
    * After ``monitor_close`` the answer is SESSION_OVER — he trades the first 60-90 minutes.

    The stop is the lower of the range low and the low of the day so far, never above the
    range low: a stop above the opening range is a stop inside the noise.
    """
    hour, minute = config.monitor_close
    if at.time() > dt.time(hour, minute):
        return TriggerVerdict(TriggerState.SESSION_OVER, None, None, None, None)
    if not opening.complete or opening.candles == 0:
        return TriggerVerdict(TriggerState.RANGE_INCOMPLETE, None, None, None, None)
    if upper_circuit is not None and upper_circuit > _ZERO and last_price >= upper_circuit:
        return TriggerVerdict(
            TriggerState.LOCKED_UPPER_CIRCUIT, None, None, opening.high, opening.low
        )
    buffer = opening.high * Decimal(str(config.break_buffer_pct)) / _PCT
    if last_price <= opening.high + buffer:
        return TriggerVerdict(TriggerState.WAITING, None, None, opening.high, opening.low)
    if pivot_high is not None and last_price <= pivot_high:
        return TriggerVerdict(TriggerState.BELOW_PIVOT, None, None, opening.high, opening.low)
    stop = min(opening.low, low_of_day)
    return TriggerVerdict(TriggerState.TRIGGERED, last_price, stop, opening.high, opening.low)


@dataclass(frozen=True, slots=True)
class LiveGapVerdict:
    is_candidate: bool
    gap_pct: Decimal
    volume_pace: Decimal


def live_gap(  # noqa: PLR0913 - one keyword per input the verdict depends on
    *,
    prev_close: Decimal,
    last_price: Decimal,
    volume_so_far: int,
    avg_daily_volume: Decimal,
    minutes_elapsed: int,
    config: OpeningRangeConfig,
) -> LiveGapVerdict:
    """Is this an EP at the open? Gap ≥ ``live_min_gap_pct`` and volume running at
    ``live_min_volume_pace`` times an average day's, pro-rated to the minutes elapsed."""
    if prev_close <= _ZERO or avg_daily_volume <= _ZERO or minutes_elapsed <= 0:
        return LiveGapVerdict(False, _ZERO, _ZERO)
    gap = ((last_price / prev_close) - 1) * _PCT
    expected = avg_daily_volume * Decimal(minutes_elapsed) / Decimal(SESSION_MINUTES)
    ratio = Decimal(volume_so_far) / expected if expected > _ZERO else _ZERO
    ok = gap >= Decimal(str(config.live_min_gap_pct)) and ratio >= Decimal(
        str(config.live_min_volume_pace)
    )
    return LiveGapVerdict(ok, gap.quantize(Decimal("0.01")), ratio.quantize(Decimal("0.01")))
