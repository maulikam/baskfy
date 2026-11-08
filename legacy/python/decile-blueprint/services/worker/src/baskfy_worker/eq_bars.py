"""One-minute equity bars for the liquid universe into ``eq_minute_bar`` (LV5).

The review's gap 1, and the precondition for its step 5: "Capture intraday equity bars for the
liquid universe, so live TWT/VBT variants can be *defined and backtested* rather than guessed."
Nothing here defines a variant. It captures the data one would be tested on.

Two writers, one upsert, the shape ``baskfy_worker.options.index_bars`` gave the index levels:

* **the session reconcile** — after the close (Beat 15:45), the whole session's minutes for
  every name the swing book calls liquid as of the last published session — one
  ``historical_data(interval="minute")`` call per name, ~570 names, about three minutes on the
  bulk lane at 3 req/s;
* **the backfill** — ``[from, to]`` in Kite's 60-day minute windows, per name, resumable: it
  starts from the newest bar already stored for the name in the range and commits after each
  window, so an interruption keeps what finished.

Only **closed** minutes are written (``ts + 1 min <= now``); the upsert is keyed on
``(instrument_id, ts)`` and overwrites prices with Kite's latest reading, so re-running any day
produces identical rows (house rule 7); prices are rounded to the column's two places at write
(rule 8); they are the exchange's raw prints, unadjusted.

**The universe is the swing book's** ``liquid_universe`` — one predicate, never a second — so
"the liquid universe" means the same names here as in the premarket scan. A name without a Kite
token is skipped and counted.

No live collector (``TICKS``) is built here: a live variant needs one, and the variant is step
5's, not this pack's (DECISIONS-LV LV5.1).
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

from baskfy_core.models import EqMinuteBar, Instrument
from baskfy_core.models.base import JsonObject
from baskfy_providers.kite import MINUTE_MAX_DAYS_PER_REQUEST
from baskfy_providers.records import MinuteBarRecord
from baskfy_worker.options.reads import BarReader
from baskfy_worker.tasks.swing import load_swing_config
from baskfy_worker.tasks.swing_premarket import liquid_universe

log = logging.getLogger("baskfy_worker.eq_bars")

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))
SOURCE_KITE: Final = "KITE_HIST"
MARKET_OPEN: Final = dt.time(9, 15)
MARKET_CLOSE: Final = dt.time(15, 30)
_CENT: Final = Decimal("0.01")
_ONE_MINUTE: Final = dt.timedelta(minutes=1)
_UPSERT_CHUNK: Final = 2000
#: Commit after this many names in the session reconcile, so a crash keeps most of the evening.
CHECKPOINT_EVERY_NAMES: Final = 25


@dataclass(frozen=True, slots=True)
class EqName:
    """One liquid name: our id, its symbol and Kite's token."""

    instrument_id: int
    symbol: str
    kite_token: int


@dataclass(slots=True)
class BarsReport:
    """What one pass did; JSON-able for the task result and the CLI."""

    names: int = 0
    calls: int = 0
    written: int = 0
    skipped: dict[str, str] = field(default_factory=dict)
    resumed_from: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0

    def as_dict(self) -> JsonObject:
        return {
            "names": self.names,
            "calls": self.calls,
            "written": self.written,
            "skipped": dict(self.skipped),
            "resumed_from": dict(self.resumed_from),
            "seconds": round(self.seconds, 3),
        }


def session_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """09:15 and 15:30 IST on ``day``, aware."""
    return (
        dt.datetime.combine(day, MARKET_OPEN, tzinfo=IST),
        dt.datetime.combine(day, MARKET_CLOSE, tzinfo=IST),
    )


def closed_bars(bars: Sequence[MinuteBarRecord], now: dt.datetime) -> list[MinuteBarRecord]:
    """Only the minutes that have finished by ``now`` — a forming bar is not written."""
    return [bar for bar in bars if bar.ts + _ONE_MINUTE <= now]


def day_windows(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """Kite's 60-day minute windows over ``[start, end]``, inclusive both ends."""
    if end < start:
        raise ValueError(f"end {end} precedes start {start}")
    out: list[tuple[dt.date, dt.date]] = []
    span = dt.timedelta(days=MINUTE_MAX_DAYS_PER_REQUEST - 1)
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + span, end)
        out.append((cursor, chunk_end))
        cursor = chunk_end + dt.timedelta(days=1)
    return out


def _cent(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


async def liquid_names(session: AsyncSession, *, as_of: dt.date, user_id: int) -> list[EqName]:
    """The swing book's liquid universe as of ``as_of``, with each name's Kite token.

    The same ``liquid_universe`` the premarket scan and "Scan now" use, over the sole user's swing
    settings — one predicate. A name the ``instrument`` table has no token for cannot be read
    from Kite's history and is left out (the caller counts it).
    """
    config = await load_swing_config(session, user_id)
    universe = await liquid_universe(session, as_of=as_of, config=config)
    if not universe:
        return []
    ids = [int(name.instrument_id) for name in universe]
    rows = (
        await session.execute(
            select(Instrument.id, Instrument.symbol, Instrument.kite_token).where(
                Instrument.id.in_(ids)
            )
        )
    ).all()
    tokens = {int(ident): token for ident, _symbol, token in rows}
    out: list[EqName] = []
    for name in universe:
        token = tokens.get(int(name.instrument_id))
        if token is None:
            continue
        out.append(EqName(int(name.instrument_id), str(name.symbol), int(token)))
    return out


async def upsert_bars(
    session: AsyncSession,
    instrument_id: int,
    bars: Sequence[MinuteBarRecord],
    *,
    source: str = SOURCE_KITE,
) -> int:
    """Write ``bars`` for one name; returns the rows sent. Idempotent on ``(instrument_id, ts)``.

    A bar whose high is below its low is not a bar Kite should send; it is dropped and logged,
    never "repaired" by swapping the two.
    """
    sane = [bar for bar in bars if bar.high >= bar.low]
    if len(sane) != len(bars):
        log.warning("dropped %d minute bars with high < low", len(bars) - len(sane))
    count = 0
    for start in range(0, len(sane), _UPSERT_CHUNK):
        batch = sane[start : start + _UPSERT_CHUNK]
        values = [
            {
                "instrument_id": instrument_id,
                "ts": bar.ts.astimezone(IST),
                "open": _cent(bar.open),
                "high": _cent(bar.high),
                "low": _cent(bar.low),
                "close": _cent(bar.close),
                "volume": int(bar.volume),
                "source": source,
            }
            for bar in batch
        ]
        stmt = insert(EqMinuteBar).values(values)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["instrument_id", "ts"],
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume,
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
    return (
        await session.execute(
            select(func.max(EqMinuteBar.ts)).where(
                EqMinuteBar.instrument_id == instrument_id,
                EqMinuteBar.ts >= lo,
                EqMinuteBar.ts <= hi,
            )
        )
    ).scalar_one_or_none()


Checkpoint = Callable[[], Awaitable[None]]


async def reconcile_session(  # noqa: PLR0913 - the day, the reader, the clock, the names and the checkpoint
    session: AsyncSession,
    reader: BarReader,
    day: dt.date,
    *,
    now: dt.datetime,
    names: Sequence[EqName],
    checkpoint: Checkpoint | None = None,
) -> BarsReport:
    """After the close: the whole of ``day``'s session, one call per name; closed minutes only."""
    started = time.monotonic()
    report = BarsReport(names=len(names))
    open_at, close_at = session_bounds(day)
    for index, name in enumerate(names, start=1):
        try:
            bars = reader.minute_bars(name.kite_token, open_at, close_at)
        except Exception as exc:
            report.skipped[name.symbol] = f"{type(exc).__name__}: {exc}"
            continue
        report.calls += 1
        report.written += await upsert_bars(session, name.instrument_id, closed_bars(bars, now))
        if checkpoint is not None and index % CHECKPOINT_EVERY_NAMES == 0:
            await checkpoint()
    if checkpoint is not None:
        await checkpoint()
    report.seconds = time.monotonic() - started
    return report


async def backfill(  # noqa: PLR0913 - the range, the reader, the clock, the names and the checkpoint
    session: AsyncSession,
    reader: BarReader,
    start: dt.date,
    end: dt.date,
    *,
    now: dt.datetime,
    names: Sequence[EqName],
    checkpoint: Checkpoint | None = None,
    windows: Callable[[dt.date, dt.date], Sequence[tuple[dt.date, dt.date]]] | None = None,
) -> BarsReport:
    """``[start, end]`` per name, resumable and idempotent.

    Per name, it resumes from the date of the newest bar already stored in the range (that day
    is read again, so a half-written day completes) and walks forward in Kite's minute windows,
    calling ``checkpoint`` (a commit, from the CLI) after each. An empty window is not an error:
    Kite's minute history starts somewhere, and a name listed later simply has no earlier bars.
    """
    if end < start:
        raise ValueError(f"end {end} precedes start {start}")
    started = time.monotonic()
    report = BarsReport(names=len(names))
    split = windows or day_windows
    lo = dt.datetime.combine(start, dt.time(0, 0), tzinfo=IST)
    hi = dt.datetime.combine(end, dt.time(23, 59, 59), tzinfo=IST)
    for name in names:
        last = await latest_ts(session, name.instrument_id, lo, hi)
        resume = max(start, last.astimezone(IST).date()) if last is not None else start
        if last is not None:
            report.resumed_from[name.symbol] = resume.isoformat()
        for w_lo, w_hi in split(resume, end):
            try:
                bars = reader.minute_bars(name.kite_token, w_lo, w_hi)
            except Exception as exc:
                report.skipped[f"{name.symbol} {w_lo}..{w_hi}"] = f"{type(exc).__name__}: {exc}"
                continue
            report.calls += 1
            report.written += await upsert_bars(session, name.instrument_id, closed_bars(bars, now))
            if checkpoint is not None:
                await checkpoint()
    report.seconds = time.monotonic() - started
    return report
