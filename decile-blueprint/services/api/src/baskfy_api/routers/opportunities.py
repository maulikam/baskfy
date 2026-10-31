from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator
from typing import Final

import anyio
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from baskfy_api import opportunities as service
from baskfy_api import opportunities_agui as agui
from baskfy_api.auth import AuthenticatedDep
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.db import SessionDep
from baskfy_api.opportunities_schemas import (
    DEFAULT_RULES,
    DEFAULT_UNIVERSE,
    OptionsIn,
    SwingScanIn,
    ValidateIn,
)
from baskfy_api.opportunities_views import (
    LIMITS,
    options_json,
    provider_json,
    quote_rules_json,
    scan_json,
    unavailable_validation,
    validation_json,
)
from baskfy_api.problems import Problem, ProblemType
from baskfy_core.opportunities.config import (
    OptionsParams,
    SetupType,
    StrategyKind,
    SwingFilters,
)
from baskfy_core.screener import canonical_json

router = APIRouter(prefix="/opportunities", tags=["opportunities"])

JSON_MEDIA_TYPE: Final = "application/json"
SSE_MEDIA_TYPE: Final = "text/event-stream"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _json(payload: object) -> Response:
    return Response(content=canonical_json(payload), media_type=JSON_MEDIA_TYPE)


def _defaults_json() -> dict[str, object]:
    body = SwingScanIn().model_dump()
    body["universe"] = DEFAULT_UNIVERSE
    return body


@router.get("/meta", summary="Opportunity scanner parameters and provider state")
async def get_meta(session: SessionDep, principal: AuthenticatedDep) -> Response:
    universes = await service.universes(session)
    sectors = await service.sectors(session)
    return _json(
        {
            "universes": [{"slug": s, "name": n} for s, n in universes],
            "sectors": [{"slug": s, "name": n} for s, n in sectors],
            "setups": [s.value for s in SetupType],
            "strategies": [s.value for s in StrategyKind],
            "swing_defaults": _defaults_json(),
            "quote_rules_defaults": quote_rules_json(DEFAULT_RULES),
            "options_provider": provider_json(service.provider_state()),
            "limits": LIMITS,
            "options_params": {"max_expiries": OptionsParams().max_expiries},
        }
    )


@router.post("/swing/scan", summary="Scan for swing setups on completed daily bars")
async def post_swing_scan(
    session: SessionDep, principal: AuthenticatedDep, body: SwingScanIn
) -> Response:
    try:
        scan = await service.run_swing_scan(
            session, universe=body.universe, filters=body.filters(), now=_now()
        )
    except service.UniverseUnavailable as exc:
        raise Problem(ProblemType.NOT_FOUND, str(exc)) from exc
    return _json(scan_json(scan))


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    maker: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return maker


def sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {canonical_json(data)}\n\n"


@router.post("/swing/scan/stream", summary="Scan with streamed progress (server-sent events)")
async def post_swing_scan_stream(
    request: Request, principal: AuthenticatedDep, body: SwingScanIn
) -> StreamingResponse:
    maker = _maker(request)
    send, receive = anyio.create_memory_object_stream[str](16)

    async def progress(stage: str, done: int, total: int) -> None:
        await send.send(sse("progress", {"stage": stage, "done": done, "total": total}))

    async def produce() -> None:
        async with send, maker() as session:
            try:
                scan = await service.run_swing_scan(
                    session,
                    universe=body.universe,
                    filters=body.filters(),
                    now=_now(),
                    progress=progress,
                    cancelled=request.is_disconnected,
                )
                await send.send(sse("result", scan_json(scan)))
            except service.ScanCancelled:
                return
            except service.UniverseUnavailable as exc:
                problem = {"title": "Not found", "detail": str(exc), "status": 404}
                await send.send(sse("error", problem))

    async def stream() -> AsyncIterator[str]:
        async with anyio.create_task_group() as group:
            group.start_soon(produce)
            async with receive:
                async for chunk in receive:
                    yield chunk

    return StreamingResponse(
        stream(), media_type=SSE_MEDIA_TYPE, headers={"Cache-Control": "no-store"}
    )


@router.post("/swing/validate", summary="Point-in-time historical test of a setup's signals")
async def post_swing_validate(
    session: SessionDep, principal: AuthenticatedDep, body: ValidateIn
) -> Response:
    filters = SwingFilters(horizon_min=1, horizon_max=body.horizon_max, min_rr=0.0)
    try:
        result = await service.run_validation(
            session,
            setup=SetupType(body.setup),
            universe=body.universe,
            start=body.start,
            end=body.end,
            filters=filters,
            slippage_bps=body.slippage_bps,
        )
    except service.UniverseUnavailable as exc:
        return _json(unavailable_validation(body.setup, body.start, body.end, str(exc)))
    return _json(validation_json(result))


@router.post("/options/analyze", summary="Compare listed option structures for an underlying")
async def post_options_analyze(
    session: SessionDep, principal: AuthenticatedDep, body: OptionsIn
) -> Response:
    await scoped_sole_user_id(session, principal.user_id, surface="opportunities")
    analysis = await service.run_options_analysis(
        body.request(), now=_now(), max_quote_age_s=body.quote_rules.max_quote_age_s
    )
    return _json(options_json(body.symbol, analysis))


@router.post("/agui", summary="AG-UI run: conversational, read-only scanning")
async def post_agui(request: Request, principal: AuthenticatedDep) -> StreamingResponse:
    try:
        raw = json.loads(await request.body())
        run = agui.RunAgentInput.model_validate(raw)
    except (ValueError, TypeError) as exc:
        raise Problem(ProblemType.BAD_REQUEST, f"Invalid AG-UI RunAgentInput: {exc}") from exc
    user_id = principal.require_user()
    if not agui.claim_thread(run.thread_id, user_id):
        raise Problem(ProblemType.NOT_FOUND, "No such conversation.")
    return StreamingResponse(
        agui.run(
            run,
            maker=_maker(request),
            user_id=user_id,
            disconnected=request.is_disconnected,
        ),
        media_type=SSE_MEDIA_TYPE,
        headers={"Cache-Control": "no-store"},
    )
