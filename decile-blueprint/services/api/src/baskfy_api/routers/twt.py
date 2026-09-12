"""``/twt/*`` — the three-weeks-tight sleeve's read surface, and its one money-free write.

    GET  /twt/today?date        the gate, the names in the state, the open book, the counter
    GET  /twt/backtest          the settled run per source, and `01` §8's terms for reading it
    POST /twt/scan              "Scan now": queue a detection run
    GET  /twt/scan/{run_id}     that run's state, its session and its funnel

WHY ``GET /twt/today`` WAS ADDED ON 12 Sep 2026
------------------------------------------------
This router was two routes for a reason worth keeping: "a router that grows read surfaces on the
way to a button is a router nobody reviewed." What that reasoning missed is that
``apps/web/src/lib/twt/fetch.ts`` has been asking for ``/twt/today`` since TW8, and
``readOrNull`` turns its 404 into ``null`` — so the hub said **"Nothing has been read for this
strategy yet"** no matter what the database held.

It held plenty. On 12 Sep 2026 Maulik pressed "Scan now" twice; both runs finished ``DONE`` and
the detector wrote a breadth row, two ``SIGNAL`` rows and 58 state rows for the published session
of 2026-09-11. A button whose result is unreadable is a button that did not work, and the empty
state was reporting a reader that had never been built as though it were a writer that had never
run.

WHY ``GET /twt/backtest`` FOLLOWED AN HOUR LATER (TW14)
-------------------------------------------------------
This paragraph used to end: *"``/twt/backtest`` is still unserved and still answers ``null``
honestly: that page's rows do not exist yet, and this router does not grow a surface on
speculation twice."* The first clause is still a fact — ``tw_backtest_run`` holds **0 rows on the
box** — and the second does not follow from it.

The Backtest tab had the same missing reader, and its empty state being *true* made it the
**more** dangerous of the two rather than the less. The hub's version was loud: 58 names in the
database against a page saying nothing had been read, found the same afternoon. This one is
silent. "No completed run has been recorded yet" is true today by accident, and it would have
stayed on the screen word for word the first evening TW9's job wrote a settled result — a run at
22.17 % CAGR sitting in a table with nobody able to see it and no symptom to notice. A reader that
does not exist and a writer that has not run are the same silence; only one of them is a bug, and
the page cannot tell you which. (DECISIONS-TW **TW14.1**. The route is a GET over one table, it
computes nothing, and on the box it answers an empty list — saying, in ``reason``, which of the
three absences that is.)

READ-ONLY EXCEPT FOR ONE ROUTE, AND THAT ROUTE MOVES NO MONEY
-------------------------------------------------------------
``docs/twt/02`` Track C §4 is blunt: **"`apps/web` gets no route under `/twt` that can reach the
gateway."** There is no ``POST /twt/execute`` here and there is not going to be one — confirming a
line is the desk's, on ``kite-momentum-rebalancer``, behind Maulik's own login (`05` §2). This
router does not import the execution package, names no broker, and has one POST: a scan, which
asks the worker to run the detectors. The detectors read closed bars and write ``tw_state_daily``,
``tw_signal_daily`` and ``tw_breadth_daily``; they cannot size, place or cancel anything. (The
execution package's name is spelt out nowhere in this file on purpose: ``test_twt_readonly.py``
asserts its absence over the source, and a mention in prose would defeat the check.)

Track A said the hub was read-only "except notes and dismissals, which change no money". A scan
changes no money either, by the same test, and the swing book's Track A admitted its own scan on
exactly that reading when SW15 built it. The widening is **recorded** rather than assumed:
DECISIONS-TW **TW12.3**, with the reversal in it.

THERE IS NO PROVISIONAL SCAN HERE
---------------------------------
The swing book's run row carries a ``provisional`` flag because its setups can be read off a bar
still being formed. This strategy's cannot: ``04`` §2 measures three *weekly* ranges that have
closed. So a scan asks for the latest **published** session to be detected again, and the answer
never claims to be about today (DECISIONS-TW **TW12.2**).

WHOSE BOOK IT IS
----------------
The sole tenant's, resolved through ``scoped_sole_user_id``, which **refuses** a principal who is
not the sole tenant rather than serving them somebody else's runs.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from baskfy_api import twt as twt_service
from baskfy_api import twt_scan
from baskfy_api.auth import AuthenticatedDep, settings_for
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.db import SessionDep
from baskfy_api.problems import not_found
from baskfy_api.settings import Settings
from baskfy_core.screener import canonical_json

router = APIRouter(prefix="/twt", tags=["twt"])

JSON_MEDIA_TYPE: Final = "application/json"

#: Where a scan requested through the web app says it came from. ``tw_scan_run.source`` admits
#: ``desk``, ``web`` and ``cli``; the desk writes ``desk``, and a row that cannot say which button
#: was pressed is a row that cannot be audited afterwards.
SOURCE_WEB: Final = "web"


def _settings(request: Request) -> Settings:
    """The settings the app was constructed with, not a freshly read singleton — the reason
    ``baskfy_api.auth.settings_for`` exists."""
    return settings_for(request)


SettingsDep = Annotated[Settings, Depends(_settings)]


class TwtGateOut(BaseModel):
    """``03`` §4's reading for one session, and the funnel behind it.

    Every funnel count is nullable, and a null is served rather than a zero: the web app's
    ``funnelSteps`` drops a missing step, because "no reading was taken" and "none of the market"
    render identically and only one of them is a statement about the market.
    """

    date: dt.date | None
    gate: str | None
    pct_above_dma: Decimal | None
    #: A PERCENTAGE. Served, never hard-coded on the page — `05` §1.1 prints it beside the reading.
    threshold_pct: Decimal
    universe_count: int | None
    with_bar_count: int | None
    measured_count: int | None
    above_count: int | None
    thin_session: bool


class TwtTightNameOut(BaseModel):
    """One name in the state, with its entry event where it had one.

    `failed_filters` carries `["TURNOVER"]` on a `SCAN_ONLY` reject and is empty otherwise. The
    rejects are served rather than filtered away (`05` §1.2): a screen that hides what it passed
    over cannot be audited by the person whose money it is.
    """

    instrument_id: int
    symbol: str
    name: str
    close_raw: Decimal
    week_close_0: Decimal
    week_close_1: Decimal
    week_close_2: Decimal
    week_range_pct: Decimal
    month_low_ratio: Decimal
    sessions_in_state: int
    signal_state: str | None
    failed_filters: list[str]
    turnover_avg_20: int | None
    locked_upper_circuit: bool


class TwtPositionOut(BaseModel):
    """One `OPEN` row of `03` §5, marked at the **last published close**.

    `last_price` is that mark, not a quote: this service has no quote path (root `CLAUDE.md`,
    "Which date the product shows"), and null means nothing has printed since the fill — a
    reason, not a zero.
    """

    id: int
    instrument_id: int
    symbol: str
    name: str
    entry_date: dt.date
    entry_avg: Decimal
    quantity_open: int
    high_since: Decimal
    high_since_date: dt.date | None
    gtt_trigger: Decimal | None
    gtt_id: str | None
    next_trigger: Decimal | None
    next_trigger_for: dt.date | None
    last_price: Decimal | None
    unrealised_inr: Decimal | None
    #: A FRACTION. `0.1870` is 18.70%, scaled once in the web app's `@/lib/twt/view`.
    unrealised_fraction: Decimal | None
    hold_sessions: int | None
    half_size: bool
    simulated: bool


class TwtHalfSizeOut(BaseModel):
    """`05` §1.4 — the first-live discipline as a counter a reader can see without a settings
    page. `execution_enabled` is false for the whole of this run, and the counter says so rather
    than counting down in the dark."""

    entries_left: int
    entries_total: int
    execution_enabled: bool


class TwtLastScanOut(BaseModel):
    """The newest "Scan now" run, inlined so the hub costs one request rather than two.

    `id` and not `run_id`: this is the shape `/swing` serves and the shape `fetch.ts` reads, and
    a sleeve that invented its own spelling would be a second thing to learn for no reason. The
    poll route `GET /twt/scan/{run_id}` keeps its own name — it answers about *a* run, this one
    names *the* run.
    """

    id: int
    status: str
    requested_at: dt.datetime
    finished_at: dt.datetime | None
    error: str | None
    #: Which published session the run read. Null until the worker has decided which that is.
    session_date: dt.date | None = None
    #: How many signals the run produced — what turns "it finished" into "it finished and found
    #: three names". ``None`` means the run did not say; ``0`` means it looked and found none,
    #: which for this sleeve is the **ordinary** answer (about eighteen entries a year) and must
    #: not be rendered as a fault. The page tells the two apart; a single field could not.
    found: int | None = None


class TwtTodayOut(BaseModel):
    """`05` §1's hub, in one call.

    `as_of` null means the detector has never written a session — **not** that the session was
    quiet, and **not** that today's bars are missing. It is the last completed trading session by
    construction (`04` §11.1, root `CLAUDE.md`'s two clocks), so on a Saturday it reads Friday.
    """

    as_of: dt.date | None
    gate: TwtGateOut
    tight: list[TwtTightNameOut]
    positions: list[TwtPositionOut]
    half_size: TwtHalfSizeOut
    last_scan: TwtLastScanOut | None


class TwtBacktestCaveatOut(BaseModel):
    """One paragraph of `01` §8, with the id `05` §3's own panel gives it."""

    id: str
    text: str


class TwtBacktestRunOut(BaseModel):
    """One settled `tw_backtest_run` row, exactly as `05` §3's card reads it.

    `params`, `stats` and `drift` are the JSONB the run stored, passed through rather than
    re-derived: every figure in them is already a decimal **string** written by the job (house
    rule 9, all the way to the database), and a route that parsed and re-rendered them would
    round a second time. A key a run could not produce is absent rather than null (TW9.5), and
    stays absent here.
    """

    id: int
    source: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    params: dict[str, object]
    stats: dict[str, object] | None
    drift: dict[str, object] | None
    error: str | None


class TwtBacktestOut(BaseModel):
    """`05` §3's page: at most one settled run per source, under `01` §8's conditions.

    `reason` is null whenever `runs` is non-empty; when `runs` is empty it names **which** of the
    three absences this is — never asked for, still running, or finished without a result — so a
    reader is told why there is no number instead of being handed one sentence that is right two
    times in three.
    """

    runs: list[TwtBacktestRunOut]
    caveats: list[TwtBacktestCaveatOut]
    reason: str | None


class TwtScanRunOut(BaseModel):
    """One "Scan now" run (TW12). ``status`` walks QUEUED -> RUNNING -> DONE | FAILED.

    ``session_date`` is null until the worker has decided which published session it is
    detecting, because the caller asks for "the latest" and only the worker knows which that is.
    ``detail`` carries the funnel on DONE; ``error`` the reason on FAILED.

    **There is no ``provisional`` field**, and its absence is the design rather than an omission:
    see the module docstring and DECISIONS-TW TW12.2.
    """

    run_id: int
    status: str
    requested_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    session_date: dt.date | None
    detail: dict[str, object] | None
    error: str | None
    #: How many signals the run produced, lifted out of ``detail`` so a poller reads the number
    #: the same way the hub's inlined run does. ``None`` is "the run did not say"; ``0`` is "it
    #: looked and found none".
    found: int | None = None


class TwtScanQueuedOut(BaseModel):
    """What ``POST /twt/scan`` answers, with a 202: the run to poll."""

    run_id: int
    status: str
    requested_at: dt.datetime


def _json(model: BaseModel, *, status_code: int = 200) -> Response:
    """The canonical encoder, for the reason ``routers/swing`` and ``routers/vbt`` use one:
    Pydantic renders ``Decimal`` through ``float``, and house rule 8 makes the stored precision
    the contract."""
    return Response(
        content=canonical_json(model.model_dump(mode="python", by_alias=True)),
        media_type=JSON_MEDIA_TYPE,
        status_code=status_code,
    )


def _json_decimals_as_strings(model: BaseModel) -> Response:
    """The hub's encoder: Pydantic's own JSON, in which a ``Decimal`` is a **quoted** string.

    WHY THIS ROUTE DOES NOT USE ``_json``
    -------------------------------------
    ``_json`` emits ``baskfy_core.screener.canonical_json``, whose decimals are unquoted tokens —
    ``149.6000`` rather than ``"149.60"``. That keeps every digit *on the wire* and is exactly
    right for the screener, whose determinism guarantee is about bytes. It is the wrong encoder
    for this payload for a reason that only shows up one hop later: the page reads it with
    ``JSON.parse``, and ``JSON.parse("149.6000")`` is the double ``149.6``. The digits survive the
    network and die in the browser.

    ``apps/web/src/lib/twt/numbers.ts`` is built on that not happening. Every figure on ``/twt``
    is derived with ``BigInt`` arithmetic over decimal **strings** — "money is a decimal string end
    to end, never a ``number`` in between" — because a stop on a line held for 600 sessions is a
    specific number on a specific evening and a float's idea of ``.5`` is not it (house rule 9).

    It is also what this service's **own OpenAPI document already declares**: Pydantic types a
    ``Decimal`` field as ``{"type": "string", "pattern": ...}``, which is what
    ``packages/api-client/openapi.json`` says about every field below and what
    ``routers/portfolio_overview.py`` — the payload ``fetch.ts`` names as its precedent — actually
    sends. So this is the route matching the contract, not inventing one.

    The scan routes keep ``_json``: their payload carries no ``Decimal`` at all, and changing an
    encoder under a shipped route to fix a different route's bug is how one fix becomes two.
    """
    return Response(content=model.model_dump_json(), media_type=JSON_MEDIA_TYPE)


def _run_out(view: twt_scan.ScanRunView) -> TwtScanRunOut:
    return TwtScanRunOut(
        run_id=view.run_id,
        status=view.status,
        requested_at=view.requested_at,
        started_at=view.started_at,
        finished_at=view.finished_at,
        session_date=view.session_date,
        detail=view.detail,
        error=view.error,
        found=view.found,
    )


@router.get("/today", response_model=TwtTodayOut, summary="The session's gate, names and book")
async def get_twt_today(
    session: SessionDep,
    principal: AuthenticatedDep,
    settings: SettingsDep,
    date: Annotated[
        dt.date | None, Query(description="a detected session; default the latest")
    ] = None,
) -> Response:
    """`05` §1's hub: may it buy at all, which names are quiet enough, and what is already open.

    **The default session is the latest one the detector wrote**, not today's date and not the
    latest `pipeline_run`. That is the whole point of the route: the writer detects "the latest
    published session", so a reader resolving the day any other way would ask for a session the
    writer has not detected — which is precisely how a scan that succeeded came to render an
    empty page on 12 Sep 2026. Asking the writer's own newest row cannot drift ahead of it.

    A `date` with no reading answers an empty view stamped with the date that was asked for, so
    the page can name the session it found nothing for rather than claiming nothing exists.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    view = await twt_service.today(
        session,
        user_id=user_id,
        day=date,
        execution_enabled=settings.twt_execution_enabled,
    )
    return _json_decimals_as_strings(
        TwtTodayOut(
            as_of=view.as_of,
            gate=TwtGateOut(
                date=view.gate.date,
                gate=view.gate.gate,
                pct_above_dma=view.gate.pct_above_dma,
                threshold_pct=view.gate.threshold_pct,
                universe_count=view.gate.universe_count,
                with_bar_count=view.gate.with_bar_count,
                measured_count=view.gate.measured_count,
                above_count=view.gate.above_count,
                thin_session=view.gate.thin_session,
            ),
            tight=[
                TwtTightNameOut(
                    instrument_id=row.instrument_id,
                    symbol=row.symbol,
                    name=row.name,
                    close_raw=row.close_raw,
                    week_close_0=row.week_close_0,
                    week_close_1=row.week_close_1,
                    week_close_2=row.week_close_2,
                    week_range_pct=row.week_range_pct,
                    month_low_ratio=row.month_low_ratio,
                    sessions_in_state=row.sessions_in_state,
                    signal_state=row.signal_state,
                    failed_filters=list(row.failed_filters),
                    turnover_avg_20=row.turnover_avg_20,
                    locked_upper_circuit=row.locked_upper_circuit,
                )
                for row in view.tight
            ],
            positions=[
                TwtPositionOut(
                    id=row.id,
                    instrument_id=row.instrument_id,
                    symbol=row.symbol,
                    name=row.name,
                    entry_date=row.entry_date,
                    entry_avg=row.entry_avg,
                    quantity_open=row.quantity_open,
                    high_since=row.high_since,
                    high_since_date=row.high_since_date,
                    gtt_trigger=row.gtt_trigger,
                    gtt_id=row.gtt_id,
                    next_trigger=row.next_trigger,
                    next_trigger_for=row.next_trigger_for,
                    last_price=row.last_price,
                    unrealised_inr=row.unrealised_inr,
                    unrealised_fraction=row.unrealised_fraction,
                    hold_sessions=row.hold_sessions,
                    half_size=row.half_size,
                    simulated=row.simulated,
                )
                for row in view.positions
            ],
            half_size=TwtHalfSizeOut(
                entries_left=view.half_size.entries_left,
                entries_total=view.half_size.entries_total,
                execution_enabled=view.half_size.execution_enabled,
            ),
            last_scan=(
                None
                if view.last_scan is None
                else TwtLastScanOut(
                    id=view.last_scan.run_id,
                    status=view.last_scan.status,
                    requested_at=view.last_scan.requested_at,
                    finished_at=view.last_scan.finished_at,
                    error=view.last_scan.error,
                    session_date=view.last_scan.session_date,
                    found=view.last_scan.found,
                )
            ),
        )
    )


@router.get("/backtest", response_model=TwtBacktestOut, summary="What the rule did, and its terms")
async def get_twt_backtest(session: SessionDep, principal: AuthenticatedDep) -> Response:
    """`05` §3's page — the twin of `/twt/today`, absent for the same reason and fixed the same way.

    `apps/web/src/lib/twt/fetch.ts` has asked for this path since TW8 and `readOrNull` turned its
    404 into `null`, so the Backtest tab rendered its empty state whatever `tw_backtest_run` held.
    That was honest while the table was empty and would have gone on looking honest after TW9's
    job ran, which is the failure: an absent reader and an absent writer are indistinguishable to
    the person the page is for.

    **On the box the table still holds 0 rows**, so this route answers `runs: []` there — and says
    in `reason` which of the three absences that is. Nothing about this route makes a backtest
    run; `make twt-backtest` and the worker's task do that.

    **The latest finished run per source, and only that.** `03` §9 makes the table append-only and
    every settled row carries a full nine-year equity curve; serving the history would grow
    without bound to answer a question about two numbers. Finished *and* carrying stats, so a run
    in flight or a failed re-run never displaces the last good number.

    **`01` §8 rides along.** House rule 9 — disclaimers are components, not footers — and `05` §3
    restates it here by name. The page renders them from its own panel; what this field adds is
    that the numbers cannot leave this service without the conditions attached, for any reader.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    view = await twt_service.backtest(session, user_id=user_id)
    return _json_decimals_as_strings(
        TwtBacktestOut(
            runs=[
                TwtBacktestRunOut(
                    id=run.id,
                    source=run.source,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    params=run.params,
                    stats=run.stats,
                    drift=run.drift,
                    error=run.error,
                )
                for run in view.runs
            ],
            caveats=[
                TwtBacktestCaveatOut(id=caveat.id, text=caveat.text) for caveat in view.caveats
            ],
            reason=view.reason,
        )
    )


@router.post(
    "/scan",
    response_model=TwtScanQueuedOut,
    status_code=202,
    summary="Scan now: queue a detection run",
)
async def post_twt_scan(
    request: Request, session: SessionDep, principal: AuthenticatedDep, settings: SettingsDep
) -> Response:
    """TW12. One row in ``tw_scan_run`` and one task name published; the worker does the rest.

    The worker detects the **latest published session** — the pipeline's date, not the exchange
    calendar's, so a press at two in the afternoon re-detects the session the bars actually know
    about rather than a today whose bars do not exist yet. It calls the nightly's own detector
    with ``force=True``, which is what makes the button useful: the nightly skips a session that
    already has a breadth row, and that is exactly the session somebody presses this about.

    A scan moves no money (`02` Track A, DECISIONS-TW TW12.3) and this route reaches no broker —
    ``baskfy_api.twt_scan`` names none. At most one in flight per user (409) and one request a
    minute (429, ``Retry-After``); both answered from the table rather than from a cache, so the
    rule holds on a box with no Redis.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    queue = getattr(request.app.state, "task_queue", None)
    row = await twt_scan.request_scan(
        session,
        user_id=user_id,
        now=dt.datetime.now(tz=dt.UTC),
        min_interval=dt.timedelta(seconds=settings.twt_scan_min_interval_seconds),
        stale_after=dt.timedelta(seconds=settings.twt_scan_stale_after_seconds),
        source=SOURCE_WEB,
        queue=queue,
    )
    return _json(
        TwtScanQueuedOut(run_id=int(row.id), status=str(row.status), requested_at=row.requested_at),
        status_code=202,
    )


@router.get("/scan/{run_id}", response_model=TwtScanRunOut, summary="One scan's state")
async def get_twt_scan(session: SessionDep, principal: AuthenticatedDep, run_id: int) -> Response:
    """That run, or a 404. A run this tenant did not request is a 404 rather than somebody
    else's row — the same scoping the rest of the sleeve's surfaces use."""
    user_id = await scoped_sole_user_id(session, principal.user_id)
    view = await twt_scan.scan_run(session, user_id=user_id, run_id=run_id)
    if view is None:
        raise not_found("scan", str(run_id))
    return _json(_run_out(view))
