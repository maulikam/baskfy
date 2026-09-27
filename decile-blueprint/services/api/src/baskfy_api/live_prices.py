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

**Market data is not order permission (LV1.1, 27 Sep 2026).** ``quotes_permitted`` used to return
``False`` whenever ``DRY_RUN`` was on, so the switch that keeps *orders* in rehearsal also removed
every live price from every screen — on a process that has no execute route at all (D9). The
read-only switch is now ``BASKFY_LIVE_QUOTES`` (:func:`market_data_enabled`, default on).
``DRY_RUN`` keeps every meaning it has elsewhere: the OAuth callback still stores a *simulated*
token under it, and a simulated token is still not a session here.
"""

from __future__ import annotations

import datetime as dt
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.broker_holdings import holdings_for_broker
from baskfy_api.broker_oauth import (
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


@dataclass(frozen=True)
class LiveQuote:
    """A last price with the exchange's own previous close — what "today's change" needs.

    ``prev_close`` comes with the quote rather than from ``ohlcv_daily`` so that a nightly that
    has not published yet (or a corporate action between the two) cannot manufacture a move.

    ``as_of`` is the exchange's own time for the print (:attr:`QuoteRecord.as_of`, which the
    provider fills from Kite's ``timestamp`` / ``last_trade_time`` and makes IST-aware). ``None``
    when Kite sent no stamp — the reader then cannot tell how old the print is, and says so.
    """

    last_price: Decimal
    prev_close: Decimal | None
    as_of: dt.datetime | None = None


class _DetailMemo:
    """The quote memo's twin for :class:`LiveQuote` — the screens' overlay reads this one."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._at: float = 0.0
        self._value: dict[str, LiveQuote] = {}

    def snapshot(self) -> dict[str, LiveQuote]:
        with self._lock:
            if _now() - self._at < CACHE_TTL_SECONDS:
                return dict(self._value)
            return {}

    def merge(self, value: dict[str, LiveQuote]) -> None:
        with self._lock:
            if _now() - self._at >= CACHE_TTL_SECONDS:
                self._value = {}
                self._at = _now()
            self._value.update(value)

    def clear(self) -> None:
        with self._lock:
            self._value = {}
            self._at = 0.0


_memo = _Memo()
_quote_memo = _QuoteMemo()
_detail_memo = _DetailMemo()


def reset_cache() -> None:
    """Drop every memo. For tests, and for a caller that has just changed the holdings."""
    _memo.clear()
    _quote_memo.clear()
    _detail_memo.clear()


#: The read-only market-data switch. Truthy spellings match ``dry_run_enabled``'s.
LIVE_QUOTES_ENV: str = "BASKFY_LIVE_QUOTES"


def market_data_enabled() -> bool:
    """``BASKFY_LIVE_QUOTES`` — on unless explicitly off. Market data only; no order path reads it.

    Default on, because the switch exists to *stop* the screens going dark by accident: the
    review's first step (docs/live/PLAN.md) is that a rehearsal flag must not remove live prices.
    An empty value is the default, as with ``DRY_RUN``.
    """
    raw = os.environ.get(LIVE_QUOTES_ENV, "true").strip().lower()
    return raw in ("", "1", "true", "yes", "on")


def quotes_permitted() -> bool:
    """True only when market data is enabled and a real, unexpired Kite session exists.

    The same local checks the brokers page uses for "connected" — no network call, and a
    ``sim_`` token is not a session. A quote against a stub would put an invented price
    behind a rupee total, which is the failure this module exists to prevent. ``DRY_RUN`` is
    **not** read here (LV1.1): it governs orders, and this process places none.
    """
    if not market_data_enabled():
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


def _quote_details(symbols: Sequence[str]) -> dict[str, LiveQuote]:
    """Read-only Kite quotes with the previous close. Empty on any failure — the close stays.

    ``KiteProvider.quotes`` is the shared, rate-limited read path (one limiter token per batch of
    at most 500); nothing here builds a second client.
    """
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
        record.symbol.strip().upper(): LiveQuote(
            last_price=record.last_price,
            prev_close=(
                record.prev_close
                if record.prev_close is not None and record.prev_close > 0
                else None
            ),
            as_of=record.as_of,
        )
        for record in records
        if record.last_price is not None and record.last_price > 0
    }


def _quote_symbols(symbols: Sequence[str]) -> dict[str, Decimal]:
    """Read-only Kite quotes, last price only. Empty on any failure — the close stays in place."""
    return {symbol: quote.last_price for symbol, quote in _quote_details(symbols).items()}


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


#: Kite's quote endpoint accepts 500 instruments per call. A screen or sleeve page never needs more
#: than the rows it is about to show; asking for the whole universe would burn the rate limit.
MAX_LIVE_MARKS = 500


async def live_marks_for_symbols(symbols: Sequence[str]) -> dict[str, Decimal]:
    """Read-only last prices for a page of names — screens, scanners, and sleeve tables.

    Holdings first (the book already carries ``last_price``), then a quote for anything the book
    omitted. Empty when there is no trustworthy session. The caller keeps the close in that case.
    Ranks, factors and sleeve signals stay on the last published session; this is the display mark
    only.
    """
    wanted: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = raw.strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        wanted.append(symbol)
        if len(wanted) >= MAX_LIVE_MARKS:
            break
    if not wanted:
        return {}
    by_book = await anyio.to_thread.run_sync(live_prices_by_symbol)
    missing = [symbol for symbol in wanted if symbol not in by_book]
    if missing:
        quoted = await anyio.to_thread.run_sync(live_quotes_by_symbol, tuple(missing))
        by_book = {**by_book, **quoted}
    return {symbol: by_book[symbol] for symbol in wanted if symbol in by_book}


def live_quote_details(symbols: Sequence[str]) -> dict[str, LiveQuote]:
    """`{symbol: LiveQuote}` for a page of screen rows — the screens' live overlay (21 Sep 2026).

    Refuses unless :func:`quotes_permitted`; cached for :data:`CACHE_TTL_SECONDS` so a table that
    polls every 30 s from several tabs costs Kite at most one batch per window. At most
    :data:`MAX_LIVE_MARKS` names — a screen asks for the rows it shows, never the universe.
    """
    wanted: list[str] = []
    for raw in symbols:
        symbol = raw.strip().upper()
        if symbol and symbol not in wanted:
            wanted.append(symbol)
        if len(wanted) >= MAX_LIVE_MARKS:
            break
    if not wanted:
        return {}
    cached = _detail_memo.snapshot()
    missing = [symbol for symbol in wanted if symbol not in cached]
    if missing and quotes_permitted():
        fetched = _quote_details(missing)
        if fetched:
            _detail_memo.merge(fetched)
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
