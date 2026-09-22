"""The once-a-minute options scan: every sleeve's state and candidates into ``op_scan`` (OP4).

``docs/options/06`` OP4: "Worker task ``baskfy.options.scan`` (each minute after the collector,
behind ``BASKFY_OPTIONS_SCAN_ENABLED``) → ``op_scan`` rows from ``op_index_minute`` +
``op_chain_snapshot``." The computation is ``baskfy_core.options.scan.scan_all`` — pure, the same
functions the plan builders will call; this module only reads its inputs and writes its rows.

What it reads, all from the database (PACK.11: **no Kite call, ever** — the collector made the
minute's only quote call, and the web tab adds no load):

* NIFTY 50's one-minute bars of today (``op_index_minute``, written by OP3's index-bar task);
* NIFTY 50's daily closes before today and India VIX's previous close (``index_snapshot_daily``,
  slugs ``nifty-50`` and ``india-vix``; VIX falls back to the last stored ``INDIA VIX`` minute
  before today) — O2's trend reads NIFTY 50 on purpose (PACK.5), not the swing gate's index;
* the NIFTY master (``op_contract``), the NSE calendar (``trading_day``);
* the chain snapshots :func:`~baskfy_core.options.scan.wanted_minutes` asks for — the latest
  minute, and each decided sleeve's decision minute (OP4.4);
* the sole tenant's event days, sleeve and book config, today's sessions and real journal count.

What it writes: one ``op_scan`` row per sleeve per minute, upserted on ``(user_id, sleeve, ts)`` —
re-running a minute rewrites the same row with the same values (idempotent, house rule 7).

Gated, cheapest first and every refusal before a database session (OP4.10): the scan flag, the
collector's flag (with no collector there is nothing to scan), the 09:15-15:30 window, a
configured sole tenant — then, inside the session, the NSE calendar. It reads prices and writes
advisory rows; it has no order path (law 2) and moves no money.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import (
    IndexDef,
    IndexSnapshotDaily,
    Instrument,
    OpBookConfig,
    OpChainSnapshot,
    OpEventDay,
    OpIndexMinute,
    OpJournal,
    OpScan,
    OpSession,
    OpSleeveConfig,
)
from baskfy_core.models.base import JsonObject
from baskfy_core.options.bars import Bar
from baskfy_core.options.chain import Level, OptionQuote
from baskfy_core.options.config import (
    Mode,
    OptionsCeilings,
    OptionsConfig,
    OptionType,
    Sleeve,
    group_of,
)
from baskfy_core.options.risk import is_paused
from baskfy_core.options.scan import (
    DayContext,
    MarketDay,
    ScanResult,
    SleeveContext,
    Snapshot,
    SnapshotBook,
    scan_all,
    scan_minute,
    wanted_minutes,
)
from baskfy_core.options.session import SessionState
from baskfy_core.options.structures import SleeveBook
from baskfy_worker.options import options_gates
from baskfy_worker.options.collector import in_session
from baskfy_worker.options.index_bars import INDIA_VIX, IST, NIFTY_50, session_bounds
from baskfy_worker.options.master import load_contracts

log = logging.getLogger("baskfy_worker.options.scan")

#: ``index_snapshot_daily`` slugs (``baskfy_core.universes.slugify_index`` of NSE's names).
NIFTY_50_SLUG: Final = "nifty-50"
INDIA_VIX_SLUG: Final = "india-vix"
#: How many daily closes the EMA is fed: ample, so the SMA seed has decayed (OP4.8).
DAILY_HISTORY: Final = 120


@dataclass(slots=True)
class ScanReport:
    """One minute's result; JSON-able for the task and the CLI."""

    ts: dt.datetime | None = None
    user_id: int | None = None
    written: int = 0
    states: dict[str, str] = field(default_factory=dict)
    snapshots: list[str] = field(default_factory=list)
    skipped: str | None = None
    seconds: float = 0.0

    def as_dict(self) -> JsonObject:
        return {
            "ts": self.ts.isoformat() if self.ts else None,
            "user_id": self.user_id,
            "written": self.written,
            "states": dict(self.states),
            "snapshots": list(self.snapshots),
            "skipped": self.skipped,
            "seconds": round(self.seconds, 3),
        }


def scan_gate_free(
    now: dt.datetime,
    *,
    scan_enabled: bool,
    collect_enabled: bool,
    user_id: int | None,
    config: OptionsConfig | None = None,
) -> str | None:
    """The refusals that cost nothing — checked before a database session is opened."""
    if not scan_enabled:
        return "BASKFY_OPTIONS_SCAN_ENABLED is false"
    if not collect_enabled:
        return "BASKFY_OPTIONS_COLLECT_ENABLED is false (no chain to scan)"
    if not in_session(now, config):
        return "outside 09:15-15:30 IST"
    if user_id is None:
        return "no BASKFY_SOLE_USER_ID configured"
    return None


# --- reads ---------------------------------------------------------------------------------------


async def load_bars(session: AsyncSession, symbol: str, day: dt.date) -> tuple[Bar, ...]:
    """``symbol``'s stored one-minute bars of ``day``'s session, oldest first."""
    start, end = session_bounds(day)
    rows = (
        await session.execute(
            select(
                OpIndexMinute.ts,
                OpIndexMinute.open,
                OpIndexMinute.high,
                OpIndexMinute.low,
                OpIndexMinute.close,
            )
            .join(Instrument, Instrument.id == OpIndexMinute.instrument_id)
            .where(Instrument.symbol == symbol, OpIndexMinute.ts >= start, OpIndexMinute.ts < end)
            .order_by(OpIndexMinute.ts)
        )
    ).all()
    return tuple(
        Bar(ts=ts, open=Decimal(o), high=Decimal(h), low=Decimal(lo), close=Decimal(c))
        for ts, o, h, lo, c in rows
    )


async def daily_levels(
    session: AsyncSession, slug: str, before: dt.date, limit: int
) -> tuple[Decimal, ...]:
    """The last ``limit`` stored daily levels of ``slug`` strictly before ``before``, oldest
    first — the previous session is the last element."""
    rows = (
        await session.execute(
            select(IndexSnapshotDaily.level)
            .join(IndexDef, IndexDef.id == IndexSnapshotDaily.index_id)
            .where(
                IndexDef.slug == slug,
                IndexSnapshotDaily.date < before,
                IndexSnapshotDaily.level.is_not(None),
            )
            .order_by(IndexSnapshotDaily.date.desc())
            .limit(limit)
        )
    ).all()
    return tuple(Decimal(level) for (level,) in reversed(rows) if level is not None)


async def vix_previous_close(session: AsyncSession, day: dt.date) -> Decimal | None:
    """India VIX's previous close: ``index_snapshot_daily``, else the last stored minute bar
    before today's open."""
    daily = await daily_levels(session, INDIA_VIX_SLUG, day, 1)
    if daily:
        return daily[-1]
    start, _ = session_bounds(day)
    row = (
        await session.execute(
            select(OpIndexMinute.close)
            .join(Instrument, Instrument.id == OpIndexMinute.instrument_id)
            .where(Instrument.symbol == INDIA_VIX, OpIndexMinute.ts < start)
            .order_by(OpIndexMinute.ts.desc())
            .limit(1)
        )
    ).first()
    return Decimal(row[0]) if row is not None else None


def _levels(raw: object) -> tuple[Level, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[Level] = []
    for item in raw:
        if isinstance(item, dict):
            price, quantity = item.get("price"), item.get("quantity")
            if isinstance(price, str | int | float) and isinstance(quantity, int):
                out.append(Level(Decimal(str(price)), quantity))
    return tuple(out)


def to_quote(row: OpChainSnapshot) -> OptionQuote:
    """A stored snapshot row as the pure core's quote (depth from ``depth_json``)."""
    depth = row.depth_json or {}
    return OptionQuote(
        instrument_token=int(row.instrument_token),
        expiry=row.expiry,
        strike=Decimal(row.strike),
        option_type=OptionType(row.option_type),
        bid=None if row.bid is None else Decimal(row.bid),
        ask=None if row.ask is None else Decimal(row.ask),
        bids=_levels(depth.get("buy")),
        asks=_levels(depth.get("sell")),
        oi=int(row.oi or 0),
        ts=row.ts,
        last=None if row.last is None else Decimal(row.last),
        volume=int(row.volume or 0),
    )


async def load_snapshot(
    session: AsyncSession, at: dt.datetime | None, now: dt.datetime, day: dt.date
) -> Snapshot | None:
    """The stored minute that prices a candidate: the first at or after ``at`` (and not after
    ``now``) for a decided sleeve, or the latest of today for ``at=None``; ``None`` if none."""
    start, _ = session_bounds(day)
    upper = scan_minute(now)
    query = select(func.min(OpChainSnapshot.ts) if at is not None else func.max(OpChainSnapshot.ts))
    query = query.where(OpChainSnapshot.ts >= (at or start), OpChainSnapshot.ts <= upper)
    minute = (await session.execute(query)).scalar_one_or_none()
    if minute is None:
        return None
    rows = (
        (await session.execute(select(OpChainSnapshot).where(OpChainSnapshot.ts == minute)))
        .scalars()
        .all()
    )
    spot = next((Decimal(r.spot) for r in rows if r.spot is not None), None)
    return Snapshot(ts=minute, spot=spot, quotes=tuple(to_quote(r) for r in rows))


async def load_market(
    session: AsyncSession, day: dt.date, trading_day: bool, config: OptionsConfig
) -> MarketDay:
    contracts = await load_contracts(session, config.calendar.underlying)
    return MarketDay(
        trade_date=day,
        trading_day=trading_day,
        contracts=tuple(contracts),
        bars=await load_bars(session, NIFTY_50, day),
        daily_closes=await daily_levels(session, NIFTY_50_SLUG, day, DAILY_HISTORY),
        vix_prev_close=await vix_previous_close(session, day),
    )


def _book(row: OpSleeveConfig | None, mode: Mode, real_rows: int) -> SleeveBook:
    """The sleeve group's ``op_sleeve_config`` as sizing reads it; no row → ``04``'s defaults
    and capital ₹0 (paper one lot, PACK.6)."""
    if row is None:
        return SleeveBook(mode=mode, real_journal_rows=real_rows)
    return SleeveBook(
        mode=mode,
        sleeve_capital_inr=Decimal(row.sleeve_capital_inr),
        risk_per_trade_pct=Decimal(row.risk_per_trade_pct),
        max_lots=int(row.max_lots),
        real_journal_rows=real_rows,
    )


async def load_context(
    session: AsyncSession,
    user_id: int,
    day: dt.date,
    mode_of: Callable[[Sleeve], Mode],
) -> DayContext:
    """The sole tenant's event days, books, pauses, sessions and real journal counts."""
    events = frozenset(
        (await session.execute(select(OpEventDay.date).where(OpEventDay.user_id == user_id)))
        .scalars()
        .all()
    )
    book = (
        await session.execute(select(OpBookConfig).where(OpBookConfig.user_id == user_id))
    ).scalar_one_or_none()
    book_paused = is_paused(book.paused_until if book else None, day)
    configs = {
        row.sleeve: row
        for row in (
            await session.execute(select(OpSleeveConfig).where(OpSleeveConfig.user_id == user_id))
        )
        .scalars()
        .all()
    }
    sessions = {
        row.sleeve: row
        for row in (
            await session.execute(
                select(OpSession).where(OpSession.user_id == user_id, OpSession.trade_date == day)
            )
        )
        .scalars()
        .all()
    }
    real: dict[str, int] = defaultdict(int)
    counted = await session.execute(
        select(OpJournal.sleeve, func.count())
        .where(OpJournal.user_id == user_id, OpJournal.simulated.is_(False))
        .group_by(OpJournal.sleeve)
    )
    for sleeve_code, count in counted.all():
        real[group_of(Sleeve(sleeve_code)).value] += int(count)
    holder = next(
        (Sleeve(s.slot_holder) for s in sessions.values() if s.slot_holder is not None), None
    )
    contexts: dict[Sleeve, SleeveContext] = {}
    for sleeve in Sleeve:
        group = group_of(sleeve).value
        row = configs.get(group)
        today = sessions.get(sleeve.value)
        contexts[sleeve] = SleeveContext(
            book=_book(row, mode_of(sleeve), real[group]),
            paused=book_paused or is_paused(row.paused_until if row else None, day),
            session_state=SessionState(today.state) if today is not None else None,
        )
    return DayContext(event_days=events, slot_holder=holder, sleeves=contexts)


# --- writes --------------------------------------------------------------------------------------


def scan_row(result: ScanResult, user_id: int) -> dict[str, object]:
    """``op_scan``'s columns for one result (``03`` §6)."""
    return {
        "user_id": user_id,
        "sleeve": result.sleeve.value,
        "trade_date": result.trade_date,
        "ts": result.ts,
        "state": result.state.value,
        "reasons": list(result.reasons),
        "numbers": result.numbers,
        "candidates": result.candidates_json(),
        "as_of_minute": result.as_of_minute,
        "stale": result.stale,
    }


async def write_rows(session: AsyncSession, rows: Sequence[dict[str, object]]) -> int:
    """Upsert on ``(user_id, sleeve, ts)``: a re-run of the minute rewrites the same row."""
    if not rows:
        return 0
    stmt = insert(OpScan).values(list(rows))
    stmt = stmt.on_conflict_do_update(
        constraint="uq_op_scan_user_sleeve_ts",
        set_={
            "trade_date": stmt.excluded.trade_date,
            "state": stmt.excluded.state,
            "reasons": stmt.excluded.reasons,
            "numbers": stmt.excluded.numbers,
            "candidates": stmt.excluded.candidates,
            "as_of_minute": stmt.excluded.as_of_minute,
            "stale": stmt.excluded.stale,
        },
    )
    await session.execute(stmt)
    return len(rows)


# --- one minute ----------------------------------------------------------------------------------


def _modes(sleeve: Sleeve) -> Mode:
    return options_gates(sleeve).mode


async def scan_minute_for(  # noqa: PLR0913 - the session, the tenant, the clock and the rules
    session: AsyncSession,
    user_id: int,
    now: dt.datetime,
    *,
    trading_day: bool,
    config: OptionsConfig | None = None,
    ceilings: OptionsCeilings | None = None,
    mode_of: Callable[[Sleeve], Mode] = _modes,
) -> ScanReport:
    """One minute's scan for ``user_id``. The caller has checked the flags, window and day."""
    started = time.monotonic()
    cfg = config or OptionsConfig()
    ceil = ceilings or OptionsCeilings()
    day = now.astimezone(IST).date()
    report = ScanReport(ts=scan_minute(now), user_id=user_id)
    market = await load_market(session, day, trading_day, cfg)
    context = await load_context(session, user_id, day, mode_of)
    wanted = wanted_minutes(market, context, now, cfg, ceil)
    loaded: dict[dt.datetime | None, Snapshot | None] = {}
    for minute in sorted(wanted, key=lambda m: (m is None, m or now)):
        loaded[minute] = await load_snapshot(session, minute, now, day)
        report.snapshots.append(
            f"{'latest' if minute is None else minute.isoformat()} -> {_stamp(loaded[minute])}"
        )
    results = scan_all(market, context, now, SnapshotBook(loaded), cfg, ceil)
    report.written = await write_rows(session, [scan_row(r, user_id) for r in results])
    report.states = {r.sleeve.value: r.state.value for r in results}
    report.seconds = time.monotonic() - started
    return report


def _stamp(snapshot: Snapshot | None) -> str:
    return "none" if snapshot is None else snapshot.ts.isoformat()
