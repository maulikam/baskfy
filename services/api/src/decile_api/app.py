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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.exceptions import HTTPException as StarletteHTTPException

from decile_api import invoices
from decile_api.db import create_engine, session_factory
from decile_api.email import Mailer, build_transport
from decile_api.logging import (
    REQUEST_ID_HEADER,
    configure_logging,
    new_request_id,
    request_id_var,
)
from decile_api.problems import (
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
from decile_api.queue import build_task_queue
from decile_api.ratelimit import RateLimiter, enforce_rate_limit
from decile_api.routers import (
    auth,
    backtests,
    billing,
    instruments,
    market_data,
    meta,
    portfolios,
    screens,
)
from decile_api.schemas import HealthOut, ProblemOut
from decile_api.screener import AsOfOutOfRange, NoPublishedData
from decile_api.settings import API_PREFIX, Settings, get_settings
from decile_api.telemetry import annotate_current_span, configure_telemetry
from decile_core.entitlements import FeatureNotEntitled
from decile_core.screener import ScreenQueryError

log = logging.getLogger(__name__)

PROBLEM_SCHEMA_REF = "#/components/schemas/ProblemOut"

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
    annotate_current_span(**{"decile.problem_type": problem.type.value})
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

        Raised by `Entitlements.require`, which lives in `decile_core` and therefore cannot know
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
    @app.middleware("http")
    async def _request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Give every request an id, put it on every log line, and echo it back.

        An inbound ``X-Request-Id`` is honoured so a trace started at the edge (or by the Next.js
        server component making the call) keeps one id end to end.
        """
        request_id = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        token = request_id_var.set(request_id)
        annotate_current_span(**{"decile.request_id": request_id})
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER, "Idempotency-Key"],
        expose_headers=[REQUEST_ID_HEADER, "X-Decile-As-Of", "X-Decile-Data-Version"],
    )


def _open_cache(settings: Settings) -> Redis | None:
    """A Redis client, or ``None`` when there is none to be had.

    ``None`` degrades the screen cache to "always compute" and idempotency to "not deduplicated";
    it does **not** degrade rate limiting, which refuses to run unmetered (see
    ``decile_api.ratelimit``).
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
    # only — see `decile_api.queue` for why it publishes by task name rather than importing the
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
    app.include_router(versioned)

    @app.get("/health", response_model=HealthOut, tags=["ops"], include_in_schema=False)
    async def health() -> HealthOut:
        """Liveness only — it deliberately does not touch the database.

        A health check that fails when PostgreSQL is briefly unreachable takes the process out of
        the load balancer at the exact moment it would have recovered on its own.
        """
        return HealthOut(status="ok", environment=resolved.environment)

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
    """Entry point for ``uvicorn decile_api.app:get_app --factory``."""
    return create_app()
