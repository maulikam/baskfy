"""NIFTY 50 and INDIA VIX one-minute bars into ``op_index_minute`` (``docs/options/03`` §4, OP3).

Three writers, one upsert:

* **intraday** — each minute in session, the bars since the last one stored today;
* **end-of-day reconcile** — the whole session once more after the close, correcting any minute
  that was read while still forming or missed while the worker restarted;
* **the Tier-1 backfill** — ``[from, to]`` in Kite's 60-day minute windows, resumable: it starts
  from the newest bar already stored in the range, and commits after every window.

Only **closed** minutes are written (``ts + 1 min <= now``): a forming bar is not a fact yet, and
writing one would let a later read of the same minute disagree with an earlier one. The upsert is
keyed on ``(instrument_id, ts)`` and overwrites prices with Kite's latest reading, so re-running
any day produces identical rows (house rule 7). Prices are rounded to the column's two places at
write (house rule 8).

The index rows are found in ``instrument`` by symbol (``NIFTY 50``, ``INDIA VIX``, both
``INDEX``), with Kite's token from the same row — never a token literal.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import Instrument, OpIndexMinute
from baskfy_core.models.base import JsonObject
from baskfy_core.options.config import CalendarConfig
from baskfy_providers.kite import MINUTE_MAX_DAYS_PER_REQUEST
from baskfy_providers.records import MinuteBarRecord
from baskfy_worker.options.reads import BarReader

log = logging.getLogger("baskfy_worker.options.index_bars")

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))

#: ``03`` §4: the two indices the sleeves read, as Kite's ``INDICES`` segment names them.
NIFTY_50: Final = "NIFTY 50"
INDIA_VIX: Final = "INDIA VIX"
INDEX_SYMBOLS: Final[tuple[str, ...]] = (NIFTY_50, INDIA_VIX)

SOURCE_KITE: Final = "KITE_HIST"
_CENT: Final = Decimal("0.01")
_ONE_MINUTE: Final = dt.timedelta(minutes=1)
_UPSERT_CHUNK: Final = 2000


@dataclass(frozen=True, slots=True)
class IndexRow:
    """An index's ``instrument`` row: our id and Kite's token."""

    symbol: str
    instrument_id: int
    kite_token: int


@dataclass(slots=True)
class BarsReport:
    """What one pass did, per index; JSON-able for the task result and the CLI."""

    written: dict[str, int] = field(default_factory=dict)
    calls: int = 0
    skipped: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0

    def as_dict(self) -> JsonObject:
        return {
            "written": dict(self.written),
            "calls": self.calls,
            "skipped": dict(self.skipped),
            "seconds": round(self.seconds, 3),
        }


def session_bounds(
    day: dt.date, calendar: CalendarConfig | None = None
) -> tuple[dt.datetime, dt.datetime]:
    """09:15 and 15:30 IST on ``day``, aware."""
    cal = calendar or CalendarConfig()
    return (
        dt.datetime.combine(day, cal.market_open, tzinfo=IST),
        dt.datetime.combine(day, cal.market_close, tzinfo=IST),
    )


def closed_bars(bars: Sequence[MinuteBarRecord], now: dt.datetime) -> list[MinuteBarRecord]:
    """Only the minutes that have finished by ``now`` — a forming bar is not written."""
    return [bar for bar in bars if bar.ts + _ONE_MINUTE <= now]


def _cent(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


async def index_rows(session: AsyncSession) -> dict[str, IndexRow]:
    """``NIFTY 50`` and ``INDIA VIX`` as the ``instrument`` table knows them, with a Kite token."""
    rows = (
        await session.execute(
            select(Instrument.symbol, Instrument.id, Instrument.kite_token).where(
                Instrument.symbol.in_(INDEX_SYMBOLS),
                Instrument.instrument_type == "INDEX",
                Instrument.kite_token.is_not(None),
            )
        )
    ).all()
    return {
        str(symbol): IndexRow(str(symbol), int(ident), int(token))
        for symbol, ident, token in rows
        if token is not None
    }


async def upsert_bars(
    session: AsyncSession,
    instrument_id: int,
    bars: Sequence[MinuteBarRecord],
    *,
    source: str = SOURCE_KITE,
) -> int:
    """Write ``bars`` for one index; returns the rows sent. Idempotent on ``(instrument_id, ts)``.

    A bar whose high is below its low is not a bar Kite should send; it is dropped and logged,
    never "repaired" by swapping the two.
    """
    sane = [bar for bar in bars if bar.high >= bar.low]
    if len(sane) != len(bars):
        log.warning("dropped %d minute bars with high < low", len(bars) - len(sane))
    bars = sane
    count = 0
    for start in range(0, len(bars), _UPSERT_CHUNK):
        batch = bars[start : start + _UPSERT_CHUNK]
        values = [
            {
                "instrument_id": instrument_id,
                "ts": bar.ts.astimezone(IST),
                "open": _cent(bar.open),
                "high": _cent(bar.high),
                "low": _cent(bar.low),
                "close": _cent(bar.close),
                "source": source,
            }
            for bar in batch
        ]
        stmt = insert(OpIndexMinute).values(values)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["instrument_id", "ts"],
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "source": stmt.excluded.source,
                },
            )
        )
        count += len(values)
    return count


async def latest_ts(
    session: AsyncSession, instrument_id: int, lo: dt.datetime, hi: dt.datetime
) -> dt.datetime | None:
    """The newest stored minute for ``instrument_id`` within ``[lo, hi]``."""
    value = (
        await session.execute(
            select(func.max(OpIndexMinute.ts)).where(
                OpIndexMinute.instrument_id == instrument_id,
                OpIndexMinute.ts >= lo,
                OpIndexMinute.ts <= hi,
            )
        )
    ).scalar_one_or_none()
    return value


async def latest_close(
    session: AsyncSession, symbol: str, since: dt.datetime
) -> tuple[dt.datetime, Decimal] | None:
    """The newest stored close for ``symbol`` at or after ``since`` — the collector's ATM hint."""
    row = (
        await session.execute(
            select(OpIndexMinute.ts, OpIndexMinute.close)
            .join(Instrument, Instrument.id == OpIndexMinute.instrument_id)
            .where(Instrument.symbol == symbol, OpIndexMinute.ts >= since)
            .order_by(OpIndexMinute.ts.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return row[0], Decimal(row[1])


async def pull_intraday(session: AsyncSession, reader: BarReader, now: dt.datetime) -> BarsReport:
    """Each minute in session: the closed bars since the last one stored today, per index.

    One ``historical_data`` call per index (two a minute). Starts at the newest stored minute of
    today (re-reading it is harmless — the upsert is idempotent), or 09:15 if none.
    """
    started = time.monotonic()
    report = BarsReport()
    open_at, close_at = session_bounds(now.astimezone(IST).date())
    rows = await index_rows(session)
    for symbol in INDEX_SYMBOLS:
        row = rows.get(symbol)
        if row is None:
            report.skipped[symbol] = "no INDEX row with a Kite token in `instrument`"
            continue
        last = await latest_ts(session, row.instrument_id, open_at, close_at)
        lo = last or open_at
        hi = min(now, close_at)
        bars = reader.minute_bars(row.kite_token, lo, hi)
        report.calls += 1
        report.written[symbol] = await upsert_bars(
            session, row.instrument_id, closed_bars(bars, now)
        )
    report.seconds = time.monotonic() - started
    return report


async def reconcile_day(
    session: AsyncSession, reader: BarReader, day: dt.date, now: dt.datetime
) -> BarsReport:
    """After the close: the whole of ``day``'s session again, per index (two calls)."""
    started = time.monotonic()
    report = BarsReport()
    open_at, close_at = session_bounds(day)
    rows = await index_rows(session)
    for symbol in INDEX_SYMBOLS:
        row = rows.get(symbol)
        if row is None:
            report.skipped[symbol] = "no INDEX row with a Kite token in `instrument`"
            continue
        bars = reader.minute_bars(row.kite_token, open_at, close_at)
        report.calls += 1
        report.written[symbol] = await upsert_bars(
            session, row.instrument_id, closed_bars(bars, now)
        )
    report.seconds = time.monotonic() - started
    return report


async def backfill(  # noqa: PLR0913 - the range, the reader, the clock and the checkpoint
    session: AsyncSession,
    reader: BarReader,
    start: dt.date,
    end: dt.date,
    *,
    now: dt.datetime,
    checkpoint: Callable[[], Awaitable[None]] | None = None,
    windows: Callable[[dt.date, dt.date], Sequence[tuple[dt.date, dt.date]]] | None = None,
) -> BarsReport:
    """The Tier-1 backfill over ``[start, end]``, resumable and idempotent.

    Per index, it resumes from the date of the newest bar already stored in the range (that day
    is read again, so a half-written day completes) and walks forward in Kite's minute windows,
    calling ``checkpoint`` (a commit, from the CLI) after each so an interruption keeps what
    finished. An empty window is not an error: Kite's minute history starts somewhere (OP3's
    probe measures where) and the windows before it are simply empty.
    """
    if end < start:
        raise ValueError(f"end {end} precedes start {start}")
    started = time.monotonic()
    report = BarsReport()
    rows = await index_rows(session)
    split = windows or _day_windows
    for symbol in INDEX_SYMBOLS:
        row = rows.get(symbol)
        if row is None:
            report.skipped[symbol] = "no INDEX row with a Kite token in `instrument`"
            continue
        lo = dt.datetime.combine(start, dt.time(0, 0), tzinfo=IST)
        hi = dt.datetime.combine(end, dt.time(23, 59, 59), tzinfo=IST)
        last = await latest_ts(session, row.instrument_id, lo, hi)
        resume = max(start, last.astimezone(IST).date()) if last is not None else start
        written = 0
        for w_lo, w_hi in split(resume, end):
            bars = reader.minute_bars(row.kite_token, w_lo, w_hi)
            report.calls += 1
            written += await upsert_bars(session, row.instrument_id, closed_bars(bars, now))
            if checkpoint is not None:
                await checkpoint()
        report.written[symbol] = written
        log.info("index backfill %s: %d bars from %s to %s", symbol, written, resume, end)
    report.seconds = time.monotonic() - started
    return report


def _day_windows(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """Kite's 60-day minute windows. Mirrors ``KiteProvider.minute_windows`` for a bare reader."""
    out: list[tuple[dt.date, dt.date]] = []
    span = dt.timedelta(days=MINUTE_MAX_DAYS_PER_REQUEST - 1)
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + span, end)
        out.append((cursor, chunk_end))
        cursor = chunk_end + dt.timedelta(days=1)
    return out
