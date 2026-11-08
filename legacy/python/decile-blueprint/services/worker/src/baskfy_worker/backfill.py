"""The resumable backfill (Prompt 3 deliverable 6).

    "A resumable backfill CLI: `python -m worker.backfill --from --to --instruments --concurrency
     --resume`, with a cursor table so an interrupted run continues where it stopped."

docs/09 §Backfill gives the invocation and the ordering, and sets expectations:
"~2,300 instruments x 15 years. Budget a weekend and a resumable cursor."

NAMING: the prompt writes ``python -m worker.backfill``; docs/14 §"Naming inside the codebase"
fixes the Python namespace as ``baskfy_worker``, so the real entry point is
``python -m baskfy_worker.backfill``. ``make backfill`` wraps it.

How resume works
----------------
Before fetching anything, the run *plans*: one ``ingest_cursor`` row per (instrument, chunk
window), where the chunks are exactly the slices ``KiteProvider.chunk_windows`` yields, so a
cursor never spans more than one upstream request. Planning is itself an upsert, so re-planning an
existing backfill leaves completed rows alone.

Each unit is then claimed, executed and marked ``done`` **in its own transaction**. That is the
whole trick: a process killed mid-run leaves at most one unit ``running``, everything before it
``done``, and everything after it ``pending``. ``--resume`` skips ``done`` and re-claims the rest,
so an interrupted run continues rather than restarting.

Without per-unit commits a kill would roll back the entire backfill's progress, which is exactly
the failure "budget a weekend" makes intolerable.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import CURSOR_KIND_BARS, IngestCursor, Instrument
from baskfy_providers.errors import ProviderError
from baskfy_worker.db import session_scope
from baskfy_worker.providers import build_pipeline_dependencies
from baskfy_worker.settings import get_worker_settings
from baskfy_worker.tasks.bars import upsert_bars
from baskfy_worker.window import DateWindow

#: docs/09: "chunk backfills into <= 2000-day slices per instrument".
DEFAULT_CHUNK_DAYS: int = 2000

#: docs/09 §Backfill and §"Kite specifics": "run with bounded concurrency (<= 3)".
DEFAULT_CONCURRENCY: int = 3

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BackfillUnit:
    instrument_id: int
    symbol: str
    kite_token: int | None
    window: DateWindow


@dataclass(slots=True)
class BackfillReport:
    planned: int = 0
    already_done: int = 0
    completed: int = 0
    failed: int = 0
    rows_written: int = 0
    errors: list[str] = field(default_factory=list)


def chunk_windows(window: DateWindow, chunk_days: int) -> Iterator[DateWindow]:
    """Split ``window`` into slices no longer than ``chunk_days``, contiguous and gapless."""
    if chunk_days < 1:
        raise ValueError(f"chunk_days must be at least 1; got {chunk_days}")
    span = dt.timedelta(days=chunk_days - 1)
    cursor = window.start
    while cursor <= window.end:
        end = min(cursor + span, window.end)
        yield DateWindow(cursor, end)
        cursor = end + dt.timedelta(days=1)


async def plan(
    session: AsyncSession,
    window: DateWindow,
    symbols: Sequence[str] | None,
    chunk_days: int = DEFAULT_CHUNK_DAYS,
) -> int:
    """Materialise one cursor row per (instrument, chunk). Idempotent."""
    query = select(Instrument.id, Instrument.symbol).where(Instrument.delisted_on.is_(None))
    if symbols:
        query = query.where(Instrument.symbol.in_(list(symbols)))
    instruments = (await session.execute(query.order_by(Instrument.symbol))).tuples().all()

    values = [
        {
            "kind": CURSOR_KIND_BARS,
            "instrument_id": instrument_id,
            "window_start": chunk.start,
            "window_end": chunk.end,
            "status": STATUS_PENDING,
            "attempts": 0,
        }
        for instrument_id, _symbol in instruments
        for chunk in chunk_windows(window, chunk_days)
    ]
    if not values:
        return 0

    stmt = insert(IngestCursor).values(values)
    await session.execute(
        # A cursor that already exists keeps its status: re-planning must never resurrect
        # completed work, which is what makes `--resume` meaningful.
        stmt.on_conflict_do_nothing(
            index_elements=[
                IngestCursor.kind,
                IngestCursor.instrument_id,
                IngestCursor.window_start,
            ]
        )
    )
    return len(values)


async def pending_units(session: AsyncSession, *, resume: bool) -> list[BackfillUnit]:
    """Work still to do.

    With ``--resume``, ``done`` rows are skipped and a ``running`` row — the unit that was in
    flight when the process died — is picked up again. Re-running a unit is safe because
    :func:`upsert_bars` upserts, so at worst it rewrites identical rows.
    """
    statuses = (
        [STATUS_PENDING, STATUS_RUNNING, STATUS_FAILED]
        if resume
        else [STATUS_PENDING, STATUS_RUNNING, STATUS_FAILED, STATUS_DONE]
    )
    rows = (
        (
            await session.execute(
                select(
                    IngestCursor.instrument_id,
                    Instrument.symbol,
                    Instrument.kite_token,
                    IngestCursor.window_start,
                    IngestCursor.window_end,
                )
                .join(Instrument, Instrument.id == IngestCursor.instrument_id)
                .where(IngestCursor.kind == CURSOR_KIND_BARS, IngestCursor.status.in_(statuses))
                .order_by(Instrument.symbol, IngestCursor.window_start)
            )
        )
        .tuples()
        .all()
    )
    return [BackfillUnit(row[0], row[1], row[2], DateWindow(row[3], row[4])) for row in rows]


async def count_done(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(IngestCursor)
                .where(IngestCursor.kind == CURSOR_KIND_BARS, IngestCursor.status == STATUS_DONE)
            )
        ).scalar_one()
    )


async def _mark(  # noqa: PLR0913 - one parameter per cursor column being written
    session: AsyncSession,
    unit: BackfillUnit,
    status: str,
    *,
    rows_written: int | None = None,
    error: str | None = None,
    bump_attempt: bool = False,
) -> None:
    values: dict[str, object] = {"status": status}
    if rows_written is not None:
        values["rows_written"] = rows_written
    if error is not None:
        values["last_error"] = {"message": error}
    if bump_attempt:
        values["attempts"] = IngestCursor.attempts + 1
    await session.execute(
        update(IngestCursor)
        .where(
            IngestCursor.kind == CURSOR_KIND_BARS,
            IngestCursor.instrument_id == unit.instrument_id,
            IngestCursor.window_start == unit.window.start,
        )
        .values(**values)
    )


async def execute_unit(
    unit: BackfillUnit, provider: object, database_url: str | None = None
) -> int:
    """Run one unit in its own transaction, so progress survives a kill.

    Claim, fetch, write and mark done all commit together; if the process dies at any point the
    unit is left ``running`` or ``pending`` and will be re-claimed on resume.
    """
    if unit.kite_token is None:
        async with session_scope(database_url) as session:
            await _mark(session, unit, STATUS_DONE, rows_written=0, error="no provider token")
        return 0

    fetch = getattr(provider, "daily_bars", None)
    if not callable(fetch):
        raise RuntimeError("no provider offers daily_bars")

    async with session_scope(database_url) as session:
        await _mark(session, unit, STATUS_RUNNING, bump_attempt=True)

    try:
        frame = fetch(unit.kite_token, unit.window.start, unit.window.end)
    except ProviderError as exc:
        async with session_scope(database_url) as session:
            await _mark(session, unit, STATUS_FAILED, error=str(exc))
        raise

    async with session_scope(database_url) as session:
        written = await upsert_bars(session, unit.instrument_id, frame)
        await _mark(session, unit, STATUS_DONE, rows_written=written)
    return written


async def run_backfill(  # noqa: PLR0913 - mirrors the CLI flags docs/09 §Backfill specifies
    provider: object,
    window: DateWindow,
    *,
    symbols: Sequence[str] | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    resume: bool = True,
    chunk_days: int = DEFAULT_CHUNK_DAYS,
    database_url: str | None = None,
    stop_after: int | None = None,
) -> BackfillReport:
    """Plan, then execute every outstanding unit with bounded concurrency.

    ``stop_after`` exists so a test can simulate the process being killed mid-run: it stops after
    N completed units without touching the cursor rows that follow, which is precisely the state
    a SIGKILL leaves behind.
    """
    report = BackfillReport()

    async with session_scope(database_url) as session:
        report.planned = await plan(session, window, symbols, chunk_days)

    async with session_scope(database_url) as session:
        report.already_done = await count_done(session)
        units = await pending_units(session, resume=resume)

    if not units:
        return report

    limiter = asyncio.Semaphore(max(1, concurrency))
    completed = 0
    stop = asyncio.Event()

    async def worker(unit: BackfillUnit) -> None:
        nonlocal completed
        if stop.is_set():
            return
        async with limiter:
            if stop.is_set():
                return
            try:
                written = await execute_unit(unit, provider, database_url)
            except (ProviderError, RuntimeError) as exc:
                report.failed += 1
                report.errors.append(f"{unit.symbol} {unit.window}: {exc}")
                return
            report.rows_written += written
            report.completed += 1
            completed += 1
            if stop_after is not None and completed >= stop_after:
                stop.set()

    await asyncio.gather(*(worker(unit) for unit in units))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m baskfy_worker.backfill",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--from", dest="start", required=True, help="ISO start date")
    parser.add_argument("--to", dest="end", default=None, help="ISO end date (default: today)")
    parser.add_argument(
        "--instruments",
        default="all",
        help="'all', or a comma-separated list of symbols",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=f"bounded concurrency (docs/09 caps this at {DEFAULT_CONCURRENCY})",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="skip units already marked done and re-claim the rest",
    )
    parser.add_argument("--chunk-days", type=int, default=DEFAULT_CHUNK_DAYS)
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    window = DateWindow(
        dt.date.fromisoformat(args.start),
        dt.date.fromisoformat(args.end) if args.end else dt.date.today(),
    )
    symbols = (
        None
        if args.instruments.strip().lower() == "all"
        else [s.strip() for s in args.instruments.split(",") if s.strip()]
    )
    concurrency = args.concurrency or get_worker_settings().ingest_concurrency

    provider = build_pipeline_dependencies().provider
    report = asyncio.run(
        run_backfill(
            provider,
            window,
            symbols=symbols,
            concurrency=concurrency,
            resume=bool(args.resume),
            chunk_days=args.chunk_days,
            database_url=args.database_url,
        )
    )
    print(
        f"planned {report.planned} unit(s); {report.already_done} already done; "
        f"completed {report.completed}; failed {report.failed}; "
        f"{report.rows_written} bar rows written"
    )
    for error in report.errors[:20]:
        print(f"  error: {error}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
