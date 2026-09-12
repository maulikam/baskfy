"""``/swing/*`` — the swing book's read surfaces, plus its one settings write (SW4).

    GET   /swing/setups?date&setup&status        the day's candidates, with the funnel
    GET   /swing/setups/{instrument_id}/bars     130 closes for the row's mini chart
    GET   /swing/market?from&to                  breadth, the gate and the rung over time
    GET   /swing/sectors?date                    the strip: sector breadth, and today's counts
    GET   /swing/config                          the settings, the ceilings, and the rung
    PATCH /swing/config                          change a setting, audited, bounded
    GET   /swing/journal                         the book's results in R, and the ladder (SW8)
    GET   /swing/signals?date&instrument_id      what the monitor raised in a session (SW14)
    POST  /swing/scan                            "Scan now": queue a detection run (SW15)
    GET   /swing/scan/{run_id}                   that run's state, session and funnel

READ-ONLY EXCEPT FOR ONE ROUTE, AND THAT ROUTE MOVES NO MONEY
-------------------------------------------------------------
``docs/swing/02-scope-and-gating.md`` Track A: the web app's `/swing` hub is "read-only — the
same rule as `/baskets`: every mutation on it is a 405 except watchlist edits, notes and the
catalyst field (they change no money)". Track C §4 is blunter: "`apps/web` gets no route under
`/swing` that can reach the gateway."

``POST /swing/scan`` (SW15) is the fifth money-free write: it inserts one ``sw_scan_run`` row and
publishes a task name. The worker then reads quotes and writes detection rows — a scan is never
an order, and the module behind the route (`baskfy_api.swing_scan`) names no broker.

``PATCH /swing/config`` is the exception the first sentence allows, and it is worth naming why it
is safe. It writes seven numbers into ``sw_config``. It cannot place, cancel or size an order; the
two fields that decide how much the *system* lets the book carry — the exposure rung and the
first-live-session countdown — are not fields of its request model at all, so a caller cannot
even name them. And every value it accepts is checked against a server-side ceiling that is not
reachable from any form (`baskfy_api.swing_settings`).

``services/api/tests/test_swing_readonly.py`` asserts all of this over the source and over the
OpenAPI document, the way ``test_desk_readonly.py`` does for the desk: no other verb, no import
of the execution package, no path that mentions an order.

WHOSE BOOK IT IS
----------------
The sole tenant's. Every route resolves the caller through ``scoped_sole_user_id``, which
**refuses** a principal that is not the sole tenant rather than quietly serving them the sole
tenant's rows — the failure M43.4 found on the watchlist. A swing book is one person's positions,
levels and results; there is no anonymous view of it and no shared one.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import swing as swing_service
from baskfy_api import swing_catalyst, swing_journal, swing_scan, swing_watch
from baskfy_api.auth import AuthenticatedDep, settings_for
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.db import SessionDep
from baskfy_api.problems import Problem, ProblemType, not_found
from baskfy_api.settings import Settings
from baskfy_api.swing_settings import (
    SwingCeilings,
    SwingConfigNotSeeded,
    SwingConfigPatch,
    SwingConfigView,
    apply_patch,
    read_config,
    to_view,
)
from baskfy_core.models import Instrument, SwConfig
from baskfy_core.screener import canonical_json

router = APIRouter(prefix="/swing", tags=["swing"])

JSON_MEDIA_TYPE: Final = "application/json"


def _settings(request: Request) -> Settings:
    """The settings the app was constructed with, not a freshly read singleton.

    The same reason `baskfy_api.auth.settings_for` exists: a test that builds an app with one
    configuration and a route that reads another is a suite passing against a config nothing
    runs in production.
    """
    return settings_for(request)


SettingsDep = Annotated[Settings, Depends(_settings)]


def _json(model: BaseModel) -> Response:
    """The canonical encoder — the same reason ``market_data`` uses one.

    Pydantic renders ``Decimal`` through ``float``, which turns a stored ``149.60`` into
    ``149.6``. House rule 8 makes the stored precision the contract, and a trigger is a price
    somebody types into a broker.
    """
    return Response(
        content=canonical_json(model.model_dump(mode="python", by_alias=True)),
        media_type=JSON_MEDIA_TYPE,
    )


def _one_of(value: str | None, allowed: frozenset[str], field: str) -> str | None:
    if value is None or value in allowed:
        return value
    raise Problem(
        ProblemType.BAD_REQUEST,
        f"{value!r} is not a {field}.",
        errors=[{"field": field, "message": f"choose one of {sorted(allowed)}"}],
    )


# --- response models ---------------------------------------------------------
#
# Every model here is prefixed `Swing`, and that is not decoration. OpenAPI names a schema by its
# Python class name, so a second `PlanOut` in this service collides with `baskfy_api.schemas`'
# billing plan: the generator then emits `baskfy_api__routers__swing__PlanOut` and
# `baskfy_api__schemas__PlanOut`, and every existing reference to `PlanOut` in the TypeScript
# client stops resolving. It did, on the first build.


class SwingCatalystOut(BaseModel):
    """SW11B (STANDING-ANSWERS A3): a link, never the filing.

    ``headline`` / ``published_at`` / ``url`` are the newest NSE announcement the feed stored
    for the name; ``earnings_date`` the nearest result meeting the event calendar lists. Any
    of the four may be null. A page renders ``url`` as a link that opens the exchange's own
    copy (``target=_blank rel=noopener``) and ``earnings_date`` as a badge — nothing here is
    text to reproduce, and nothing here is redistributed (Track C §7 amendment).
    """

    headline: str | None
    published_at: dt.datetime | None
    url: str | None
    earnings_date: dt.date | None


def _catalyst_out(view: swing_catalyst.CatalystView | None) -> SwingCatalystOut | None:
    if view is None or view.empty:
        return None
    return SwingCatalystOut(
        headline=view.headline,
        published_at=view.published_at,
        url=view.url,
        earnings_date=view.earnings_date,
    )


class SwingSetupOut(BaseModel):
    """One candidate row. Every price is an **exchange** price (SW3 divides by ``adj_factor``)."""

    instrument_id: int
    symbol: str
    name: str
    setup: str
    status: str
    #: ``Decimal``, not ``float``, all the way to the wire. House rule 8 makes the stored
    #: precision the contract, and `_json`'s canonical encoder serialises from the Decimal — a
    #: `float` here would turn a trigger of ``149.60`` into ``149.6`` before it ever got there,
    #: and that number is typed into a broker.
    score: Decimal
    close: Decimal | None
    trigger: Decimal | None
    stop_ref: Decimal | None
    pivot_high: Decimal | None
    #: Derived, not stored: ``(trigger - stop_ref) / trigger``. The number a person actually reads
    #: when deciding whether a setup is worth its risk.
    stop_distance_pct: Decimal | None
    adr_pct: Decimal | None
    prior_move_pct: Decimal | None
    base_depth_pct: Decimal | None
    tightness_adr: Decimal | None
    dryup_ratio: Decimal | None
    rvol: Decimal | None
    gap_pct: Decimal | None
    turnover_avg: int | None
    base_bars: int | None
    up_streak: int | None
    locked_upper_circuit: bool
    sector_slug: str | None
    listed_within_2y: bool
    #: SW11B: the feed's link for the name, or null when it has none.
    catalyst_feed: SwingCatalystOut | None = None


class SwingScanRunOut(BaseModel):
    """One "Scan now" run (SW15). ``status`` walks QUEUED -> RUNNING -> DONE | FAILED.

    ``session_date`` and ``provisional`` are null / false until the worker has decided which
    session it is scanning; ``funnel`` is the same shape as the setups page's and is filled on
    DONE; ``error`` is the reason on FAILED. ``detail`` carries the rest — the quote count and
    the skips — for a page that wants to say "1,812 of 1,830 names answered".
    """

    run_id: int
    status: str
    requested_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    session_date: dt.date | None
    provisional: bool
    funnel: dict[str, object] | None
    detail: dict[str, object] | None
    error: str | None


class SwingScanQueuedOut(BaseModel):
    """What `POST /swing/scan` answers, with a 202: the run to poll."""

    run_id: int
    status: str
    requested_at: dt.datetime


class SwingSetupsOut(BaseModel):
    as_of: dt.date | None
    #: SW15: the rows are from a bar built out of live quotes during the session, not from a
    #: published close — the header must say so ("provisional — scanned 13:42 IST from live
    #: quotes"), because a base that is tight at 13:42 can be wide by 15:30.
    as_of_provisional: bool = False
    #: When the day was last scanned on demand; null for a day the nightly wrote.
    scanned_at: dt.datetime | None = None
    #: This user's newest "Scan now" run, whatever its state, so the page can show "scanning…"
    #: or "the last scan failed: …" beside the button without a second call.
    last_scan: SwingScanRunOut | None = None
    #: The gate and the tier for the same day, so a page never has to make a second call to find
    #: out whether the candidates it is showing may be acted on at all.
    gate: str | None
    exposure_level: int | None
    max_open_positions: int | None
    max_exposure_pct: Decimal | None
    new_entries_allowed: bool | None
    #: universe → with a bar today → liquid → candidates per setup. `05` §2's empty state is
    #: written from this, and without it an empty list cannot be told from a job that never ran.
    funnel: dict[str, object] | None
    data: list[SwingSetupOut]


class SwingBarOut(BaseModel):
    date: dt.date
    close: Decimal
    ma_fast: Decimal | None
    ma_slow: Decimal | None


class SwingBarsOut(BaseModel):
    instrument_id: int
    symbol: str
    #: **Adjusted** closes: the chart shows the shape, and a raw series with a split in it shows
    #: a cliff that never happened. The tradeable levels beside it are exchange prices.
    adjusted: bool = True
    data: list[SwingBarOut]


class SwingMarketDayOut(BaseModel):
    date: dt.date
    constituent_count: int
    pct_up_strong_1m: Decimal | None
    pct_new_52w_high: Decimal | None
    pct_above_ma_slow: Decimal | None
    index_slug: str | None
    index_close: Decimal | None
    index_ma_fast: Decimal | None
    index_ma_slow: Decimal | None
    gate: str
    exposure_level: int
    max_open_positions: int
    max_exposure_pct: Decimal
    new_entries_allowed: bool
    parabolic_count: int
    #: `04` §8.5 (SW9.5): the allocation's drawdown from its peak that evening, and whether the
    #: lock-out held. Read from the row `swing-eod` wrote; the page shows "Locked out · 15.3%
    #: below its peak" in place of the rung while `drawdown_locked` is true.
    drawdown_pct: Decimal = Decimal(0)
    drawdown_locked: bool = False


class SwingMarketOut(BaseModel):
    data: list[SwingMarketDayOut]


class SwingSignalOut(BaseModel):
    """One verdict the monitor raised. A record, never an instruction."""

    id: int
    watch_id: int | None
    instrument_id: int
    symbol: str
    name: str
    setup: str
    session_date: dt.date
    raised_at: dt.datetime
    state: str
    or_window_minutes: int | None
    range_high: Decimal | None
    range_low: Decimal | None
    low_of_day: Decimal | None
    last_price: Decimal | None
    entry: Decimal | None
    stop: Decimal | None
    #: Set when the signal became a SIGNAL plan line. The id is shown, not followed: the line
    #: lives on the desk console and this surface has no route to it.
    plan_line_id: int | None


class SwingSignalsOut(BaseModel):
    #: The session the rows belong to; `None` when the monitor has never written one.
    session_date: dt.date | None
    data: list[SwingSignalOut]


class SwingSectorOut(BaseModel):
    slug: str
    pct_above_ma_slow: float
    members: int
    candidates: int
    #: In the top three, and therefore worth `+5` on a candidate's score (`04` §2.6).
    hot: bool


class SwingSectorsOut(BaseModel):
    as_of: dt.date | None
    data: list[SwingSectorOut]


class SwingWatchOut(BaseModel):
    id: int
    instrument_id: int
    symbol: str
    name: str
    setup: str
    source: str
    added_on: dt.date
    expires_on: dt.date | None
    trigger: Decimal | None
    stop_ref: Decimal | None
    #: How far the last close sits below the trigger, as a percentage of it. The one number a
    #: person scanning a watchlist actually reads: "how close is this to going".
    distance_to_trigger_pct: Decimal | None
    last_close: Decimal | None
    note: str | None
    catalyst: str | None
    state: str
    #: SW10.5 (STANDING-ANSWERS A14): the score the row is ranked by, the ADR it was watched
    #: with, whether it is in today's focus (top 5 flags by score + every EP — pushed and shown
    #: on top), and when a MANUAL row was last re-confirmed (its expiry runs from there).
    score: Decimal | None = None
    adr_pct: Decimal | None = None
    focus: bool = False
    reconfirmed_on: dt.date | None = None
    #: SW11B (A3): the earnings flag the 09:10 feed keeps on the row, and the newest filing's
    #: link. ``catalyst`` above is the text — typed, or auto-filled from that headline while it
    #: was empty; `PATCH` still owns it.
    earnings_date: dt.date | None = None
    catalyst_feed: SwingCatalystOut | None = None


class SwingWatchListOut(BaseModel):
    data: list[SwingWatchOut]


class SwingWatchIn(BaseModel):
    """Adding a name by hand. Levels optional — a name with no trigger is one to look at.

    The name is given either by ``instrument_id`` or by ``symbol`` (SW14): the web form's
    lookup is `GET /search`, whose instrument hit carries the symbol and not the id, and a
    server action that had to make a second call to translate one into the other would be a
    second place for the two to disagree. Exactly one of the two is required.
    """

    model_config = ConfigDict(extra="forbid")

    instrument_id: int | None = None
    symbol: str | None = Field(default=None, min_length=1, max_length=32)
    setup: Literal["FLAG", "EP"]
    trigger: Decimal | None = Field(default=None, gt=0)
    stop_ref: Decimal | None = Field(default=None, gt=0)
    note: str | None = Field(default=None, max_length=2000)
    catalyst: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _one_name(self) -> SwingWatchIn:
        if (self.instrument_id is None) == (self.symbol is None):
            raise ValueError("give exactly one of instrument_id or symbol")
        return self


class SwingWatchPatch(BaseModel):
    """The two free-text fields and the re-confirmation, and nothing else.

    `docs/swing/02` Track A allows the watchlist's writes because they "change no money" — which
    is true of a note, a catalyst and a re-confirmation (A14: a MANUAL row expires after ten
    sessions unless a person says they still want it; `reconfirm: true` restarts that clock and
    changes no level), and would stop being true the moment this model grew a `trigger`. A
    level a person can edit after the fact is a level that can be edited to match a price,
    which is how a plan comes to justify a trade rather than the other way round.
    """

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2000)
    catalyst: str | None = Field(default=None, max_length=2000)
    reconfirm: bool = False


class SwingPlanLineOut(BaseModel):
    id: int
    kind: str
    symbol: str
    name: str
    setup: str | None
    quantity: int
    trigger: Decimal | None
    stop: Decimal | None
    risk_inr: Decimal
    position_value: Decimal
    trail: str | None
    note: str | None
    state: str


class SwingPlanSkipOut(BaseModel):
    symbol: str
    reason: str
    detail: str | None


class SwingPlanOut(BaseModel):
    plan_id: str
    as_of: dt.date
    source: str
    built_at: dt.datetime
    expires_at: dt.datetime
    gate: str
    exposure_level: int
    total_risk_inr: Decimal
    total_new_exposure_inr: Decimal
    lines: list[SwingPlanLineOut]
    #: Never omitted, even when empty. A plan without its refusals is half a document (`04` §9).
    skips: list[SwingPlanSkipOut]


class SwingPositionOut(BaseModel):
    id: int
    instrument_id: int
    symbol: str
    name: str
    setup: str
    entry_date: dt.date
    entry_avg: Decimal
    quantity_entered: int
    quantity_open: int
    initial_stop: Decimal
    stop: Decimal
    gtt_id: str | None
    #: Open with no resting stop. Rendered in red, and the EOD email leads with it.
    naked: bool
    trail: str
    partial_done: bool
    state: str
    closed_on: dt.date | None
    exit_avg: Decimal | None
    close_reason: str | None
    r_multiple: Decimal | None
    pnl_inr: Decimal | None
    simulated: bool
    last_close: Decimal | None


class SwingPositionsOut(BaseModel):
    data: list[SwingPositionOut]
    #: The plan preview `05` §2 shows beside the book. `None` before the first EOD run.
    plan: SwingPlanOut | None


class SwingJournalStatsOut(BaseModel):
    """`04` §10's statistics, in R. An empty book is a row of zeros, not an error."""

    trades: int
    win_rate_pct: Decimal
    avg_win_r: Decimal
    avg_loss_r: Decimal
    expectancy_r: Decimal
    #: Gross win R over gross loss R; ``None`` when there is no loss to divide by.
    profit_factor: Decimal | None
    net_r: Decimal
    largest_win_r: Decimal
    largest_loss_r: Decimal
    current_loss_streak: int


class SwingHistogramBarOut(BaseModel):
    #: One of `swing_journal.HISTOGRAM_BUCKETS`, always all six, in order.
    bucket: str
    count: int


class SwingSetupStatsOut(BaseModel):
    setup: str
    trades: int
    net_r: Decimal
    expectancy_r: Decimal


class SwingMonthStatsOut(BaseModel):
    #: ``YYYY-MM`` of the exit date.
    month: str
    trades: int
    net_r: Decimal


class SwingJournalTradeOut(BaseModel):
    """One closed trade, with the numbers that were written at its close."""

    symbol: str
    setup: str
    entry_date: dt.date
    exit_date: dt.date
    entry: Decimal
    initial_stop: Decimal
    exit_avg: Decimal
    quantity: int
    r_multiple: Decimal
    pnl_inr: Decimal
    close_reason: str | None


class SwingJournalCardOut(BaseModel):
    """One book — real or simulated, never both (`04` §10)."""

    stats: SwingJournalStatsOut
    histogram: list[SwingHistogramBarOut]
    by_setup: list[SwingSetupStatsOut]
    by_month: list[SwingMonthStatsOut]
    #: Newest first, at most `swing_journal.MAX_TRADES`.
    trades: list[SwingJournalTradeOut]


class SwingSessionsOut(BaseModel):
    """The counter — "14 of 20 paper sessions logged" — information, not a gate (`02` §3, A11)."""

    logged: int
    required: int


class SwingLadderOut(BaseModel):
    """The rung in force, what it allows, and the closes it was computed from."""

    level: int
    gate: str
    max_open_positions: int
    max_exposure_pct: Decimal
    new_entries_allowed: bool
    #: The last `lookback_trades` R values of the book the ladder reads, oldest first.
    last_r: list[Decimal]
    #: ``SIMULATED`` while `BASKFY_SWING_EXECUTION_ENABLED` is false, ``REAL`` after (PACK.6).
    reads: Literal["SIMULATED", "REAL"]


class SwingBacktestCardOut(BaseModel):
    """SW9's card. The shape is C3's; SW9 fills it and owns its ``stats``.

    Declared now so the TypeScript client carries the type before the run exists, and the page
    can render "not run yet" against ``None`` rather than against an absent key.
    """

    run_id: int
    params: dict[str, object]
    started_at: str
    finished_at: str | None
    stats: dict[str, object]
    #: `04` §11's sentences, verbatim, so the page cannot paraphrase a caveat away.
    caveats: list[str]


class SwingJournalOut(BaseModel):
    real: SwingJournalCardOut
    simulated: SwingJournalCardOut
    sessions: SwingSessionsOut
    ladder: SwingLadderOut
    #: ``None`` until SW9 has stored a run.
    backtest: SwingBacktestCardOut | None


# --- routes ------------------------------------------------------------------


@router.get("/setups", response_model=SwingSetupsOut, summary="The day's swing candidates")
async def get_setups(
    session: SessionDep,
    principal: AuthenticatedDep,
    date: Annotated[dt.date | None, Query()] = None,
    setup: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
) -> Response:
    """`docs/swing/05` §2's Setups tab, in one call.

    ``date`` defaults to the last day the **detectors** wrote, not to the published pipeline
    date: the swing step can be skipped while the screener publishes normally, and a page asking
    for the published date would then render an empty day rather than the last real one.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    page = await swing_service.setups(
        session,
        user_id=user_id,
        on=date,
        setup=_one_of(setup, swing_service.SETUPS, "setup"),
        status=_one_of(status, swing_service.STATUSES, "status"),
    )
    return _json(
        SwingSetupsOut(
            as_of=page.as_of,
            as_of_provisional=page.as_of_provisional,
            scanned_at=page.scanned_at,
            last_scan=_scan_run_out(page.last_scan),
            gate=page.gate,
            exposure_level=page.exposure_level,
            max_open_positions=page.max_open_positions,
            max_exposure_pct=page.max_exposure_pct,
            new_entries_allowed=page.new_entries_allowed,
            funnel=page.funnel,
            data=[
                SwingSetupOut(
                    instrument_id=row.instrument_id,
                    symbol=row.symbol,
                    name=row.name,
                    setup=row.setup,
                    status=row.status,
                    score=row.score,
                    close=row.close,
                    trigger=row.trigger,
                    stop_ref=row.stop_ref,
                    pivot_high=row.pivot_high,
                    stop_distance_pct=row.stop_distance_pct,
                    adr_pct=row.adr_pct,
                    prior_move_pct=row.prior_move_pct,
                    base_depth_pct=row.base_depth_pct,
                    tightness_adr=row.tightness_adr,
                    dryup_ratio=row.dryup_ratio,
                    rvol=row.rvol,
                    gap_pct=row.gap_pct,
                    turnover_avg=row.turnover_avg,
                    base_bars=row.base_bars,
                    up_streak=row.up_streak,
                    locked_upper_circuit=row.locked_upper_circuit,
                    sector_slug=row.sector_slug,
                    listed_within_2y=row.listed_within_2y,
                    catalyst_feed=_catalyst_out(row.catalyst_feed),
                )
                for row in page.rows
            ],
        )
    )


def _scan_run_out(view: swing_service.ScanRunView | None) -> SwingScanRunOut | None:
    if view is None:
        return None
    return SwingScanRunOut(
        run_id=view.run_id,
        status=view.status,
        requested_at=view.requested_at,
        started_at=view.started_at,
        finished_at=view.finished_at,
        session_date=view.session_date,
        provisional=view.provisional,
        funnel=view.funnel,
        detail=view.detail,
        error=view.error,
    )


@router.post(
    "/scan",
    response_model=SwingScanQueuedOut,
    status_code=202,
    summary="Scan now: queue a detection run",
)
async def post_scan(
    request: Request, session: SessionDep, principal: AuthenticatedDep, settings: SettingsDep
) -> Response:
    """SW15. One row in ``sw_scan_run`` and one task name published; the worker does the rest.

    During the session (09:15-15:30 IST on a trading day) the worker builds a provisional bar
    per liquid name from live Kite quotes and detects on it; at any other time it re-detects the
    last published session. A scan moves no money (`02` Track A) and this route reaches no
    broker — `baskfy_api.swing_scan` names none. At most one in flight per user (409) and one
    request a minute (429, ``Retry-After``); both answered from the table.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    queue = getattr(request.app.state, "task_queue", None)
    row = await swing_scan.request_scan(
        session,
        user_id=user_id,
        now=dt.datetime.now(tz=dt.UTC),
        min_interval=dt.timedelta(seconds=settings.swing_scan_min_interval_seconds),
        stale_after=dt.timedelta(seconds=settings.swing_scan_stale_after_seconds),
        source="web",
        queue=queue,
    )
    return Response(
        content=canonical_json(
            SwingScanQueuedOut(
                run_id=int(row.id), status=str(row.status), requested_at=row.requested_at
            ).model_dump(mode="python")
        ),
        media_type=JSON_MEDIA_TYPE,
        status_code=202,
    )


@router.get("/scan/{run_id}", response_model=SwingScanRunOut, summary="One scan's state")
async def get_scan(session: SessionDep, principal: AuthenticatedDep, run_id: int) -> Response:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    view = await swing_service.scan_run(session, user_id=user_id, run_id=run_id)
    out = _scan_run_out(view)
    if out is None:
        raise not_found("scan", str(run_id))
    return _json(out)


@router.get(
    "/setups/{instrument_id}/bars", response_model=SwingBarsOut, summary="The mini chart's series"
)
async def get_bars(
    session: SessionDep,
    principal: AuthenticatedDep,
    instrument_id: int,
    date: Annotated[dt.date | None, Query()] = None,
    count: Annotated[int, Query(ge=20, le=500)] = swing_service.MINI_CHART_BARS,
) -> Response:
    """The last ``count`` adjusted closes with their 10- and 20-day averages.

    Bounded at 500 because this is a *mini* chart: an unbounded count would let one request pull
    a decade of bars for every row on a page of forty candidates.
    """
    await scoped_sole_user_id(session, principal.user_id)
    symbol = (
        await session.execute(
            Instrument.__table__.select()
            .with_only_columns(Instrument.symbol)
            .where(Instrument.id == instrument_id)
        )
    ).scalar_one_or_none()
    if symbol is None:
        raise not_found("instrument", str(instrument_id))
    points = await swing_service.bars(session, instrument_id=instrument_id, end=date, count=count)
    return _json(
        SwingBarsOut(
            instrument_id=instrument_id,
            symbol=str(symbol),
            data=[
                SwingBarOut(
                    date=point.date,
                    close=point.close,
                    ma_fast=point.ma_fast,
                    ma_slow=point.ma_slow,
                )
                for point in points
            ],
        )
    )


@router.get("/market", response_model=SwingMarketOut, summary="Breadth, the gate and the rung")
async def get_market(
    session: SessionDep,
    principal: AuthenticatedDep,
    date_from: Annotated[dt.date | None, Query(alias="from")] = None,
    date_to: Annotated[dt.date | None, Query(alias="to")] = None,
) -> Response:
    """`05` §2's Market tab: the three breadth series, the gate band and the ladder over time."""
    user_id = await scoped_sole_user_id(session, principal.user_id)
    if date_from is not None and date_to is not None:
        if date_from > date_to:
            raise Problem(
                ProblemType.BAD_REQUEST,
                "`from` is after `to`.",
                errors=[{"field": "from", "message": "the range runs forwards"}],
            )
        if date_to - date_from > swing_service.MAX_MARKET_SPAN:
            raise Problem(
                ProblemType.BAD_REQUEST,
                f"that span is wider than {swing_service.MAX_MARKET_SPAN.days} days.",
                errors=[{"field": "from", "message": "ask for a narrower range"}],
            )
    rows = await swing_service.market_history(
        session, user_id=user_id, start=date_from, end=date_to
    )
    return _json(
        SwingMarketOut(
            data=[
                SwingMarketDayOut(
                    date=row.date,
                    constituent_count=row.constituent_count,
                    pct_up_strong_1m=row.pct_up_strong_1m,
                    pct_new_52w_high=row.pct_new_52w_high,
                    pct_above_ma_slow=row.pct_above_ma_slow,
                    index_slug=row.index_slug,
                    index_close=row.index_close,
                    index_ma_fast=row.index_ma_fast,
                    index_ma_slow=row.index_ma_slow,
                    gate=row.gate,
                    exposure_level=row.exposure_level,
                    max_open_positions=row.max_open_positions,
                    max_exposure_pct=row.max_exposure_pct,
                    new_entries_allowed=row.new_entries_allowed,
                    parabolic_count=row.parabolic_count,
                    drawdown_pct=row.drawdown_pct,
                    drawdown_locked=row.drawdown_locked,
                )
                for row in rows
            ]
        )
    )


@router.get("/signals", response_model=SwingSignalsOut, summary="What the monitor raised")
async def get_signals(
    session: SessionDep,
    principal: AuthenticatedDep,
    date: Annotated[dt.date | None, Query()] = None,
    instrument_id: Annotated[int | None, Query()] = None,
) -> Response:
    """`05` §2's "fired 09:23, 5-min range 412.30 to 418.90" under a watchlist row (SW14).

    A read of `sw_signal`, the append-only record of every verdict — `TRIGGERED`, `BELOW_PIVOT`,
    `LOCKED_UPPER_CIRCUIT` and the rest — for one session; the newest session when `date` is
    absent. It answers "what did the system see yesterday" and nothing else: a row here is a
    record, the plan line it may have become lives on the desk console, and there is no route on
    this surface that reaches it.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    as_of, rows = await swing_service.signals(
        session, user_id=user_id, on=date, instrument_id=instrument_id
    )
    return _json(
        SwingSignalsOut(
            session_date=as_of,
            data=[
                SwingSignalOut(
                    id=row.id,
                    watch_id=row.watch_id,
                    instrument_id=row.instrument_id,
                    symbol=row.symbol,
                    name=row.name,
                    setup=row.setup,
                    session_date=row.session_date,
                    raised_at=row.raised_at,
                    state=row.state,
                    or_window_minutes=row.or_window_minutes,
                    range_high=row.range_high,
                    range_low=row.range_low,
                    low_of_day=row.low_of_day,
                    last_price=row.last_price,
                    entry=row.entry,
                    stop=row.stop,
                    plan_line_id=row.plan_line_id,
                )
                for row in rows
            ],
        )
    )


@router.get("/sectors", response_model=SwingSectorsOut, summary="The sector strip")
async def get_sectors(
    session: SessionDep,
    principal: AuthenticatedDep,
    date: Annotated[dt.date | None, Query()] = None,
) -> Response:
    """Sector breadth for the day, with how many of the day's candidates sit in each.

    The breadth figures were computed by the detection job over its own liquid universe, not read
    from `market_health_daily` — which covers only the twelve size universes and has no sector
    row at all (DECISIONS-SW SW3.1).
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    as_of, rows = await swing_service.sectors(session, user_id=user_id, on=date)
    return _json(
        SwingSectorsOut(
            as_of=as_of,
            data=[
                SwingSectorOut(
                    slug=row.slug,
                    pct_above_ma_slow=row.pct_above_ma_slow,
                    members=row.members,
                    candidates=row.candidates,
                    hot=row.hot,
                )
                for row in rows
            ],
        )
    )


def _distance(trigger: Decimal | None, close: Decimal | None) -> Decimal | None:
    """How far ``close`` sits below ``trigger``, as a percentage of the trigger.

    Negative when the price is already above it — which is a real state on a watchlist read after
    the open, and rounding it to zero would hide the one row a person needs to look at first.
    """
    if trigger is None or close is None or trigger <= 0:
        return None
    return ((trigger - close) / trigger * 100).quantize(Decimal("0.01"))


def _watch_out(row: swing_watch.WatchRow) -> SwingWatchOut:
    return SwingWatchOut(
        id=row.id,
        instrument_id=row.instrument_id,
        symbol=row.symbol,
        name=row.name,
        setup=row.setup,
        source=row.source,
        added_on=row.added_on,
        expires_on=row.expires_on,
        trigger=row.trigger,
        stop_ref=row.stop_ref,
        distance_to_trigger_pct=_distance(row.trigger, row.last_close),
        last_close=row.last_close,
        note=row.note,
        catalyst=row.catalyst,
        state=row.state,
        score=row.score,
        adr_pct=row.adr_pct,
        focus=row.focus,
        reconfirmed_on=row.reconfirmed_on,
        earnings_date=row.earnings_date,
        catalyst_feed=_catalyst_out(row.catalyst_feed),
    )


@router.get("/watch", response_model=SwingWatchListOut, summary="The watchlist")
async def get_watch(
    session: SessionDep,
    principal: AuthenticatedDep,
    state: Annotated[str | None, Query()] = swing_watch.WATCHING,
) -> Response:
    """`05` §2's Watchlist tab. ``state=all`` includes what expired and what was dismissed.

    Expired rows are readable on purpose: the record of what was watched is the record of what
    was passed over, and a list that forgot them could not answer "what did I miss".
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    wanted = None if state in (None, "all") else state
    rows = await swing_watch.list_watch(session, user_id=user_id, state=wanted)
    return _json(SwingWatchListOut(data=[_watch_out(row) for row in rows]))


@router.post("/watch", response_model=SwingWatchOut, summary="Watch a name")
async def post_watch(
    session: SessionDep, principal: AuthenticatedDep, payload: SwingWatchIn
) -> Response:
    """Add a name by hand. It moves no money (`02` Track A) and it reaches no broker.

    A `MANUAL` row expires after ten sessions unless re-confirmed (A14, SW10.5): the person is
    watching for a reason the detectors cannot see, but a typed level nobody has looked at in
    two weeks is stale, and `PATCH … {"reconfirm": true}` is how they say they still want it.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    lookup = (
        Instrument.id == payload.instrument_id
        if payload.instrument_id is not None
        else Instrument.symbol == (payload.symbol or "").strip().upper()
    )
    instrument_id = (
        await session.execute(select(Instrument.id).where(lookup))
    ).scalar_one_or_none()
    if instrument_id is None:
        raise not_found("instrument", str(payload.instrument_id or payload.symbol))
    row = await swing_watch.add_manual(
        session,
        user_id=user_id,
        instrument_id=int(instrument_id),
        setup=payload.setup,
        on=dt.datetime.now(tz=dt.UTC).date(),
        trigger=payload.trigger,
        stop_ref=payload.stop_ref,
        note=payload.note,
        catalyst=payload.catalyst,
    )
    return _json(await _one_watch(session, user_id=user_id, watch_id=row.id))


@router.patch("/watch/{watch_id}", response_model=SwingWatchOut, summary="Annotate a watched name")
async def patch_watch(
    session: SessionDep, principal: AuthenticatedDep, watch_id: int, payload: SwingWatchPatch
) -> Response:
    """The note and the catalyst — `01` §3's "news check", which a person does and Baskfy cannot
    — and, with `reconfirm: true`, a MANUAL row's clock restarted (A14).

    Levels are deliberately not editable here. A trigger a person can revise after the fact is a
    trigger that can be revised to match a price they already paid. Re-confirming a detector's
    row is refused: its expiry is the detector's, and it is refreshed every evening.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    try:
        await swing_watch.annotate(
            session,
            user_id=user_id,
            watch_id=watch_id,
            note=payload.note,
            catalyst=payload.catalyst,
        )
        if payload.reconfirm:
            await swing_watch.reconfirm(
                session,
                user_id=user_id,
                watch_id=watch_id,
                on=dt.datetime.now(tz=dt.UTC).date(),
            )
    except swing_watch.WatchNotFound as exc:
        raise not_found("watchlist row", str(watch_id)) from exc
    except swing_watch.NotReconfirmable as exc:
        raise Problem(
            ProblemType.BAD_REQUEST,
            f"Only a WATCHING MANUAL row can be re-confirmed: {exc}.",
            errors=[{"field": "reconfirm", "message": str(exc)}],
        ) from exc
    return _json(await _one_watch(session, user_id=user_id, watch_id=watch_id))


@router.delete("/watch/{watch_id}", response_model=SwingWatchOut, summary="Stop watching a name")
async def delete_watch(session: SessionDep, principal: AuthenticatedDep, watch_id: int) -> Response:
    """A state change to `DISMISSED`, not a delete.

    "I looked and said no" and "it ran out of time" are different facts, and a watchlist that
    could not tell them apart could not answer the only question worth asking about it.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    try:
        await swing_watch.dismiss(session, user_id=user_id, watch_id=watch_id)
    except swing_watch.WatchNotFound as exc:
        raise not_found("watchlist row", str(watch_id)) from exc
    return _json(await _one_watch(session, user_id=user_id, watch_id=watch_id, state=None))


async def _one_watch(
    session: AsyncSession, *, user_id: int, watch_id: int, state: str | None = None
) -> SwingWatchOut:
    rows = await swing_watch.list_watch(session, user_id=user_id, state=state)
    for row in rows:
        if row.id == watch_id:
            return _watch_out(row)
    raise not_found("watchlist row", str(watch_id))


@router.get("/positions", response_model=SwingPositionsOut, summary="The book, and tomorrow's plan")
async def get_positions(session: SessionDep, principal: AuthenticatedDep) -> Response:
    """`05` §2's Positions tab, with the EOD plan preview beside it.

    One call, because the two are read together: what is open, and what the rules want done with
    it at tomorrow's open.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    rows = await swing_service.positions(session, user_id=user_id)
    plan = await swing_service.latest_plan(session, user_id=user_id)
    return _json(
        SwingPositionsOut(
            data=[
                SwingPositionOut(
                    id=row.id,
                    instrument_id=row.instrument_id,
                    symbol=row.symbol,
                    name=row.name,
                    setup=row.setup,
                    entry_date=row.entry_date,
                    entry_avg=row.entry_avg,
                    quantity_entered=row.quantity_entered,
                    quantity_open=row.quantity_open,
                    initial_stop=row.initial_stop,
                    stop=row.stop,
                    gtt_id=row.gtt_id,
                    naked=row.naked,
                    trail=row.trail,
                    partial_done=row.partial_done,
                    state=row.state,
                    closed_on=row.closed_on,
                    exit_avg=row.exit_avg,
                    close_reason=row.close_reason,
                    r_multiple=row.r_multiple,
                    pnl_inr=row.pnl_inr,
                    simulated=row.simulated,
                    last_close=row.last_close,
                )
                for row in rows
            ],
            plan=_plan_out(plan),
        )
    )


def _plan_out(plan: swing_service.PlanView | None) -> SwingPlanOut | None:
    if plan is None:
        return None
    return SwingPlanOut(
        plan_id=plan.plan_id,
        as_of=plan.as_of,
        source=plan.source,
        built_at=plan.built_at,
        expires_at=plan.expires_at,
        gate=plan.gate,
        exposure_level=plan.exposure_level,
        total_risk_inr=plan.total_risk_inr,
        total_new_exposure_inr=plan.total_new_exposure_inr,
        lines=[
            SwingPlanLineOut(
                id=line.id,
                kind=line.kind,
                symbol=line.symbol,
                name=line.name,
                setup=line.setup,
                quantity=line.quantity,
                trigger=line.trigger,
                stop=line.stop,
                risk_inr=line.risk_inr,
                position_value=line.position_value,
                trail=line.trail,
                note=line.note,
                state=line.state,
            )
            for line in plan.lines
        ],
        skips=[
            SwingPlanSkipOut(symbol=symbol, reason=reason, detail=detail)
            for symbol, reason, detail in plan.skips
        ],
    )


@router.get("/config", response_model=SwingConfigView, summary="The swing book's settings")
async def get_config(
    session: SessionDep, principal: AuthenticatedDep, settings: SettingsDep
) -> Response:
    """The settings, the server's ceilings, and the two fields only a job may write.

    The ceilings come back on every read so the form can render "max 1.0% — set by the server"
    rather than discovering the limit by being refused.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    row = await _config_or_404(session, user_id)
    return _json(
        to_view(
            row,
            ceilings=SwingCeilings.from_settings(settings),
            execution_enabled=settings.swing_execution_enabled,
        )
    )


@router.patch("/config", response_model=SwingConfigView, summary="Change a swing setting")
async def patch_config(
    session: SessionDep,
    principal: AuthenticatedDep,
    settings: SettingsDep,
    patch: SwingConfigPatch,
) -> Response:
    """The one write on this surface, and it moves no money.

    Seven numbers. No order path, no broker, no exposure rung: `SwingConfigPatch` forbids unknown
    fields, so `{"exposure_level": 3}` is refused rather than ignored — a caller who asked to
    climb the ladder is told the field does not exist here, instead of being answered `200` and
    believing they had.

    A value above its ceiling answers **422 `setting-above-ceiling`** naming the ceiling and the
    environment variable that sets it, and the refusal is atomic: a two-field patch that crosses a
    ceiling on the second field changes neither.

    ``now`` is not a query parameter: an audit row's clock is the server's (AUDIT 2.15).
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    await _config_or_404(session, user_id)
    row = await apply_patch(
        session,
        user_id=user_id,
        patch=patch,
        ceilings=SwingCeilings.from_settings(settings),
        changed_by=f"user:{user_id}",
        now=dt.datetime.now(tz=dt.UTC),
    )
    # No `commit()` here: `baskfy_api.db.get_session` owns the transaction and commits when the
    # handler returns. Committing inside would also break the contract tests, which hand the app
    # a transaction they intend to roll back.
    return _json(
        to_view(
            row,
            ceilings=SwingCeilings.from_settings(settings),
            execution_enabled=settings.swing_execution_enabled,
        )
    )


@router.get("/journal", response_model=SwingJournalOut, summary="The journal, in R")
async def get_journal(
    session: SessionDep, principal: AuthenticatedDep, settings: SettingsDep
) -> Response:
    """`05` §2's Journal tab: two cards, the ladder, the session count, and SW9's card.

    Real and simulated closes come back in **separate** cards and are never summed together
    (`04` §10). The ladder card's ``level`` is `sw_config.exposure_level` — the number the EOD
    job wrote back tonight — and ``reads`` says which book it read, so a paper rung is never
    mistaken for a real one. An empty book is a page of zeros, not a 404: the surface exists
    before the first trade does.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    view = await swing_journal.journal(
        session, user_id=user_id, execution_enabled=settings.swing_execution_enabled
    )
    return _json(
        SwingJournalOut(
            real=_journal_card(view.real),
            simulated=_journal_card(view.simulated),
            sessions=SwingSessionsOut(logged=view.sessions.logged, required=view.sessions.required),
            ladder=SwingLadderOut(
                level=view.ladder.level,
                gate=view.ladder.gate,
                max_open_positions=view.ladder.max_open_positions,
                max_exposure_pct=view.ladder.max_exposure_pct,
                new_entries_allowed=view.ladder.new_entries_allowed,
                last_r=list(view.ladder.last_r),
                reads=view.ladder.reads,
            ),
            backtest=view.backtest,
        )
    )


def _journal_card(card: swing_journal.JournalCard) -> SwingJournalCardOut:
    stats = card.stats
    return SwingJournalCardOut(
        stats=SwingJournalStatsOut(
            trades=stats.trades,
            win_rate_pct=stats.win_rate_pct,
            avg_win_r=stats.avg_win_r,
            avg_loss_r=stats.avg_loss_r,
            expectancy_r=stats.expectancy_r,
            profit_factor=stats.profit_factor,
            net_r=stats.net_r,
            largest_win_r=stats.largest_win_r,
            largest_loss_r=stats.largest_loss_r,
            current_loss_streak=stats.current_loss_streak,
        ),
        histogram=[SwingHistogramBarOut(bucket=b.bucket, count=b.count) for b in card.histogram],
        by_setup=[
            SwingSetupStatsOut(
                setup=row.setup, trades=row.trades, net_r=row.net_r, expectancy_r=row.expectancy_r
            )
            for row in card.by_setup
        ],
        by_month=[
            SwingMonthStatsOut(month=row.month, trades=row.trades, net_r=row.net_r)
            for row in card.by_month
        ],
        trades=[
            SwingJournalTradeOut(
                symbol=row.symbol,
                setup=row.setup,
                entry_date=row.entry_date,
                exit_date=row.exit_date,
                entry=row.entry,
                initial_stop=row.initial_stop,
                exit_avg=row.exit_avg,
                quantity=row.quantity,
                r_multiple=row.r_multiple,
                pnl_inr=row.pnl_inr,
                close_reason=row.close_reason,
            )
            for row in card.trades
        ],
    )


async def _config_or_404(session: AsyncSession, user_id: int) -> SwConfig:
    try:
        return await read_config(session, user_id)
    except SwingConfigNotSeeded as exc:
        raise Problem(
            ProblemType.PIPELINE_DEGRADED,
            "The swing book is not set up on this deployment; run `make seed`.",
        ) from exc
