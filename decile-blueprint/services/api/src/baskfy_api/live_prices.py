"""Live marks for the instruments a person actually holds — M82.

The portfolio valued everything at `ohlcv_daily.close_raw`, the previous session's close. That is
right for a screener and wrong for the page that answers "what is my money worth", which Maulik
asked to show the market rather than yesterday: on 2 Sep 2026 ATHERENERG closed at one price and
was trading at 1692.50, and the portfolio showed the close.

**No new Kite surface.** Kite's holdings endpoint already returns `last_price` per position, and
`HoldingRow` already carries it — `holdings_for_broker` has been reading and discarding it. So a
live mark costs the same one call the holdings sync already makes, and covers exactly the
instruments a portfolio needs: the ones held.

**Cached, because a page render must not become a broker call.** The overview refetches on
navigation and on a poll; without a cache each of those is a round trip to Kite and a step towards
its rate limit. The window is short enough that the number still reads as live.

**Silent fallback is deliberate here.** A failed or stale quote leaves the close in place, which is
a correct number with a known meaning, and the page keeps working. This is the one place where
degrading quietly is right: the alternative is an empty portfolio because a quote timed out.
"""

from __future__ import annotations

import datetime as dt
import threading
from decimal import Decimal

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.broker_holdings import holdings_for_broker
from baskfy_core.models import Instrument

#: Long enough that a page render is not a broker call, short enough to still be "live". Kite's own
#: holdings payload does not update faster than this in practice.
CACHE_TTL_SECONDS: float = 20.0


def _now() -> float:
    return dt.datetime.now(tz=dt.UTC).timestamp()


class _Memo:
    """A tiny TTL cache with its own lock, rather than a module-level `global`.

    The state is the same; holding it on an object means the mutation is an attribute write the
    reader can see the scope of, which is what PLW0603 is asking for.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._at: float = 0.0
        self._value: dict[str, Decimal] | None = None

    def get(self) -> dict[str, Decimal] | None:
        with self._lock:
            if self._value is not None and _now() - self._at < CACHE_TTL_SECONDS:
                return self._value
            return None

    def put(self, value: dict[str, Decimal]) -> None:
        with self._lock:
            self._at, self._value = _now(), value

    def clear(self) -> None:
        with self._lock:
            self._value = None


_memo = _Memo()


def reset_cache() -> None:
    """Drop the memo. For tests, and for a caller that has just changed the holdings."""
    _memo.clear()


def live_prices_by_symbol(broker_id: str = "zerodha") -> dict[str, Decimal]:
    """`{symbol: last_price}` from the broker, or `{}` when there is nothing trustworthy.

    Only a `live` read is used. A fixture carries invented prices, and putting those behind a real
    rupee total is the failure this codebase guards against everywhere else.
    """
    cached = _memo.get()
    if cached is not None:
        return cached
    try:
        result = holdings_for_broker(broker_id)
    except Exception:
        return {}
    if result.source != "live" or result.degraded:
        return {}
    prices = {
        row.symbol: row.last_price
        for row in result.rows
        if row.last_price is not None and row.last_price > 0
    }
    _memo.put(prices)
    return prices


async def live_prices_by_instrument(
    session: AsyncSession, instrument_ids: list[int], *, broker_id: str = "zerodha"
) -> dict[int, Decimal]:
    """The same marks, keyed by instrument id so a price map can be overlaid directly.

    Kite I/O is sync (and may ``time.sleep`` on the rate limiter). Run it in a worker thread so an
    async handler does not stall the whole worker on every memo miss (audit 4.11 / 4.12).
    """
    if not instrument_ids:
        return {}
    by_symbol = await anyio.to_thread.run_sync(live_prices_by_symbol, broker_id)
    if not by_symbol:
        return {}
    rows = (
        await session.execute(
            select(Instrument.id, Instrument.symbol).where(Instrument.id.in_(instrument_ids))
        )
    ).all()
    return {int(row.id): by_symbol[row.symbol] for row in rows if row.symbol in by_symbol}
