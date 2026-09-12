"""Audit 3.12 — one IST calendar day across the portfolio / scan surfaces.

Between 00:00 and 05:30 IST, ``date.today()`` (server-local) and ``datetime.now(UTC).date()``
stamp yesterday. ``invoices.today_ist()`` is the one clock; every owned caller must use it.
"""

from __future__ import annotations

import datetime as dt
import inspect

from baskfy_api import vbt
from baskfy_api.invoices import today_ist
from baskfy_api.routers import curated_sip, explore, kite, portfolio_overview, portfolios


def test_today_ist_is_the_ist_calendar_day_not_utc() -> None:
    """00:30 UTC on 12 Sep is still 06:00 IST on 12 Sep; 20:30 UTC is already 13 Sep IST."""
    still_same = dt.datetime(2026, 9, 12, 0, 30, tzinfo=dt.UTC)
    assert today_ist(still_same) == dt.date(2026, 9, 12)

    next_ist_day = dt.datetime(2026, 9, 12, 20, 30, tzinfo=dt.UTC)
    assert today_ist(next_ist_day) == dt.date(2026, 9, 13)


def test_owned_modules_do_not_call_date_today_or_utc_date() -> None:
    """The audit's three clocks collapse to ``today_ist`` in every module Lane C owns for dates."""
    modules = (vbt, portfolios, kite, explore, curated_sip, portfolio_overview)
    forbidden = ("dt.date.today()", "date.today()", "datetime.now(tz=dt.UTC).date()")
    for module in modules:
        source = inspect.getsource(module)
        for token in forbidden:
            assert token not in source, f"{module.__name__} still stamps dates with {token}"
        assert "today_ist" in source, f"{module.__name__} must import and use today_ist"
