"""``/overlap`` — the day's candidates across the sleeves, one row per stock.

    GET    /overlap?scope=actionable|all  every name on any strategy's latest session, with
                                          each strategy's own fact about it, the screens it is
                                          on, the swing feed's catalyst link and earnings date,
                                          and the live mark
    PUT    /overlap/tags                  a person's correction of a headline's tag
    DELETE /overlap/tags?headline=        the correction removed; the readers' word shows again
    GET    /overlap/tags/export           every correction, as NDJSON — the fine-tuning set
    POST   /overlap/catalyst-scan         read every listed name's filings now, then wake Laya
    GET    /overlap/catalyst-scan         that scan's progress

ONE READ, AND A CORRECTION THAT MOVES NOTHING
---------------------------------------------
`/build/overlap` computed its membership in the web app from three separate page reads and
showed symbols only. This route serves the same membership from the same stored tables in one
read, with the facts the page used to send a person away for. It intersects; it does not scan,
rank, size or order. ``baskfy_api.overlap`` names no broker, no plan and no flag, and
``services/api/tests/test_overlap_readonly.py`` asserts that over the source and the OpenAPI
document, the way the sleeves' own read-only suites do.

The two writes are the one money-free thing this page was always going to need
(`baskfy_core.catalyst_tags`: "corrections collected against [the baseline], and only then a
fine-tuned model"). A correction is a label on the tag — display context that never reached a
rank, a size or an order — stored in one table (`catalyst_tag_correction`) so the chip shows the
person's word and the export can train on it. `test_overlap_readonly.py` names exactly these two
verbs on exactly this path and nothing else.

WHOSE SCANS THEY ARE
--------------------
The strategy tables are the sole tenant's. A caller who is not the sole tenant is not refused
the read — their screens are still theirs — but the strategies are not read for them and the
payload says so (``strategies_read: false``), rather than answering an empty page that reads as
"no candidates today". The instrument page's ``/appearances`` makes the same choice. The writes
and the export **are** refused to anyone but the sole tenant, the way `/swing`'s are: the
corrections are one person's labels.

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

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from baskfy_api import overlap as overlap_service
from baskfy_api import overlap_scan
from baskfy_api.auth import AuthenticatedDep
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.db import SessionDep
from baskfy_api.live_prices import live_marks_for_symbols
from baskfy_api.problems import Problem, ProblemType, not_found
from baskfy_core.catalyst_tags import SOURCE_CORRECTED, CatalystTag, EventType, ReviewPriority
from baskfy_core.screener import canonical_json

router = APIRouter(prefix="/overlap", tags=["overlap"])

JSON_MEDIA_TYPE: Final = "application/json"
#: The export: one JSON document per line, the shape fine-tuning loaders read directly.
NDJSON_MEDIA_TYPE: Final = "application/x-ndjson"


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
    """The word on the headline (`baskfy_core.catalyst_tags`).

    Display context on a candidate row and nothing more: it is not read by any rank, filter,
    size or order path, and the page labels it so. ``source`` names what produced it — ``rules``
    for the keyword baseline, ``laya`` for the model when it was sure, ``corrected`` for a
    person's word, which wins over both. ``matched`` is why: the phrases that decided the type.
    """

    event_type: EventType
    review_priority: ReviewPriority
    matched: list[str]
    source: str
    #: The model's probability for its choice; null for the rules and for a correction.
    confidence: float | None
    #: ``"<source>:<event_type>"`` of the other reader when the two disagreed — the correction
    #: seed — or, on a corrected tag, of the reader the person overruled.
    disagrees_with: str | None
    #: ``source == "corrected"``: a person's word, not a reader's.
    corrected: bool


def _tag_out(tag: CatalystTag) -> OverlapTagOut:
    return OverlapTagOut(
        event_type=tag.event_type,
        review_priority=tag.review_priority,
        matched=list(tag.matched),
        source=tag.source,
        confidence=tag.confidence,
        disagrees_with=tag.disagrees_with,
        corrected=tag.source == SOURCE_CORRECTED,
    )


class OverlapTagCorrectionIn(BaseModel):
    """A person's word on one headline. The headline is the key: the correction applies to
    that exact text wherever it appears, not to one row."""

    headline: str = Field(min_length=1, max_length=2000)
    event_type: EventType
    note: str | None = Field(default=None, max_length=2000)


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


class OverlapLayaOut(BaseModel):
    """Is Laya working: when the sidecar last passed, and on how many of these filings it spoke."""

    last_pass_at: dt.datetime | None
    answered: int
    shown: int
    of: int


class OverlapOut(BaseModel):
    sessions: OverlapSessionsOut
    scope: Literal["actionable", "all"]
    strategies_read: bool
    screens_checked: int
    laya: OverlapLayaOut
    data: list[OverlapRowOut]


def _cache(request: Request) -> Redis | None:
    """The process-wide Redis client, or ``None`` when this deployment has none."""
    client = getattr(request.app.state, "cache", None)
    return client if isinstance(client, Redis) else None


@router.get("", response_model=OverlapOut, summary="Today's candidates across the sleeves")
async def get_overlap(
    request: Request,
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
        session, user_id=user_id, strategies_user_id=sole, scope=scope, cache=_cache(request)
    )
    marks = await live_marks_for_symbols([row.symbol for row in view.rows])
    status = overlap_service.laya_status(
        view.rows, await overlap_service.laya_last_pass(_cache(request))
    )
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
            laya=OverlapLayaOut(
                last_pass_at=status.last_pass_at,
                answered=status.answered,
                shown=status.shown,
                of=status.of,
            ),
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
                            tag=None if row.catalyst_tag is None else _tag_out(row.catalyst_tag),
                        )
                    ),
                )
                for row in view.rows
            ],
        )
    )


# --- Corrections ------------------------------------------------------------------------------


async def _laya_answer(request: Request, headline: str) -> object:
    answers = await overlap_service.laya_answers(_cache(request), [headline])
    return answers.get(headline)


@router.put("/tags", response_model=OverlapTagOut, summary="Correct a headline's tag")
async def put_tag(
    request: Request,
    session: SessionDep,
    principal: AuthenticatedDep,
    payload: OverlapTagCorrectionIn,
) -> Response:
    """Store the person's word on a headline and answer with the tag the page now shows.

    Idempotent on the headline: a second correction updates the one row. What the rules and Laya
    said is recorded beside it at this moment — the training signal. Sole tenant only.
    """
    user_id = await scoped_sole_user_id(session, principal.require_user(), surface="corrections")
    headline = payload.headline.strip()
    if not headline:
        raise Problem(
            ProblemType.BAD_REQUEST,
            "A correction needs a headline to correct.",
            errors=[{"field": "headline", "message": "blank"}],
        )
    tag = await overlap_service.correct_tag(
        session,
        user_id=user_id,
        headline=headline,
        event_type=payload.event_type,
        note=(payload.note or "").strip() or None,
        laya_answer=await _laya_answer(request, headline),
    )
    return _json(_tag_out(tag))


@router.delete(
    "/tags", status_code=status.HTTP_204_NO_CONTENT, summary="Remove a headline's correction"
)
async def delete_tag(
    session: SessionDep,
    principal: AuthenticatedDep,
    headline: Annotated[str, Query(min_length=1, description="the headline, exactly as shown")],
) -> Response:
    """The readers' word shows again. 404 when there was no correction to remove."""
    user_id = await scoped_sole_user_id(session, principal.require_user(), surface="corrections")
    if not await overlap_service.clear_correction(session, user_id=user_id, headline=headline):
        raise not_found("correction", headline)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/tags/export",
    summary="Every correction, as NDJSON",
    response_class=Response,
    responses={200: {"content": {NDJSON_MEDIA_TYPE: {}}}},
)
async def get_tags_export(session: SessionDep, principal: AuthenticatedDep) -> Response:
    """The fine-tuning set: one line per corrected headline, oldest first — the headline, the
    person's label, what each reader said at the time, the note, and when. Sole tenant only."""
    user_id = await scoped_sole_user_id(session, principal.require_user(), surface="corrections")
    records = await overlap_service.corrections_export(session, user_id=user_id)
    lines = [
        canonical_json(
            {
                "headline": record.headline,
                "event_type": record.event_type.value,
                "rules_event_type": (
                    None if record.rules_event_type is None else record.rules_event_type.value
                ),
                "laya_event_type": (
                    None if record.laya_event_type is None else record.laya_event_type.value
                ),
                "laya_confidence": record.laya_confidence,
                "note": record.note,
                "corrected_at": record.corrected_at.isoformat(),
            }
        )
        for record in records
    ]
    return Response(content="".join(f"{line}\n" for line in lines), media_type=NDJSON_MEDIA_TYPE)


# --- Scan filings (the page's Laya scan button) ------------------------------------------------


class OverlapScanOut(BaseModel):
    """The last filings scan (`baskfy_api.overlap_scan`): queued, running, done or failed."""

    state: overlap_scan.ScanState
    scope: Literal["actionable", "all"] | None = None
    #: Names the scan reads, known once the worker has resolved the page; then how many are done.
    total: int | None = None
    done: int = 0
    announcements: int | None = None
    earnings_dates: int | None = None
    rows_written: int | None = None
    #: Names whose NSE read failed; skipped, counted, named — the feed's fail-soft rule.
    failed: list[str] = Field(default_factory=list)
    error: str | None = None
    queued_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


def _scan_out(status_payload: dict[str, object]) -> Response:
    return _json(OverlapScanOut.model_validate(status_payload))


@router.post(
    "/catalyst-scan",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OverlapScanOut,
    summary="Read every listed name's filings now",
)
async def post_catalyst_scan(
    request: Request,
    session: SessionDep,
    principal: AuthenticatedDep,
    scope: Annotated[
        Literal["actionable", "all"], Query(description="the rows the page is showing")
    ] = "actionable",
) -> Response:
    """Queue one scan of the names the page lists; Laya tags what it finds. Sole tenant only.

    Refused (409) while one is running and during 09:10-09:30 IST, when the NSE limiter belongs
    to the swing monitor. Reads filings and result dates into the catalyst feed: display context,
    never a rank, a size or an order.
    """
    user_id = await scoped_sole_user_id(session, principal.require_user(), surface="corrections")
    queued = await overlap_scan.start_scan(
        _cache(request),
        getattr(request.app.state, "task_queue", None),
        user_id=user_id,
        scope=scope,
        now=dt.datetime.now(tz=dt.UTC),
    )
    queued.pop("task_id", None)
    response = _scan_out(queued)
    response.status_code = status.HTTP_202_ACCEPTED
    return response


@router.get("/catalyst-scan", response_model=OverlapScanOut, summary="The filings scan's progress")
async def get_catalyst_scan(
    request: Request, session: SessionDep, principal: AuthenticatedDep
) -> Response:
    """``state: idle`` when no scan has run; otherwise the last one's progress. Sole tenant only."""
    user_id = await scoped_sole_user_id(session, principal.require_user(), surface="corrections")
    return _scan_out(await overlap_scan.read_status(_cache(request), user_id))
