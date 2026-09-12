"""``/vbt/*`` — the volume-breakout sleeve's read surfaces, plus its one settings write (VB8).

    GET   /vbt/today?date               the session's candidates, its rejects, the gate, the funnel
    GET   /vbt/today/{id}/bars          130 closes for the row's mini chart
    GET   /vbt/breadth?from&to          the `vb_breadth_daily` series behind the gauge
    GET   /vbt/book                     working orders, the book, and the fill rate
    GET   /vbt/backtest                 the latest finished run per source, and the study's numbers
    GET   /vbt/config                   the settings, the ceilings, the two only a job may write
    PATCH /vbt/config                   change a setting, audited, bounded
    POST  /vbt/scan                     "Scan now": queue a re-detection of the last session
    GET   /vbt/scan/{run_id}            that run's state, its session and its funnel

READ-ONLY EXCEPT FOR ONE ROUTE, AND THAT ROUTE MOVES NO MONEY
-------------------------------------------------------------
``docs/vbt/02`` Track C §4 is blunt: **"`apps/web` gets no route under `/vbt` that can reach the
gateway."** There is no `POST /vbt/execute` here and there is not going to be one — confirming a
line is the desk's, on `kite-momentum-rebalancer`, behind Maulik's own login (`05` §3). This
router does not import the execution package, names no broker, and has no verb but GET and one
PATCH. (The package's name is spelt out nowhere in this file on purpose: `test_vbt_readonly.py`
asserts its absence over the source, and a mention in prose would defeat the check.)

``PATCH /vbt/config`` writes four numbers into ``vb_config`` (`baskfy_api.vbt_settings`). It
cannot place, cancel or size an order. The two fields that decide what the *system* allows —
``dry_run_sessions`` and ``first_live_sessions_left`` — are not fields of its request model at
all, so a caller cannot name them; and every value it accepts is checked against a server-side
ceiling that no form can reach. ``VbtConfigPatch`` forbids unknown fields, so a patch asking for
something it does not own is **refused** rather than silently ignored.

``POST /vbt/scan`` is the second write and the first POST, and it moves no money either. It
inserts one ``vb_scan_run`` row and publishes one task **name**; the worker behind that name
(`baskfy.vbt.rescan`, shipped with VB12) reads bars and writes ``vb_signal_daily`` and
``vb_breadth_daily``. A detection is not a plan and a plan is not an order — the evening job is
still what turns a signal into a plan line, and a person at the desk is still what turns a plan
line into an order. `baskfy_api.vbt_scan` names no broker and constructs no row but that one.

``services/api/tests/test_vbt_readonly.py`` asserts all of it over the source and over the
OpenAPI document, the way ``test_swing_readonly.py`` does for the swing book.

WHOSE BOOK IT IS
----------------
The sole tenant's, resolved through ``scoped_sole_user_id``, which **refuses** a principal who is
not the sole tenant rather than serving them somebody else's positions. A sleeve is one person's
money; there is no shared view of it and no anonymous one.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import vbt as vbt_service
from baskfy_api import vbt_scan
from baskfy_api.auth import AuthenticatedDep, settings_for
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.db import SessionDep
from baskfy_api.problems import not_found
from baskfy_api.settings import Settings
from baskfy_api.vbt_settings import (
    VbtCeilings,
    VbtConfigNotSeeded,
    VbtConfigPatch,
    VbtConfigView,
    apply_patch,
    read_config,
    to_view,
)
from baskfy_core.models import VbConfig
from baskfy_core.screener import canonical_json
from baskfy_core.vbt.config import DRY_RUN_SESSIONS_REQUIRED
from baskfy_core.vbt.published import PUBLISHED

router = APIRouter(prefix="/vbt", tags=["vbt"])

JSON_MEDIA_TYPE: Final = "application/json"

#: How far back `GET /vbt/breadth` looks when the caller names no window (`05` §2's "last year").
DEFAULT_BREADTH_DAYS: Final = vbt_service.BREADTH_DEFAULT_DAYS


def _settings(request: Request) -> Settings:
    """The settings the app was built with, not a freshly read singleton — `swing.py`'s reason."""
    return settings_for(request)


SettingsDep = Annotated[Settings, Depends(_settings)]


def _json(model: BaseModel) -> Response:
    """The canonical encoder. Pydantic renders ``Decimal`` through ``float``, which turns a
    stored ``149.60`` into ``149.6``; house rule 8 makes the stored precision the contract, and
    a limit price is a number somebody types into a broker."""
    return Response(
        content=canonical_json(model.model_dump(mode="python", by_alias=True)),
        media_type=JSON_MEDIA_TYPE,
    )


# --- response models ---------------------------------------------------------
#
# Every model is prefixed `Vbt`, for the reason `swing.py` records: OpenAPI names a schema by its
# Python class name, and a second `PlanOut` in this service silently renames every existing
# reference in the generated TypeScript client.


class VbtCandidateOut(BaseModel):
    """One candidate, or one reject. `failed_filters` is empty for a signal and carries the
    letters (`04` §3.2's A…F) for a `SCAN_ONLY` row — `01` §3's ablation is the argument for the
    filters, and a page that never shows what they rejected makes that argument unreadable."""

    instrument_id: int
    symbol: str
    name: str
    state: str
    failed_filters: list[str]
    close: Decimal
    limit_price: Decimal
    stop_price: Decimal
    change_pct: Decimal | None
    rvol: Decimal | None
    close_position: Decimal | None
    ret_20_pct: Decimal | None
    turnover_avg_20: int | None
    sma_200: Decimal | None
    ema_21: Decimal | None
    high_20_prior: Decimal | None
    #: Derived: how far the close sits above its 200-day average, in per cent.
    pct_above_dma: Decimal | None
    locked_upper_circuit: bool
    rank_key: int


class VbtTodayOut(BaseModel):
    """`05` §2's Today tab.

    `as_of` null means the detector has never written a session — **not** that today had no
    candidates. The two look identical on a page without the funnel, which is why the funnel is
    on it even at zero.
    """

    as_of: dt.date | None
    gate: str | None
    pct_above_dma: Decimal | None
    above_count: int | None
    measured_count: int | None
    gate_threshold_pct: Decimal
    thin_session: bool
    funnel: dict[str, object] | None
    shut_sessions_recent: int
    shut_window: int
    candidates: list[VbtCandidateOut]
    rejects: list[VbtCandidateOut]


class VbtBarOut(BaseModel):
    date: dt.date
    close: Decimal


class VbtBarsOut(BaseModel):
    instrument_id: int
    #: **Adjusted** closes: a chart shows a shape, and a raw series with a split in it shows a
    #: cliff that never happened. The levels drawn on it are exchange prices.
    adjusted: bool = True
    data: list[VbtBarOut]


class VbtBreadthPointOut(BaseModel):
    date: dt.date
    pct_above_dma: Decimal
    above_count: int
    measured_count: int
    gate: str
    thin_session: bool


class VbtBreadthOut(BaseModel):
    threshold_pct: Decimal
    data: list[VbtBreadthPointOut]


class VbtWorkingOut(BaseModel):
    id: int
    instrument_id: int
    symbol: str
    name: str
    limit_price: Decimal
    stop_price: Decimal
    quantity: int
    value_inr: Decimal
    state: str
    signal_date: dt.date
    working_from: dt.date | None
    expires_after_session: dt.date | None
    sessions_worked: int
    sessions_allowed: int
    #: True when this is the last session the limit works (`04` §7.2). The page says
    #: "cancels tonight", which is the one thing a person can act on before the evening does.
    expires_tonight: bool
    broker_order_id: str | None
    filled_quantity: int
    simulated: bool


class VbtPositionOut(BaseModel):
    id: int
    instrument_id: int
    symbol: str
    name: str
    entry_date: dt.date
    entry_avg: Decimal
    quantity_open: int
    initial_stop: Decimal
    stop_price: Decimal
    gtt_id: str | None
    #: Shares open and no resting GTT — the one state the method forbids (`04` §6.1). Red on the
    #: page, and `VBT_POSITION_NAKED` in the evening.
    naked: bool
    last_close: Decimal | None
    ema_21: Decimal | None
    distance_to_ema_pct: Decimal | None
    return_pct: Decimal | None
    r_multiple: Decimal | None
    sessions_held: int | None
    exit_queued_for: dt.date | None
    exit_reason_queued: str | None
    simulated: bool


class VbtClosedOut(BaseModel):
    id: int
    instrument_id: int
    symbol: str
    name: str
    entry_date: dt.date
    closed_on: dt.date | None
    entry_avg: Decimal
    exit_avg: Decimal | None
    quantity_entered: int
    close_reason: str | None
    return_pct: Decimal | None
    r_multiple: Decimal | None
    simulated: bool


class VbtFillRateOut(BaseModel):
    """`04` §7.3: the single most likely place the live result parts company with the study.

    `rate_pct` is null until an order has resolved — a book with three working limits and no
    history has no fill rate, and showing 0% would be a lie about the machinery rather than a
    fact about the market.
    """

    filled: int
    resolved: int
    rate_pct: Decimal | None
    modelled_pct: Decimal


class VbtBookOut(BaseModel):
    working: list[VbtWorkingOut]
    open_positions: list[VbtPositionOut]
    closed_positions: list[VbtClosedOut]
    fill_rate: VbtFillRateOut


class VbtPublishedOut(BaseModel):
    """STRATEGY §4's numbers, so the page never transcribes them into a template."""

    start: str
    end: str
    years: float
    cagr_pct: float
    max_drawdown_pct: float
    trades: int
    win_rate_pct: float
    profit_factor: float
    avg_hold_sessions: float
    exposure_pct: float
    sharpe: float
    in_sample_cagr_pct: float
    in_sample_dd_pct: float
    out_of_sample_cagr_pct: float
    out_of_sample_dd_pct: float
    modelled_fill_rate_pct: float


class VbtBacktestRunOut(BaseModel):
    id: int
    source: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    params: dict[str, object]
    stats: dict[str, object] | None
    #: VB9's comparison against the published numbers, and whether it is more than a CAGR point
    #: out. Null on a run written before the drift computation existed.
    drift: dict[str, object] | None
    error: str | None


class VbtBacktestOut(BaseModel):
    published: VbtPublishedOut
    runs: list[VbtBacktestRunOut]


# --- routes ------------------------------------------------------------------
#
# Every handler is named `*_vbt_*` for the reason the response models are prefixed: FastAPI
# derives an operation id from the function name, the generated TypeScript client derives a
# method name from that, and a second `getBook` in this service silently renames somebody else's.


def _candidate_out(row: vbt_service.CandidateRow) -> VbtCandidateOut:
    return VbtCandidateOut(
        instrument_id=row.instrument_id,
        symbol=row.symbol,
        name=row.name,
        state=row.state,
        failed_filters=list(row.failed_filters),
        close=row.close,
        limit_price=row.limit_price,
        stop_price=row.stop_price,
        change_pct=row.change_pct,
        rvol=row.rvol,
        close_position=row.close_position,
        ret_20_pct=row.ret_20_pct,
        turnover_avg_20=row.turnover_avg_20,
        sma_200=row.sma_200,
        ema_21=row.ema_21,
        high_20_prior=row.high_20_prior,
        pct_above_dma=row.pct_above_dma,
        locked_upper_circuit=row.locked_upper_circuit,
        rank_key=row.rank_key,
    )


@router.get("/today", response_model=VbtTodayOut, summary="The session's candidates and the gate")
async def get_vbt_today(
    session: SessionDep,
    principal: AuthenticatedDep,
    date: Annotated[
        dt.date | None, Query(description="a published session; default the latest")
    ] = None,
) -> Response:
    """`05` §2's Today tab, in one call.

    The default is **the latest session the detector wrote**, not today's date: `04` §10's clock
    is the published session's, so during a session "today" has no bar and asking for it would
    answer with an empty page instead of the last real one.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    view = await vbt_service.today(session, user_id=user_id, day=date)
    return _json(
        VbtTodayOut(
            as_of=view.as_of,
            gate=view.gate,
            pct_above_dma=view.pct_above_dma,
            above_count=view.above_count,
            measured_count=view.measured_count,
            gate_threshold_pct=view.gate_threshold_pct,
            thin_session=view.thin_session,
            funnel=view.funnel,
            shut_sessions_recent=view.shut_sessions_recent,
            shut_window=view.shut_window,
            candidates=[_candidate_out(row) for row in view.candidates],
            rejects=[_candidate_out(row) for row in view.rejects],
        )
    )


@router.get(
    "/today/{instrument_id}/bars", response_model=VbtBarsOut, summary="Closes for the mini chart"
)
async def get_vbt_bars(
    session: SessionDep,
    principal: AuthenticatedDep,
    instrument_id: int,
    date: Annotated[dt.date | None, Query(description="as of; default the latest session")] = None,
) -> Response:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    as_of = date or (await vbt_service.today(session, user_id=user_id)).as_of
    if as_of is None:
        raise not_found("published volume-breakout session", str(instrument_id))
    data = await vbt_service.bars_for(session, instrument_id=instrument_id, as_of=as_of)
    return _json(
        VbtBarsOut(
            instrument_id=instrument_id,
            data=[VbtBarOut(date=point.date, close=point.close) for point in data],
        )
    )


@router.get("/breadth", response_model=VbtBreadthOut, summary="The breadth gauge's series")
async def get_vbt_breadth(
    session: SessionDep,
    principal: AuthenticatedDep,
    date_from: Annotated[dt.date | None, Query(alias="from")] = None,
    date_to: Annotated[dt.date | None, Query(alias="to")] = None,
) -> Response:
    """The `vb_breadth_daily` series with the 40% line that decides the gate.

    With no window the last year is served (`05` §2), because an unbounded default would grow
    into a nine-year payload on a page that draws one chart.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    start = date_from
    if start is None and date_to is None:
        latest = await vbt_service.today(session, user_id=user_id)
        if latest.as_of is not None:
            start = latest.as_of - dt.timedelta(days=DEFAULT_BREADTH_DAYS)
    view = await vbt_service.breadth(session, user_id=user_id, start=start, end=date_to)
    return _json(
        VbtBreadthOut(
            threshold_pct=view.threshold_pct,
            data=[
                VbtBreadthPointOut(
                    date=point.date,
                    pct_above_dma=point.pct_above_dma,
                    above_count=point.above_count,
                    measured_count=point.measured_count,
                    gate=point.gate,
                    thin_session=point.thin_session,
                )
                for point in view.data
            ],
        )
    )


@router.get("/book", response_model=VbtBookOut, summary="Working orders, the book, the fill rate")
async def get_vbt_book(session: SessionDep, principal: AuthenticatedDep) -> Response:
    """`05` §2's Book tab. Simulated rows are labelled and never share a total with real ones."""
    user_id = await scoped_sole_user_id(session, principal.user_id)
    latest = await vbt_service.today(session, user_id=user_id)
    view = await vbt_service.book(session, user_id=user_id, as_of=latest.as_of)
    return _json(
        VbtBookOut(
            working=[
                VbtWorkingOut(
                    id=row.id,
                    instrument_id=row.instrument_id,
                    symbol=row.symbol,
                    name=row.name,
                    limit_price=row.limit_price,
                    stop_price=row.stop_price,
                    quantity=row.quantity,
                    value_inr=row.value_inr,
                    state=row.state,
                    signal_date=row.signal_date,
                    working_from=row.working_from,
                    expires_after_session=row.expires_after_session,
                    sessions_worked=row.sessions_worked,
                    sessions_allowed=row.sessions_allowed,
                    expires_tonight=row.expires_tonight,
                    broker_order_id=row.broker_order_id,
                    filled_quantity=row.filled_quantity,
                    simulated=row.simulated,
                )
                for row in view.working
            ],
            open_positions=[
                VbtPositionOut(
                    id=row.id,
                    instrument_id=row.instrument_id,
                    symbol=row.symbol,
                    name=row.name,
                    entry_date=row.entry_date,
                    entry_avg=row.entry_avg,
                    quantity_open=row.quantity_open,
                    initial_stop=row.initial_stop,
                    stop_price=row.stop_price,
                    gtt_id=row.gtt_id,
                    naked=row.naked,
                    last_close=row.last_close,
                    ema_21=row.ema_21,
                    distance_to_ema_pct=row.distance_to_ema_pct,
                    return_pct=row.return_pct,
                    r_multiple=row.r_multiple,
                    sessions_held=row.sessions_held,
                    exit_queued_for=row.exit_queued_for,
                    exit_reason_queued=row.exit_reason_queued,
                    simulated=row.simulated,
                )
                for row in view.open_positions
            ],
            closed_positions=[
                VbtClosedOut(
                    id=row.id,
                    instrument_id=row.instrument_id,
                    symbol=row.symbol,
                    name=row.name,
                    entry_date=row.entry_date,
                    closed_on=row.closed_on,
                    entry_avg=row.entry_avg,
                    exit_avg=row.exit_avg,
                    quantity_entered=row.quantity_entered,
                    close_reason=row.close_reason,
                    return_pct=row.return_pct,
                    r_multiple=row.r_multiple,
                    simulated=row.simulated,
                )
                for row in view.closed_positions
            ],
            fill_rate=VbtFillRateOut(
                filled=view.fill_rate.filled,
                resolved=view.fill_rate.resolved,
                rate_pct=view.fill_rate.rate_pct,
                modelled_pct=view.fill_rate.modelled_pct,
            ),
        )
    )


@router.get("/backtest", response_model=VbtBacktestOut, summary="The backtest, and the study")
async def get_vbt_backtest(session: SessionDep, principal: AuthenticatedDep) -> Response:
    """The latest finished run per source, beside STRATEGY §4's published numbers.

    The caveats are `01` §5's and they belong **above** these numbers on the page, as a component
    rather than a footer (house rule 9). This route serves the numbers; `05` §2 says where the
    caveats go, and the read-only test does not let the page forget them.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    runs = await vbt_service.backtests(session, user_id=user_id)
    return _json(
        VbtBacktestOut(
            published=VbtPublishedOut(
                start=PUBLISHED.start,
                end=PUBLISHED.end,
                years=PUBLISHED.years,
                cagr_pct=PUBLISHED.cagr_pct,
                max_drawdown_pct=PUBLISHED.max_drawdown_pct,
                trades=PUBLISHED.trades,
                win_rate_pct=PUBLISHED.win_rate_pct,
                profit_factor=PUBLISHED.profit_factor,
                avg_hold_sessions=PUBLISHED.avg_hold_sessions,
                exposure_pct=PUBLISHED.exposure_pct,
                sharpe=PUBLISHED.sharpe,
                in_sample_cagr_pct=PUBLISHED.in_sample_cagr_pct,
                in_sample_dd_pct=PUBLISHED.in_sample_dd_pct,
                out_of_sample_cagr_pct=PUBLISHED.out_of_sample_cagr_pct,
                out_of_sample_dd_pct=PUBLISHED.out_of_sample_dd_pct,
                modelled_fill_rate_pct=PUBLISHED.modelled_fill_rate_pct,
            ),
            runs=[
                VbtBacktestRunOut(
                    id=run.id,
                    source=run.source,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    params=run.params,
                    stats=run.stats,
                    drift=run.drift,
                    error=run.error,
                )
                for run in runs
            ],
        )
    )


async def _config_or_404(session: AsyncSession, user_id: int) -> VbConfig:
    try:
        return await read_config(session, user_id)
    except VbtConfigNotSeeded as exc:
        raise not_found("volume-breakout sleeve", f"user:{user_id}") from exc


@router.get("/config", response_model=VbtConfigView, summary="The sleeve's settings")
async def get_vbt_config(
    session: SessionDep, principal: AuthenticatedDep, settings: SettingsDep
) -> Response:
    """The four editable numbers, the server's ceilings, and the two only a job may write.

    The ceilings come back on every read so a form can render "max 15% — set by the server"
    rather than discovering the limit by being refused.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    row = await _config_or_404(session, user_id)
    return _json(
        to_view(
            row,
            ceilings=VbtCeilings.from_settings(settings),
            execution_enabled=settings.vbt_execution_enabled,
            dry_run_sessions_required=DRY_RUN_SESSIONS_REQUIRED,
        )
    )


@router.patch("/config", response_model=VbtConfigView, summary="Change a sleeve setting")
async def patch_vbt_config(
    session: SessionDep,
    principal: AuthenticatedDep,
    settings: SettingsDep,
    patch: VbtConfigPatch,
) -> Response:
    """The one write on this surface, and it moves no money.

    Four numbers. No order path, no broker, no session counter: `VbtConfigPatch` forbids unknown
    fields, so `{"dry_run_sessions": 20}` is refused rather than ignored — a caller who tried to
    declare the paper run finished is told the field does not exist here instead of being
    answered `200` and believing they had.

    A value above its ceiling answers **422** naming the ceiling and the environment variable
    that sets it, and the refusal is atomic: a two-field patch that crosses a ceiling on the
    second field changes neither.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    await _config_or_404(session, user_id)
    row = await apply_patch(
        session,
        user_id=user_id,
        patch=patch,
        ceilings=VbtCeilings.from_settings(settings),
        changed_by=f"user:{user_id}",
        now=dt.datetime.now(tz=dt.UTC),
    )
    # No `commit()`: `baskfy_api.db.get_session` owns the transaction and commits when the
    # handler returns. Committing here would also break the contract tests, which hand the app a
    # transaction they intend to roll back.
    return _json(
        to_view(
            row,
            ceilings=VbtCeilings.from_settings(settings),
            execution_enabled=settings.vbt_execution_enabled,
            dry_run_sessions_required=DRY_RUN_SESSIONS_REQUIRED,
        )
    )


class VbtScanRunOut(BaseModel):
    """One "Scan now" run (VB12). ``status`` walks QUEUED -> RUNNING -> DONE | FAILED.

    ``session_date`` is null until the worker has decided which published session it is
    re-detecting — the caller asks for "the latest" and only the worker knows which that is.
    ``funnel`` is the detector's own counts and is filled on DONE; ``error`` is the reason on
    FAILED; ``detail`` carries the rest, in the shape the nightly step writes so the two read
    the same. There is no ``provisional`` here and there is no column for one: this sleeve
    re-detects a **closed** session, never a partial one.
    """

    run_id: int
    status: str
    source: str
    requested_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    session_date: dt.date | None
    funnel: dict[str, object] | None
    detail: dict[str, object] | None
    error: str | None


class VbtScanQueuedOut(BaseModel):
    """What `POST /vbt/scan` answers, with a 202: the run to poll."""

    run_id: int
    status: str
    requested_at: dt.datetime


def _scan_run_out(view: vbt_scan.ScanRunView) -> VbtScanRunOut:
    return VbtScanRunOut(
        run_id=view.run_id,
        status=view.status,
        source=view.source,
        requested_at=view.requested_at,
        started_at=view.started_at,
        finished_at=view.finished_at,
        session_date=view.session_date,
        funnel=view.funnel,
        detail=view.detail,
        error=view.error,
    )


@router.post(
    "/scan",
    response_model=VbtScanQueuedOut,
    status_code=202,
    summary="Scan now: queue a re-detection of the last published session",
)
async def post_vbt_scan(
    request: Request, session: SessionDep, principal: AuthenticatedDep, settings: SettingsDep
) -> Response:
    """VB12, over the API. One row in ``vb_scan_run`` and one task name published.

    **What it re-detects is a session that has already closed** — the latest one the pipeline has
    published, which the worker resolves. It is deliberately not the swing book's "scan today
    from live quotes": three of VBT-1's five lines read the day's volume against its 50-day
    average, the close's position inside the day's range and the day's change, and the entry
    limit *is* the signal bar's close, so an intraday answer would name a price that does not
    exist yet.

    A scan moves no money. This route reaches no broker and `baskfy_api.vbt_scan` names none. At
    most one in flight per tenant (409) and one request a minute (429, ``Retry-After``); both are
    answered from the table rather than a cache, so they hold on a box with no Redis.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    queue = getattr(request.app.state, "task_queue", None)
    row = await vbt_scan.request_scan(
        session,
        user_id=user_id,
        now=dt.datetime.now(tz=dt.UTC),
        min_interval=dt.timedelta(seconds=settings.vbt_scan_min_interval_seconds),
        stale_after=dt.timedelta(seconds=settings.vbt_scan_stale_after_seconds),
        source="web",
        queue=queue,
    )
    return Response(
        content=canonical_json(
            VbtScanQueuedOut(
                run_id=int(row.id), status=str(row.status), requested_at=row.requested_at
            ).model_dump(mode="python")
        ),
        media_type=JSON_MEDIA_TYPE,
        status_code=202,
    )


@router.get("/scan/{run_id}", response_model=VbtScanRunOut, summary="One scan's state")
async def get_vbt_scan(session: SessionDep, principal: AuthenticatedDep, run_id: int) -> Response:
    """That run, if it is this tenant's. Somebody else's id is a 404, not a peek."""
    user_id = await scoped_sole_user_id(session, principal.user_id)
    view = await vbt_scan.scan_run(session, user_id=user_id, run_id=run_id)
    if view is None:
        raise not_found("scan", str(run_id))
    return _json(_scan_run_out(view))
