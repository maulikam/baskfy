"""The FastAPI application (Prompt 7 deliverable 1).

Wires the pieces the rest of this package defines: settings, one engine and session factory for
the process, structured logging with a request id, OpenTelemetry, the Redis result cache and rate
limiter, and one RFC 9457 handler per class of failure.

Two decisions worth stating out loud
------------------------------------
**Validation failures answer 400, not FastAPI's default 422.** docs/07 §"Error catalogue" assigns
``400 invalid-screen-definition`` to "schema violation; `errors[]` lists field paths" and reserves
``422`` for ``no-trading-day``. Leaving the framework default in place would mean two different
meanings on one status code, and a client that cannot tell "your JSON is wrong" from "that date is
outside the range we hold".

**Rate limiting is a dependency on the versioned router, not middleware.** The limit depends on
who is calling, and that answer comes from the same token verification the routes use; deciding it
in middleware would decode the bearer token twice, in two places, from two code paths.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from hmac import compare_digest

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from baskfy_api import invoices, metrics
from baskfy_api.db import create_engine, session_factory
from baskfy_api.email import Mailer, build_transport
from baskfy_api.http_cache import register_http_cache
from baskfy_api.logging import (
    REQUEST_ID_HEADER,
    configure_logging,
    new_request_id,
    request_id_var,
)
from baskfy_api.problems import (
    CONTENT_TYPE,
    STATUS_FOR,
    TITLE_FOR,
    Problem,
    ProblemType,
    invalid_screen_definition,
    no_trading_day,
    payment_required,
    pipeline_degraded,
)
from baskfy_api.queue import build_task_queue
from baskfy_api.ratelimit import RateLimiter, enforce_rate_limit
from baskfy_api.routers import (
    admin,
    alerts,
    api_keys,
    auth,
    backtests,
    billing,
    instruments,
    market_data,
    meta,
    portfolios,
    public,
    screens,
    support,
    webhook_endpoints,
)
from baskfy_api.schemas import HealthOut, ProblemOut
from baskfy_api.screener import AsOfOutOfRange, NoPublishedData
from baskfy_api.sentry import configure_sentry
from baskfy_api.settings import API_PREFIX, Settings, get_settings
from baskfy_api.telemetry import annotate_current_span, configure_telemetry, current_trace_id
from baskfy_core.entitlements import FeatureNotEntitled
from baskfy_core.screener import ScreenQueryError

log = logging.getLogger(__name__)

PROBLEM_SCHEMA_REF = "#/components/schemas/ProblemOut"

#: Echoed on every response when a trace is active, so an operator can jump from a response
#: (or a screenshot of one) straight to the trace in Tempo. W3C names the *inbound* header
#: `traceparent`; this is a response header and carries the id alone, which is what a human
#: pastes into a search box.
TRACE_ID_HEADER = "X-Trace-Id"

#: The queues whose depth `/metrics` publishes. Duplicated from
#: `baskfy_worker.celery_app.QUEUES` rather than imported: `baskfy-worker` depends on
#: `baskfy-api`, so the import cannot go the other way (the same reason `baskfy_api.queue`
#: publishes by task name). `services/api/tests/test_metrics.py` asserts the two agree.
CELERY_QUEUES: tuple[str, ...] = ("ingest", "compute", "backtest", "default")

#: Statuses at or above this are ours to explain, so they are logged at ERROR.
SERVER_ERROR_FLOOR = 500

TITLE = "Decile API"
DESCRIPTION = "India-equities momentum screener. See docs/07-api-spec.md."
VERSION = "0.1.0"


def operation_id(route: APIRoute) -> str:
    """``run_saved_screen`` -> ``runSavedScreen``.

    FastAPI's default operation id is the handler name with the path and method appended
    (``run_saved_screen_api_v1_screens__public_id__run_post``), which becomes the method name on
    the generated TypeScript client. Naming them here is the difference between
    ``client.runSavedScreen(...)`` and something no one will type twice.
    """
    head, *rest = route.name.split("_")
    return head + "".join(word.capitalize() for word in rest)


class DecileAPI(FastAPI):
    """FastAPI, plus one schema the generator would otherwise miss.

    ``ProblemOut`` is referenced by every route's error responses but returned by no handler, so
    FastAPI never walks a response model that would register it. Injecting it keeps the ``$ref``
    in :data:`_DOCUMENTED_ERRORS` resolvable — an unresolvable ``$ref`` makes ``openapi-typescript``
    emit ``unknown``, which takes the error shape out of the generated client entirely.
    """

    def openapi(self) -> dict[str, object]:
        document = super().openapi()
        components = document.setdefault("components", {})
        schemas = components.setdefault("schemas", {})
        schemas.setdefault("ProblemOut", ProblemOut.model_json_schema())
        return document


def _problem_response(problem: Problem, request: Request) -> JSONResponse:
    annotate_current_span({"baskfy.problem_type": problem.type.value})
    headers = dict(problem.headers)
    request_id = request_id_var.get()
    if request_id is not None:
        headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(
        status_code=problem.status,
        content=problem.body(instance=request.url.path),
        media_type=CONTENT_TYPE,
        headers=headers,
    )


def _field_path(location: tuple[object, ...]) -> str:
    """``("body", "definition", "sort_by")`` -> ``definition.sort_by``.

    docs/07 asks for field *paths*; the leading ``body``/``query`` marker is FastAPI's, not the
    client's, and repeating it in every entry only makes the list harder to read.
    """
    parts = [str(part) for part in location]
    if parts and parts[0] in {"body", "query", "path", "header", "cookie"}:
        parts = parts[1:]
    return ".".join(parts) or "<root>"


def register_error_handlers(app: FastAPI) -> None:
    """One handler per class of failure; every one of them answers problem+json."""

    @app.exception_handler(Problem)
    async def _handle_problem(request: Request, exc: Problem) -> JSONResponse:
        if exc.status >= SERVER_ERROR_FLOOR:
            log.error("problem", extra={"problem_type": exc.type.value, "detail": exc.detail})
        return _problem_response(exc, request)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "field": _field_path(tuple(error.get("loc", ()))),
                "message": str(error.get("msg", "")),
                "type": str(error.get("type", "")),
            }
            for error in exc.errors()
        ]
        return _problem_response(invalid_screen_definition(errors), request)

    @app.exception_handler(ScreenQueryError)
    async def _handle_screen_query(request: Request, exc: ScreenQueryError) -> JSONResponse:
        """A definition that passed schema validation but names something the registry does not."""
        return _problem_response(
            invalid_screen_definition([{"field": "definition", "message": str(exc)}]), request
        )

    @app.exception_handler(AsOfOutOfRange)
    async def _handle_as_of(request: Request, exc: AsOfOutOfRange) -> JSONResponse:
        return _problem_response(no_trading_day(exc.requested, exc.earliest, exc.latest), request)

    @app.exception_handler(NoPublishedData)
    async def _handle_unpublished(request: Request, exc: NoPublishedData) -> JSONResponse:
        """docs/07: "503 `pipeline-degraded`".

        docs/11 §Reliability says a failed run should still serve "the last good `data_version`
        with a banner", and the analytics endpoints do exactly that — this is the other case,
        where there is no good version to fall back to at all.
        """
        return _problem_response(pipeline_degraded(str(exc)), request)

    @app.exception_handler(FeatureNotEntitled)
    async def _handle_entitlement(request: Request, exc: FeatureNotEntitled) -> JSONResponse:
        """docs/07 §Entitlements: 'A 402 `payment_required` problem response carries
        `{"upgrade_url": "/pricing"}`.'

        Raised by `Entitlements.require`, which lives in `baskfy_core` and therefore cannot know
        about HTTP. Translating it here is what keeps the gate one line at every call site.
        """
        return _problem_response(payment_required(exc.feature, detail=exc.detail), request)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Anything the framework raises before a route runs — an unrouted path, a bad method."""
        problem_type = _TYPE_FOR_STATUS.get(exc.status_code, ProblemType.INTERNAL_ERROR)
        detail = str(exc.detail) if exc.detail else TITLE_FOR[problem_type]
        problem = Problem(problem_type, detail)
        problem.status = exc.status_code
        problem.title = TITLE_FOR.get(problem_type, "Error")
        return _problem_response(problem, request)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Never leak a stack trace to a client; never lose one from the logs."""
        log.exception("unhandled error", extra={"path": request.url.path})
        return _problem_response(
            Problem(ProblemType.INTERNAL_ERROR, "An unexpected error occurred."), request
        )


#: The subset of HTTP statuses the framework can raise on its own that map onto docs/07's
#: catalogue. Anything else becomes `internal-error` with its original status preserved.
_TYPE_FOR_STATUS = {
    401: ProblemType.UNAUTHENTICATED,
    402: ProblemType.PAYMENT_REQUIRED,
    404: ProblemType.NOT_FOUND,
    409: ProblemType.STALE_DATA_VERSION,
    422: ProblemType.NO_TRADING_DAY,
    429: ProblemType.RATE_LIMITED,
    503: ProblemType.PIPELINE_DEGRADED,
}


def register_middleware(app: FastAPI, settings: Settings) -> None:
    # Registered first, which in Starlette means it sits *inside* everything added after it: the
    # request-id middleware below wraps it, so a 304 still carries an `X-Request-Id`. Prompt 16
    # deliverable 3.
    register_http_cache(app, settings)

    @app.middleware("http")
    async def _request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Give every request an id, time it, put both on every log line, and echo the id back.

        An inbound ``X-Request-Id`` is honoured so a trace started at the edge (or by the Next.js
        server component making the call) keeps one id end to end.

        The timing is Prompt 17 deliverable 2's "API latency by route". It is measured here rather
        than in a second middleware because the two want the same ``perf_counter`` pair and the
        same ``finally`` — and because a metrics middleware registered separately would sit either
        inside or outside this one, and would then disagree with it about what a request cost.
        The status is recorded even when the handler raised: an exception that becomes a 500 is
        exactly the request an error-rate alert exists for.
        """
        request_id = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        token = request_id_var.set(request_id)
        annotate_current_span({"baskfy.request_id": request_id})
        started = time.perf_counter()
        status = SERVER_ERROR_FLOOR
        try:
            response = await call_next(request)
            status = response.status_code
        finally:
            request_id_var.reset(token)
            if settings.metrics_enabled:
                metrics.observe_request(
                    method=request.method,
                    route=metrics.route_label(request),
                    status=status,
                    duration_seconds=time.perf_counter() - started,
                )
        response.headers[REQUEST_ID_HEADER] = request_id
        trace_id = current_trace_id()
        if trace_id is not None:
            # docs/02 sends traces to Tempo and logs to Loki. Echoing the trace id lets a support
            # conversation ("here is the id from the error page") reach the trace, not just the log.
            response.headers[TRACE_ID_HEADER] = trace_id
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            REQUEST_ID_HEADER,
            "Idempotency-Key",
            # W3C trace context, set by `@baskfy/api-client` so a browser call joins the
            # trace the page render started (Prompt 17 deliverable 1).
            "traceparent",
            "tracestate",
        ],
        expose_headers=[
            REQUEST_ID_HEADER,
            TRACE_ID_HEADER,
            "X-Decile-As-Of",
            "X-Decile-Data-Version",
        ],
    )


def _open_cache(settings: Settings) -> Redis | None:
    """A Redis client, or ``None`` when there is none to be had.

    ``None`` degrades the screen cache to "always compute" and idempotency to "not deduplicated";
    it does **not** degrade rate limiting, which refuses to run unmetered (see
    ``baskfy_api.ratelimit``).
    """
    try:
        client: Redis = Redis.from_url(settings.redis_url)
        return client
    except (RedisError, ValueError) as exc:  # pragma: no cover - a malformed URL
        log.error("redis unavailable", extra={"error": str(exc)})
        return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """One engine and one Redis client for the process, opened once and closed once."""
    settings: Settings = app.state.settings
    engine = create_engine(settings.database_url)
    app.state.engine = engine
    app.state.session_factory = session_factory(engine)

    cache = _open_cache(settings)
    app.state.cache = cache
    # One transport for the process (docs/02 §Email). Built here rather than per request so a
    # Resend client's connection pool is reused and a misconfiguration fails at startup.
    app.state.mailer = Mailer(build_transport(settings))
    # docs/02 §"Object storage": Cloudflare R2, or a directory when no bucket is configured.
    # Built once, like the mailer, so an S3 client's connection pool is reused across invoices.
    app.state.invoice_archive = invoices.build_invoice_archive(settings)
    # docs/03 §"Scaling plan" step 4: backtests run on their own queue. The API is a producer
    # only — see `baskfy_api.queue` for why it publishes by task name rather than importing the
    # worker.
    app.state.task_queue = build_task_queue(settings)
    app.state.rate_limiter = (
        RateLimiter(cache, settings) if cache is not None and settings.rate_limit_enabled else None
    )
    if settings.rate_limit_enabled and cache is None:
        log.error("rate limiting is enabled but Redis is unavailable; requests will be refused")

    configure_telemetry(app, settings)
    log.info("api started", extra={"environment": settings.environment})
    try:
        yield
    finally:
        if cache is not None:
            await cache.aclose()
        await engine.dispose()
        log.info("api stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Called by uvicorn, by the tests, and by the OpenAPI emitter."""
    resolved = settings or get_settings()
    resolved.require_configured()
    configure_logging(level=resolved.log_level, json_output=resolved.log_json)
    # Before the app object exists, so an exception raised while wiring it is still reported.
    configure_sentry(resolved, service=resolved.otel_service_name)

    app = DecileAPI(
        title=TITLE,
        description=DESCRIPTION,
        version=VERSION,
        lifespan=lifespan,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
        responses=_DOCUMENTED_ERRORS,
        generate_unique_id_function=operation_id,
    )
    app.state.settings = resolved

    register_error_handlers(app)
    register_middleware(app, resolved)

    versioned = APIRouter(prefix=API_PREFIX, dependencies=[Depends(enforce_rate_limit)])
    versioned.include_router(meta.router)
    versioned.include_router(screens.router)
    versioned.include_router(instruments.router)
    versioned.include_router(market_data.router)
    versioned.include_router(auth.router)
    versioned.include_router(billing.router)
    versioned.include_router(portfolios.router)
    versioned.include_router(backtests.router)
    # PROMPTS.md Prompt 18 §2's contact form. Not in docs/07 (`docs/DECISIONS.md` §18.5).
    versioned.include_router(support.router)
    # docs/09 §Observability: "`pipeline_run_step` is the operator UI; expose it at
    # `/admin/pipeline` behind staff auth." Every route on it depends on `require_staff`.
    versioned.include_router(admin.router)
    # PROMPTS.md Prompt 20 §1, §3 and §4. Not in docs/07 — see each router's docstring.
    versioned.include_router(api_keys.router)
    versioned.include_router(alerts.router)
    versioned.include_router(webhook_endpoints.router)
    app.include_router(versioned)

    # PROMPTS.md Prompt 20 §2: "Gate the entire feature behind a flag that stays OFF until the
    # data-redistribution review in docs/11 is signed off." The router is not *mounted* when the
    # gate is shut, so there is no handler to reach, nothing in the OpenAPI document and nothing
    # in the generated TypeScript client — a stronger statement than a dependency that answers
    # 404. `baskfy_api.routers.public.is_enabled` is the one place both locks are read.
    if public.is_enabled(resolved):  # pragma: no cover - False in every committed configuration
        log.warning(
            "public read API is ENABLED",
            extra={"prefix": public.router.prefix},
        )
        app.include_router(public.router)

    @app.get("/health", response_model=HealthOut, tags=["ops"], include_in_schema=False)
    async def health() -> HealthOut:
        """Liveness only — it deliberately does not touch the database.

        A health check that fails when PostgreSQL is briefly unreachable takes the process out of
        the load balancer at the exact moment it would have recovered on its own.
        """
        return HealthOut(status="ok", environment=resolved.environment)

    @app.get("/metrics", tags=["ops"], include_in_schema=False)
    async def prometheus_metrics(request: Request) -> Response:
        """The Prometheus scrape target (Prompt 17 deliverable 2).

        Outside ``/api/v1`` on purpose: it is not part of docs/07's public surface, it carries no
        rate limit (a scrape every fifteen seconds would eat the anonymous bucket in a minute),
        and it is not in the OpenAPI document, so it never reaches the generated client.

        It refreshes the database-derived gauges first — see ``baskfy_api.metrics`` for why the
        pipeline's numbers are read from ``pipeline_run_step`` rather than held in this process.
        A database that is briefly unreachable degrades the scrape to the process counters rather
        than failing it: a monitoring endpoint that goes down with its dependencies is the one
        thing that must not.
        """
        if not resolved.metrics_enabled:
            raise StarletteHTTPException(status_code=404)
        if resolved.metrics_token:
            presented = request.headers.get("Authorization", "")
            if not compare_digest(presented, f"Bearer {resolved.metrics_token}"):
                raise StarletteHTTPException(status_code=401)

        factory = getattr(app.state, "session_factory", None)
        if factory is not None:
            try:
                async with factory() as session:
                    await metrics.refresh_pipeline_metrics(session)
            except SQLAlchemyError as exc:
                log.warning("pipeline metrics unavailable", extra={"error": str(exc)})
        cache = getattr(app.state, "cache", None)
        if cache is not None:
            await metrics.refresh_queue_depth(cache, CELERY_QUEUES)

        return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE)

    return app


#: Attached to every route so the generated client knows the error shape (docs/07 §"OpenAPI ->
#: TypeScript"). Referenced by name rather than repeated per route.
_DOCUMENTED_ERRORS: dict[int | str, dict[str, object]] = {
    status: {
        "description": TITLE_FOR[problem_type],
        # Content-only, with no ``model``: passing the model would make FastAPI *also* advertise
        # ``application/json`` for every error, and this service returns problem+json for errors
        # and nothing else. :func:`_install_openapi` puts the schema into components.
        "content": {CONTENT_TYPE: {"schema": {"$ref": PROBLEM_SCHEMA_REF}}},
    }
    for problem_type, status in STATUS_FOR.items()
}


def get_app() -> FastAPI:
    """Entry point for ``uvicorn baskfy_api.app:get_app --factory``."""
    return create_app()
