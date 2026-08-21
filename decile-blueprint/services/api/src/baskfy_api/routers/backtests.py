"""``/backtests/*`` — docs/07 §Backtests (Prompt 15 deliverable 5).

docs/07 lists five routes:

```
POST   /backtests            { config }        → 202 { public_id, status:"queued" }
GET    /backtests/{id}                          → status + metrics + equity curve
GET    /backtests/{id}/trades?cursor=           → paginated fills
GET    /backtests/{id}/export                   → CSV/Parquet signed URL
DELETE /backtests/{id}
```

Four are added, each recorded in ``docs/DECISIONS.md`` §15:

* ``GET /backtests`` — docs/08 §Routes names a ``/backtests`` **page**, and a page listing runs
  needs an endpoint listing runs.
* ``GET /backtests/{id}/holdings`` — docs/08 §Backtests asks the results page for "per-period
  holdings", and docs/10 §Artefacts stores them in R2. Paginating them out of the artefact is
  the read side of that.
* ``GET /backtests/{id}/events`` — PROMPTS.md Prompt 15 §4 requires "progress events streamed to
  the client over SSE". docs/07's list predates that sentence and has no route for it.
* ``GET /backtests/{id}/download/{artefact}`` — what the signed URL from ``/export`` redeems
  against. See ``baskfy_api.backtests`` for why the link is signed by this service.

Entitlement
-----------
``Feature.BACKTESTS``, on every route. docs/07 §Entitlements lists it, Prompt 13 §6 gates it, and
CLAUDE.md has carried "Backtests are now a paid entitlement, and nothing enforces it — wire
``entitlements.require(Feature.BACKTESTS)`` into ``/backtests`` when it lands" since Prompt 13.
This is where it lands.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Mapping, Sequence
from typing import Annotated, Final, Protocol

import anyio
from fastapi import APIRouter, Header, Path, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import backtests as service
from baskfy_api import idempotency, invoices
from baskfy_api.auth import AuthenticatedDep, Principal, settings_for
from baskfy_api.db import SessionDep
from baskfy_api.entitlements import EntitlementsDep, Feature
from baskfy_api.problems import Problem, ProblemType, not_found, rate_limited, stale_data_version
from baskfy_api.schemas import (
    DEFAULT_PAGE_SIZE,
    BacktestAcceptedOut,
    BacktestCreate,
    BacktestHoldingOut,
    BacktestListOut,
    BacktestOut,
    BacktestSummaryOut,
    DrawdownPointOut,
    EquityPointOut,
    ExportLinkOut,
    FragilityRunOut,
    HoldingPage,
    Limit,
    MonthlyReturnOut,
    TradeOut,
    TradePage,
)
from baskfy_api.screener import current_data_version
from baskfy_api.settings import API_PREFIX, Settings
from baskfy_core.backtest import BacktestConfig
from baskfy_core.models import Backtest, Screen
from baskfy_core.models.base import JsonObject
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_providers.archive import RawArchive
from baskfy_providers.errors import ArchiveError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/backtests", tags=["backtests"])

#: docs/10 §"What to show the user (honesty features)": "A prominent 'Past backtest results do not
#: predict future results' line — the reference product says this and it is both ethically right
#: and legally necessary." Served by the API so the page cannot forget it.
DISCLAIMER: Final = "Past backtest results do not predict future results."

#: The name of the task the worker binds in ``baskfy_worker.tasks.celery_tasks``.
BACKTEST_TASK: Final = "baskfy.backtest.run"

#: How long the SSE endpoint waits for a frame before sending a comment to keep the connection
#: alive. Proxies commonly close an idle stream at 60 seconds.
SSE_HEARTBEAT_SECONDS: Final = 15.0

#: A backstop on the stream: a run that has not finished in this long has a bigger problem than a
#: disconnected browser, and the client reconnects on its own.
SSE_MAX_SECONDS: Final = 30 * 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cache(request: Request) -> Redis | None:
    client = getattr(request.app.state, "cache", None)
    return client if isinstance(client, Redis) else None


def _archive(request: Request) -> RawArchive:
    """The process-wide archive, built once by ``create_app`` (docs/02 §"Object storage").

    The same store the invoices use: one abstraction over R2-or-a-directory, not two.
    """
    archive = getattr(request.app.state, "invoice_archive", None)
    if archive is None:  # pragma: no cover - lifespan always sets it
        archive = invoices.build_invoice_archive(settings_for(request))
        request.app.state.invoice_archive = archive
    resolved: RawArchive = archive
    return resolved


def _queue(request: Request) -> object | None:
    return getattr(request.app.state, "task_queue", None)


async def _load(session: AsyncSession, public_id: str, principal: Principal) -> Backtest:
    """Yours, or a 404. A 403 would confirm the id exists — the rule screens already follow."""
    row = (
        await session.execute(select(Backtest).where(Backtest.public_id == public_id))
    ).scalar_one_or_none()
    if row is None or row.user_id != principal.user_id:
        raise not_found("backtest", public_id)
    return row


async def _screen_labels(
    session: AsyncSession, rows: Sequence[Backtest]
) -> dict[int, tuple[str, str]]:
    ids = sorted({row.screen_id for row in rows if row.screen_id is not None})
    if not ids:
        return {}
    found = (
        await session.execute(
            select(Screen.id, Screen.public_id, Screen.name).where(Screen.id.in_(ids))
        )
    ).all()
    return {int(row[0]): (str(row[1]), str(row[2])) for row in found}


def _metric(metrics: object, key: str) -> float | None:
    if not isinstance(metrics, Mapping):
        return None
    value = metrics.get(key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _config_of(row: Backtest) -> BacktestConfig:
    return BacktestConfig.model_validate(row.config)


def _summary(row: Backtest, labels: Mapping[int, tuple[str, str]]) -> BacktestSummaryOut:
    config = _config_of(row)
    label = labels.get(row.screen_id) if row.screen_id is not None else None
    return BacktestSummaryOut(
        public_id=row.public_id,
        status=_status(row),
        created_at=row.created_at,
        finished_at=row.finished_at,
        screen_public_id=label[0] if label else config.screen_public_id,
        screen_name=label[1] if label else None,
        start=config.start,
        end=config.end,
        top_n=config.selection.top_n,
        weighting=config.weighting.value,
        rebalance_frequency=config.rebalance.frequency.value,
        cagr=_metric(row.metrics, "cagr"),
        total_return=_metric(row.metrics, "total_return"),
        max_drawdown=_metric(row.metrics, "max_drawdown"),
        error=row.error,
    )


def _status(row: Backtest) -> str:
    """Narrow the column to the four docs/04 allows. A CHECK constraint guarantees it."""
    if row.status not in {"queued", "running", "done", "failed"}:  # pragma: no cover - CHECK
        raise Problem(ProblemType.INTERNAL_ERROR, f"backtest {row.public_id} has a bad status")
    return row.status


def _sub(metrics: object, key: str) -> list[JsonObject]:
    if not isinstance(metrics, Mapping):
        return []
    value = metrics.get(key)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(metrics: object, key: str) -> list[str]:
    if not isinstance(metrics, Mapping):
        return []
    value = metrics.get(key)
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _curve(payload: object, key: str) -> list[JsonObject]:
    if not isinstance(payload, Mapping):
        return []
    value = payload.get(key)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _detail(row: Backtest) -> BacktestOut:
    metrics = row.metrics
    label_hash = metrics.get("metrics_hash") if isinstance(metrics, Mapping) else None
    version = metrics.get("data_version") if isinstance(metrics, Mapping) else None
    return BacktestOut(
        public_id=row.public_id,
        status=_status(row),
        created_at=row.created_at,
        finished_at=row.finished_at,
        config=_config_of(row),
        screen_public_id=_config_of(row).screen_public_id,
        error=row.error,
        data_version=int(version) if isinstance(version, int) else None,
        metrics_hash=str(label_hash) if isinstance(label_hash, str) else None,
        metrics=_json_value(metrics),
        equity_curve=[
            EquityPointOut.model_validate(point) for point in _curve(row.equity_curve, "points")
        ],
        drawdown=[
            DrawdownPointOut.model_validate(point) for point in _curve(row.equity_curve, "drawdown")
        ],
        monthly_returns=[
            MonthlyReturnOut.model_validate(point) for point in _sub(metrics, "monthly_returns")
        ],
        fragility=[FragilityRunOut.model_validate(run) for run in _sub(metrics, "fragility")],
        assumptions=_strings(metrics, "assumptions"),
        disclaimer=DISCLAIMER,
    )


def _json_value(metrics: object) -> JsonObject | None:
    """The stored metric block, minus the members promoted to their own response fields."""
    if not isinstance(metrics, Mapping):
        return None
    promoted = {"fragility", "assumptions", "monthly_returns"}
    return {key: value for key, value in metrics.items() if key not in promoted}


# ---------------------------------------------------------------------------
# POST /backtests
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=BacktestAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a backtest",
)
async def create_backtest(  # noqa: PLR0913, PLR0917 - FastAPI injects one parameter per dependency
    request: Request,
    body: BacktestCreate,
    session: SessionDep,
    principal: AuthenticatedDep,
    entitlements: EntitlementsDep,
    idempotency_key: Annotated[str | None, Header(alias=idempotency.HEADER)] = None,
) -> BacktestAcceptedOut:
    """docs/07: `POST /backtests { config } → 202 { public_id, status:"queued" }`."""
    entitlements.require(Feature.BACKTESTS)
    user_id = principal.require_user()
    config = body.config

    current = await current_data_version(session)
    if body.data_version is not None and body.data_version != current:
        raise stale_data_version(body.data_version, current)

    scope = f"backtest:create:{user_id}"
    replayed = await idempotency.replay(_cache(request), scope, idempotency_key)
    if replayed is not None:
        existing = (
            await session.execute(select(Backtest).where(Backtest.public_id == replayed))
        ).scalar_one_or_none()
        if existing is not None and existing.user_id == user_id:
            return _accepted(existing)

    screen = await _resolve_screen(session, config, principal)
    # The ₹0 tier's universe restriction (Prompt 13 §5) applies to the definition the backtest
    # will run, whether that came from a saved screen or was posted inline. Checking only the
    # saved-screen branch would leave the inline one as a way around the restriction.
    definition = screen.definition if screen is not None else config.screen_definition
    universe = _definition_index(definition)
    if universe is not None:
        entitlements.require_universe(universe)

    try:
        await service.capacity_check(
            session,
            user_id,
            per_user=settings_for(request).backtest_user_concurrency,
            global_limit=settings_for(request).backtest_global_concurrency,
        )
    except service.ConcurrencyExceeded as exc:
        # docs/07's catalogue has no "busy" type; 429 with `Retry-After` is the row that means
        # "come back later", and the detail names which cap was hit. `docs/DECISIONS.md` §15.
        problem = rate_limited(exc.retry_after_seconds, exc.limit)
        problem.detail = (
            f"You already have a backtest running. The {exc.scope} limit is {exc.limit}; "
            "wait for it to finish, or cancel it."
            if exc.scope == "per-user"
            else f"The service is running its maximum of {exc.limit} backtests. Try again shortly."
        )
        raise problem from exc

    row = Backtest(
        public_id=service.new_public_id(),
        user_id=user_id,
        screen_id=screen.id if screen is not None else None,
        config=json.loads(config.model_dump_json()),
        status="queued",
    )
    session.add(row)
    await session.flush()
    await idempotency.remember(_cache(request), scope, idempotency_key, row.public_id)
    _dispatch(request, row.public_id, fragility=body.fragility)
    return _accepted(row)


def _accepted(row: Backtest) -> BacktestAcceptedOut:
    return BacktestAcceptedOut(
        public_id=row.public_id,
        status=_status(row),
        created_at=row.created_at,
        events_url=f"{API_PREFIX}/backtests/{row.public_id}/events",
    )


def _definition_index(definition: object) -> str | None:
    return (
        str(definition.get("index"))
        if isinstance(definition, Mapping) and definition.get("index") is not None
        else None
    )


async def _resolve_screen(
    session: AsyncSession, config: BacktestConfig, principal: Principal
) -> Screen | None:
    """docs/10 §Config: "screen_public_id … or an inline definition"."""
    if config.screen_public_id is None:
        if config.screen_definition is None:
            raise Problem(
                ProblemType.INVALID_SCREEN_DEFINITION,
                "A backtest needs either 'screen_public_id' or an inline 'screen_definition'.",
            )
        # Validated here rather than on first contact with the worker, so a malformed inline
        # definition is a 400 at the moment it is posted instead of a `failed` row twenty minutes
        # later. `ScreenDefinition` forbids unknown keys and gates every factor key against the
        # registry (docs/06 §"Reference SQL skeleton").
        try:
            ScreenDefinition.model_validate(config.screen_definition)
        except ValidationError as exc:
            raise Problem(
                ProblemType.INVALID_SCREEN_DEFINITION,
                "The inline screen definition failed validation.",
                errors=[
                    {"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
                    for error in exc.errors()
                ],
            ) from exc
        return None
    screen = (
        await session.execute(select(Screen).where(Screen.public_id == config.screen_public_id))
    ).scalar_one_or_none()
    if screen is None or not (screen.user_id is None or screen.user_id == principal.user_id):
        raise not_found("screen", config.screen_public_id)
    return screen


def _dispatch(request: Request, public_id: str, *, fragility: bool) -> None:
    """Hand the run to the ``backtest`` queue.

    A missing broker is **not** silently swallowed: the row stays ``queued`` and the log says so,
    which is a state an operator can see and re-drive. Raising here would leave the user with a
    500 for a run that was in fact recorded.
    """
    queue = _queue(request)
    sender = getattr(queue, "send_task", None)
    if not callable(sender):
        log.error(
            "backtest queued with no broker to run it",
            extra={"public_id": public_id},
        )
        return
    try:
        sender(BACKTEST_TASK, [public_id, fragility])
    except Exception as exc:  # any broker failure has the same remedy: leave it queued
        log.error(
            "backtest could not be dispatched",
            extra={"public_id": public_id, "error": str(exc)},
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@router.get("", response_model=BacktestListOut, summary="List your backtests")
async def list_backtests(
    session: SessionDep,
    principal: AuthenticatedDep,
    entitlements: EntitlementsDep,
    limit: Limit = DEFAULT_PAGE_SIZE,
) -> BacktestListOut:
    entitlements.require(Feature.BACKTESTS)
    rows = (
        (
            await session.execute(
                select(Backtest)
                .where(Backtest.user_id == principal.require_user())
                .order_by(Backtest.created_at.desc(), Backtest.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    labels = await _screen_labels(session, rows)
    return BacktestListOut(data=[_summary(row, labels) for row in rows])


@router.get("/{public_id}", response_model=BacktestOut, summary="One backtest")
async def get_backtest(
    public_id: str,
    session: SessionDep,
    principal: AuthenticatedDep,
    entitlements: EntitlementsDep,
) -> BacktestOut:
    """docs/07: "status + metrics + equity curve"."""
    entitlements.require(Feature.BACKTESTS)
    row = await _load(session, public_id, principal)
    detail = _detail(row)
    labels = await _screen_labels(session, [row])
    if row.screen_id is not None and row.screen_id in labels:
        public, name = labels[row.screen_id]
        return detail.model_copy(update={"screen_public_id": public, "screen_name": name})
    return detail


@router.delete("/{public_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a backtest")
async def delete_backtest(
    public_id: str,
    session: SessionDep,
    principal: AuthenticatedDep,
    entitlements: EntitlementsDep,
) -> Response:
    """docs/07: `DELETE /backtests/{id}`.

    The R2 artefacts are **not** deleted here. They are keyed by ``public_id``, which is never
    reissued, so nothing can read them once the row is gone; and deleting them inside the request
    would make a delete depend on an object store being reachable. A sweeper belongs with the
    other retention jobs (docs/04 §"Retention & size estimates"), and does not exist yet —
    ``docs/DECISIONS.md`` §15.
    """
    entitlements.require(Feature.BACKTESTS)
    row = await _load(session, public_id, principal)
    await session.execute(delete(Backtest).where(Backtest.id == row.id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Artefacts
# ---------------------------------------------------------------------------


def _read_rows(archive: RawArchive, key: str) -> list[dict[str, str]]:
    try:
        payload = archive.get(key)
    except ArchiveError as exc:
        raise not_found("backtest artefact", key) from exc
    return list(csv.DictReader(io.StringIO(payload.decode("utf-8"))))


async def _artefact_rows(archive: RawArchive, key: str) -> list[dict[str, str]]:
    """boto3 is synchronous; reading a fifteen-year trade log must not block the event loop."""
    return await anyio.to_thread.run_sync(lambda: _read_rows(archive, key))


def _page(
    rows: Sequence[Mapping[str, str]], cursor: str | None, limit: int
) -> tuple[Sequence[Mapping[str, str]], str | None]:
    """docs/07 §Conventions cursor pagination, over an artefact that is a list.

    The cursor is the row offset. It is stable because the artefact is immutable once written —
    a backtest's trade log never gains a row.
    """
    start = 0
    if cursor is not None:
        if not cursor.isdigit():
            raise Problem(ProblemType.NOT_FOUND, f"{cursor!r} is not a valid pagination cursor.")
        start = int(cursor)
    window = rows[start : start + limit]
    following = start + limit
    return window, str(following) if following < len(rows) else None


def _require_done(row: Backtest) -> None:
    if row.status != "done":
        raise Problem(
            ProblemType.NOT_FOUND,
            f"Backtest {row.public_id} is {row.status!r}; its artefacts do not exist yet.",
        )


@router.get("/{public_id}/trades", response_model=TradePage, summary="The trade log")
async def get_trades(  # noqa: PLR0913, PLR0917 - FastAPI injects one parameter per dependency
    request: Request,
    public_id: str,
    session: SessionDep,
    principal: AuthenticatedDep,
    entitlements: EntitlementsDep,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> TradePage:
    """docs/07: `GET /backtests/{id}/trades?cursor=` → "paginated fills"."""
    entitlements.require(Feature.BACKTESTS)
    row = await _load(session, public_id, principal)
    _require_done(row)
    rows = await _artefact_rows(_archive(request), service.artefact_key(public_id, "trades"))
    window, following = _page(rows, cursor, limit)
    return TradePage(
        data=[TradeOut.model_validate(_blanks_to_none(item)) for item in window],
        next_cursor=following,
    )


@router.get("/{public_id}/holdings", response_model=HoldingPage, summary="Per-rebalance holdings")
async def get_holdings(  # noqa: PLR0913, PLR0917 - FastAPI injects one parameter per dependency
    request: Request,
    public_id: str,
    session: SessionDep,
    principal: AuthenticatedDep,
    entitlements: EntitlementsDep,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
    rebalance_date: Annotated[dt.date | None, Query()] = None,
) -> HoldingPage:
    """docs/08 §Backtests: the results page shows "per-period holdings"."""
    entitlements.require(Feature.BACKTESTS)
    row = await _load(session, public_id, principal)
    _require_done(row)
    rows = await _artefact_rows(_archive(request), service.artefact_key(public_id, "holdings"))
    if rebalance_date is not None:
        wanted = rebalance_date.isoformat()
        rows = [item for item in rows if item.get("rebalance_date") == wanted]
    window, following = _page(rows, cursor, limit)
    return HoldingPage(
        data=[BacktestHoldingOut.model_validate(_blanks_to_none(item)) for item in window],
        next_cursor=following,
    )


def _blanks_to_none(row: Mapping[str, str]) -> dict[str, str | None]:
    """A CSV has no null. An empty cell is one."""
    return {key: (value if value != "" else None) for key, value in row.items()}


@router.get("/{public_id}/export", response_model=ExportLinkOut, summary="A signed download link")
async def export_backtest(  # noqa: PLR0913, PLR0917 - FastAPI injects one parameter per dependency
    request: Request,
    public_id: str,
    session: SessionDep,
    principal: AuthenticatedDep,
    entitlements: EntitlementsDep,
    artefact: Annotated[str, Query(pattern="^(trades|holdings|equity)$")] = "trades",
) -> ExportLinkOut:
    """docs/07: `GET /backtests/{id}/export` → "CSV/Parquet signed URL".

    CSV, not Parquet: docs/07 offers either, ``docs/02`` locks no Parquet writer for the API, and
    a trade log is a table a user opens in a spreadsheet. ``docs/DECISIONS.md`` §15.
    """
    entitlements.require(Feature.BACKTESTS)
    row = await _load(session, public_id, principal)
    _require_done(row)
    settings: Settings = settings_for(request)
    signed = service.sign_download(settings, public_id, artefact)
    url = (
        f"{API_PREFIX}/backtests/{public_id}/download/{artefact}"
        f"?expires={signed.expires}&token={signed.token}"
    )
    return ExportLinkOut(
        artefact=_artefact_literal(artefact),
        format="csv",
        url=url,
        expires_at=signed.expires_at,
    )


def _artefact_literal(artefact: str) -> str:
    if artefact not in service.ARTEFACTS:  # pragma: no cover - the route pattern guarantees it
        raise Problem(ProblemType.NOT_FOUND, f"{artefact!r} is not an artefact of a backtest")
    return artefact


@router.get(
    "/{public_id}/download/{artefact}",
    response_class=Response,
    summary="Redeem a signed download link",
)
async def download_artefact(
    request: Request,
    public_id: str,
    artefact: Annotated[str, Path(pattern="^(trades|holdings|equity)$")],
    expires: Annotated[int, Query()],
    token: Annotated[str, Query()],
) -> Response:
    """Deliberately **unauthenticated**: the signature is the credential.

    That is what makes the link usable from a download manager or a spreadsheet's "open from
    URL", which is the whole reason docs/07 asks for a signed URL rather than an authenticated
    stream. It carries no bearer token, expires in fifteen minutes, and names exactly one object.
    """
    settings: Settings = settings_for(request)
    if not service.verify_download(settings, public_id, artefact, expires, token):
        raise not_found("backtest artefact", f"{public_id}/{artefact}")
    payload = await anyio.to_thread.run_sync(
        lambda: _bytes(_archive(request), service.artefact_key(public_id, artefact))
    )
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="baskfy-backtest-{public_id}-{artefact}.csv"'
            ),
            "Cache-Control": "private, no-store",
        },
    )


def _bytes(archive: RawArchive, key: str) -> bytes:
    try:
        return archive.get(key)
    except ArchiveError as exc:
        raise not_found("backtest artefact", key) from exc


# ---------------------------------------------------------------------------
# SSE (Prompt 15 §4)
# ---------------------------------------------------------------------------


class _PubSub(Protocol):
    """The four calls this module makes on a redis-py subscription.

    Declared structurally because ``redis.asyncio.client.PubSub`` types ``aclose`` as untyped,
    and CLAUDE.md house rule 3 forbids the ``type: ignore`` that calling it would otherwise need.
    """

    def subscribe(self, *channels: str) -> Awaitable[object]: ...

    def unsubscribe(self, *channels: str) -> Awaitable[object]: ...

    def get_message(
        self, ignore_subscribe_messages: bool = False, timeout: float | None = None
    ) -> Awaitable[object]: ...

    def aclose(self) -> Awaitable[object]: ...


def _sse(event: str, data: str) -> bytes:
    return f"event: {event}\ndata: {data}\n\n".encode()


async def event_stream(
    cache: Redis | None, public_id: str, initial: str, terminal: bool
) -> AsyncGenerator[bytes, None]:
    """One SSE connection.

    Public rather than private so it can be driven directly by a test. httpx's ``ASGITransport``
    buffers a response to completion before returning it, so a long-lived stream cannot be
    exercised over the in-process client at all — the only way to assert that a frame published
    mid-run reaches a subscriber is to drive this generator.

    The first frame is always the *current* state — read from Redis if the worker has published
    one, else synthesised from the row — so a client that connects after the run started is not
    left staring at an empty progress bar until the next event.

    **The subscription is opened before that first frame is sent.** Sending first and subscribing
    afterwards leaves a window in which the worker publishes and nobody is listening, and the
    frame that goes missing is disproportionately likely to be the terminal one — the run would
    then appear to hang at 97% until the client's poll caught up.
    """
    if terminal or cache is None:
        yield _sse("progress", initial)
        yield _sse("end", initial)
        return

    pubsub: _PubSub = cache.pubsub()
    began = anyio.current_time()
    try:
        await pubsub.subscribe(service.events_channel(public_id))
        yield _sse("progress", initial)
        while anyio.current_time() - began < SSE_MAX_SECONDS:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=SSE_HEARTBEAT_SECONDS
            )
            if not isinstance(message, Mapping):
                yield b": keep-alive\n\n"
                continue
            payload = _payload(message)
            if payload is None:
                continue
            yield _sse("progress", payload)
            if _is_terminal(payload):
                yield _sse("end", payload)
                return
        yield _sse("end", initial)
    except RedisError as exc:  # pragma: no cover - a broker that dies mid-stream
        log.warning("backtest event stream failed", extra={"error": str(exc)})
        yield _sse("progress", initial)
        yield _sse("end", initial)
    finally:
        try:
            await pubsub.unsubscribe()
            await pubsub.aclose()
        except RedisError:  # pragma: no cover - closing a broken subscription
            log.debug("closing the backtest event subscription failed")


def _payload(message: Mapping[str, object]) -> str | None:
    data = message.get("data")
    if isinstance(data, bytes):
        return data.decode("utf-8")
    return data if isinstance(data, str) else None


def _is_terminal(payload: str) -> bool:
    try:
        body = json.loads(payload)
    except json.JSONDecodeError:  # pragma: no cover - we write these frames ourselves
        return False
    return isinstance(body, dict) and body.get("status") in {"done", "failed"}


@router.get(
    "/{public_id}/events",
    response_class=StreamingResponse,
    summary="Progress events (Server-Sent Events)",
    # An event stream is not a JSON document; documenting it as one would make the generated
    # client offer a typed response body that never arrives.
    include_in_schema=True,
    responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
)
async def backtest_events(
    request: Request,
    public_id: str,
    session: SessionDep,
    principal: AuthenticatedDep,
    entitlements: EntitlementsDep,
) -> StreamingResponse:
    """PROMPTS.md Prompt 15 §4: "progress events streamed to the client over SSE"."""
    entitlements.require(Feature.BACKTESTS)
    row = await _load(session, public_id, principal)
    cache = _cache(request)
    stored = await _stored_frame(cache, public_id)
    terminal = row.status in {"done", "failed"}
    initial = (
        service.progress_frame(
            public_id,
            _status(row),
            stage=row.status,
            completed=1 if terminal else 0,
            total=1,
            detail=row.error,
        )
        if terminal or stored is None
        else stored
    )
    return StreamingResponse(
        event_stream(cache, public_id, initial, terminal),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


async def _stored_frame(cache: Redis | None, public_id: str) -> str | None:
    if cache is None:
        return None
    try:
        stored = await cache.get(service.progress_key(public_id))
    except RedisError:  # pragma: no cover - a broker that is down
        return None
    if isinstance(stored, bytes):
        return stored.decode("utf-8")
    return stored if isinstance(stored, str) else None
