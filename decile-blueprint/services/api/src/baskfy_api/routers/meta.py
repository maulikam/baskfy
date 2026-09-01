"""``/meta/*`` — docs/07 §Metadata.

Everything here is reference data the UI needs before it can render a screen form: the factor
registry behind the sort dropdown, the column picker, the fourteen universes, the trading calendar
behind the historical-date picker, and the freshness of the data itself.

The first three are served straight out of ``baskfy_core`` with no database access at all —
docs/06 §"The factor registry" makes the registry "the single source of truth for: the `sort_by`
dropdown, the custom-filter operand list, the column picker, the API enum", so the API's job is to
publish it, not to keep a second copy.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Final

from fastapi import APIRouter, Query
from sqlalchemy import select

from baskfy_api.db import SessionDep
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.schemas import (
    ColumnOut,
    FactorOut,
    PipelineRunOut,
    StatusOut,
    TradingDaysOut,
    UniverseOut,
)
from baskfy_api.screener import DATA_START_DATE, current_data_version, latest_published_date
from baskfy_core.factor_registry import FACTORS, columns
from baskfy_core.models import PipelineRun, TradingDay
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.universes import UNIVERSES

router = APIRouter(prefix="/meta", tags=["meta"])

#: The widest span ``/meta/trading-days`` will answer in one call. The picker asks for a year at a
#: time; an unbounded range would let one request ask for the whole calendar table.
MAX_TRADING_DAY_SPAN = dt.timedelta(days=5 * 366)

#: docs/03: a run whose gate failed never publishes, so these two states mean "the last attempt
#: did not produce data" (docs/07: "503 `pipeline-degraded` — last run failed its QA gate").
FAILED_RUN_STATUSES: Final[frozenset[str]] = frozenset({"failed", "aborted"})
#: The third state. Neither published nor failed — in flight, and the UI must say so.
RUNNING_RUN_STATUS: Final[str] = "running"


@router.get("/factors", response_model=list[FactorOut], summary="The factor registry")
async def get_factors() -> list[FactorOut]:
    """docs/07: "factor registry (key, label, family, unit, higher_is_better)".

    All 64 of them. docs/01 §3 is headed "62 ranking factors" and enumerates 64; see the note in
    ``baskfy_core.factor_registry``.
    """
    return [
        FactorOut(
            key=factor.key,
            label=factor.label,
            family=factor.family.value,
            unit=factor.unit.value,
            higher_is_better=factor.higher_is_better,
        )
        for factor in FACTORS.values()
    ]


@router.get("/columns", response_model=list[ColumnOut], summary="Selectable result columns")
async def get_columns() -> list[ColumnOut]:
    """docs/07: "the 34 selectable result columns" — docs/01 §4 in fact enumerates 36."""
    return [
        ColumnOut(key=c.key, label=c.label, unit=c.unit.value, is_factor=c.is_factor)
        for c in columns()
    ]


@router.get("/universes", response_model=list[UniverseOut], summary="The selectable universes")
async def get_universes() -> list[UniverseOut]:
    """docs/07: "index_def rows where is_universe" — the fourteen of docs/01 §2.1, in UI order.

    Served from ``baskfy_core.universes`` rather than from ``index_def``: the ids are load-bearing
    (they are ``factor_daily.universe_mask`` bit positions), the seeder writes the table *from*
    this tuple, and a dashboard-only index must never appear in the screener's dropdown.
    """
    return [
        UniverseOut(
            index_id=u.index_id,
            slug=u.slug,
            name=u.name,
            sort_order=u.ui_order,
            market_health=u.market_health,
        )
        for u in sorted(UNIVERSES, key=lambda u: u.ui_order)
    ]


@router.get("/trading-days", response_model=TradingDaysOut, summary="NSE trading days in a range")
async def get_trading_days(
    session: SessionDep,
    from_: Annotated[dt.date, Query(alias="from", description="Inclusive start date.")],
    to: Annotated[dt.date, Query(description="Inclusive end date.")],
) -> TradingDaysOut:
    """docs/07: "list of trading dates (for the historical date picker)"."""
    if to < from_:
        raise Problem(
            ProblemType.NO_TRADING_DAY,
            f"`to` ({to.isoformat()}) is before `from` ({from_.isoformat()}).",
        )
    if to - from_ > MAX_TRADING_DAY_SPAN:
        raise Problem(
            ProblemType.NO_TRADING_DAY,
            f"Range exceeds the {MAX_TRADING_DAY_SPAN.days}-day maximum for one request.",
        )

    rows = (
        await session.execute(
            select(TradingDay.date)
            .where(
                TradingDay.exchange_id == NSE_EXCHANGE_ID,
                TradingDay.is_trading_day.is_(True),
                TradingDay.date >= from_,
                TradingDay.date <= to,
            )
            .order_by(TradingDay.date.asc())
        )
    ).scalars()
    return TradingDaysOut(**{"from": from_, "to": to, "dates": list(rows)})


@router.get("/status", response_model=StatusOut, summary="Data freshness")
async def get_status(session: SessionDep) -> StatusOut:
    """docs/07: "{ as_of, data_version, last_pipeline_run }".

    ``degraded`` is the extra member docs/11 §Reliability implies: "if the pipeline fails, serve
    the last good `data_version` with a banner". The banner needs something to read, and this is
    it — the analytics endpoints keep answering from the last published version.
    """
    last_run = (
        await session.execute(select(PipelineRun).order_by(PipelineRun.trade_date.desc()).limit(1))
    ).scalar_one_or_none()

    return StatusOut(
        as_of=await latest_published_date(session),
        data_version=await current_data_version(session),
        last_pipeline_run=(
            None
            if last_run is None
            else PipelineRunOut(
                trade_date=last_run.trade_date,
                status=last_run.status,
                started_at=last_run.started_at,
                finished_at=last_run.finished_at,
                data_version=last_run.data_version,
            )
        ),
        degraded=last_run is not None and last_run.status in FAILED_RUN_STATUSES,
        pipeline_running=last_run is not None and last_run.status == RUNNING_RUN_STATUS,
        data_start_date=DATA_START_DATE,
    )
