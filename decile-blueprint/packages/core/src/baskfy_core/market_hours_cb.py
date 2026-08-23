"""NSE cash-session hours for curated-basket plan gates (docs/smallcase/04 §5, §9).

Pure: caller supplies ``now`` and the set of trading dates. No database, no network, no
ambient clock. Asia/Kolkata is the only timezone that matters for these checks.
"""

from __future__ import annotations

import datetime as dt
from typing import Final
from zoneinfo import ZoneInfo

__all__ = [
    "IST",
    "SESSION_CLOSE",
    "SESSION_OPEN",
    "closed_market_payload",
    "is_nse_session_open",
    "next_session_open",
]

IST: Final = ZoneInfo("Asia/Kolkata")

#: Continuous equity session on the NSE cash segment (docs/smallcase/04 §5).
SESSION_OPEN: Final = dt.time(9, 15)
SESSION_CLOSE: Final = dt.time(15, 30)


def _as_ist(now: dt.datetime) -> dt.datetime:
    """Normalise *now* to Asia/Kolkata. Naive datetimes are treated as already IST."""
    if now.tzinfo is None:
        return now.replace(tzinfo=IST)
    return now.astimezone(IST)


def is_nse_session_open(now: dt.datetime, trading_dates: set[dt.date]) -> bool:
    """True iff *now* falls on a trading day inside [09:15, 15:30] IST inclusive."""
    ist = _as_ist(now)
    if ist.date() not in trading_dates:
        return False
    clock = ist.timetz().replace(tzinfo=None)
    return SESSION_OPEN <= clock <= SESSION_CLOSE


def next_session_open(now: dt.datetime, trading_dates: set[dt.date]) -> dt.datetime:
    """Earliest 09:15 IST strictly at or after *now* on a date in *trading_dates*.

    Used for the closed-market payload. Never invents a calendar day — if the set has no
    eligible future open, raises ``ValueError``.
    """
    if not trading_dates:
        raise ValueError("trading_dates cannot be empty")

    ist = _as_ist(now)
    today = ist.date()
    clock = ist.timetz().replace(tzinfo=None)

    if today in trading_dates and clock < SESSION_OPEN:
        return dt.datetime.combine(today, SESSION_OPEN, tzinfo=IST)

    candidates = sorted(d for d in trading_dates if d > today)
    if not candidates:
        raise ValueError(f"no trading day after {today.isoformat()} in the provided calendar")
    return dt.datetime.combine(candidates[0], SESSION_OPEN, tzinfo=IST)


def closed_market_payload(next_open: dt.datetime) -> dict[str, object]:
    """Body returned by plan endpoints when the cash session is closed (04 §5)."""
    ist = _as_ist(next_open)
    return {
        "market_open": False,
        "next_open_ist": ist.isoformat(),
    }
