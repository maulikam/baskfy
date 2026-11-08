"""``factors_cli backfill-ranking`` — Phase 2's columns over history, and nothing else.

    python -m baskfy_worker.factors_cli backfill-ranking --from 2025-01-01 --to 2026-09-11
    python -m baskfy_worker.factors_cli backfill-ranking --from 2025-01-01 --to 2026-09-11 --resume
    python -m baskfy_worker.factors_cli backfill-ranking --from 2026-09-01 --to 2026-09-11 --force

docs/ranking/PLAN.md added columns to ``factor_daily`` (C1, plus ``mom_pctile`` and
``rank_persist_20``) and a table, ``desk_score_daily`` (C2). The nightly writes both for tonight;
this fills them for every trading day before it. For each day, oldest first:

1. the per-instrument C1 columns, recomputed by the same engine the nightly runs
   (``PolarsFactorEngine`` over ``load_history``, NIFTY 50 and NIFTY 500 included) and written
   with an UPDATE of exactly those columns on rows that already exist;
2. ``mom_pctile``, then ``rank_persist_20`` — oldest first is what lets persistence see the
   ``mom_pctile`` of the nineteen dates before it;
3. ``desk_score_daily``, through the book's service, exactly as the nightly step writes it.

**Every pre-Phase-2 column is left byte-identical.** Nothing here inserts a ``factor_daily`` row
or names a column outside C1: a day whose legacy rows are wrong is a ``recompute`` job, not this
one, and rewriting them here would silently move published numbers from under saved screens.

Each day is its own transaction, committed before the next begins, so a killed run keeps every
day it finished. A day is **complete** when its desk scores are all present at the current
``DESK_SCORE_VERSION`` and no row that could carry a ``mom_pctile`` lacks one; complete days are
skipped unless ``--force``. ``--resume`` states that skip explicitly for a restarted run: it is
the default behaviour, accepted so the command an operator re-types after an interruption says
what it does.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.desk_score_service import DESK_SCORE_VERSION
from baskfy_core.factors_ranking import MOM_PCTILE_COMPONENTS
from baskfy_core.models import DeskScoreDaily, FactorDaily, Instrument, TradingDay
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_worker.engine import PolarsFactorEngine, load_history
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.desk_score import run_compute_desk_score
from baskfy_worker.tasks.ranking import run_rank_columns, write_phase2_columns

log = logging.getLogger(__name__)

#: What a day's outcome is called in the progress log and the report.
COMPUTED: Final = "computed"
SKIPPED_COMPLETE: Final = "skipped_complete"
SKIPPED_NO_ROWS: Final = "skipped_no_factor_rows"
FAILED: Final = "failed"


@dataclass(slots=True)
class DayResult:
    day: dt.date
    status: str
    factor_rows: int = 0
    desk_rows: int = 0
    seconds: float = 0.0
    error: str | None = None


@dataclass(slots=True)
class RankingBackfillReport:
    days: list[DayResult] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for day in self.days if day.status == status)

    @property
    def errors(self) -> list[str]:
        return [f"{d.day.isoformat()}: {d.error}" for d in self.days if d.status == FAILED]


async def trading_days_between(
    session: AsyncSession, start: dt.date, end: dt.date
) -> list[dt.date]:
    """NSE trading days in ``[start, end]``, oldest first."""
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


async def day_is_complete(session: AsyncSession, day: dt.date) -> bool:
    """Every NSE factor row has a current-version desk score, and every rankable row a percentile.

    The C1 columns themselves cannot mark completeness — each is legitimately NULL on a short
    window — so the two things that are *always* written for a finished day stand in for it.
    """
    nse_rows = (
        await session.execute(
            select(func.count())
            .select_from(FactorDaily)
            .join(Instrument, Instrument.id == FactorDaily.instrument_id)
            .where(FactorDaily.date == day, Instrument.exchange_id == NSE_EXCHANGE_ID)
        )
    ).scalar_one()
    if int(nse_rows) == 0:
        return False
    desk_rows = (
        await session.execute(
            select(func.count())
            .select_from(DeskScoreDaily)
            .where(DeskScoreDaily.date == day, DeskScoreDaily.score_version == DESK_SCORE_VERSION)
        )
    ).scalar_one()
    if int(desk_rows) != int(nse_rows):
        return False
    rankable_without_pctile = (
        await session.execute(
            select(func.count())
            .select_from(FactorDaily)
            .where(
                and_(
                    FactorDaily.date == day,
                    FactorDaily.universe_mask != 0,
                    FactorDaily.mom_pctile.is_(None),
                    *(getattr(FactorDaily, c).is_not(None) for c in MOM_PCTILE_COMPONENTS),
                )
            )
        )
    ).scalar_one()
    return int(rankable_without_pctile) == 0


async def backfill_ranking_day(
    session: AsyncSession, day: dt.date, *, force: bool = False
) -> DayResult:
    """Phase 2's columns and desk scores for one day, on ``session``, without committing."""
    started = time.monotonic()
    if not force and await day_is_complete(session, day):
        return DayResult(day, SKIPPED_COMPLETE)

    existing = (
        await session.execute(
            select(func.count()).select_from(FactorDaily).where(FactorDaily.date == day)
        )
    ).scalar_one()
    if int(existing) == 0:
        return DayResult(day, SKIPPED_NO_ROWS)

    history = await load_history(session, day)
    result = PolarsFactorEngine().run(history, day)
    factor_rows = await write_phase2_columns(session, result.frame, day)
    await run_rank_columns(session, day)
    # Its own savepoint, as in the nightly: the book's scorer can refuse a day (on a history too
    # short for any window, `score.apply_filters` cannot compare an all-NULL column), and that
    # must not throw away the factor columns just computed. The day is then reported as failed,
    # stays incomplete, and is retried by the next run.
    try:
        async with session.begin_nested():
            desk_rows = await run_compute_desk_score(session, StepOutcome(), day)
    except Exception as exc:
        return DayResult(
            day,
            FAILED,
            factor_rows=factor_rows,
            seconds=time.monotonic() - started,
            error=f"desk_score: {type(exc).__name__}: {exc}",
        )
    return DayResult(
        day,
        COMPUTED,
        factor_rows=factor_rows,
        desk_rows=desk_rows,
        seconds=time.monotonic() - started,
    )


async def run_backfill_ranking(
    session: AsyncSession,
    start: dt.date,
    end: dt.date,
    *,
    force: bool = False,
    checkpoint: Callable[[], Awaitable[None]] | None = None,
) -> RankingBackfillReport:
    """Every trading day in ``[start, end]``, oldest first.

    ``checkpoint`` is called after each day — the CLI passes ``session.commit`` so a killed run
    keeps what it finished; a test passes nothing and keeps one transaction. A day that raises is
    rolled back to its own savepoint, recorded and logged, and the run moves on: one bad day must
    not end a run that is hours long. It still fails the command's exit code.
    """
    report = RankingBackfillReport()
    days = await trading_days_between(session, start, end)
    log.info("backfill-ranking %s..%s: %d trading days", start, end, len(days))
    for index, day in enumerate(days, start=1):
        try:
            async with session.begin_nested():
                result = await backfill_ranking_day(session, day, force=force)
        except Exception as exc:
            result = DayResult(day, FAILED, error=f"{type(exc).__name__}: {exc}")
        if result.status == FAILED:
            log.warning("backfill-ranking %s failed: %s", day, result.error)
        if checkpoint is not None:
            await checkpoint()
        report.days.append(result)
        log.info(
            "backfill-ranking [%d/%d] %s %s factor_rows=%d desk_rows=%d %.1fs",
            index,
            len(days),
            day.isoformat(),
            result.status,
            result.factor_rows,
            result.desk_rows,
            result.seconds,
        )
    return report


def render(report: RankingBackfillReport) -> str:
    computed = [d for d in report.days if d.status == COMPUTED]
    seconds = sum(d.seconds for d in computed)
    lines = [
        "BACKFILL-RANKING "
        f"days={len(report.days)} computed={len(computed)} "
        f"skipped_complete={report.count(SKIPPED_COMPLETE)} "
        f"skipped_no_factor_rows={report.count(SKIPPED_NO_ROWS)} "
        f"failed={report.count(FAILED)} "
        f"seconds_per_computed_day={seconds / len(computed) if computed else 0.0:.2f}"
    ]
    lines += [f"  {error}" for error in report.errors[:20]]
    return "\n".join(lines)


__all__ = [
    "COMPUTED",
    "FAILED",
    "SKIPPED_COMPLETE",
    "SKIPPED_NO_ROWS",
    "DayResult",
    "RankingBackfillReport",
    "backfill_ranking_day",
    "day_is_complete",
    "render",
    "run_backfill_ranking",
    "trading_days_between",
]
