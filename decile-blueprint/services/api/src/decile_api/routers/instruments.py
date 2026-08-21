"""``/instruments/*`` — docs/07 §Instruments (Prompt 10 deliverable 1).

    GET /instruments?search=…&limit=…                → typeahead
    GET /instruments/{symbol}                        → factsheet payload
    GET /instruments/{symbol}/history?from&to&field= → sparkline series
    GET /instruments/{symbol}/corporate-actions
    GET /instruments/{symbol}/rank-history?screen=…  → this stock's rank over time in a screen

`/listings` is the sixth entry in that block and belongs to Prompt 11.

Everything here is a **read of published data**, so every response carries the `as_of` and
`data_version` docs/07 §Conventions requires, and every one of them resolves that date the same way
a screen does (docs/06 §step 1) — a factsheet that quietly used an unpublished row would be the
half-written day the document rules out.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Final

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel
from sqlalchemy import select

from decile_api.db import SessionDep
from decile_api.http_cache import snapshot_headers
from decile_api.instruments import (
    InstrumentNotFound,
    build_factsheet,
    load_instrument,
    search_instruments,
)
from decile_api.problems import Problem, ProblemType, not_found
from decile_api.schemas import (
    CorporateActionOut,
    CorporateActionsOut,
    FactsheetOut,
    HistoryPointOut,
    InstrumentHistoryOut,
    InstrumentHitOut,
    InstrumentSearchOut,
    RankHistoryOut,
    RankPointOut,
)
from decile_api.screener import current_data_version, resolve_as_of
from decile_core.models import CorporateAction, FactorDaily, OhlcvDaily, Screen, ScreenRun
from decile_core.screener import canonical_json

router = APIRouter(prefix="/instruments", tags=["instruments"])

JSON_MEDIA_TYPE: Final = "application/json"


def _json(
    model: BaseModel, *, as_of: dt.date | None = None, data_version: int | None = None
) -> Response:
    """Serialise with the screener's canonical encoder rather than Pydantic's.

    CLAUDE.md house rule 8 — "round at write time … the API, the UI and the CSV export can never
    disagree". Pydantic's JSON mode renders `Decimal` through `float`, which turns the stored
    `13.00` into `13.0`; `canonical_json` emits the stored digits verbatim, exactly as
    `/screens/{id}/run` already does. Same numbers on the factsheet as in the table they came
    from.
    """
    return Response(
        content=canonical_json(model.model_dump(mode="python", by_alias=True)),
        media_type=JSON_MEDIA_TYPE,
        # Prompt 16 deliverable 3: the ETag `decile_api.http_cache` attaches is derived from
        # `data_version`, and it reads it from this header.
        headers=snapshot_headers(as_of, data_version),
    )


DEFAULT_SEARCH_LIMIT: Final = 8
MAX_SEARCH_LIMIT: Final = 50

#: The widest span `/history` will answer in one call. A sparkline wants a year; the whole series
#: for 2,300 instruments is not a sparkline, it is a data export.
MAX_HISTORY_DAYS: Final = 366 * 15

#: Fields `/history` can serve, and which table holds each one.
#:
#: `close` and `close_raw` are the bar series (docs/02 rule 2: adjusted for maths, the exchange
#: print for display). Everything else is a stored factor. A field outside this map is refused
#: rather than interpolated into SQL — the same whitelist rule docs/06 applies to sort keys.
HISTORY_FIELDS: Final[dict[str, str]] = {
    "close": "ohlcv",
    "close_raw": "ohlcv",
    "volume": "ohlcv",
    "volume_raw": "ohlcv",
    "ret_12m": "factor",
    "sharpe_12m": "factor",
    "vol_12m": "factor",
    "rsi_12m": "factor",
    "beta_12m": "factor",
    "pe": "factor",
    "marketcap_cr": "factor",
    "ma_200": "factor",
    "median_vol_12m": "factor",
}

DEFAULT_HISTORY_FIELD: Final = "close"


@router.get("", response_model=InstrumentSearchOut, summary="Search instruments")
async def search(
    session: SessionDep,
    search: Annotated[str, Query(min_length=1, max_length=64, description="Symbol or name.")],
    limit: Annotated[int, Query(ge=1, le=MAX_SEARCH_LIMIT)] = DEFAULT_SEARCH_LIMIT,
) -> InstrumentSearchOut:
    """docs/07: the ⌘K typeahead. Open to anonymous callers — it names public securities."""
    hits = await search_instruments(session, search, limit)
    return InstrumentSearchOut(
        data=[InstrumentHitOut(symbol=hit.symbol, name=hit.name, series=hit.series) for hit in hits]
    )


@router.get("/{symbol}", response_model=FactsheetOut, summary="Instrument factsheet")
async def factsheet(
    symbol: str,
    session: SessionDep,
    as_of: Annotated[dt.date | None, Query()] = None,
) -> Response:
    """docs/01 §5's eleven blocks, in the reference product's order."""
    resolution = await resolve_as_of(session, as_of)
    data_version = await current_data_version(session)
    try:
        sheet = await build_factsheet(session, symbol, resolution.as_of, data_version)
    except InstrumentNotFound as exc:
        raise not_found("instrument", symbol) from exc
    return _json(
        FactsheetOut.model_validate(sheet, from_attributes=True),
        as_of=resolution.as_of,
        data_version=data_version,
    )


@router.get("/{symbol}/history", response_model=InstrumentHistoryOut, summary="One field's series")
async def history(
    symbol: str,
    session: SessionDep,
    field: Annotated[str, Query()] = DEFAULT_HISTORY_FIELD,
    from_: Annotated[dt.date | None, Query(alias="from")] = None,
    to: Annotated[dt.date | None, Query()] = None,
) -> Response:
    """docs/07: "sparkline series"."""
    source = HISTORY_FIELDS.get(field)
    if source is None:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            f"{field!r} is not a series this endpoint serves.",
            errors=[{"field": "field", "message": f"choose one of {sorted(HISTORY_FIELDS)}"}],
        )

    resolution = await resolve_as_of(session, to)
    end = resolution.as_of
    start = from_ or (end - dt.timedelta(days=365))
    if start > end:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            f"`from` ({start.isoformat()}) is after `to` ({end.isoformat()}).",
            errors=[{"field": "from", "message": "must be on or before `to`"}],
        )
    if (end - start).days > MAX_HISTORY_DAYS:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            f"Range exceeds the {MAX_HISTORY_DAYS}-day maximum for one request.",
            errors=[{"field": "from", "message": f"at most {MAX_HISTORY_DAYS} days before `to`"}],
        )

    try:
        instrument = await load_instrument(session, symbol)
    except InstrumentNotFound as exc:
        raise not_found("instrument", symbol) from exc

    if source == "ohlcv":
        table = OhlcvDaily.__table__
        column = table.c[field]
    else:
        table = FactorDaily.__table__
        column = table.c[field]

    rows = (
        await session.execute(
            select(table.c.date, column.label("value"))
            .where(
                table.c.instrument_id == instrument.id,
                table.c.date >= start,
                table.c.date <= end,
            )
            .order_by(table.c.date.asc())
        )
    ).all()

    return _json(
        InstrumentHistoryOut(
            symbol=instrument.symbol,
            field=field,
            to=end,
            points=[HistoryPointOut(date=row[0], value=row[1]) for row in rows],
            **{"from": start},
        ),
        as_of=end,
        data_version=await current_data_version(session),
    )


@router.get(
    "/{symbol}/corporate-actions",
    response_model=CorporateActionsOut,
    summary="Corporate actions",
)
async def corporate_actions(symbol: str, session: SessionDep) -> Response:
    """docs/01 §5 block 11. Newest first — the recent ones are the ones that explain a price gap."""
    try:
        instrument = await load_instrument(session, symbol)
    except InstrumentNotFound as exc:
        raise not_found("instrument", symbol) from exc

    rows = (
        (
            await session.execute(
                select(CorporateAction)
                .where(CorporateAction.instrument_id == instrument.id)
                .order_by(CorporateAction.ex_date.desc())
            )
        )
        .scalars()
        .all()
    )
    return _json(
        CorporateActionsOut(
            symbol=instrument.symbol,
            data=[
                CorporateActionOut(
                    action_type=row.action_type,
                    ex_date=row.ex_date,
                    ratio_from=row.ratio_from,
                    ratio_to=row.ratio_to,
                    amount=row.amount,
                )
                for row in rows
            ],
        )
    )


@router.get(
    "/{symbol}/rank-history", response_model=RankHistoryOut, summary="Rank over time in a screen"
)
async def rank_history(
    symbol: str,
    session: SessionDep,
    screen: Annotated[str, Query(description="The screen's public id.")],
    limit: Annotated[int, Query(ge=1, le=500)] = 120,
) -> Response:
    """docs/07: "this stock's rank over time in a screen".

    Read from ``screen_run.results``, which docs/04 defines as "[{rank, instrument_id,
    factor_value}]" — the audit trail of what the screen actually returned on each date. It is
    *not* recomputed: the point of a rank history is what the screen said at the time, and
    re-running today's engine over past data would answer a different question.

    That also means the series is only as long as the screen's run history. A screen run once has
    one point, and the response says so rather than padding it.
    """
    try:
        instrument = await load_instrument(session, symbol)
    except InstrumentNotFound as exc:
        raise not_found("instrument", symbol) from exc

    found = (
        await session.execute(select(Screen).where(Screen.public_id == screen))
    ).scalar_one_or_none()
    if found is None:
        raise not_found("screen", screen)

    runs = (
        (
            await session.execute(
                select(ScreenRun)
                .where(ScreenRun.screen_id == found.id)
                .order_by(ScreenRun.as_of.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    points: list[RankPointOut] = []
    for run in runs:
        for entry in run.results:
            if entry.get("instrument_id") == instrument.id:
                rank = entry.get("rank")
                if isinstance(rank, int):
                    points.append(
                        RankPointOut(as_of=run.as_of, rank=rank, result_count=run.result_count)
                    )
                break

    points.sort(key=lambda point: point.as_of)
    return _json(
        RankHistoryOut(
            symbol=instrument.symbol,
            screen_public_id=found.public_id,
            screen_name=found.name,
            data=points,
        )
    )
