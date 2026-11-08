"""Expiries, day roles and lot sizes — from the NFO master, never from a weekday (``04`` §1).

The rule this module exists to keep: **no weekday arithmetic anywhere**. NIFTY expires on
Tuesdays (NSE/FAOP/68747, ``DECISIONS-OP`` OP0.2) *until a Tuesday is a holiday*, when the
exchange moves the expiry to the previous trading day and the master says so. A weekday rule
agrees with the master on almost every date and is wrong on exactly the dates that matter, so
nothing here knows what a Tuesday is: the expiry set is the distinct ``expiry`` of the master's
NIFTY CE/PE rows, and the monthly is the last of them in its calendar month.

Ported by re-implementation from the frozen lab's ``strategies/strangle/calendar_nse.py``
(PACK.2), keeping its lesson — the listed expiry is the evidence — and dropping its forward
weekday inference, which this run does not need: the calendar is rebuilt nightly from the master
(OP2), and a day's role is asked of that, not predicted.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from baskfy_core.options.config import ExpiryKind, OptionType, Sleeve


@dataclass(frozen=True, slots=True)
class Contract:
    """One row of ``op_contract`` — the master's facts, never parsed from the trading symbol."""

    instrument_token: int
    tradingsymbol: str
    underlying: str
    expiry: dt.date
    strike: Decimal
    option_type: OptionType
    lot_size: int
    tick_size: Decimal


class RoleReason(StrEnum):
    """Why a sleeve does or does not trade on a date (``04`` §1.2)."""

    TRADES = "TRADES"
    NOT_TRADING_DAY = "NOT_TRADING_DAY"
    EVENT_DAY = "EVENT_DAY"
    NOT_EXPIRY = "NOT_EXPIRY"
    NOT_MONTHLY = "NOT_MONTHLY"
    MONTHLY_EXPIRY = "MONTHLY_EXPIRY"
    SETUP_DISABLED = "SETUP_DISABLED"


@dataclass(frozen=True, slots=True)
class DayRole:
    trades: bool
    reason: RoleReason


def _options_of(rows: Iterable[Contract], underlying: str) -> list[Contract]:
    return [row for row in rows if row.underlying == underlying]


def expiries(rows: Iterable[Contract], underlying: str = "NIFTY") -> tuple[dt.date, ...]:
    """The distinct expiries of the master's CE/PE rows for ``underlying``, ascending."""
    return tuple(sorted({row.expiry for row in _options_of(rows, underlying)}))


def monthly_expiry(
    rows: Iterable[Contract], year: int, month: int, underlying: str = "NIFTY"
) -> dt.date | None:
    """The **last** master expiry inside the calendar month, or ``None`` if the month has none."""
    inside = [d for d in expiries(rows, underlying) if d.year == year and d.month == month]
    return inside[-1] if inside else None


def kind(rows: Iterable[Contract], expiry: dt.date, underlying: str = "NIFTY") -> ExpiryKind:
    """``MONTHLY`` iff ``expiry`` is its month's last master expiry. Raises if not an expiry."""
    listed = expiries(rows, underlying)
    if expiry not in listed:
        raise ValueError(f"{expiry.isoformat()} is not a {underlying} expiry in the master")
    same_month = [d for d in listed if (d.year, d.month) == (expiry.year, expiry.month)]
    return ExpiryKind.MONTHLY if expiry == same_month[-1] else ExpiryKind.WEEKLY


def is_event_day(day: dt.date, event_days: Iterable[dt.date]) -> bool:
    return day in set(event_days)


def role(  # noqa: PLR0913, PLR0911 - one named return per role reason (04 §1.2)
    day: dt.date,
    sleeve: Sleeve,
    *,
    rows: Iterable[Contract],
    event_days: Iterable[dt.date],
    trading_day: bool,
    underlying: str = "NIFTY",
) -> DayRole:
    """Whether ``sleeve`` trades on ``day`` (``04`` §1.2). Everything downstream asks this.

    ``trading_day`` is the NSE calendar's answer for ``day`` (``trading_day`` table); the pure
    core does not own that calendar. An event day is skipped, never shifted (§1.3).
    """
    if not trading_day:
        return DayRole(False, RoleReason.NOT_TRADING_DAY)
    if is_event_day(day, event_days):
        return DayRole(False, RoleReason.EVENT_DAY)
    if sleeve is Sleeve.O2:
        return DayRole(True, RoleReason.TRADES)
    materialised = list(rows)
    if day not in expiries(materialised, underlying):
        return DayRole(False, RoleReason.NOT_EXPIRY)
    day_kind = kind(materialised, day, underlying)
    if sleeve is Sleeve.O1M and day_kind is not ExpiryKind.MONTHLY:
        return DayRole(False, RoleReason.NOT_MONTHLY)
    if sleeve is Sleeve.O1W and day_kind is ExpiryKind.MONTHLY:
        return DayRole(False, RoleReason.MONTHLY_EXPIRY)
    return DayRole(True, RoleReason.TRADES)


def next_session(
    after: dt.date,
    sleeve: Sleeve,
    *,
    rows: Iterable[Contract],
    event_days: Iterable[dt.date],
    underlying: str = "NIFTY",
) -> dt.date | None:
    """The next master expiry strictly after ``after`` on which an expiry sleeve trades.

    For the scans' ``NOT_TODAY`` "next date" (``04`` §10). Every listed expiry is a trading day
    by construction, so no calendar is needed. O2 trades every non-event trading day and has no
    answer from the master alone — it raises rather than guess.
    """
    if sleeve is Sleeve.O2:
        raise ValueError("O2 trades on every non-event trading day; ask the trading calendar")
    materialised = list(rows)
    events = set(event_days)
    for candidate in expiries(materialised, underlying):
        if candidate <= after:
            continue
        verdict = role(
            candidate,
            sleeve,
            rows=materialised,
            event_days=events,
            trading_day=True,
            underlying=underlying,
        )
        if verdict.trades:
            return candidate
    return None


def _one_value[T](values: set[T]) -> T | None:
    return next(iter(values)) if len(values) == 1 else None


def lot_size_for(
    rows: Iterable[Contract], expiry: dt.date, underlying: str = "NIFTY"
) -> int | None:
    """The master's lot size for ``expiry`` (``04`` §1.4).

    ``None`` when the expiry has no rows, a row says 0, or its rows disagree — every one of which
    the sizing turns into ``REJECTED_NO_LOT_SIZE`` rather than a guess.
    """
    sizes = {row.lot_size for row in _options_of(rows, underlying) if row.expiry == expiry}
    size = _one_value(sizes)
    return size if size is not None and size > 0 else None


def tick_size_for(
    rows: Iterable[Contract], expiry: dt.date, underlying: str = "NIFTY"
) -> Decimal | None:
    """The master's tick size for ``expiry``; ``None`` when missing, zero or ambiguous."""
    ticks = {row.tick_size for row in _options_of(rows, underlying) if row.expiry == expiry}
    tick = _one_value(ticks)
    return tick if tick is not None and tick > 0 else None


def expiry_for_o2(
    day: dt.date, rows: Iterable[Contract], underlying: str = "NIFTY"
) -> dt.date | None:
    """The smallest master expiry **strictly after** ``day`` (``04`` §1.5).

    On a Tuesday expiry that is next week's; on the Monday before it, Tuesday's. 0-DTE buying is
    O3's domain (PACK.5).
    """
    later = [d for d in expiries(rows, underlying) if d > day]
    return later[0] if later else None


def expiry_for_o1_o3(day: dt.date) -> dt.date:
    """O1 and O3 trade the contract expiring today (``04`` §1.5)."""
    return day
