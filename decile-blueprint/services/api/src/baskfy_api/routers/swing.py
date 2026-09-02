"""``/swing/*`` — the swing book's read surfaces, plus its one settings write (SW4).

    GET   /swing/setups?date&setup&status        the day's candidates, with the funnel
    GET   /swing/setups/{instrument_id}/bars     130 closes for the row's mini chart
    GET   /swing/market?from&to                  breadth, the gate and the rung over time
    GET   /swing/sectors?date                    the strip: sector breadth, and today's counts
    GET   /swing/config                          the settings, the ceilings, and the rung
    PATCH /swing/config                          change a setting, audited, bounded

READ-ONLY EXCEPT FOR ONE ROUTE, AND THAT ROUTE MOVES NO MONEY
-------------------------------------------------------------
``docs/swing/02-scope-and-gating.md`` Track A: the web app's `/swing` hub is "read-only — the
same rule as `/baskets`: every mutation on it is a 405 except watchlist edits, notes and the
catalyst field (they change no money)". Track C §4 is blunter: "`apps/web` gets no route under
`/swing` that can reach the gateway."

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
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import swing as swing_service
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
        ProblemType.INVALID_SCREEN_DEFINITION,
        f"{value!r} is not a {field}.",
        errors=[{"field": field, "message": f"choose one of {sorted(allowed)}"}],
    )


# --- response models ---------------------------------------------------------


class SetupOut(BaseModel):
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


class SetupsOut(BaseModel):
    as_of: dt.date | None
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
    data: list[SetupOut]


class BarOut(BaseModel):
    date: dt.date
    close: Decimal
    ma_fast: Decimal | None
    ma_slow: Decimal | None


class BarsOut(BaseModel):
    instrument_id: int
    symbol: str
    #: **Adjusted** closes: the chart shows the shape, and a raw series with a split in it shows
    #: a cliff that never happened. The tradeable levels beside it are exchange prices.
    adjusted: bool = True
    data: list[BarOut]


class MarketDayOut(BaseModel):
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


class MarketOut(BaseModel):
    data: list[MarketDayOut]


class SectorOut(BaseModel):
    slug: str
    pct_above_ma_slow: float
    members: int
    candidates: int
    #: In the top three, and therefore worth `+5` on a candidate's score (`04` §2.6).
    hot: bool


class SectorsOut(BaseModel):
    as_of: dt.date | None
    data: list[SectorOut]


# --- routes ------------------------------------------------------------------


@router.get("/setups", response_model=SetupsOut, summary="The day's swing candidates")
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
        SetupsOut(
            as_of=page.as_of,
            gate=page.gate,
            exposure_level=page.exposure_level,
            max_open_positions=page.max_open_positions,
            max_exposure_pct=page.max_exposure_pct,
            new_entries_allowed=page.new_entries_allowed,
            funnel=page.funnel,
            data=[
                SetupOut(
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
                )
                for row in page.rows
            ],
        )
    )


@router.get(
    "/setups/{instrument_id}/bars", response_model=BarsOut, summary="The mini chart's series"
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
        BarsOut(
            instrument_id=instrument_id,
            symbol=str(symbol),
            data=[
                BarOut(
                    date=point.date,
                    close=point.close,
                    ma_fast=point.ma_fast,
                    ma_slow=point.ma_slow,
                )
                for point in points
            ],
        )
    )


@router.get("/market", response_model=MarketOut, summary="Breadth, the gate and the rung")
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
                ProblemType.INVALID_SCREEN_DEFINITION,
                "`from` is after `to`.",
                errors=[{"field": "from", "message": "the range runs forwards"}],
            )
        if date_to - date_from > swing_service.MAX_MARKET_SPAN:
            raise Problem(
                ProblemType.INVALID_SCREEN_DEFINITION,
                f"that span is wider than {swing_service.MAX_MARKET_SPAN.days} days.",
                errors=[{"field": "from", "message": "ask for a narrower range"}],
            )
    rows = await swing_service.market_history(
        session, user_id=user_id, start=date_from, end=date_to
    )
    return _json(
        MarketOut(
            data=[
                MarketDayOut(
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
                )
                for row in rows
            ]
        )
    )


@router.get("/sectors", response_model=SectorsOut, summary="The sector strip")
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
        SectorsOut(
            as_of=as_of,
            data=[
                SectorOut(
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
    now: dt.datetime | None = None,
) -> Response:
    """The one write on this surface, and it moves no money.

    Seven numbers. No order path, no broker, no exposure rung: `SwingConfigPatch` forbids unknown
    fields, so `{"exposure_level": 3}` is refused rather than ignored — a caller who asked to
    climb the ladder is told the field does not exist here, instead of being answered `200` and
    believing they had.

    A value above its ceiling answers **422 `setting-above-ceiling`** naming the ceiling and the
    environment variable that sets it, and the refusal is atomic: a two-field patch that crosses a
    ceiling on the second field changes neither.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    await _config_or_404(session, user_id)
    row = await apply_patch(
        session,
        user_id=user_id,
        patch=patch,
        ceilings=SwingCeilings.from_settings(settings),
        changed_by=f"user:{user_id}",
        now=now or dt.datetime.now(tz=dt.UTC),
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


async def _config_or_404(session: AsyncSession, user_id: int) -> SwConfig:
    try:
        return await read_config(session, user_id)
    except SwingConfigNotSeeded as exc:
        raise Problem(
            ProblemType.PIPELINE_DEGRADED,
            "The swing book is not set up on this deployment; run `make seed`.",
        ) from exc
