"""Live marks for the instruments a person actually holds — M82, then quotes for the rest.

The portfolio valued everything at `ohlcv_daily.close_raw`, the previous session's close. That is
right for a screener and wrong for the page that answers "what is my money worth", which Maulik
asked to show the market rather than yesterday: on 2 Sep 2026 ATHERENERG closed at one price and
was trading at 1692.50, and the portfolio showed the close.

Kite's holdings endpoint already returns `last_price` per position, and `HoldingRow` already
carries it — that covers names the broker book lists. A Kite login is a live session, though,
and a portfolio can hold names that are not on that payload (CAS import, a second book, a name
Kite omitted). Those stay on close unless we ask for a quote.

**No new execution surface.** `KiteProvider.quotes` is read-only, the same rate-limited path
the swing scan already uses. Nothing here can place an order (law 2).

**Cached, because a page render must not become a broker call.** The overview refetches on
navigation and on a poll; without a cache each of those is a round trip to Kite and a step towards
its rate limit. The window is short enough that the number still reads as live.

**Silent fallback is deliberate here.** A failed or stale quote leaves the close in place, which is
a correct number with a known meaning, and the page keeps working. This is the one place where
degrading quietly is right: the alternative is an empty portfolio because a quote timed out.
"""

from __future__ import annotations

import datetime as dt
import os
import threading
from collections.abc import Sequence
from decimal import Decimal

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.broker_holdings import holdings_for_broker
from baskfy_api.broker_oauth import (
    dry_run_enabled,
    is_simulated_token,
    token_encryption_key,
    token_store_for,
    token_store_path,
)
from baskfy_core.models import Instrument
from baskfy_providers.errors import CredentialsMissing, ProviderError
from baskfy_providers.factory import build_kite_provider
from baskfy_providers.settings import get_provider_settings

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


class _QuoteMemo:
    """Same window as the holdings memo, but mergeable so a second page can reuse quotes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._at: float = 0.0
        self._value: dict[str, Decimal] = {}

    def snapshot(self) -> dict[str, Decimal]:
        with self._lock:
            if _now() - self._at < CACHE_TTL_SECONDS:
                return dict(self._value)
            return {}

    def merge(self, value: dict[str, Decimal]) -> None:
        with self._lock:
            if _now() - self._at >= CACHE_TTL_SECONDS:
                self._value = {}
                self._at = _now()
            self._value.update(value)
            if self._at == 0.0:
                self._at = _now()

    def clear(self) -> None:
        with self._lock:
            self._value = {}
            self._at = 0.0


_memo = _Memo()
_quote_memo = _QuoteMemo()


def reset_cache() -> None:
    """Drop both memos. For tests, and for a caller that has just changed the holdings."""
    _memo.clear()
    _quote_memo.clear()


def quotes_permitted() -> bool:
    """True only when a real, unexpired Kite session exists and DRY_RUN is off.

    The same local checks the brokers page uses for "connected" — no network call, and a
    ``sim_`` token is not a session. A quote against a stub would put an invented price
    behind a rupee total, which is the failure this module exists to prevent.
    """
    if dry_run_enabled():
        return False
    if not os.environ.get("BASKFY_KITE_API_KEY", "").strip():
        return False
    try:
        token = token_store_for().load()
    except CredentialsMissing:
        return False
    return not (is_simulated_token(token.value) or token.is_expired())


def live_prices_by_symbol(broker_id: str = "zerodha") -> dict[str, Decimal]:
    """`{symbol: last_price}` from the broker book, or `{}` when there is nothing trustworthy.

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


def _quote_symbols(symbols: Sequence[str]) -> dict[str, Decimal]:
    """Read-only Kite quotes. Empty on any failure — the close stays in place."""
    wanted = [symbol for symbol in symbols if symbol]
    if not wanted:
        return {}
    api_key = os.environ.get("BASKFY_KITE_API_KEY", "").strip()
    if not api_key:
        return {}
    settings = get_provider_settings().model_copy(
        update={
            "kite_api_key": api_key,
            "kite_token_path": str(token_store_path()),
            "kite_token_encryption_key": token_encryption_key(),
        }
    )
    try:
        records = build_kite_provider(settings).quotes(wanted)
    except (ProviderError, OSError):
        return {}
    return {
        record.symbol.strip().upper(): record.last_price
        for record in records
        if record.last_price is not None and record.last_price > 0
    }


def live_quotes_by_symbol(symbols: Sequence[str]) -> dict[str, Decimal]:
    """`{symbol: last_price}` from Kite quotes for names the holdings payload did not cover.

    Refuses unless :func:`quotes_permitted`. Cached for the same window as the holdings memo.
    """
    wanted = [symbol.strip().upper() for symbol in symbols if symbol and symbol.strip()]
    if not wanted:
        return {}
    cached = _quote_memo.snapshot()
    missing = [symbol for symbol in wanted if symbol not in cached]
    if not missing:
        return {symbol: cached[symbol] for symbol in wanted if symbol in cached}
    if not quotes_permitted():
        return {symbol: cached[symbol] for symbol in wanted if symbol in cached}
    fetched = _quote_symbols(missing)
    if fetched:
        _quote_memo.merge(fetched)
        cached.update(fetched)
    return {symbol: cached[symbol] for symbol in wanted if symbol in cached}


async def live_prices_by_instrument(
    session: AsyncSession, instrument_ids: list[int], *, broker_id: str = "zerodha"
) -> dict[int, Decimal]:
    """The same marks, keyed by instrument id so a price map can be overlaid directly.

    Holdings `last_price` first; then a read-only quote for any held name the book omitted.
    Kite I/O is sync (and may ``time.sleep`` on the rate limiter). Run it in a worker thread so an
    async handler does not stall the whole worker on every memo miss (audit 4.11 / 4.12).
    """
    if not instrument_ids:
        return {}
    by_symbol = await anyio.to_thread.run_sync(live_prices_by_symbol, broker_id)
    rows = (
        await session.execute(
            select(Instrument.id, Instrument.symbol).where(Instrument.id.in_(instrument_ids))
        )
    ).all()
    mapped = {int(row.id): by_symbol[row.symbol] for row in rows if row.symbol in by_symbol}
    missing = [row.symbol for row in rows if row.symbol not in by_symbol]
    if missing:
        quoted = await anyio.to_thread.run_sync(live_quotes_by_symbol, tuple(missing))
        for row in rows:
            if int(row.id) not in mapped and row.symbol in quoted:
                mapped[int(row.id)] = quoted[row.symbol]
    return mapped
