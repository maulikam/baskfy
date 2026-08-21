"""``/api/public/v1`` — the public read API (Prompt 20 deliverable 2).

    "A versioned public read API exposing ONLY derived analytics (screen results, factor values,
     breadth), never raw vendor bars, with a machine-readable terms-of-use endpoint. Gate the
     entire feature behind a flag that stays OFF until the data-redistribution review in docs/11
     is signed off; make that dependency explicit in the code and the admin UI."

**Two locks, and both must be open.**

1. ``DECILE_PUBLIC_API_ENABLED`` — a setting, ``False`` in every environment file in this
   repository, asserted by ``services/api/tests/test_public_api.py``.
2. ``decile_core.public_api.DATA_REDISTRIBUTION_REVIEW.signed_off`` — a **source constant**, and
   the one that encodes docs/11's requirement. An operator can turn the feature off with an
   environment variable; nobody can turn it on without a commit.

:func:`is_enabled` is the single place both are read, and ``decile_api.app`` only mounts this
router when it returns True. A caller who finds the flag off gets a plain 404 from the framework,
because a 403 would confirm that a public tier exists and is merely switched off — which is a
different statement from "we do not have one", and only one of them is currently true.

What is served, and what is not
-------------------------------
``decile_core.public_api.PUBLIC_COLUMNS`` is the whitelist, and the rule that produces it is "no
field denominated in rupees per share". This module additionally refuses to run a screen whose
*sorting factor* is a withheld column — docs/06 always projects ``sorting_factor`` alongside the
requested columns, so a screen sorted by ``close_raw`` would put an exchange print in the payload
through a door the column whitelist does not cover.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from sqlalchemy import select

from decile_api import api_keys as key_service
from decile_api import market_data, public_docs
from decile_api.auth import API_KEY_HEADER
from decile_api.db import SessionDep
from decile_api.problems import Problem, ProblemType, not_found, rate_limited, unauthenticated
from decile_api.ratelimit import RateLimiter
from decile_api.schemas import (
    PublicBreadthOut,
    PublicFactorsOut,
    PublicScreenOut,
    PublicScreenRowOut,
    PublicStatusOut,
    PublicTermsOut,
)
from decile_api.screener import (
    current_data_version,
    execute_screen,
    latest_published_date,
    resolve_as_of,
)
from decile_api.settings import Settings, get_settings
from decile_core.api_keys import Scope
from decile_core.models import ApiKey, FactorDaily, Instrument, Screen
from decile_core.public_api import (
    DATA_REDISTRIBUTION_REVIEW,
    PUBLIC_API_PREFIX,
    PUBLIC_API_VERSION,
    PUBLIC_COLUMNS,
    TERMS_VERSION,
    is_public_sort,
    terms_of_use,
)
from decile_core.screen_definition import ScreenDefinition
from decile_core.screener import ScreenResult

log = logging.getLogger(__name__)

router = APIRouter(prefix=PUBLIC_API_PREFIX, tags=["public"])

#: Rows one public screen response returns. Far below the 4,000 the product API allows: a public
#: tier is for looking things up, and a caller who needs the whole universe is asking for the
#: dataset rather than for an analytic. ``docs/DECISIONS.md`` §20.10.
PUBLIC_ROW_LIMIT: Final = 500

#: Cached for a trading day — the data changes once a night (docs/09 §Schedule). The same number
#: the terms document publishes as ``caching_max_age_seconds``.
PUBLIC_CACHE_SECONDS: Final = 86_400


def is_enabled(settings: Settings) -> bool:
    """Both locks. See the module docstring — this is the whole gate, in one place."""
    return settings.public_api_enabled and DATA_REDISTRIBUTION_REVIEW.signed_off


def _settings(request: Request) -> Settings:
    resolved = getattr(request.app.state, "settings", None)
    return resolved if isinstance(resolved, Settings) else get_settings()


def _terms_url(settings: Settings) -> str:
    return f"{settings.public_api_base_url.rstrip('/')}{PUBLIC_API_PREFIX}/terms"


async def authenticated_key(request: Request, session: SessionDep) -> ApiKey:
    """Resolve ``X-API-Key``, meter it, and count the request. **No cache** — see
    ``decile_api.api_keys``, and Prompt 20's first acceptance criterion.

    Ordering matters. The key is authenticated first, because a rate limit keyed on an
    unauthenticated caller is keyed on an IP and would let a thousand bad keys share one bucket.
    The throttle counter is written *before* the 429 is raised, so a caller who is being refused
    can still see why on their dashboard.
    """
    presented = request.headers.get(API_KEY_HEADER)
    if not presented:
        raise unauthenticated(f"Present an API key in the {API_KEY_HEADER} header.")
    key = await key_service.authenticate(session, presented)
    if key is None:
        raise unauthenticated("That API key is not valid.")

    settings = _settings(request)
    limiter = getattr(request.app.state, "rate_limiter", None)
    if isinstance(limiter, RateLimiter):
        limit = key.rate_limit_per_minute or settings.rate_limit_api_key_per_minute
        decision = await limiter.check_key(key.prefix, limit)
        if not decision.allowed:
            await key_service.record_usage(session, key.id, throttled=1)
            await session.commit()
            raise rate_limited(decision.retry_after_seconds, decision.limit_per_minute)

    await key_service.record_usage(session, key.id, requests=1)
    await key_service.touch(session, key)
    # Committed here rather than left to the request's teardown: the counters are the usage
    # dashboard's only source, and a request that ends in a 404 still consumed quota.
    await session.commit()
    return key


KeyDep = Annotated[ApiKey, Depends(authenticated_key)]


def require_scope(key: ApiKey, scope: Scope) -> None:
    """A 402, not a 403. docs/07's catalogue has no ``forbidden``, and "your credential does not
    include this" is exactly what ``payment-required`` already means on this service."""
    if scope.value not in key.scopes:
        raise Problem(
            ProblemType.PAYMENT_REQUIRED,
            f"This key does not carry the {scope.value!r} scope.",
            feature=scope.value,
            upgrade_url="/api-keys",
        )


def _cache_headers() -> dict[str, str]:
    return {"Cache-Control": f"public, max-age={PUBLIC_CACHE_SECONDS}"}


# ---------------------------------------------------------------------------
# Terms of use and status
# ---------------------------------------------------------------------------


@router.get("/terms", response_model=PublicTermsOut, summary="Machine-readable terms of use")
async def public_terms(request: Request, response: Response) -> PublicTermsOut:
    """No API key required.

    An integrator has to be able to read what they may do with the data *before* they hold a
    credential, and the document contains no market data — it is the licence, and the list of
    fields this API withholds because of it.
    """
    settings = _settings(request)
    response.headers.update(_cache_headers())
    document = terms_of_use(contact=settings.public_api_contact_email)
    return PublicTermsOut(**document.as_dict())  # type-checked by the model, not by this call


@router.get("/status", response_model=PublicStatusOut, summary="What this key may read, and when")
async def public_status(session: SessionDep, request: Request, key: KeyDep) -> PublicStatusOut:
    require_scope(key, Scope.META_READ)
    settings = _settings(request)
    as_of = await latest_published_date(session)
    if as_of is None:
        raise Problem(ProblemType.PIPELINE_DEGRADED, "No data has been published yet.")
    return PublicStatusOut(
        as_of=as_of,
        data_version=await current_data_version(session),
        api_version=PUBLIC_API_VERSION,
        terms_version=TERMS_VERSION,
        scopes=list(key.scopes),
        rate_limit_per_minute=key.rate_limit_per_minute or settings.rate_limit_api_key_per_minute,
    )


# ---------------------------------------------------------------------------
# Derived analytics
# ---------------------------------------------------------------------------


def _public_row(row_values: dict[str, object], rank: int) -> PublicScreenRowOut:
    """Whitelist, not blacklist: the payload is built from the allowed keys, so a column that
    appears in the result set and not in the whitelist cannot reach a caller by omission."""
    payload: dict[str, object] = {
        "rank": rank,
        "symbol": str(row_values.get("symbol", "")),
        "name": str(row_values.get("name", "")),
    }
    for column in PUBLIC_COLUMNS:
        if column in row_values:
            payload[column] = row_values[column]
    return PublicScreenRowOut(**payload)


def _refuse_withheld_sort(definition: ScreenDefinition) -> None:
    if not is_public_sort(definition.sort_by):
        raise Problem(
            ProblemType.PAYMENT_REQUIRED,
            f"This screen sorts by {definition.sort_by!r}, which the public API does not serve. "
            "See the terms-of-use endpoint's `withheld_fields`.",
            feature="withheld_field",
            upgrade_url=PUBLIC_API_PREFIX + "/terms",
        )


@router.get(
    "/screens/{public_id}/results",
    response_model=PublicScreenOut,
    summary="Run a screen and return its derived analytics",
)
async def public_screen_results(  # noqa: PLR0913, PLR0917 - FastAPI injects one per dependency
    session: SessionDep,
    request: Request,
    response: Response,
    key: KeyDep,
    public_id: Annotated[str, Path()],
    as_of: Annotated[dt.date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=PUBLIC_ROW_LIMIT)] = 100,
) -> PublicScreenOut:
    """The screen's own definition, projected onto the public column whitelist.

    Deliberately **not** served through the Redis screen cache. The cached bytes are the product
    API's full payload, and a public response has to be assembled field by field from the
    whitelist; reading a cached blob and filtering it afterwards is the shape of bug that leaks a
    column when somebody adds one. Recorded in ``docs/DECISIONS.md`` §20.11.
    """
    require_scope(key, Scope.SCREENS_READ)
    settings = _settings(request)
    screen = (
        await session.execute(select(Screen).where(Screen.public_id == public_id))
    ).scalar_one_or_none()
    if screen is None or (screen.user_id is not None and screen.user_id != key.user_id):
        raise not_found("screen", public_id)

    definition = ScreenDefinition.model_validate(screen.definition)
    _refuse_withheld_sort(definition)
    resolution = await resolve_as_of(session, as_of)
    data_version = await current_data_version(session)
    result: ScreenResult = await execute_screen(
        session,
        definition,
        as_of=resolution.as_of,
        data_version=data_version,
        columns=PUBLIC_COLUMNS,
        requested_as_of=as_of,
        limit=limit,
    )
    document = terms_of_use(contact=settings.public_api_contact_email)
    response.headers.update(_cache_headers())
    return PublicScreenOut(
        as_of=result.as_of,
        data_version=result.data_version,
        screen=screen.name,
        sorting_factor=result.sorting_factor.key,
        columns=["rank", "symbol", "name", *PUBLIC_COLUMNS],
        result_count=result.result_count,
        rows=[_public_row(dict(row.values), row.rank) for row in result.rows],
        attribution=document.attribution_text,
        disclaimer=document.disclaimer,
        terms_url=_terms_url(settings),
    )


@router.get(
    "/instruments/{symbol}/factors",
    response_model=PublicFactorsOut,
    summary="Factor values for one instrument",
)
async def public_factors(  # noqa: PLR0913, PLR0917 - FastAPI injects one per dependency
    session: SessionDep,
    request: Request,
    response: Response,
    key: KeyDep,
    symbol: Annotated[str, Path()],
    as_of: Annotated[dt.date | None, Query()] = None,
) -> PublicFactorsOut:
    """Every whitelisted factor for one name on one day. No price, no volume, no moving average."""
    require_scope(key, Scope.FACTORS_READ)
    settings = _settings(request)
    instrument = (
        await session.execute(select(Instrument).where(Instrument.symbol == symbol.upper()))
    ).scalar_one_or_none()
    if instrument is None:
        raise not_found("instrument", symbol)

    resolution = await resolve_as_of(session, as_of)
    row = (
        await session.execute(
            select(FactorDaily).where(
                FactorDaily.instrument_id == instrument.id,
                FactorDaily.date == resolution.as_of,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("factor row", f"{symbol}@{resolution.as_of.isoformat()}")

    factors: dict[str, object] = {}
    for column in PUBLIC_COLUMNS:
        if hasattr(row, column):
            value = getattr(row, column)
            factors[column] = str(value) if isinstance(value, Decimal) else value
    document = terms_of_use(contact=settings.public_api_contact_email)
    response.headers.update(_cache_headers())
    return PublicFactorsOut(
        as_of=resolution.as_of,
        data_version=await current_data_version(session),
        symbol=instrument.symbol,
        name=instrument.name,
        factors=_json_safe(factors),
        attribution=document.attribution_text,
        disclaimer=document.disclaimer,
        terms_url=_terms_url(settings),
    )


def _json_safe(values: dict[str, object]) -> dict[str, str | int | float | bool | None]:
    """Decimals as strings, everything else as itself. House rule 9: money is never a float."""
    safe: dict[str, str | int | float | bool | None] = {}
    for name, value in values.items():
        if value is None or isinstance(value, (int, float, bool)):
            safe[name] = value
        else:
            safe[name] = str(value)
    return safe


@router.get("/breadth", response_model=PublicBreadthOut, summary="Market breadth for one universe")
async def public_breadth(  # noqa: PLR0913, PLR0917 - FastAPI injects one per dependency
    session: SessionDep,
    request: Request,
    response: Response,
    key: KeyDep,
    universe: Annotated[str, Query()] = "nifty-500",
    as_of: Annotated[dt.date | None, Query()] = None,
) -> PublicBreadthOut:
    """docs/01 §6's four breadth series, for one universe on one day."""
    require_scope(key, Scope.BREADTH_READ)
    settings = _settings(request)
    resolution = await resolve_as_of(session, as_of)
    try:
        health = await market_data.market_health(session, universe, resolution.as_of)
    except market_data.UnknownUniverse as exc:
        raise not_found("universe", universe) from exc
    document = terms_of_use(contact=settings.public_api_contact_email)
    response.headers.update(_cache_headers())
    return PublicBreadthOut(
        as_of=health.as_of,
        data_version=await current_data_version(session),
        universe=universe,
        pct_above_200dma=_gauge(health, "pct_above_200dma"),
        pct_above_50dma=_gauge(health, "pct_above_50dma"),
        pct_within_10pct_ath=_gauge(health, "pct_within_10pct_ath"),
        pct_ret_1y_positive=_gauge(health, "pct_ret_1y_positive"),
        constituent_count=health.constituent_count,
        attribution=document.attribution_text,
        disclaimer=document.disclaimer,
        terms_url=_terms_url(settings),
    )


def _gauge(health: market_data.MarketHealth, key: str) -> Decimal | None:
    for gauge in health.gauges:
        if gauge.key == key:
            return gauge.value
    return None


# ---------------------------------------------------------------------------
# The interactive reference (Prompt 20 deliverable 5)
# ---------------------------------------------------------------------------


@router.get("/openapi.json", include_in_schema=False)
async def public_openapi(request: Request) -> Response:
    """The public half of the service's spec, with ``x-codeSamples`` attached."""
    settings = _settings(request)
    document = public_docs.public_document(
        request.app.openapi(), base_url=settings.public_api_base_url
    )
    return Response(
        content=public_docs.render_document(document),
        media_type="application/json",
    )


@router.get("/docs", include_in_schema=False)
async def public_reference(request: Request) -> Response:
    """Redoc, pointed at the document above. See ``decile_api.public_docs``."""
    settings = _settings(request)
    return Response(
        content=public_docs.redoc_page(
            spec_url=f"{PUBLIC_API_PREFIX}/openapi.json",
            script_url=settings.redoc_script_url,
        ),
        media_type="text/html",
    )
