"""VB9: the study, re-run from the plant's own bars, with its drift named (`docs/vbt/06` VB9).

    load the universe and its bars → indicators → signals → breadth
      → three books over **one** detection pass (`04` §11)
      → one appended `vb_backtest_run` row

**The three books share a detection pass on purpose.** `full`, `gate_off` and `raw_scan` differ
only in what they are allowed to act on — the gate vector and the signal column — so detecting
three times would spend three times the work to produce the same signals and would leave open
the possibility of them disagreeing.

**The row is written on the way in and finished on the way out**, which is `03` §8's contract:
a run that dies still says what it was asked for. `params` before, `stats` and `drift` after,
`error` instead of `stats` when it raised — and `finished_at` in every case, so "still running"
and "failed" are different states on the page rather than the same silence.

**Append-only.** Nothing here edits a stored row. The number that was on the page when the
execution flag was considered has to survive a recalibration that produces a different one.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import traceback
from decimal import Decimal
from typing import Final

import numpy as np
import polars as pl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import VbBacktestRun
from baskfy_core.models.base import JsonObject
from baskfy_core.vbt.backtest import (
    BacktestParams,
    BacktestResult,
    gate_vector,
    panel_from_frame,
    run_backtest,
    summarise,
    yearly,
)
from baskfy_core.vbt.breadth import breadth_series
from baskfy_core.vbt.calendar import drop_thin_sessions
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, VbtConfig
from baskfy_core.vbt.drift import compare
from baskfy_core.vbt.indicators import with_vbt_indicators
from baskfy_core.vbt.signals import with_signal_columns
from baskfy_worker.tasks.vbt import load_universe, load_vbt_bars

log = logging.getLogger(__name__)

#: `03` §8: the run's own bars, as against VB2's reproduction from the research export.
SOURCE_PLANT: Final = "PLANT"

#: The study's window opens here (`01` §4). Bars before it are warm-up: the 200-day average does
#: not exist yet, and a book that traded through its own warm-up is a different strategy.
DEFAULT_START: Final = dt.date(2017, 10, 16)

#: How much history to load before `start`, so the 200-session window is full on day one.
#: `04` §1's `bars_required` is 201 sessions; 400 calendar days is comfortably more than that
#: and cheaper to express as a date than as a session count we do not have yet.
WARMUP_DAYS: Final = 400

__all__ = [
    "DEFAULT_START",
    "SOURCE_PLANT",
    "WARMUP_DAYS",
    "BacktestBooks",
    "book_stats",
    "run_vbt_backtest",
    "three_books",
]


@dataclasses.dataclass(frozen=True, slots=True)
class BacktestBooks:
    """`04` §11's three, over one detection pass.

    `full` is the primary book. Breadth's contribution is `full - gate_off`; the trend filters'
    is `full - raw_scan`. Both differences are what `01` §3's ablation argues, recomputed on
    whatever bars the plant currently holds rather than quoted from the note.
    """

    full: BacktestResult
    gate_off: BacktestResult
    raw_scan: BacktestResult


def three_books(
    tagged: pl.DataFrame, breadth: pl.DataFrame, params: BacktestParams
) -> BacktestBooks:
    """Run the same history three ways: as specified, without the gate, and on the raw scan."""
    panel = panel_from_frame(tagged, "state")
    gate = gate_vector(breadth, panel.sessions)
    always_open = np.ones(panel.sessions_count, dtype=bool)
    scan_panel = panel_from_frame(tagged, "scan_hit")
    return BacktestBooks(
        full=run_backtest(panel, gate, dataclasses.replace(params, label="full")),
        gate_off=run_backtest(panel, always_open, dataclasses.replace(params, label="gate_off")),
        raw_scan=run_backtest(
            scan_panel,
            gate_vector(breadth, scan_panel.sessions),
            dataclasses.replace(params, label="raw_scan"),
        ),
    )


def book_stats(result: BacktestResult) -> JsonObject:
    """One book, in the JSONB shape `vb_backtest_run.stats` stores.

    The equity curve and the yearly table are here because `05` §2 draws both, and a page that
    had to recompute them would be a second implementation of the arithmetic.
    """
    stats = summarise(result)
    if stats is None:
        return {"trades": 0, "sessions": len(result.sessions), "skipped": dict(result.skipped)}
    payload = dict(stats.to_json())
    payload["yearly"] = [row.to_json() for row in yearly(result)]
    # Money as a string of its exact decimal — house rule 9, all the way to the database.
    payload["equity_curve"] = [
        {"date": session.isoformat(), "equity_inr": str(value)}
        for session, value in zip(result.sessions, result.equity, strict=True)
    ]
    payload["skipped"] = dict(result.skipped)
    payload["orders_offered"] = result.orders_offered
    payload["fill_rate_pct"] = result.fill_rate_pct()
    return payload


def _params_json(params: BacktestParams, start: dt.date, end: dt.date | None) -> JsonObject:
    """`03` §8: `start`, `end`, `sleeve_inr`, the cost and the whole config, written on the way in.

    The config is flattened field by field rather than stored as a repr, so a reader comparing
    two runs can diff them, and so a field added to `VbtConfig` appears here without anybody
    remembering to add it.
    """
    config: JsonObject = {}
    for group in dataclasses.fields(params.config):
        sub = getattr(params.config, group.name)
        for spec in dataclasses.fields(sub):
            value = getattr(sub, spec.name)
            config[f"{group.name}.{spec.name}"] = list(value) if isinstance(value, tuple) else value
    return {
        "start": start.isoformat(),
        "end": end.isoformat() if end else None,
        "sleeve_inr": str(params.sleeve_inr),
        "cost_pct_per_side": str(params.cost_pct_per_side),
        "tick": str(params.tick),
        "source": SOURCE_PLANT,
        "config": config,
    }


async def _open_run(
    session: AsyncSession, *, user_id: int, params: JsonObject, now: dt.datetime
) -> VbBacktestRun:
    row = VbBacktestRun(user_id=user_id, source=SOURCE_PLANT, params=params, started_at=now)
    session.add(row)
    await session.flush()
    return row


async def run_vbt_backtest(  # noqa: PLR0913 - one keyword per input the stored row records
    session: AsyncSession,
    *,
    user_id: int,
    start: dt.date = DEFAULT_START,
    end: dt.date | None = None,
    sleeve_inr: Decimal = Decimal("1000000"),
    config: VbtConfig = DEFAULT_VBT_CONFIG,
    now: dt.datetime | None = None,
) -> VbBacktestRun:
    """Re-run the study over the plant's bars and append one `vb_backtest_run` row.

    Raises whatever the engine raised, **after** recording it: `03` §8 wants the error on the row
    and the exception in the logs, because a run that failed silently would leave the page showing
    the last good number with nothing to say why it had not moved.
    """
    stamp = now or dt.datetime.now(tz=dt.UTC)
    params = BacktestParams(sleeve_inr=sleeve_inr, start=start, end=end, config=config)
    row = await _open_run(
        session, user_id=user_id, params=_params_json(params, start, end), now=stamp
    )
    try:
        universe = await load_universe(session, config)
        bars = await load_vbt_bars(
            session,
            start - dt.timedelta(days=WARMUP_DAYS),
            end or dt.date.today(),
            universe,
        )
        if bars.is_empty():
            raise ValueError(
                "the plant holds no bars for the volume-breakout universe in this window; "
                "run the backfill before asking for a backtest"
            )
        clean, calendar = drop_thin_sessions(bars, config)
        tagged = with_signal_columns(with_vbt_indicators(clean, calendar, config), config)
        books = three_books(tagged, breadth_series(tagged, config), params)
        primary = summarise(books.full)
        if primary is None:
            raise ValueError(
                "the run produced no closed trades, so there is no result to compare — "
                "usually too little history for the 200-session window"
            )
        row.stats = {
            "full": book_stats(books.full),
            "gate_off": book_stats(books.gate_off),
            "raw_scan": book_stats(books.raw_scan),
            "universe": len(universe),
            "sessions": len(calendar.sessions),
            "thin_sessions_dropped": [day.isoformat() for day in calendar.dropped],
        }
        row.drift = compare(
            cagr_pct=primary.cagr_pct,
            max_drawdown_pct=primary.max_drawdown_pct,
            trades=primary.trades,
        ).to_json()
    except Exception as error:
        row.error = f"{type(error).__name__}: {error}\n{traceback.format_exc()}"
        row.finished_at = dt.datetime.now(tz=dt.UTC)
        await session.flush()
        log.exception("vbt backtest failed", extra={"run_id": row.id, "user_id": user_id})
        raise
    row.finished_at = dt.datetime.now(tz=dt.UTC)
    await session.flush()
    return row


async def latest_finished(
    session: AsyncSession, *, user_id: int, source: str = SOURCE_PLANT
) -> VbBacktestRun | None:
    """The page's one query: this user's newest **finished** run for a source.

    Finished, not started — a run in flight or a failed re-run never displaces the last good
    number (`03` §8).
    """
    return (
        await session.execute(
            select(VbBacktestRun)
            .where(
                VbBacktestRun.user_id == user_id,
                VbBacktestRun.source == source,
                VbBacktestRun.finished_at.is_not(None),
                VbBacktestRun.stats.is_not(None),
            )
            .order_by(VbBacktestRun.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
