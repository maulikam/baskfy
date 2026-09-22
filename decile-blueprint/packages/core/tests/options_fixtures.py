"""Fixtures for the ``baskfy_core.options`` tests (OP1).

Literal lot sizes, strikes and expiry dates live **here and only here** (``04`` §1.4): the
package reads them from the master. The master below is a hand-built slice of what
``instruments("NFO")`` would return for NIFTY in October-November 2026 — Tuesday expiries (NSE/
FAOP/68747), a 50-point grid near the money and 100 points further out, lot size 65 (the value at
write time, ``docs/options/01`` §1) — plus a BANKNIFTY row that must never leak into a NIFTY
answer.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from baskfy_core.options.calendar import Contract
from baskfy_core.options.chain import Level, OptionQuote
from baskfy_core.options.config import OptionType

LOT = 65
TICK = Decimal("0.05")

#: The Tuesdays of Oct-Nov 2026. The monthlies are the 27th and the 24th.
OCT_TUESDAYS = (
    dt.date(2026, 10, 6),
    dt.date(2026, 10, 13),
    dt.date(2026, 10, 20),
    dt.date(2026, 10, 27),
)
NOV_TUESDAYS = (
    dt.date(2026, 11, 3),
    dt.date(2026, 11, 10),
    dt.date(2026, 11, 17),
    dt.date(2026, 11, 24),
)
EXPIRIES = OCT_TUESDAYS + NOV_TUESDAYS

#: The holiday fixture: the exchange moved October's last Tuesday to the Monday before it.
SHIFTED_MONTHLY = dt.date(2026, 10, 26)


def strikes_around(centre: int = 25000) -> tuple[Decimal, ...]:
    """A 50-point grid within ±1,000 of ``centre`` and a 100-point grid out to ±2,000."""
    near = range(centre - 1000, centre + 1001, 50)
    far = [k for k in range(centre - 2000, centre + 2001, 100) if abs(k - centre) > 1000]
    return tuple(Decimal(k) for k in sorted({*near, *far}))


def contract(  # noqa: PLR0913 - one argument per master column
    expiry: dt.date,
    strike: Decimal,
    kind: OptionType,
    *,
    lot_size: int = LOT,
    tick: Decimal = TICK,
    underlying: str = "NIFTY",
    token: int = 0,
) -> Contract:
    return Contract(
        instrument_token=token,
        tradingsymbol=f"{underlying}{expiry:%y%m%d}{strike}{kind.value}",
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=kind,
        lot_size=lot_size,
        tick_size=tick,
    )


def master(
    expiries: tuple[dt.date, ...] = EXPIRIES, *, with_banknifty: bool = True
) -> tuple[Contract, ...]:
    """A few strikes per expiry — enough for the calendar, which reads only the expiry column."""
    rows = [
        contract(expiry, strike, kind, token=i)
        for i, (expiry, strike, kind) in enumerate(
            (e, k, t)
            for e in expiries
            for k in (Decimal(24950), Decimal(25000), Decimal(25050))
            for t in OptionType
        )
    ]
    if with_banknifty:
        # BANKNIFTY's monthly is on another day entirely; it must never move a NIFTY answer.
        rows.append(
            contract(
                dt.date(2026, 10, 29),
                Decimal(52000),
                OptionType.CE,
                underlying="BANKNIFTY",
                lot_size=30,
            )
        )
    return tuple(rows)


def shifted_master() -> tuple[Contract, ...]:
    """The same master with October's monthly moved from Tuesday 27th to Monday 26th."""
    moved = tuple(SHIFTED_MONTHLY if d == dt.date(2026, 10, 27) else d for d in EXPIRIES)
    return master(moved)


def quote(  # noqa: PLR0913 - one argument per quote field
    strike: Decimal | int,
    kind: OptionType,
    bid: str | Decimal | None,
    ask: str | Decimal | None,
    *,
    expiry: dt.date = dt.date(2026, 10, 27),
    bids: tuple[Level, ...] | None = None,
    asks: tuple[Level, ...] | None = None,
    oi: int = 1_000_000,
    ts: dt.datetime = dt.datetime(2026, 10, 27, 10, 0),
) -> OptionQuote:
    """A quote with, by default, three deep levels a side at the touch."""
    b = None if bid is None else Decimal(bid)
    a = None if ask is None else Decimal(ask)
    return OptionQuote(
        instrument_token=0,
        expiry=expiry,
        strike=Decimal(strike),
        option_type=kind,
        bid=b,
        ask=a,
        bids=bids if bids is not None else ((Level(b, 6500),) * 3 if b else ()),
        asks=asks if asks is not None else ((Level(a, 6500),) * 3 if a else ()),
        oi=oi,
        ts=ts,
    )
