"""``fo_index_daily``: the index's own daily OHLC from Kite's history (``docs/fno/03`` §9, F3).

The F&O bhavcopy carries no index candle and ``index_snapshot_daily`` keeps only a level, while
F3's levels, trend and weekly range (``04`` §11) need the highs and lows. So the two underlyings
are read from Kite's ``historical_data(interval="day")`` on the index token — one throttled call
per 2,000 days per index — and stored rounded to the paisa, idempotent on ``(underlying,
trade_date)`` (house rules 7 and 8).

Two entry points: :func:`backfill` for a range, :func:`extend` for the evening, which re-reads
the last few sessions so a candle Kite revised after the close is overwritten. Both take a
``DailyBarReader`` (the Kite adapter in production, a fake in tests) and a session; neither
places anything.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Protocol

import polars as pl
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.fno.directional import DailyBar
from baskfy_core.models import FoIndexDaily, Instrument
from baskfy_core.models.base import JsonObject

log = logging.getLogger("baskfy_worker.fno.index_daily")

#: ``fo_index_daily.underlying`` → the index's symbol in Kite's ``INDICES`` segment.
INDEX_SYMBOL_FOR: Final[dict[str, str]] = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}
SOURCE_KITE: Final = "KITE_HIST"
#: The evening re-reads this many calendar days back so a revised candle is overwritten.
EXTEND_OVERLAP_DAYS: Final = 7
#: Two years of history covers ``f3_pivot_lookback`` (250 max) with room for a re-test.
DEFAULT_BACKFILL_START: Final = dt.date(2024, 1, 1)
_CENT: Final = Decimal("0.01")
_UPSERT_CHUNK: Final = 1000


class DailyBarReader(Protocol):
    """``KiteProvider.daily_bars``: ``historical_data(interval="day")`` as a ``DAILY_BARS_SCHEMA``
    frame — the ``historical`` family (3 req/s), one call per 2,000 days."""

    def daily_bars(self, token: int, start: dt.date, end: dt.date) -> pl.DataFrame: ...


@dataclass(frozen=True, slots=True)
class IndexToken:
    underlying: str
    symbol: str
    kite_token: int


@dataclass(slots=True)
class IndexDailyReport:
    calls: int = 0
    written: dict[str, int] = field(default_factory=dict)
    latest: dict[str, str | None] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    def as_dict(self) -> JsonObject:
        return {
            "calls": self.calls,
            "written": dict(self.written),
            "latest": dict(self.latest),
            "skipped": list(self.skipped),
        }


def _cent(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


async def index_tokens(session: AsyncSession) -> dict[str, IndexToken]:
    """Each underlying's index as ``instrument`` knows it, with a Kite token; a missing row or
    token leaves the underlying out (reported, never guessed)."""
    symbols = tuple(INDEX_SYMBOL_FOR.values())
    rows = (
        await session.execute(
            select(Instrument.symbol, Instrument.kite_token).where(
                Instrument.symbol.in_(symbols),
                Instrument.instrument_type == "INDEX",
                Instrument.kite_token.is_not(None),
            )
        )
    ).all()
    by_symbol = {str(symbol): int(token) for symbol, token in rows if token is not None}
    return {
        underlying: IndexToken(underlying, symbol, by_symbol[symbol])
        for underlying, symbol in INDEX_SYMBOL_FOR.items()
        if symbol in by_symbol
    }


async def upsert_daily(session: AsyncSession, underlying: str, bars: pl.DataFrame) -> int:
    """Write the frame's candles for ``underlying``, overwriting a day already stored. Returns
    rows sent. A candle missing a price, or printing a high below its low, is left out."""
    if underlying not in INDEX_SYMBOL_FOR:
        raise ValueError(f"{underlying!r} is not an fo_index_daily underlying (03 §9)")
    values: list[dict[str, object]] = []
    for row in bars.select("date", "open", "high", "low", "close").iter_rows(named=True):
        prices = [row[k] for k in ("open", "high", "low", "close")]
        if row["date"] is None or any(p is None for p in prices):
            continue
        o, h, lo, c = (Decimal(str(p)) for p in prices)
        if h < lo:
            continue
        values.append(
            {
                "underlying": underlying,
                "trade_date": row["date"],
                "open": _cent(o),
                "high": _cent(h),
                "low": _cent(lo),
                "close": _cent(c),
                "source": SOURCE_KITE,
            }
        )
    for start in range(0, len(values), _UPSERT_CHUNK):
        chunk = values[start : start + _UPSERT_CHUNK]
        stmt = insert(FoIndexDaily).values(chunk)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[FoIndexDaily.underlying, FoIndexDaily.trade_date],
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "source": stmt.excluded.source,
                    "updated_at": func.now(),
                },
            )
        )
    return len(values)


async def latest_date(session: AsyncSession, underlying: str) -> dt.date | None:
    return (
        await session.execute(
            select(func.max(FoIndexDaily.trade_date)).where(FoIndexDaily.underlying == underlying)
        )
    ).scalar_one_or_none()


async def daily_bars(
    session: AsyncSession, underlying: str, *, end: dt.date, limit: int
) -> list[DailyBar]:
    """The last ``limit`` sessions of ``underlying`` at or before ``end``, oldest first — the
    shape the pure core reads."""
    rows = (
        await session.execute(
            select(FoIndexDaily)
            .where(FoIndexDaily.underlying == underlying, FoIndexDaily.trade_date <= end)
            .order_by(FoIndexDaily.trade_date.desc())
            .limit(limit)
        )
    ).scalars()
    bars = [DailyBar(r.trade_date, r.open, r.high, r.low, r.close) for r in rows]
    bars.reverse()
    return bars


async def _pull(  # noqa: PLR0913, PLR0917 - the session, the reader, the index, the window, the report
    session: AsyncSession,
    reader: DailyBarReader,
    token: IndexToken,
    start: dt.date,
    end: dt.date,
    report: IndexDailyReport,
) -> None:
    bars = reader.daily_bars(token.kite_token, start, end)
    report.calls += 1
    written = await upsert_daily(session, token.underlying, bars)
    report.written[token.underlying] = report.written.get(token.underlying, 0) + written
    latest = await latest_date(session, token.underlying)
    report.latest[token.underlying] = latest.isoformat() if latest else None
    log.info(
        "fo_index_daily %s: %d bars %s..%s",
        token.underlying,
        written,
        start.isoformat(),
        end.isoformat(),
    )


async def backfill(
    session: AsyncSession, reader: DailyBarReader, start: dt.date, end: dt.date
) -> IndexDailyReport:
    """Every session in ``[start, end]`` for both indices; a stored day is overwritten."""
    report = IndexDailyReport()
    tokens = await index_tokens(session)
    for underlying in INDEX_SYMBOL_FOR:
        token = tokens.get(underlying)
        if token is None:
            report.skipped.append(underlying)
            continue
        await _pull(session, reader, token, start, end, report)
    return report


async def extend(session: AsyncSession, reader: DailyBarReader, today: dt.date) -> IndexDailyReport:
    """The evening's extension: from a week before the latest stored day (or the default start
    when nothing is stored) to ``today``. A candle Kite revised is overwritten."""
    report = IndexDailyReport()
    tokens = await index_tokens(session)
    for underlying in INDEX_SYMBOL_FOR:
        token = tokens.get(underlying)
        if token is None:
            report.skipped.append(underlying)
            continue
        latest = await latest_date(session, underlying)
        start = (
            latest - dt.timedelta(days=EXTEND_OVERLAP_DAYS) if latest else DEFAULT_BACKFILL_START
        )
        await _pull(session, reader, token, min(start, today), today, report)
    return report
