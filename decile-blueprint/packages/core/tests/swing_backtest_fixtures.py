"""Synthetic frames for the swing backtest (SW9): one planted flag whose R is known to 2 dp.

The technique is ``swing_fixtures``': draw the shapes the method describes on purpose. The flag
itself is ``swing_fixtures.flag_series`` — the textbook base the detector tests already prove
is ``SETTING_UP`` on its last bar — re-dated onto a weekday calendar and followed by a scripted
tail whose every open, high, low and close is a constant of this module, so the expected trade
can be worked out by hand from `04` §5, §6, §10 and §11 (it is, in :data:`PLANTED`).

**Re-planted at SW9.5** for `04` §6's widest stop — one ADR, never more. ``flag_series`` draws
every bar ±2% around its close, an ADR of 4.08%, and the planted stop (the detection day's low,
6.67% under the entry) would be refused ``STOP_TOO_WIDE`` on such a name. The method's stop is
tight *relative to the stock's own range*, so the fixture is a leader with a leader's range:
:func:`_leaders_range` sets every low **before** the detection bar at ``LOW_FACTOR`` (7%)
under its close, and leaves the detection bar itself the ±2% bar it was — the tight last bar of
a contracting base, which is what makes its low a usable stop. The 20-bar ADR on the detection
day is then ``(19 x (1.02 / 0.93 - 1) + (1.02 / 0.98 - 1)) / 20 x 100`` = **9.40%**
(:data:`PLANTED_ADR_PCT`), the stop is inside it, and — because 9.40% is above the 6% fast-trail
line — the position trails the **10-day** MA. The pivot (a high) and the stop (the detection
day's low) are exactly where they were, so the hand-worked trade in :data:`PLANTED` is unchanged.

Two other names sit beside the flag so the tape is not empty: a steadily rising *tape* name that
keeps breadth above the GREEN threshold (a universe of one flat name reads RED and no entry is
ever allowed) and ``FLATCO``, which never sets up. Neither triggers anything.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import polars as pl
from swing_fixtures import ep_series, flag_series, flat_series

START = dt.date(2025, 10, 1)
FLAG_BARS = 140
#: Every low before a planted name's detection bar sits this far under its close (see the module
#: docstring). ``0.93`` with ``swing_fixtures``' ±2% highs is a 9.68% daily range.
LOW_FACTOR = 0.93
#: The planted flag's 20-bar ADR on its detection day: nineteen leader's-range bars and the one
#: tight bar, worked by hand in the module docstring.
PLANTED_ADR_PCT = 9.40
#: ``flag_series`` is SETTING_UP on its last bar; the day after it is the entry day.
DETECTION_BAR = FLAG_BARS - 1
ENTRY_BAR = FLAG_BARS


def weekdays(start: dt.date, n: int) -> list[dt.date]:
    """``n`` weekdays from ``start`` — the calendar the run is handed."""
    out: list[dt.date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


@dataclass(frozen=True, slots=True)
class Bar:
    open: float
    high: float
    low: float
    close: float


#: The scripted tail after the detection bar, entry day first. Prices are chosen so that:
#: day 0 opens above any plausible pivot (entry at the open); days 1-2 hold; day 3 closes above
#: the entry (`04` §6.4.4: the partial fires at the next open and the stop rises to breakeven);
#: day 4 opens at 162 (the partial's fill) and holds; day 5 trades down through the breakeven
#: stop with its open still above it (the GTT fills at the stop, `04` §6.4.1); the rest are
#: uneventful sessions that keep the calendar going.
WIN_TAIL: tuple[Bar, ...] = (
    Bar(152.0, 155.0, 150.0, 154.0),
    Bar(154.0, 157.0, 152.0, 156.0),
    Bar(156.0, 159.0, 154.0, 158.0),
    Bar(158.0, 161.0, 156.0, 160.0),
    Bar(162.0, 164.0, 158.0, 163.0),
    Bar(153.0, 154.0, 149.0, 150.0),
    Bar(150.0, 152.0, 148.0, 151.0),
    Bar(151.0, 153.0, 149.0, 150.0),
    Bar(150.0, 152.0, 148.0, 151.0),
    Bar(151.0, 153.0, 149.0, 150.0),
    Bar(150.0, 152.0, 148.0, 151.0),
    Bar(151.0, 153.0, 149.0, 150.0),
    Bar(150.0, 152.0, 148.0, 151.0),
)

#: A losing tail: entered at the same open, closes below the entry for seven sessions (no partial,
#: no breakeven), then gaps down through the initial stop on day 8 — a fill at the open, not at
#: the stop (DECISIONS-SW SW9.3).
LOSE_TAIL: tuple[Bar, ...] = (
    Bar(152.0, 155.0, 150.0, 151.0),
    Bar(151.0, 153.0, 149.0, 150.5),
    Bar(150.5, 152.0, 148.0, 151.0),
    Bar(151.0, 153.0, 149.0, 150.0),
    Bar(150.0, 152.0, 148.0, 151.0),
    Bar(151.0, 153.0, 149.0, 150.0),
    Bar(150.0, 152.0, 148.0, 151.0),
    Bar(151.0, 153.0, 149.0, 150.0),
    Bar(140.0, 141.0, 136.0, 138.0),
    Bar(138.0, 140.0, 136.0, 139.0),
    Bar(139.0, 141.0, 137.0, 138.0),
    Bar(138.0, 140.0, 136.0, 139.0),
    Bar(139.0, 141.0, 137.0, 138.0),
)

#: A holding tail for the drawdown cases (SW9.5): entered at the same open, it closes at or
#: under the entry through day 5 (so no partial fires — `04` §6.4.4 needs a close above the
#: entry), creeps up 0.40 a day above a 10-day average that lags it (so the trail never sells
#: it), never nears the stop, and then rallies on days 10-12. Its mark is what lets a sleeve in
#: drawdown recover without a new entry — the only way a locked sleeve can.
HOLD_TAIL: tuple[Bar, ...] = (
    Bar(152.0, 153.0, 148.5, 150.0),
    Bar(150.0, 152.0, 148.5, 150.4),
    Bar(150.4, 152.5, 149.0, 150.8),
    Bar(150.8, 153.0, 149.5, 151.2),
    Bar(151.2, 153.0, 149.5, 151.6),
    Bar(151.6, 153.5, 150.0, 152.0),
    Bar(152.0, 154.0, 150.5, 152.4),
    Bar(152.4, 154.5, 151.0, 152.8),
    Bar(152.8, 155.0, 151.0, 153.2),
    Bar(153.2, 155.5, 151.5, 153.6),
    Bar(154.0, 161.0, 153.0, 160.0),
    Bar(161.0, 169.0, 160.0, 168.0),
    Bar(169.0, 176.0, 168.0, 175.0),
)

#: The entry-day bar variants for the entry rule (`04` §11): open below the pivot but the high
#: through it (fill at the trigger), and a day that never reaches it (no trade).
THROUGH_TRIGGER_DAY = Bar(147.0, 152.0, 146.0, 151.0)
BELOW_TRIGGER_DAY = Bar(145.0, 147.0, 144.0, 146.0)


@dataclass(frozen=True, slots=True)
class Planted:
    """What the winning planted trade must come out as, worked by hand.

    Sleeve ₹10,00,000, risk 0.5% = ₹5,000; entry 152.00 (the open, above the pivot); stop =
    the detection day's low, 144.75 x 0.98 = 141.855 → 141.8550 on the four-decimal grid;
    distance 10.145 → 492 shares by risk (the position, 7.5% of the sleeve, is inside every other
    cap). Partial 492 // 3 = 164 at the day-4 open 162.00; the remaining 328 at the breakeven
    stop 152.00 on day 5. With 0.13% a side: entry 152.1976; exit average
    (164 x 162 + 328 x 152) / 492 x (1 - 0.0013) = 155.3333 x 0.9987 = 155.1314 → 155.13 (2 dp,
    `exit_average`); R = (155.13 - 152.1976) / (152.1976 - 141.855) = 0.2835 → 0.28.
    """

    symbol: str = "FLAGWIN"
    entry_open: Decimal = Decimal("152.0000")
    stop: Decimal = Decimal("141.8550")
    #: `04` §6: the stop may sit no further below the entry than one ADR. 10.145 / 152 = 6.67%,
    #: inside the 9.40% ADR — so the plan lines it rather than refusing it ``STOP_TOO_WIDE``.
    stop_distance_pct: Decimal = Decimal("6.67")
    adr_pct: Decimal = Decimal("9.40")
    #: §6.2: ADR at or above 6% trails the fast (10-day) average.
    trail: str = "MA10"
    quantity: int = 492
    partial_quantity: int = 164
    partial_fill: Decimal = Decimal("162.0000")
    final_fill: Decimal = Decimal("152.0000")
    entry_paid: Decimal = Decimal("152.1976")
    exit_avg: Decimal = Decimal("155.13")
    r_multiple: Decimal = Decimal("0.28")
    #: Sessions after the entry day on which the partial fills and the stop fills.
    partial_day: int = 4
    exit_day: int = 5


PLANTED = Planted()


def _leaders_range(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """A leader's daily range on every bar but the last (the detection bar keeps its own).

    Only the lows move: the highs — and so the pivot, which is a high — stay exactly where
    ``swing_fixtures`` drew them, and so does the last bar, whose low is the planted stop.
    """
    for row in rows[:-1]:
        close = row["close"]
        assert isinstance(close, float)
        row["low"] = close * LOW_FACTOR
    return rows


def _flag_rows(
    instrument_id: int, symbol: str, calendar: list[dt.date], tail: tuple[Bar, ...]
) -> list[dict[str, object]]:
    rows = _leaders_range(flag_series(instrument_id, symbol, n=FLAG_BARS))
    for bar in tail:
        rows.append(
            {
                "instrument_id": instrument_id,
                "symbol": symbol,
                "date": START,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": 1.2e6,
            }
        )
    return _redate(rows, calendar)


def _redate(rows: list[dict[str, object]], calendar: list[dt.date]) -> list[dict[str, object]]:
    for index, row in enumerate(rows):
        row["date"] = calendar[index]
    return rows


def tape_series(
    instrument_id: int,
    symbol: str,
    calendar: list[dt.date],
    *,
    daily_gain: float = 0.013,
    rise_from: int = 0,
) -> list[dict[str, object]]:
    """A liquid name up ~29% every 20 sessions, so `04` §8.1's breadth reads GREEN.

    It is never a setup: the pole's high is always yesterday's, so the base is one bar long.
    ``rise_from`` keeps it flat at 100 for that many sessions first — a tape that turns.
    """
    n = len(calendar)
    close = 100.0 * (1 + daily_gain) ** np.maximum(np.arange(n) - rise_from, 0)
    return [
        {
            "instrument_id": instrument_id,
            "symbol": symbol,
            "date": calendar[i],
            "open": float(close[i]),
            "high": float(close[i] * 1.02),
            "low": float(close[i] * 0.98),
            "close": float(close[i]),
            "volume": 1e6,
        }
        for i in range(n)
    ]


def planted_calendar(tail: tuple[Bar, ...] = WIN_TAIL) -> list[dt.date]:
    return weekdays(START, FLAG_BARS + len(tail))


def planted_frame(
    *,
    tail: tuple[Bar, ...] = WIN_TAIL,
    symbol: str = PLANTED.symbol,
    extra: list[dict[str, object]] | None = None,
) -> tuple[pl.DataFrame, list[dt.date]]:
    """The planted year: the flag, the tape name, a flat name; and its calendar."""
    calendar = planted_calendar(tail)
    rows = _flag_rows(1, symbol, calendar, tail)
    rows += tape_series(2, "TAPECO", calendar)
    rows += _redate(flat_series(3, "FLATCO", n=len(calendar)), calendar)
    if extra:
        rows += extra
    return pl.DataFrame(rows, infer_schema_length=None), calendar


def second_flag(
    symbol: str,
    tail: tuple[Bar, ...],
    calendar: list[dt.date],
    *,
    instrument_id: int = 4,
    offset: int = 0,
) -> list[dict[str, object]]:
    """Another copy of the flag with its own tail, for runs that need two trades.

    ``offset`` shifts the whole series that many sessions later, so it is detected — and
    entered — after the first one, with the first still on the book.
    """
    rows = _flag_rows(instrument_id, symbol, calendar[offset:], tail[: len(tail) - offset])
    return rows


EP_BARS = 140


def ep_rows(
    calendar: list[dt.date], *, locked: bool = False, instrument_id: int = 5, symbol: str = "EPCO"
) -> list[dict[str, object]]:
    """``swing_fixtures.ep_series`` (GAP_DAY on its last bar) re-dated, then a tail that opens
    above the gap day's high, closes red every session and never nears the stop — so the only
    rule that could close it is one the backtest must *not* apply to a next-day entry
    (`04` §6.4.2 is for the gap day itself).

    The bars before the gap carry :func:`_leaders_range` for the same reason the flag does: the
    entry is 2% over the gap day's high and the stop is that day's low, 8.5% under the entry,
    which one ADR of ±2% bars (4.2%) would refuse and one ADR of a leader's bars (9.5%) admits.
    """
    rows = _leaders_range(ep_series(instrument_id, symbol, n=EP_BARS, locked=locked))
    gap_high = rows[-1]["high"]  # the trigger
    assert isinstance(gap_high, float)
    for _ in range(len(calendar) - EP_BARS):
        rows.append(
            {
                "instrument_id": instrument_id,
                "symbol": symbol,
                "date": START,
                "open": gap_high * 1.02,
                "high": gap_high * 1.04,
                "low": gap_high * 1.00,
                "close": gap_high * 1.01,
                "volume": 2e6,
                "upper_circuit": 1e9,
            }
        )
    return _redate(rows, calendar)


def entry_variant(day: Bar) -> tuple[Bar, ...]:
    """The winning tail with a different entry-day bar."""
    return (day, *WIN_TAIL[1:])


def speed_frame(
    *, instruments: int = 300, sessions: int = 2000, seed: int = 7
) -> tuple[pl.DataFrame, list[dt.date]]:
    """``instruments`` random walks over ``sessions`` weekdays — the SW9 speed fixture.

    Daily ranges of about 5% keep the universe liquid so the detectors and the trade loop both do
    real work; nothing about the paths is tuned to produce setups, and the run's numbers mean
    nothing beyond "it ran".
    """
    rng = np.random.default_rng(seed)
    calendar = weekdays(dt.date(2018, 1, 1), sessions)
    steps = rng.normal(0.0002, 0.02, size=(instruments, sessions))
    close = 100.0 * np.exp(np.cumsum(steps, axis=1))
    prev = np.concatenate([close[:, :1], close[:, :-1]], axis=1)
    open_ = prev * (1 + rng.normal(0, 0.005, size=close.shape))
    span = 0.02 + np.abs(rng.normal(0, 0.01, size=close.shape))
    high = np.maximum(open_, close) * (1 + span)
    low = np.minimum(open_, close) * (1 - span)
    volume = 1e6 * np.exp(rng.normal(0, 0.4, size=close.shape))
    frame = pl.DataFrame(
        {
            "instrument_id": np.repeat(np.arange(1, instruments + 1), sessions),
            "symbol": [f"SYM{i:04d}" for i in range(1, instruments + 1) for _ in range(sessions)],
            "date": pl.Series(calendar * instruments, dtype=pl.Date),
            "open": open_.ravel(),
            "high": high.ravel(),
            "low": low.ravel(),
            "close": close.ravel(),
            "volume": volume.ravel(),
        }
    )
    return frame, calendar
