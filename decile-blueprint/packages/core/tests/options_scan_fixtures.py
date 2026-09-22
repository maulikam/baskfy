"""Fixture days for OP4's signal and scan tests — shaped as OP3 stores them.

``op_index_minute`` rows become :class:`Bar` (one per minute, IST starts); ``op_chain_snapshot``
rows become :class:`Snapshot` (one minute's quotes for the two nearest expiries, the spot quoted
in the same call). The chain is **priced by Black-76 from a stated forward and IV smile**, rounded
to the tick with a stated spread and depth, so a test can say which strikes the rules must pick and
recompute every rupee from the quotes alone.

Literal lot sizes, strikes and dates live here and in ``options_fixtures`` only (``04`` §1.4).

The days (``06`` OP4's list):

* ``QUIET_MONTHLY`` — Tue 27 Oct 2026, the monthly: a small gap, a tight zig-zag, the 09:59 close
  inside the opening range, low ER → O1-M ``WOULD_TRADE``.
* ``TREND_WEEKLY`` — Tue 20 Oct 2026, a weekly: a steady climb → O1-W ``WOULD_SKIP`` for
  containment and ER; the 09:15-10:14 range is narrow and the 10:15-10:19 bar breaks it with a
  high ER → O3-A ``TRIGGERED``; no gap → O3-B ``DAY_SKIPPED``.
* ``GAP_HOLD`` — Tue 13 Oct 2026, a weekly: +0.8 % gap that holds half the gap to 09:44 → O3-B
  ``TRIGGERED`` at 09:45.
* ``O2_UP_BREAK`` — Mon 19 Oct 2026: up-trend, the 10:00-10:04 bar breaks the opening range → O2
  ``TRIGGERED``; being the Monday before a Tuesday expiry, O2 uses **Tuesday's** contract.
* ``O2_COUNTER`` — Wed 21 Oct 2026: up-trend, the market breaks *down* — seen, never traded.
* ``O2_TUESDAY`` — Tue 20 Oct 2026 again, for O2: an expiry day, so O2 uses **next week's**
  contract.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Sequence
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from options_fixtures import EXPIRIES, TICK, contract

from baskfy_core.options.bars import IST, Bar
from baskfy_core.options.calendar import Contract
from baskfy_core.options.chain import Level, OptionQuote
from baskfy_core.options.config import OptionType
from baskfy_core.options.greeks import black76_price, year_fraction
from baskfy_core.options.scan import MarketDay, Snapshot

PREV_CLOSE = Decimal("25000")
QUIET_MONTHLY = dt.date(2026, 10, 27)
TREND_WEEKLY = dt.date(2026, 10, 20)
GAP_HOLD = dt.date(2026, 10, 13)
O2_UP_BREAK = dt.date(2026, 10, 19)
O2_COUNTER = dt.date(2026, 10, 21)
O2_TUESDAY = TREND_WEEKLY
SETTLE = dt.time(15, 30)
RATE = 0.065
#: Units resting on each of three levels a side (20 lots), and the OI (units) of every contract.
DEPTH = 1300
OI = 500_000


def at(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)


def grid(centre: int = 25000, half: int = 1500, step: int = 50) -> tuple[Decimal, ...]:
    return tuple(Decimal(k) for k in range(centre - half, centre + half + 1, step))


def nifty_master() -> tuple[Contract, ...]:
    """Every fixture expiry, a 50-point grid ±1,500 around 25,000, CE and PE, lot 65, tick 0.05."""
    rows: list[Contract] = []
    token = 1
    for expiry in EXPIRIES:
        for strike in grid():
            for kind in OptionType:
                rows.append(contract(expiry, strike, kind, token=token))
                token += 1
    return tuple(rows)


MASTER = nifty_master()


def token_of(expiry: dt.date, strike: Decimal, kind: OptionType) -> int:
    return next(
        c.instrument_token
        for c in MASTER
        if c.expiry == expiry and c.strike == strike and c.option_type is kind
    )


# --- bars ----------------------------------------------------------------------------------------


def bars_from_closes(
    day: dt.date, first_open: Decimal, closes: Sequence[Decimal], wick: Decimal = Decimal("2")
) -> tuple[Bar, ...]:
    """One bar a minute from 09:15: each opens at the previous close, wicks ``wick`` beyond."""
    out: list[Bar] = []
    prev = first_open
    for i, close in enumerate(closes):
        start = at(day, 9, 15) + dt.timedelta(minutes=i)
        out.append(
            Bar(
                ts=start,
                open=prev,
                high=max(prev, close) + wick,
                low=min(prev, close) - wick,
                close=close,
            )
        )
        prev = close
    return tuple(out)


def zigzag(centre: Decimal, amplitude: Decimal, count: int) -> list[Decimal]:
    return [centre + (amplitude if i % 2 == 0 else -amplitude) for i in range(count)]


def ramp(start: Decimal, per_minute: Decimal, count: int) -> list[Decimal]:
    return [start + per_minute * (i + 1) for i in range(count)]


def to_minutes(hh: int, mm: int) -> int:
    """Minutes after 09:15."""
    return (hh * 60 + mm) - (9 * 60 + 15)


def quiet_monthly_bars() -> tuple[Bar, ...]:
    """Open 25,010 (gap 0.04 %); a ±8-point zig-zag around 25,010 all day → OR contains the 09:59
    close, ER near 0, range 0.14 %."""
    closes = zigzag(Decimal("25010"), Decimal("8"), to_minutes(15, 30))
    return bars_from_closes(QUIET_MONTHLY, Decimal("25010"), closes)


def trend_weekly_bars() -> tuple[Bar, ...]:
    """Open 25,000; +3 points a minute to 10:14 (a 60-bar range 25,003-25,180 plus wicks), then
    +6 a minute — the 10:15-10:19 bar closes 25,210, above 25,182 * 1.0005."""
    first = ramp(Decimal("25000"), Decimal("3"), to_minutes(10, 15))
    rest = ramp(first[-1], Decimal("6"), to_minutes(15, 30) - len(first))
    return bars_from_closes(TREND_WEEKLY, Decimal("25000"), first + rest)


def gap_hold_bars() -> tuple[Bar, ...]:
    """Open 25,200 (+0.8 %), half-gap 25,100: a ±10 zig-zag around 25,200 that never nears it."""
    closes = zigzag(Decimal("25200"), Decimal("10"), to_minutes(15, 30))
    return bars_from_closes(GAP_HOLD, Decimal("25200"), closes)


def o2_up_break_bars() -> tuple[Bar, ...]:
    """Open 25,020 (gap 0.08 %); a ±10 zig-zag to 09:59 (OR 25,008-25,032), then +4 a minute:
    the 10:00-10:04 bar closes 25,050 > 25,032 * 1.0005 = 25,044.52 — the trigger."""
    before = zigzag(Decimal("25020"), Decimal("10"), to_minutes(10, 0))
    after = ramp(before[-1], Decimal("4"), to_minutes(15, 30) - len(before))
    return bars_from_closes(O2_UP_BREAK, Decimal("25020"), before + after)


def o2_counter_bars() -> tuple[Bar, ...]:
    """Up-trend history, but the tape falls: the same OR, then -4 a minute — a counter-trend
    break, recorded and never traded."""
    before = zigzag(Decimal("25020"), Decimal("10"), to_minutes(10, 0))
    after = ramp(before[-1], Decimal("-4"), to_minutes(15, 30) - len(before))
    return bars_from_closes(O2_COUNTER, Decimal("25020"), before + after)


def rising_closes(last: Decimal = PREV_CLOSE, count: int = 60) -> tuple[Decimal, ...]:
    """Daily closes rising 20 points a session to ``last`` — prev close above its EMA20 (UP)."""
    return tuple(last - Decimal(20) * (count - 1 - i) for i in range(count))


def falling_closes(last: Decimal = PREV_CLOSE, count: int = 60) -> tuple[Decimal, ...]:
    return tuple(last + Decimal(20) * (count - 1 - i) for i in range(count))


def market(
    day: dt.date,
    bars: tuple[Bar, ...],
    *,
    daily: tuple[Decimal, ...] | None = None,
    vix: Decimal | None = Decimal("14"),
    trading_day: bool = True,
) -> MarketDay:
    return MarketDay(
        trade_date=day,
        trading_day=trading_day,
        contracts=MASTER,
        bars=bars,
        daily_closes=daily if daily is not None else rising_closes(),
        vix_prev_close=vix,
    )


# --- the chain -----------------------------------------------------------------------------------


Smile = Callable[[Decimal, OptionType], float]


def flat(vol: float) -> Smile:
    return lambda _strike, _kind: vol


def skew(call_vol: float, put_vol: float) -> Smile:
    """Calls at one vol, puts at another — enough skew for a fixture."""
    return lambda _strike, kind: call_vol if kind is OptionType.CE else put_vol


def _tick_down(value: Decimal) -> Decimal:
    return (value / TICK).to_integral_value(rounding=ROUND_FLOOR) * TICK


def _tick_up(value: Decimal) -> Decimal:
    return (value / TICK).to_integral_value(rounding=ROUND_CEILING) * TICK


def priced_quote(  # noqa: PLR0913 - one argument per input of the model and the book
    expiry: dt.date,
    strike: Decimal,
    kind: OptionType,
    *,
    forward: Decimal,
    now: dt.datetime,
    vol: float,
    spread_pct: Decimal = Decimal("1.0"),
    depth: int = DEPTH,
    oi: int = OI,
) -> OptionQuote:
    """A quote whose mid is Black-76 at ``vol`` on ``forward``; bid/ask ``spread_pct`` apart
    (at least a tick either side), three levels of ``depth`` units a side."""
    years = year_fraction(now, expiry, SETTLE)
    model = Decimal(repr(black76_price(float(forward), float(strike), years, RATE, vol, kind)))
    mid = model.quantize(TICK, rounding=ROUND_HALF_UP) if model > 0 else Decimal(0)
    half = max(TICK, mid * spread_pct / Decimal(200))
    bid = _tick_down(mid - half)
    ask = _tick_up(mid + half)
    bid_value = bid if bid > 0 else None
    return OptionQuote(
        instrument_token=token_of(expiry, strike, kind),
        expiry=expiry,
        strike=strike,
        option_type=kind,
        bid=bid_value,
        ask=ask,
        bids=(Level(bid, depth),) * 3 if bid_value is not None else (),
        asks=(Level(ask, depth),) * 3,
        oi=oi,
        ts=now,
    )


def snapshot(  # noqa: PLR0913 - the minute, the spot, the expiries and the model
    now: dt.datetime,
    spot: Decimal,
    expiries: Sequence[dt.date],
    smile: Smile,
    *,
    forwards: dict[dt.date, Decimal] | None = None,
    strikes: int = 15,
    spread_pct: Decimal = Decimal("1.0"),
    depth: int = DEPTH,
) -> Snapshot:
    """One collector minute: ATM ±``strikes`` on a 50 grid for each expiry, CE and PE."""
    centre = (spot / 50).to_integral_value(rounding=ROUND_HALF_UP) * 50
    quotes: list[OptionQuote] = []
    for expiry in expiries:
        fwd = (forwards or {}).get(expiry, spot)
        for i in range(-strikes, strikes + 1):
            strike = centre + 50 * i
            for kind in OptionType:
                quotes.append(
                    priced_quote(
                        expiry,
                        strike,
                        kind,
                        forward=fwd,
                        now=now,
                        vol=smile(strike, kind),
                        spread_pct=spread_pct,
                        depth=depth,
                    )
                )
    return Snapshot(ts=now, spot=spot, quotes=tuple(quotes))


def carry_forward(spot: Decimal, now: dt.datetime, expiry: dt.date) -> Decimal:
    """The forward a cost-of-carry chain would imply: ``spot * e^{rT}``, to the paisa."""
    years = max(year_fraction(now, expiry, SETTLE), 0.0)
    return (spot * Decimal(repr(math.exp(RATE * years)))).quantize(Decimal("0.01"))
