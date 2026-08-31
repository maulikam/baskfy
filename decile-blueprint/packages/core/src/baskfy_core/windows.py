"""Calendar-offset factor windows (Prompt 5 deliverable 1).

docs/05 §Notation, CORRECTED 2026-08-31 (M11 — the anniversary day is the base, not a
member of the window):

    anniversary = as_of - relativedelta(months=K)
    window      = all trading days in (anniversary, as_of]   # K in {1, 3, 6, 9, 12}
    start       = the first trading day STRICTLY AFTER the anniversary

docs/05 §Notation used to say ``snap_forward_to_trading_day(anniversary)`` — the first trading
day *on or after* the anniversary — which differs by exactly one bar whenever the anniversary
itself is a trading day, and agrees otherwise. That prose contradicted the table immediately
below it in the same section, and the table is the empirically recovered ground truth. Measured
against the exchange calendar in ``ohlcv_daily`` for as-of 2026-08-18 (docs/PARITY-M11.md):

    K     (anniversary, as_of]   [anniversary, as_of]   docs/13 §3 recovered
    1M    22                     22                     22
    3M    64                     65                     64
    6M    121                    122                    121
    9M    185                    186                    185
    12M   247                    248                    247

The half-open interval reproduces all five; the closed one reproduces one. 1M agrees only by
accident: 2026-07-18 was a Saturday, which is why every ``*_one_month`` column was the single
family that reproduced before this was fixed.

**Not** fixed bar counts. docs/13 §3 recovered this empirically: ``positive_days_percent`` is a
ratio ``k/N``, so the denominator can be solved for, and only one minimal ``N`` fits each
window — 22 / 64 / 121 / 185 / 247 as of 2026-08-18. Critically the *same* ``N`` fits every one
of the 271 rows, which can only happen if the window start is a shared calendar date rather than
a per-instrument bar count.

The consequence, quoted from docs/05:

    "An instrument without a full window of history gets NULL for that window and is excluded —
     never computed on a short window, because that would break the shared-denominator property."

Counting convention
-------------------
A window of ``N`` trading days spans ``N`` bars, and yields ``N`` daily returns: the return on the
first day of the window is measured against the last bar *before* it. That is what makes
``pos_days_N`` a multiple of ``1/N`` — the property docs/13 §3 used to recover the lengths — and
it is also what docs/05 §1 means by ``ret_N = P_t / P_{t-N} - 1``: ``P_{t-N}`` is the bar
immediately preceding the window, so a return window needs ``N+1`` bars.
"""

from __future__ import annotations

import bisect
import datetime as dt
from dataclasses import dataclass
from typing import Final

#: The five windows every factor family is computed over (docs/01 §3, docs/05).
WINDOW_MONTHS: Final[tuple[int, ...]] = (1, 3, 6, 9, 12)

#: docs/05 and docs/13 §3 — the lengths these offsets resolve to for as-of 2026-08-18.
#: Derived, not constants: they are asserted against the calendar, never used in place of it.
EXPECTED_WINDOW_LENGTHS_2026_08_18: Final[dict[int, int]] = {
    1: 22,
    3: 64,
    6: 121,
    9: 185,
    12: 247,
}

#: docs/05 §6 — beta needs "≥ 200 overlapping observations else NULL".
MIN_BETA_OBSERVATIONS: Final = 200

#: docs/05 §9 — simple moving-average lengths, in bars.
MA_LENGTHS: Final[tuple[int, ...]] = (20, 50, 100, 200)

#: docs/05 §10 — "max(P over last 252 bars)". A bar count, not a calendar window: the doc says
#: 252 bars explicitly, and the export's `high_one_year` is consistent with it.
HIGH_1Y_BARS: Final = 252

#: docs/05 §13 — the rolling volume windows, in bars.
VOL_AVG_BARS: Final[dict[str, int]] = {
    "1w": 5,
    "1m": 21,
    "3m": 63,
    "6m": 126,
    "9m": 189,
    "12m": 252,
}
MEDIAN_VOL_BARS: Final = 252

#: docs/05 §15 — the regime classifier's sample length.
REGIME_WINDOW_BARS: Final = 63


def subtract_months(day: dt.date, months: int) -> dt.date:
    """``relativedelta(months=K)`` without pulling in dateutil.

    Clamps to the last day of the target month, which is what ``relativedelta`` does: 31 March
    minus one month is 28 (or 29) February, not 3 March. Getting that wrong would shift a window
    start by up to three days and silently change every 1-month factor at month ends.
    """
    if months < 0:
        raise ValueError(f"months must not be negative; got {months}")
    total = (day.year * 12 + day.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    last_day = _days_in_month(year, month)
    return dt.date(year, month, min(day.day, last_day))


def _days_in_month(year: int, month: int) -> int:
    if month == 12:  # noqa: PLR2004 - December rolls into the next year
        return 31
    return (dt.date(year, month + 1, 1) - dt.timedelta(days=1)).day


@dataclass(frozen=True, slots=True)
class FactorWindow:
    """One resolved window: its calendar start, its snapped start, and its trading days."""

    months: int
    as_of: dt.date
    #: ``as_of - relativedelta(months=K)`` — the anniversary. The window's *base* date: the
    #: last bar at or before it supplies ``P_{t-N}``, and it is itself excluded from the window.
    calendar_start: dt.date
    #: The first trading day **strictly after** ``calendar_start`` (docs/05 §Notation as
    #: corrected by M11; see this module's docstring for the measurement).
    start: dt.date
    #: Every trading day in ``(calendar_start, as_of]``, ascending.
    trading_days: tuple[dt.date, ...]
    #: The first day of the calendar this window was resolved against. Not the window's own
    #: start: it is how the window knows whether the calendar reached back far enough.
    calendar_first: dt.date

    @property
    def spans_full_window(self) -> bool:
        """Did the supplied calendar actually reach back to the calendar offset?

        docs/05 §Notation: a factor is "never computed on a short window, because that would
        break the shared-denominator property". Until 2026-08-22 that guarantee was held up by
        an off-by-one rather than by a check — the return base sat one bar *before* the window,
        so a calendar with exactly N days produced a null by running out of bars. Correcting the
        base to the window's first bar (docs/05 §1) removed the accident, which is how a golden
        test caught it. This is the check that was missing.
        """
        return self.calendar_first <= self.calendar_start

    @property
    def length(self) -> int:
        """``N`` — the shared denominator docs/13 §3 recovered."""
        return len(self.trading_days)

    @property
    def key(self) -> str:
        """The suffix used throughout the schema: ``12m``, ``6m``, ..."""
        return f"{self.months}m"


def resolve_window(
    as_of: dt.date, months: int, trading_days: list[dt.date] | tuple[dt.date, ...]
) -> FactorWindow:
    """Resolve one calendar-offset window against a trading calendar.

    ``trading_days`` must be sorted ascending and must contain ``as_of``; a window that ends on a
    day the exchange was closed is not a window, it is a bug upstream.
    """
    ordered = list(trading_days)
    if not ordered:
        raise ValueError("the trading calendar is empty")
    if as_of not in ordered:
        raise ValueError(f"{as_of.isoformat()} is not a trading day in the supplied calendar")

    calendar_start = subtract_months(as_of, months)
    # STRICTLY AFTER the anniversary: docs/05 §Notation as corrected by M11. ``bisect_right``,
    # not ``bisect_left`` — the two differ only when the anniversary is itself a trading day,
    # and on that day ``bisect_left`` admits the anniversary into the window and inflates N by
    # one. Four of the five windows for as-of 2026-08-18 land on a trading-day anniversary; only
    # 1M does not (2026-07-18 was a Saturday), which is why 1M was the one family that already
    # reproduced. See this module's docstring for the measured table.
    index = bisect.bisect_right(ordered, calendar_start)
    if index >= len(ordered):
        raise ValueError(
            f"no trading day after {calendar_start.isoformat()} in the supplied calendar"
        )
    start = ordered[index]
    end_index = bisect.bisect_right(ordered, as_of)
    span = tuple(ordered[index:end_index])
    return FactorWindow(months, as_of, calendar_start, start, span, ordered[0])


def resolve_windows(
    as_of: dt.date,
    trading_days: list[dt.date] | tuple[dt.date, ...],
    months: tuple[int, ...] = WINDOW_MONTHS,
) -> dict[int, FactorWindow]:
    """All five windows for one as-of date."""
    return {k: resolve_window(as_of, k, trading_days) for k in months}


def window_lengths(
    as_of: dt.date,
    trading_days: list[dt.date] | tuple[dt.date, ...],
    months: tuple[int, ...] = WINDOW_MONTHS,
) -> dict[int, int]:
    """``{months: N}`` — what docs/13 §3 recovered as 22 / 64 / 121 / 185 / 247."""
    return {k: w.length for k, w in resolve_windows(as_of, trading_days, months).items()}


def bars_required(window: FactorWindow) -> int:
    """How many bars an instrument needs to have a value for this window.

    ``N + 1``: ``N`` bars inside the window plus the one immediately before it, which supplies
    ``P_{t-N}`` for the return and the first daily return for volatility and positive days.
    """
    return window.length + 1
