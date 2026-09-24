"""``/overlap`` — the day's candidates across the sleeves, one row per stock.

    GET /overlap?scope=actionable|all     every name on any strategy's latest session, with
                                          each strategy's own fact about it, the screens it is
                                          on, the swing feed's catalyst link and earnings date,
                                          and the live mark

ONE ROUTE, ONE VERB, AND IT MOVES NOTHING
-----------------------------------------
`/build/overlap` computed its membership in the web app from three separate page reads and
showed symbols only. This route serves the same membership from the same stored tables in one
read, with the facts the page used to send a person away for. It intersects; it does not scan,
rank, size or order. ``baskfy_api.overlap`` names no broker, no plan and no flag, and
``services/api/tests/test_overlap_readonly.py`` asserts that over the source and the OpenAPI
document, the way the sleeves' own read-only suites do.

WHOSE SCANS THEY ARE
--------------------
The strategy tables are the sole tenant's. A caller who is not the sole tenant is not refused —
their screens are still theirs — but the strategies are not read for them and the payload says
so (``strategies_read: false``), rather than answering an empty page that reads as "no
candidates today". The instrument page's ``/appearances`` makes the same choice.

WHICH DAY
---------
Each strategy's own latest session, named separately. They differ when a nightly step was
skipped, and a single "as of" over three sessions would be a lie on exactly the day it matters.
The live mark is the shared overlay every screen page carries (``/meta/live-marks``): close,
plus Kite's last price while a session exists. Ranks, states and the sessions do not move.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel

from baskfy_api import overlap as overlap_service
from baskfy_api.auth import AuthenticatedDep
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.db import SessionDep
from baskfy_api.live_prices import live_marks_for_symbols
from baskfy_api.problems import Problem
from baskfy_core.catalyst_tags import EventType, ReviewPriority
from baskfy_core.screener import canonical_json

router = APIRouter(prefix="/overlap", tags=["overlap"])

JSON_MEDIA_TYPE: Final = "application/json"


def _json(model: BaseModel) -> Response:
    """The canonical encoder: ``Decimal`` as stored, not through ``float`` (house rule 8)."""
    return Response(
        content=canonical_json(model.model_dump(mode="python", by_alias=True)),
        media_type=JSON_MEDIA_TYPE,
    )


class OverlapStrategyOut(BaseModel):
    """One strategy's fact about the row, in that strategy's own words."""

    strategy: overlap_service.Strategy
    name: str
    #: Where the fact came from: the strategy's own page.
    ref: str
    as_of: dt.date
    detail: str
    #: The strategy's plan builder could take the row as it stands. Display only.
    actionable: bool


class OverlapScreenOut(BaseModel):
    name: str
    public_id: str
    is_template: bool
    as_of: dt.date
    rank: int | None
    of: int | None
    definition_changed: bool


class OverlapTagOut(BaseModel):
    """The rules baseline's word on the headline (`baskfy_core.catalyst_tags`).

    Display context on a candidate row and nothing more: it is not read by any rank, filter,
    size or order path, and the page labels it so. ``source`` names what produced it — ``rules``
    today; a model, when one is fine-tuned and shadowed, writes its own name here and the wire
    shape does not change. ``matched`` is why: the phrases that decided the type.
    """

    event_type: EventType
    review_priority: ReviewPriority
    matched: list[str]
    source: str


class OverlapCatalystOut(BaseModel):
    """SW11B (A3): the swing feed's newest link and the earnings date — never the filing."""

    headline: str | None
    published_at: dt.datetime | None
    url: str | None
    earnings_date: dt.date | None
    #: Present exactly when there is a headline to read.
    tag: OverlapTagOut | None


class OverlapRowOut(BaseModel):
    instrument_id: int
    symbol: str
    name: str
    #: An exchange print, as a decimal — the first the strategies offer.
    close: Decimal | None
    #: Kite last price while a session exists; absent otherwise, and the page keeps the close.
    last_price: Decimal | None
    strategy_count: int
    actionable: bool
    strategies: list[OverlapStrategyOut]
    screens: list[OverlapScreenOut]
    catalyst: OverlapCatalystOut | None


class OverlapSessionsOut(BaseModel):
    swing: dt.date | None
    volume_breakout: dt.date | None
    three_weeks_tight: dt.date | None


class OverlapOut(BaseModel):
    sessions: OverlapSessionsOut
    scope: Literal["actionable", "all"]
    strategies_read: bool
    screens_checked: int
    data: list[OverlapRowOut]


@router.get("", response_model=OverlapOut, summary="Today's candidates across the sleeves")
async def get_overlap(
    session: SessionDep,
    principal: AuthenticatedDep,
    scope: Annotated[
        Literal["actionable", "all"],
        Query(description="rows a strategy could act on (default), or every row the scans wrote"),
    ] = "actionable",
) -> Response:
    """Read-only, and nothing is re-run. See the module docstring for whose scans and which day."""
    user_id = principal.require_user()
    try:
        sole: int | None = await scoped_sole_user_id(session, user_id, surface="overlap")
    except Problem:
        sole = None
    view = await overlap_service.overlap(
        session, user_id=user_id, strategies_user_id=sole, scope=scope
    )
    marks = await live_marks_for_symbols([row.symbol for row in view.rows])
    return _json(
        OverlapOut(
            sessions=OverlapSessionsOut(
                swing=view.sessions[overlap_service.Strategy.SWING],
                volume_breakout=view.sessions[overlap_service.Strategy.VOLUME_BREAKOUT],
                three_weeks_tight=view.sessions[overlap_service.Strategy.THREE_WEEKS_TIGHT],
            ),
            scope=scope,
            strategies_read=view.strategies_read,
            screens_checked=view.screens_checked,
            data=[
                OverlapRowOut(
                    instrument_id=row.instrument_id,
                    symbol=row.symbol,
                    name=row.name,
                    close=row.close,
                    last_price=marks.get(row.symbol.strip().upper()),
                    strategy_count=row.strategy_count,
                    actionable=row.actionable,
                    strategies=[
                        OverlapStrategyOut(
                            strategy=hit.strategy,
                            name=hit.name,
                            ref=hit.ref,
                            as_of=hit.as_of,
                            detail=hit.detail,
                            actionable=hit.actionable,
                        )
                        for hit in row.strategies
                    ],
                    screens=[
                        OverlapScreenOut(
                            name=screen.name,
                            public_id=screen.public_id,
                            is_template=screen.is_template,
                            as_of=screen.as_of,
                            rank=screen.rank,
                            of=screen.of,
                            definition_changed=screen.definition_changed,
                        )
                        for screen in row.screens
                    ],
                    catalyst=(
                        None
                        if row.catalyst is None or row.catalyst.empty
                        else OverlapCatalystOut(
                            headline=row.catalyst.headline,
                            published_at=row.catalyst.published_at,
                            url=row.catalyst.url,
                            earnings_date=row.catalyst.earnings_date,
                            tag=(
                                None
                                if row.catalyst_tag is None
                                else OverlapTagOut(
                                    event_type=row.catalyst_tag.event_type,
                                    review_priority=row.catalyst_tag.review_priority,
                                    matched=list(row.catalyst_tag.matched),
                                    source=row.catalyst_tag.source,
                                )
                            ),
                        )
                    ),
                )
                for row in view.rows
            ],
        )
    )
