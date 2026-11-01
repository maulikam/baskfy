from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Final

import anyio
import polars as pl
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.live_prices import kite_read_provider, quotes_permitted
from baskfy_core.market_hours_cb import IST
from baskfy_core.models import (
    CorporateAction,
    IndexDef,
    IndexMemberDaily,
    IndexSnapshotDaily,
    Instrument,
    OhlcvDaily,
    TradingDay,
)
from baskfy_core.models.fno import FoUnderlyingDaily
from baskfy_core.opportunities.config import (
    DEFAULT_OPTIONS_PARAMS,
    DEFAULT_SWING_PARAMS,
    DataStatus,
    SetupType,
    SwingFilters,
)
from baskfy_core.opportunities.indicators import with_indicators
from baskfy_core.opportunities.options import (
    ContractQuote,
    OptionsRequest,
    OptionsResult,
    analyze,
)
from baskfy_core.opportunities.swing import (
    InstrumentContext,
    ScanCounts,
    SwingScanResult,
    missing_sessions,
    scan,
)
from baskfy_core.opportunities.validation import ValidationResult, validate_setup
from baskfy_core.options.config import CostRates, OptionType
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.universes import SECTOR_INDEX_SLUGS
from baskfy_providers.errors import CredentialsMissing, ProviderError

CASH_SERIES: Final[tuple[str, ...]] = ("EQ", "BE")
BENCHMARK_SLUGS: Final[tuple[str, ...]] = ("nifty-500", "nifty-50")
SESSION_DATA_READY_IST: Final = dt.time(18, 0)
HISTORY_CALENDAR_DAYS: Final = 420
MAX_UNIVERSE: Final = 1000
MAX_RESULTS: Final = 100
MAX_VALIDATION_DAYS: Final = 3 * 366
VALIDATION_MAX_UNIVERSE: Final = 500
CACHE_TTL_SECONDS: Final = 300.0
CONTRACT_CACHE_TTL_SECONDS: Final = 6 * 3600.0
MAX_CONCURRENT_SCANS: Final = 2
UPCOMING_ACTION_DAYS: Final = 30
INDEX_SPOT_KEYS: Final[dict[str, str]] = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
}
KITE_DEPENDENCY: Final = (
    "Kite Connect read session: BASKFY_KITE_API_KEY set, BASKFY_LIVE_QUOTES on, and a broker "
    "login completed today (quote + NFO instrument master)."
)

ProgressFn = Callable[[str, int, int], Awaitable[None]]


class ScanCancelled(Exception):
    pass


class UniverseUnavailable(Exception):
    pass


_scan_slots = asyncio.Semaphore(MAX_CONCURRENT_SCANS)


@dataclass(frozen=True, slots=True)
class Freshness:
    data_as_of: dt.date | None
    expected: dt.date | None
    status: DataStatus
    reason: str | None


@dataclass(frozen=True, slots=True)
class SwingScan:
    scan_id: str
    scanned_at: dt.datetime
    universe: str
    freshness: Freshness
    result: SwingScanResult
    warnings: tuple[str, ...] = field(default_factory=tuple)


class _TtlCache[V]:
    def __init__(self, ttl: float, size: int = 32) -> None:
        self._ttl = ttl
        self._size = size
        self._items: dict[str, tuple[float, V]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> V | None:
        with self._lock:
            hit = self._items.get(key)
            if hit is None or time.monotonic() - hit[0] > self._ttl:
                self._items.pop(key, None)
                return None
            return hit[1]

    def put(self, key: str, value: V) -> None:
        with self._lock:
            if len(self._items) >= self._size:
                oldest = min(self._items, key=lambda k: self._items[k][0])
                self._items.pop(oldest, None)
            self._items[key] = (time.monotonic(), value)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_scan_cache: _TtlCache[SwingScan] = _TtlCache(CACHE_TTL_SECONDS)
_contract_cache: _TtlCache[list[ContractQuote]] = _TtlCache(CONTRACT_CACHE_TTL_SECONDS, size=64)


def reset_caches() -> None:
    _scan_cache.clear()
    _contract_cache.clear()


def expected_session(now: dt.datetime, trading_days: Sequence[dt.date]) -> dt.date | None:
    local = now.astimezone(IST)
    cutoff = local.date()
    if local.time() < SESSION_DATA_READY_IST:
        cutoff -= dt.timedelta(days=1)
    eligible = [d for d in trading_days if d <= cutoff]
    return max(eligible) if eligible else None


def freshness(data_as_of: dt.date | None, expected: dt.date | None) -> Freshness:
    if data_as_of is None:
        return Freshness(None, expected, DataStatus.UNAVAILABLE, "No daily bars are stored.")
    if expected is not None and data_as_of < expected:
        return Freshness(
            data_as_of,
            expected,
            DataStatus.STALE,
            f"Latest stored session is {data_as_of.isoformat()}; "
            f"{expected.isoformat()} has closed and its bars are not loaded yet.",
        )
    return Freshness(data_as_of, expected, DataStatus.FRESH, None)


async def universes(session: AsyncSession) -> list[tuple[str, str]]:
    rows = await session.execute(
        select(IndexDef.slug, IndexDef.name)
        .where(IndexDef.is_universe.is_(True))
        .order_by(IndexDef.sort_order, IndexDef.slug)
    )
    return [(str(slug), str(name)) for slug, name in rows]


async def sectors(session: AsyncSession) -> list[tuple[str, str]]:
    rows = await session.execute(
        select(IndexDef.slug, IndexDef.name)
        .where(IndexDef.slug.in_(SECTOR_INDEX_SLUGS))
        .order_by(IndexDef.slug)
    )
    return [(str(slug), str(name)) for slug, name in rows]


async def _trading_days(session: AsyncSession, start: dt.date, end: dt.date) -> list[dt.date]:
    rows = await session.execute(
        select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date >= start,
            TradingDay.date <= end,
        )
        .order_by(TradingDay.date)
    )
    return [row[0] for row in rows]


async def latest_bar_date(session: AsyncSession, on_or_before: dt.date) -> dt.date | None:
    value = await session.scalar(
        select(func.max(OhlcvDaily.date)).where(OhlcvDaily.date <= on_or_before)
    )
    return value if isinstance(value, dt.date) else None


async def current_freshness(session: AsyncSession, now: dt.datetime) -> Freshness:
    today = now.astimezone(IST).date()
    days = await _trading_days(session, today - dt.timedelta(days=15), today)
    as_of = await latest_bar_date(session, today)
    return freshness(as_of, expected_session(now, days))


async def _index_id(session: AsyncSession, slug: str) -> int | None:
    value = await session.scalar(select(IndexDef.id).where(IndexDef.slug == slug))
    return int(value) if value is not None else None


async def members_on(session: AsyncSession, slug: str, on: dt.date) -> frozenset[int]:
    index_id = await _index_id(session, slug)
    if index_id is None:
        raise UniverseUnavailable(f"Universe {slug!r} is not defined.")
    latest = await session.scalar(
        select(func.max(IndexMemberDaily.date)).where(
            IndexMemberDaily.index_id == index_id, IndexMemberDaily.date <= on
        )
    )
    if latest is None:
        raise UniverseUnavailable(f"Universe {slug!r} has no membership rows on or before {on}.")
    rows = await session.execute(
        select(IndexMemberDaily.instrument_id).where(
            IndexMemberDaily.index_id == index_id, IndexMemberDaily.date == latest
        )
    )
    return frozenset(int(r[0]) for r in rows)


async def membership_history(
    session: AsyncSession, slug: str, start: dt.date, end: dt.date
) -> dict[dt.date, frozenset[int]]:
    index_id = await _index_id(session, slug)
    if index_id is None:
        raise UniverseUnavailable(f"Universe {slug!r} is not defined.")
    first = await session.scalar(
        select(func.max(IndexMemberDaily.date)).where(
            IndexMemberDaily.index_id == index_id, IndexMemberDaily.date <= start
        )
    )
    rows = await session.execute(
        select(IndexMemberDaily.date, IndexMemberDaily.instrument_id).where(
            IndexMemberDaily.index_id == index_id,
            IndexMemberDaily.date >= (first or start),
            IndexMemberDaily.date <= end,
        )
    )
    grouped: dict[dt.date, set[int]] = defaultdict(set)
    for day, instrument_id in rows:
        grouped[day].add(int(instrument_id))
    return {day: frozenset(ids) for day, ids in grouped.items()}


BAR_SCHEMA: Final = pl.Schema(
    {
        "instrument_id": pl.Int64,
        "symbol": pl.String,
        "date": pl.Date,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Float64,
        "turnover": pl.Float64,
        "adj_factor": pl.Float64,
    }
)


async def load_bars(
    session: AsyncSession, ids: frozenset[int], start: dt.date, end: dt.date
) -> pl.DataFrame:
    if not ids:
        return pl.DataFrame(schema=BAR_SCHEMA)
    result = await session.execute(
        select(
            OhlcvDaily.instrument_id,
            Instrument.symbol,
            OhlcvDaily.date,
            OhlcvDaily.open,
            OhlcvDaily.high,
            OhlcvDaily.low,
            OhlcvDaily.close,
            OhlcvDaily.volume,
            OhlcvDaily.turnover,
            OhlcvDaily.adj_factor,
        )
        .join(Instrument, Instrument.id == OhlcvDaily.instrument_id)
        .where(
            OhlcvDaily.instrument_id.in_(sorted(ids)),
            OhlcvDaily.date >= start,
            OhlcvDaily.date <= end,
            Instrument.instrument_type == "EQ",
            Instrument.series.in_(CASH_SERIES),
        )
        .order_by(OhlcvDaily.instrument_id, OhlcvDaily.date)
    )
    rows = [
        {
            "instrument_id": int(r.instrument_id),
            "symbol": r.symbol,
            "date": r.date,
            "open": float(r.open),
            "high": float(r.high),
            "low": float(r.low),
            "close": float(r.close),
            "volume": float(r.volume),
            "turnover": None if r.turnover is None else float(r.turnover),
            "adj_factor": float(r.adj_factor) if r.adj_factor is not None else 1.0,
        }
        for r in result
    ]
    return pl.DataFrame(rows, schema=BAR_SCHEMA) if rows else pl.DataFrame(schema=BAR_SCHEMA)


async def load_benchmark(
    session: AsyncSession, start: dt.date, end: dt.date
) -> tuple[str | None, pl.DataFrame | None]:
    for slug in BENCHMARK_SLUGS:
        rows = (
            await session.execute(
                select(IndexSnapshotDaily.date, IndexSnapshotDaily.level)
                .join(IndexDef, IndexDef.id == IndexSnapshotDaily.index_id)
                .where(
                    IndexDef.slug == slug,
                    IndexSnapshotDaily.date >= start,
                    IndexSnapshotDaily.date <= end,
                    IndexSnapshotDaily.level.is_not(None),
                )
                .order_by(IndexSnapshotDaily.date)
            )
        ).all()
        if rows:
            frame = pl.DataFrame(
                {"date": [r[0] for r in rows], "level": [float(r[1]) for r in rows]},
                schema={"date": pl.Date, "level": pl.Float64},
            )
            return slug, frame
    return None, None


async def _sector_map(session: AsyncSession, on: dt.date) -> dict[int, str]:
    latest = await session.scalar(
        select(func.max(IndexMemberDaily.date))
        .join(IndexDef, IndexDef.id == IndexMemberDaily.index_id)
        .where(IndexDef.slug.in_(SECTOR_INDEX_SLUGS), IndexMemberDaily.date <= on)
    )
    if latest is None:
        return {}
    rows = await session.execute(
        select(
            IndexMemberDaily.instrument_id,
            IndexDef.slug,
            func.count().over(partition_by=IndexMemberDaily.index_id),
        )
        .join(IndexDef, IndexDef.id == IndexMemberDaily.index_id)
        .where(IndexMemberDaily.date == latest, IndexDef.slug.in_(SECTOR_INDEX_SLUGS))
    )
    best: dict[int, tuple[int, str]] = {}
    for instrument_id, slug, members in rows:
        current = best.get(int(instrument_id))
        if current is None or int(members) < current[0]:
            best[int(instrument_id)] = (int(members), str(slug))
    return {key: slug for key, (_, slug) in best.items()}


async def _contexts(
    session: AsyncSession,
    ids: frozenset[int],
    as_of: dt.date,
    bars: pl.DataFrame,
    calendar: Sequence[dt.date],
) -> dict[int, InstrumentContext]:
    data = DEFAULT_SWING_PARAMS.data
    names = dict(
        (int(i), str(n))
        for i, n in await session.execute(
            select(Instrument.id, Instrument.name).where(Instrument.id.in_(sorted(ids)))
        )
    )
    sectors_by_id = await _sector_map(session, as_of)
    window = sorted(calendar)[-data.window_sessions :]
    window_start = window[0] if window else as_of
    actions = await session.execute(
        select(CorporateAction.instrument_id, CorporateAction.ex_date, CorporateAction.action_type)
        .where(
            CorporateAction.instrument_id.in_(sorted(ids)),
            CorporateAction.ex_date >= window_start,
            CorporateAction.ex_date <= as_of + dt.timedelta(days=UPCOMING_ACTION_DAYS),
        )
        .order_by(CorporateAction.ex_date)
    )
    recent: dict[int, list[tuple[dt.date, str]]] = defaultdict(list)
    upcoming: dict[int, list[tuple[dt.date, str]]] = defaultdict(list)
    for instrument_id, ex_date, kind in actions:
        target = upcoming if ex_date > as_of else recent
        target[int(instrument_id)].append((ex_date, str(kind)))
    bans = await _fno_bans(session, as_of)
    present: dict[int, list[dt.date]] = defaultdict(list)
    recent_bars = bars.filter(pl.col("date") >= window_start).select("instrument_id", "date")
    for instrument_id, day in recent_bars.iter_rows():
        present[int(instrument_id)].append(day)
    symbols = dict(bars.select("instrument_id", "symbol").unique().iter_rows())
    return {
        i: InstrumentContext(
            name=names.get(i, ""),
            sector=sectors_by_id.get(i),
            missing_sessions=missing_sessions(present.get(i, []), window, data.window_sessions),
            recent_actions=tuple(recent.get(i, [])),
            upcoming_actions=tuple(upcoming.get(i, [])),
            in_fno_ban=str(symbols.get(i, "")) in bans,
        )
        for i in ids
    }


async def _fno_bans(session: AsyncSession, as_of: dt.date) -> frozenset[str]:
    latest = await session.scalar(
        select(func.max(FoUnderlyingDaily.trade_date)).where(FoUnderlyingDaily.trade_date <= as_of)
    )
    if latest is None:
        return frozenset()
    rows = await session.execute(
        select(FoUnderlyingDaily.symbol).where(
            FoUnderlyingDaily.trade_date == latest, FoUnderlyingDaily.in_ban.is_(True)
        )
    )
    return frozenset(str(r[0]) for r in rows)


def cache_key(universe: str, filters: SwingFilters, as_of: dt.date | None) -> str:
    payload = {
        "universe": universe,
        "as_of": None if as_of is None else as_of.isoformat(),
        "filters": {
            "setups": sorted(s.value for s in filters.setups),
            "sectors": sorted(filters.sectors),
            "min_turnover_cr": filters.min_turnover_cr,
            "min_price": filters.min_price,
            "horizon": [filters.horizon_min, filters.horizon_max],
            "atr_pct": [filters.atr_pct_min, filters.atr_pct_max],
            "min_rr": filters.min_rr,
            "limit": filters.limit,
        },
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def _noop_progress(stage: str, done: int, total: int) -> None:
    return None


async def run_swing_scan(  # noqa: PLR0913 - the request, its clock and its progress hooks
    session: AsyncSession,
    *,
    universe: str,
    filters: SwingFilters,
    now: dt.datetime,
    progress: ProgressFn = _noop_progress,
    cancelled: Callable[[], Awaitable[bool]] | None = None,
) -> SwingScan:
    total = 5

    async def step(stage: str, done: int) -> None:
        if cancelled is not None and await cancelled():
            raise ScanCancelled(stage)
        await progress(stage, done, total)

    await step("Checking data freshness", 0)
    fresh = await current_freshness(session, now)
    key = cache_key(universe, filters, fresh.data_as_of)
    cached = _scan_cache.get(key)
    if cached is not None:
        await progress("Served from cache", total, total)
        return cached
    scan_id = uuid.uuid4().hex
    if fresh.data_as_of is None:
        return SwingScan(scan_id, now, universe, fresh, SwingScanResult(None, ScanCounts(), ()))
    as_of = fresh.data_as_of
    async with _scan_slots:
        await step("Resolving universe", 1)
        ids = await members_on(session, universe, as_of)
        warnings: list[str] = []
        if len(ids) > MAX_UNIVERSE:
            warnings.append(f"Universe trimmed to {MAX_UNIVERSE} names for this scan.")
            ids = frozenset(sorted(ids)[:MAX_UNIVERSE])
        start = as_of - dt.timedelta(days=HISTORY_CALENDAR_DAYS)
        await step("Loading daily bars", 2)
        bars = await load_bars(session, ids, start, as_of)
        bench_slug, bench = await load_benchmark(session, start, as_of)
        if bench_slug is None:
            warnings.append("No benchmark series stored; relative-strength setup unavailable.")
        elif bench_slug != BENCHMARK_SLUGS[0]:
            warnings.append(f"Benchmark fell back to {bench_slug}.")
        calendar = await _trading_days(session, start, as_of)
        contexts = await _contexts(session, ids, as_of, bars, calendar)
        await step("Computing indicators", 3)
        indicated = await anyio.to_thread.run_sync(lambda: with_indicators(bars, bench))
        await step("Evaluating setups", 4)
        result = await anyio.to_thread.run_sync(lambda: scan(indicated, as_of, contexts, filters))
    out = SwingScan(
        scan_id,
        now,
        universe,
        fresh,
        result,
        tuple(warnings) + result.warnings,
    )
    _scan_cache.put(key, out)
    await progress("Done", total, total)
    return out


async def run_validation(  # noqa: PLR0913 - the full test specification
    session: AsyncSession,
    *,
    setup: SetupType,
    universe: str,
    start: dt.date,
    end: dt.date,
    filters: SwingFilters,
    slippage_bps: float,
) -> ValidationResult:
    async with _scan_slots:
        history = await membership_history(session, universe, start, end)
        ids: frozenset[int] = frozenset().union(*history.values()) if history else frozenset()
        if not ids:
            raise UniverseUnavailable(
                f"Universe {universe!r} has no membership rows between {start} and {end}."
            )
        if len(ids) > VALIDATION_MAX_UNIVERSE:
            ids = frozenset(sorted(ids)[:VALIDATION_MAX_UNIVERSE])
        warm = start - dt.timedelta(days=HISTORY_CALENDAR_DAYS)
        tail = end + dt.timedelta(days=DEFAULT_SWING_PARAMS.relative_strength.horizon_max * 2)
        bars = await load_bars(session, ids, warm, tail)
        _, bench = await load_benchmark(session, warm, tail)
        indicated = await anyio.to_thread.run_sync(lambda: with_indicators(bars, bench))
        return await anyio.to_thread.run_sync(
            lambda: validate_setup(
                indicated,
                setup,
                start,
                end,
                membership=history,
                filters=filters,
                slippage_bps=slippage_bps,
            )
        )


@dataclass(frozen=True, slots=True)
class ProviderState:
    available: bool
    reason: str | None
    dependency: str | None


@dataclass(frozen=True, slots=True)
class OptionsAnalysis:
    provider: ProviderState
    spot: Decimal | None
    spot_as_of: dt.datetime | None
    analyzed_at: dt.datetime
    status: DataStatus
    result: OptionsResult | None
    warnings: tuple[str, ...]


def provider_state() -> ProviderState:
    if not quotes_permitted():
        return ProviderState(
            False,
            "No live Kite session is available to this API, so option quotes cannot be read.",
            KITE_DEPENDENCY,
        )
    return ProviderState(True, None, None)


def _contracts_for(underlying: str) -> list[ContractQuote]:
    cached = _contract_cache.get(underlying)
    if cached is not None:
        return cached
    provider = kite_read_provider()
    if provider is None:
        raise CredentialsMissing("BASKFY_KITE_API_KEY is not set")
    contracts = [
        ContractQuote(
            tradingsymbol=c.tradingsymbol,
            underlying=c.underlying,
            expiry=c.expiry,
            strike=c.strike,
            option_type=OptionType(c.option_type),
            lot_size=c.lot_size,
            bid=None,
            ask=None,
            volume=None,
            oi=None,
            quote_ts=None,
        )
        for c in provider.option_contracts(underlying)
    ]
    _contract_cache.put(underlying, contracts)
    return contracts


def _quote_contracts(
    request: OptionsRequest, now: dt.datetime
) -> tuple[Decimal | None, dt.datetime | None, list[ContractQuote]]:
    provider = kite_read_provider()
    if provider is None:
        raise CredentialsMissing("BASKFY_KITE_API_KEY is not set")
    underlying = request.underlying.upper()
    spot_key = INDEX_SPOT_KEYS.get(underlying, f"NSE:{underlying}")
    spot_rows = provider.option_quotes([spot_key])
    spot_row = next((r for r in spot_rows if r.key == spot_key), None)
    if spot_row is None or spot_row.last_price is None or spot_row.last_price <= 0:
        return None, None, []
    spot = spot_row.last_price
    today = now.astimezone(IST).date()
    contracts = [
        c
        for c in _contracts_for(underlying)
        if request.min_dte <= (c.expiry - today).days <= request.max_dte
    ]
    expiries = sorted({c.expiry for c in contracts})[: DEFAULT_OPTIONS_PARAMS.max_expiries]
    wanted: list[ContractQuote] = []
    each_side = DEFAULT_OPTIONS_PARAMS.strikes_each_side + 1
    for expiry in expiries:
        pool = [c for c in contracts if c.expiry == expiry]
        strikes = sorted({c.strike for c in pool}, key=lambda k: (abs(k - spot), k))
        keep = set(strikes[: 2 * each_side + 1])
        wanted.extend(c for c in pool if c.strike in keep)
    quotes = {
        r.symbol: r for r in provider.option_quotes([f"NFO:{c.tradingsymbol}" for c in wanted])
    }
    out: list[ContractQuote] = []
    for contract in wanted:
        q = quotes.get(contract.tradingsymbol)
        out.append(
            ContractQuote(
                tradingsymbol=contract.tradingsymbol,
                underlying=contract.underlying,
                expiry=contract.expiry,
                strike=contract.strike,
                option_type=contract.option_type,
                lot_size=contract.lot_size,
                bid=q.bids[0].price if q is not None and q.bids else None,
                ask=q.asks[0].price if q is not None and q.asks else None,
                volume=None if q is None else q.volume,
                oi=None if q is None else q.oi,
                quote_ts=None if q is None else q.as_of,
            )
        )
    return spot, spot_row.as_of, out


async def run_options_analysis(
    request: OptionsRequest, *, now: dt.datetime, max_quote_age_s: int
) -> OptionsAnalysis:
    state = provider_state()
    if not state.available:
        return OptionsAnalysis(state, None, None, now, DataStatus.UNAVAILABLE, None, ())
    try:
        spot, spot_ts, quotes = await anyio.to_thread.run_sync(
            lambda: _quote_contracts(request, now)
        )
    except (ProviderError, CredentialsMissing, OSError) as exc:
        failed = ProviderState(False, f"Kite read failed: {exc}", KITE_DEPENDENCY)
        return OptionsAnalysis(failed, None, None, now, DataStatus.UNAVAILABLE, None, ())
    if spot is None:
        return OptionsAnalysis(
            state,
            None,
            None,
            now,
            DataStatus.UNAVAILABLE,
            None,
            (f"No live quote for underlying {request.underlying}.",),
        )
    warnings: list[str] = []
    status = DataStatus.FRESH
    if spot_ts is None or (now - spot_ts).total_seconds() > max_quote_age_s:
        status = DataStatus.STALE
        warnings.append("Underlying quote is older than the quote-age limit (market may be shut).")
    if not quotes:
        warnings.append(f"{request.underlying} has no listed options in the requested window.")
    result = analyze(request, quotes, spot=spot, now=now, rates=CostRates())
    if status is DataStatus.STALE:
        result = OptionsResult(
            candidates=tuple(replace(c, actionable=False) for c in result.candidates),
            rejected=result.rejected,
            expiries=result.expiries,
            warnings=result.warnings,
        )
    return OptionsAnalysis(state, spot, spot_ts, now, status, result, tuple(warnings))
