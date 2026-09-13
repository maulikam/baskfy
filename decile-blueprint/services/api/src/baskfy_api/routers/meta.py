"""``/meta/*`` — docs/07 §Metadata.

Everything here is reference data the UI needs before it can render a screen form: the factor
registry behind the sort dropdown, the column picker, the fourteen universes, the named ranking
presets, the trading calendar behind the historical-date picker, and the freshness of the data.

The factors, columns, universes and ranking presets are served straight out of ``baskfy_core``
with no database access at all — docs/06 §"The factor registry" makes the registry "the single
source of truth for: the `sort_by` dropdown, the custom-filter operand list, the column picker,
the API enum", so the API's job is to publish it, not to keep a second copy.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Final

from fastapi import APIRouter, Query
from pydantic import JsonValue, TypeAdapter
from sqlalchemy import select

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.db import SessionDep
from baskfy_api.live_prices import live_marks_for_symbols, quotes_permitted
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.schemas import (
    ColumnOut,
    FactorOut,
    LiveMarksOut,
    PipelineRunOut,
    RankingPresetOut,
    StatusOut,
    TradingDaysOut,
    UniverseOut,
)
from baskfy_api.screener import DATA_START_DATE, current_data_version, latest_published_date
from baskfy_core.factor_registry import FACTORS, columns
from baskfy_core.models import PipelineRun, TradingDay
from baskfy_core.ranking_presets import PRESET_SPECS, list_presets
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
#: Validates a core preset patch (typed ``dict[str, object]``) as the JSON the wire carries.
_PRESET_PATCH: Final = TypeAdapter(dict[str, JsonValue])


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
            preference=factor.preference,
            rankable=factor.rankable,
            weight_family=factor.weight_family,
            validation_status=factor.validation_status,
            definition=factor.definition,
        )
        for factor in FACTORS.values()
    ]


@router.get(
    "/ranking-presets",
    response_model=list[RankingPresetOut],
    summary="Named ranking presets",
)
async def get_ranking_presets() -> list[RankingPresetOut]:
    """docs/ranking/PLAN.md §1.5 / C6: the named presets with their promotion status.

    Reference data like ``/meta/factors``: served straight from ``baskfy_core.ranking_presets``,
    no database, no principal. ``nse_momentum`` is absent because core refuses it (PLAN
    correction #8). ``patch`` is what the editor merges into a definition to apply one.
    """
    return [
        RankingPresetOut(
            key=row["key"],
            description=row["description"],
            status=row["status"],
            sort_by=row["sort_by"],
            patch=_json_patch(PRESET_SPECS[row["key"]].patch),
        )
        for row in list_presets()
    ]


def _json_patch(patch: dict[str, object]) -> dict[str, JsonValue]:
    """Core types a patch as ``object``; the wire needs JSON. Validating proves it is."""
    return _PRESET_PATCH.validate_python(patch)


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

    **Why the ordering carries a second key.** A date can hold more than one run: a failure is
    retried, and ``open_run`` only reuses a run still ``running``. Ordering on ``trade_date``
    alone left the tie to the planner, so with four runs for 2026-09-11 on the box — three
    failed, then one that passed the gate and published ``data_version`` 18 — this endpoint
    picked a *failed* one and served ``degraded: true`` for a day that had published
    successfully. The banner then said the data was stale while ``as_of`` said otherwise, which
    is the one thing a freshness endpoint must never do. ``started_at`` decides, ``id`` breaks
    a same-instant tie: the newest attempt is the one that describes today.
    """
    last_run = (
        await session.execute(
            select(PipelineRun)
            .order_by(
                PipelineRun.trade_date.desc(), PipelineRun.started_at.desc(), PipelineRun.id.desc()
            )
            .limit(1)
        )
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
        live_quotes=quotes_permitted(),
    )


@router.get("/live-marks", response_model=LiveMarksOut, summary="Live last prices")
async def get_live_marks(
    principal: AuthenticatedDep,
    symbols: Annotated[str, Query(description="Comma-separated NSE symbols, at most 500.")] = "",
) -> LiveMarksOut:
    """Display marks only. Ranks, factors and sleeve signals stay on the published session.

    Empty when there is no real Kite session. The page keeps the close in that case.
    """
    del principal
    names = [part.strip() for part in symbols.split(",") if part.strip()]
    marks = await live_marks_for_symbols(names)
    return LiveMarksOut(live_overlay=bool(marks), marks=marks)
