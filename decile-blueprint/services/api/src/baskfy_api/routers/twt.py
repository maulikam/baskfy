"""``/twt/*`` — the three-weeks-tight sleeve's one write, and it moves no money (TW12).

    POST /twt/scan              "Scan now": queue a detection run
    GET  /twt/scan/{run_id}     that run's state, its session and its funnel

WHY THIS ROUTER EXISTS, AND WHY IT IS TWO ROUTES AND NOT TWENTY
---------------------------------------------------------------
The ``/twt`` hub in ``apps/web`` was built ahead of its data (TW8) and reads through
``src/lib/twt/fetch.ts``, which asks for ``/twt/today`` and ``/twt/backtest`` — routes that do not
exist yet and answer ``null`` by design, so the page renders its empty state. Serving those is
somebody else's module. **This router serves the one thing "Scan now" needs and nothing else**,
because a router that grows read surfaces on the way to a button is a router nobody reviewed.

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
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

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
