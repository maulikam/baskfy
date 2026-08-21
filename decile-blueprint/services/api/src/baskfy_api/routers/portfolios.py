"""``/portfolios/*`` — docs/07 §"Portfolios & rebalance" (Prompt 14 deliverables 1, 2 and 4).

docs/07 lists six routes:

```
GET    /portfolios
POST   /portfolios                      { name, holdings:[{symbol, quantity?, avg_price?}] }
POST   /portfolios/import-csv           multipart; returns parse report + unmatched symbols
GET    /portfolios/{id}
PUT    /portfolios/{id}/holdings
POST   /portfolios/{id}/rebalance
```

Four are added here, each recorded in ``docs/DECISIONS.md`` §14:

* ``PATCH /portfolios/{id}`` and ``DELETE /portfolios/{id}`` — Prompt 14 §1 asks for "Portfolio
  CRUD" and docs/07's list has no update or delete. A portfolio a user can create and never rename
  or remove is not CRUD.
* ``GET /portfolios/sample-csv`` — docs/01 §8 and docs/08 both require a downloadable sample.
* ``GET /portfolios/{id}/rebalances`` — Prompt 14 §4's history has to be readable to be worth
  persisting.

``{id}`` is the numeric ``portfolio.id``, not a public id: docs/04 gives ``portfolio`` no
``public_id`` column, unlike ``screen``. Not-yours is a 404 for the same reason it is on screens —
a 403 confirms the id exists.

Entitlements
------------
docs/07 §Entitlements gates seven things and a portfolio is none of them, so the tracker is open
to any signed-in account. The *screen* a rebalance runs still carries its own gates: the ₹0 tier's
universe restriction, and ``historical_ranks`` when the caller asks for a past ``as_of``.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, File, Header, Query, Request, Response, UploadFile, status
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import idempotency
from baskfy_api import portfolios as service
from baskfy_api.auth import AuthenticatedDep, Principal
from baskfy_api.db import SessionDep
from baskfy_api.entitlements import EntitlementsDep, Feature
from baskfy_api.problems import Problem, ProblemType, not_found, stale_data_version
from baskfy_api.schemas import (
    DEFAULT_PAGE_SIZE,
    CandidateOut,
    HoldingIn,
    HoldingOut,
    HoldingsIn,
    ImportReportOut,
    ImportRowOut,
    Limit,
    PortfolioCreate,
    PortfolioListOut,
    PortfolioOut,
    PortfolioRename,
    PortfolioSummaryOut,
    PortfolioWriteOut,
    RebalanceHistoryPage,
    RebalanceIn,
    RebalanceNameOut,
    RebalanceOut,
    RebalancePayload,
    RebalanceScreenOut,
    RebalanceSummaryOut,
    SkippedRowOut,
    TargetWeightOut,
)
from baskfy_api.screener import current_data_version
from baskfy_core.models import (
    Instrument,
    Portfolio,
    PortfolioHolding,
    PortfolioRebalance,
    Screen,
)
from baskfy_core.models.base import JsonObject
from baskfy_core.portfolio_csv import (
    MAX_IMPORT_ROWS,
    SAMPLE_CSV,
    SAMPLE_CSV_FILENAME,
    CsvParseError,
    MatchStatus,
    ParsedCsv,
    ParsedHolding,
    SkippedRow,
    SkipReason,
    classify_symbol,
    normalise_symbol,
    parse_portfolio_csv,
)
from baskfy_core.rebalance import RebalancePlan, RebalanceRow, TargetWeight
from baskfy_core.screen_definition import ScreenDefinition

log = logging.getLogger(__name__)

router = APIRouter(prefix="/portfolios", tags=["portfolios"])

#: An upload larger than this is not a portfolio. 500 rows of `SYMBOL,qty,price` is well under
#: 32 KiB; the margin is for broker exports with fifteen columns of their own.
MAX_UPLOAD_BYTES = 1_048_576

CSV_MEDIA_TYPE = "text/csv; charset=utf-8"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cache(request: Request) -> Redis | None:
    client = getattr(request.app.state, "cache", None)
    return client if isinstance(client, Redis) else None


async def _load(session: AsyncSession, portfolio_id: int, principal: Principal) -> Portfolio:
    try:
        return await service.load_portfolio(session, portfolio_id, principal.require_user())
    except service.PortfolioNotFound as exc:
        raise not_found("portfolio", str(portfolio_id)) from exc


async def _holdings_out(session: AsyncSession, portfolio_id: int) -> list[HoldingOut]:
    rows = (
        await session.execute(
            select(
                PortfolioHolding.instrument_id,
                PortfolioHolding.quantity,
                PortfolioHolding.avg_price,
                PortfolioHolding.added_on,
                Instrument.symbol,
                Instrument.name,
                Instrument.delisted_on,
            )
            .join(Instrument, Instrument.id == PortfolioHolding.instrument_id)
            .where(PortfolioHolding.portfolio_id == portfolio_id)
            .order_by(Instrument.symbol)
        )
    ).all()
    return [
        HoldingOut(
            instrument_id=row.instrument_id,
            symbol=row.symbol,
            name=row.name,
            quantity=row.quantity,
            avg_price=row.avg_price,
            added_on=row.added_on,
            delisted_on=row.delisted_on,
        )
        for row in rows
    ]


async def _portfolio_out(session: AsyncSession, portfolio: Portfolio) -> PortfolioOut:
    return PortfolioOut(
        id=portfolio.id,
        name=portfolio.name,
        created_at=portfolio.created_at,
        holdings=await _holdings_out(session, portfolio.id),
    )


def _rows_from_body(holdings: Sequence[HoldingIn]) -> ParsedCsv:
    """A JSON ``holdings`` array, put through the same shape rules as an uploaded file.

    ``POST /portfolios`` and ``POST /portfolios/import-csv`` then differ only in how the symbols
    arrived, and both answer with the same report. A client that posts ``["infy "]`` gets the same
    normalisation — and the same "ambiguous" answer — as one that uploads it.
    """
    parsed: list[ParsedHolding] = []
    skipped: list[SkippedRow] = []
    seen: set[str] = set()
    for index, holding in enumerate(holdings, start=1):
        token, issues = normalise_symbol(holding.symbol)
        if not token:
            skipped.append(SkippedRow(line_number=index, reason=SkipReason.NO_SYMBOL))
            continue
        if token in seen:
            skipped.append(SkippedRow(line_number=index, reason=SkipReason.DUPLICATE, raw=token))
            continue
        seen.add(token)
        parsed.append(
            ParsedHolding(
                line_number=index,
                raw_symbol=holding.symbol,
                symbol=token,
                shape=classify_symbol(token),
                quantity=holding.quantity,
                avg_price=holding.avg_price,
                issues=issues,
            )
        )
    return ParsedCsv(rows=tuple(parsed), skipped=tuple(skipped))


async def _apply(session: AsyncSession, portfolio: Portfolio, parsed: ParsedCsv) -> ImportReportOut:
    """Resolve every parsed symbol, replace the holdings with the matched ones, and report.

    Ambiguous and unmatched symbols are **not** imported and **not** dropped quietly: they are in
    the report with a status and a reason, which is the whole of Prompt 14 §1.
    """
    resolved = await service.resolve_symbols(session, service.lookup_symbols(parsed.rows))
    rows: list[ImportRowOut] = []
    matched: list[tuple[int, Decimal | None, Decimal | None]] = []
    for row in parsed.rows:
        resolution = service.resolution_for(row, resolved)
        candidate = resolution.candidates[0] if resolution.candidates else None
        rows.append(
            ImportRowOut(
                line=row.line_number,
                raw_symbol=row.raw_symbol,
                symbol=row.symbol,
                status=resolution.status,
                reason=resolution.reason,
                instrument_id=resolution.instrument_id,
                name=(
                    candidate.name
                    if resolution.status is MatchStatus.MATCHED and candidate is not None
                    else None
                ),
                candidates=[
                    CandidateOut(
                        instrument_id=item.instrument_id,
                        symbol=item.symbol,
                        name=item.name,
                        series=item.series,
                    )
                    for item in resolution.candidates
                ]
                if resolution.status is MatchStatus.AMBIGUOUS
                else [],
                issues=list(row.issues),
                matched_via_alias=resolution.matched_via_alias,
                quantity=row.quantity,
                avg_price=row.avg_price,
            )
        )
        instrument_id = resolution.instrument_id
        if instrument_id is not None:
            matched.append((instrument_id, row.quantity, row.avg_price))

    imported = await service.replace_holdings(
        session, portfolio.id, matched, added_on=dt.date.today()
    )
    return ImportReportOut(
        total_lines=parsed.total_lines,
        matched=sum(1 for row in rows if row.status is MatchStatus.MATCHED),
        ambiguous=sum(1 for row in rows if row.status is MatchStatus.AMBIGUOUS),
        unmatched=sum(1 for row in rows if row.status is MatchStatus.UNMATCHED),
        skipped=len(parsed.skipped),
        imported=imported,
        rows=rows,
        skipped_rows=[
            SkippedRowOut(line=row.line_number, reason=row.reason, raw=row.raw)
            for row in parsed.skipped
        ],
        ignored_columns=list(parsed.ignored_columns),
    )


# ---------------------------------------------------------------------------
# The sample file
# ---------------------------------------------------------------------------


@router.get("/sample-csv", summary="Download the sample portfolio CSV", response_class=Response)
async def sample_csv() -> Response:
    """docs/01 §8: "(sample CSV provided)"; docs/08: "with a downloadable sample".

    Declared before ``/{portfolio_id}`` so the literal path wins the route match. Anonymous on
    purpose — a file that shows the expected format is useful before anyone signs in.
    """
    return Response(
        content=SAMPLE_CSV,
        media_type=CSV_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{SAMPLE_CSV_FILENAME}"'},
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=PortfolioListOut, summary="List portfolios")
async def list_portfolios(session: SessionDep, principal: AuthenticatedDep) -> PortfolioListOut:
    user_id = principal.require_user()
    counts = (
        select(
            PortfolioHolding.portfolio_id.label("portfolio_id"),
            func.count().label("holdings"),
        )
        .group_by(PortfolioHolding.portfolio_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Portfolio, func.coalesce(counts.c.holdings, 0))
            .outerjoin(counts, counts.c.portfolio_id == Portfolio.id)
            .where(Portfolio.user_id == user_id)
            .order_by(Portfolio.created_at.desc(), Portfolio.id.desc())
        )
    ).all()
    return PortfolioListOut(
        data=[
            PortfolioSummaryOut(
                id=portfolio.id,
                name=portfolio.name,
                created_at=portfolio.created_at,
                holdings_count=int(holdings),
            )
            for portfolio, holdings in rows
        ]
    )


@router.post(
    "",
    response_model=PortfolioWriteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a portfolio",
)
async def create_portfolio(
    request: Request,
    body: PortfolioCreate,
    session: SessionDep,
    principal: AuthenticatedDep,
    idempotency_key: Annotated[str | None, Header(alias=idempotency.HEADER)] = None,
) -> PortfolioWriteOut:
    """docs/07: `POST /portfolios { name, holdings:[{symbol, quantity?, avg_price?}] }`."""
    user_id = principal.require_user()
    if len(body.holdings) > MAX_IMPORT_ROWS:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            f"A portfolio may hold at most {MAX_IMPORT_ROWS} names; "
            f"{len(body.holdings)} were sent.",
        )
    scope = f"portfolio:create:{user_id}"
    replayed = await _replayed(session, principal, _cache(request), scope, idempotency_key)
    if replayed is not None:
        return replayed

    portfolio = Portfolio(user_id=user_id, name=body.name)
    session.add(portfolio)
    await session.flush()
    report = await _apply(session, portfolio, _rows_from_body(body.holdings))
    await idempotency.remember(_cache(request), scope, idempotency_key, str(portfolio.id))
    return PortfolioWriteOut(portfolio=await _portfolio_out(session, portfolio), report=report)


async def _replayed(
    session: AsyncSession,
    principal: Principal,
    cache: Redis | None,
    scope: str,
    key: str | None,
) -> PortfolioWriteOut | None:
    """The portfolio a previous request with this ``Idempotency-Key`` created, if it still exists.

    Same rule as ``routers.screens``: an unresolvable record is treated as no record, because
    answering 404 to a retry is a worse lie than creating the portfolio again.

    The report on a replay is **reconstructed from the stored holdings**, not the original one —
    only the portfolio id is remembered. It therefore says what is in the portfolio now, and a
    symbol the first attempt could not match is absent from it rather than listed as unmatched. A
    retry that needs the original report should read it from the first response.
    """
    remembered = await idempotency.replay(cache, scope, key)
    if remembered is None or not remembered.isdigit():
        return None
    try:
        portfolio = await service.load_portfolio(session, int(remembered), principal.require_user())
    except service.PortfolioNotFound:
        return None
    holdings = await _holdings_out(session, portfolio.id)
    return PortfolioWriteOut(
        portfolio=await _portfolio_out(session, portfolio),
        report=ImportReportOut(
            total_lines=len(holdings),
            matched=len(holdings),
            ambiguous=0,
            unmatched=0,
            skipped=0,
            imported=len(holdings),
            rows=[],
            skipped_rows=[],
            ignored_columns=[],
        ),
    )


@router.post(
    "/import-csv",
    response_model=PortfolioWriteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Import a portfolio from a CSV file",
)
async def import_csv(  # noqa: PLR0913, PLR0917 - FastAPI injects one parameter per dependency
    request: Request,
    session: SessionDep,
    principal: AuthenticatedDep,
    file: Annotated[UploadFile, File(description="A CSV with at least a symbol column")],
    name: Annotated[str | None, Query(max_length=120)] = None,
    portfolio_id: Annotated[int | None, Query()] = None,
    idempotency_key: Annotated[str | None, Header(alias=idempotency.HEADER)] = None,
) -> PortfolioWriteOut:
    """docs/07: "multipart; returns parse report + unmatched symbols".

    ``portfolio_id`` replaces the holdings of an existing portfolio; without it a new one is
    created, named from ``name`` or from the uploaded file.
    """
    user_id = principal.require_user()
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            f"The file is {len(raw)} bytes; the limit is {MAX_UPLOAD_BYTES}.",
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            "The file is not UTF-8 text. Export it as CSV rather than as a spreadsheet.",
        ) from exc
    try:
        parsed = parse_portfolio_csv(text)
    except CsvParseError as exc:
        raise Problem(ProblemType.INVALID_SCREEN_DEFINITION, str(exc)) from exc

    if portfolio_id is not None:
        portfolio = await _load(session, portfolio_id, principal)
    else:
        scope = f"portfolio:import:{user_id}"
        replayed = await _replayed(session, principal, _cache(request), scope, idempotency_key)
        if replayed is not None:
            return replayed
        portfolio = Portfolio(user_id=user_id, name=name or _name_from(file.filename))
        session.add(portfolio)
        await session.flush()
        await idempotency.remember(_cache(request), scope, idempotency_key, str(portfolio.id))

    report = await _apply(session, portfolio, parsed)
    return PortfolioWriteOut(portfolio=await _portfolio_out(session, portfolio), report=report)


def _name_from(filename: str | None) -> str:
    stem = (filename or "").rsplit("/", maxsplit=1)[-1]
    stem = stem[: -len(".csv")] if stem.lower().endswith(".csv") else stem
    return stem.strip()[:120] or "Imported portfolio"


@router.get("/{portfolio_id}", response_model=PortfolioOut, summary="Fetch one portfolio")
async def get_portfolio(
    portfolio_id: int, session: SessionDep, principal: AuthenticatedDep
) -> PortfolioOut:
    return await _portfolio_out(session, await _load(session, portfolio_id, principal))


@router.patch("/{portfolio_id}", response_model=PortfolioOut, summary="Rename a portfolio")
async def rename_portfolio(
    portfolio_id: int,
    body: PortfolioRename,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> PortfolioOut:
    portfolio = await _load(session, portfolio_id, principal)
    portfolio.name = body.name
    await session.flush()
    return await _portfolio_out(session, portfolio)


@router.delete(
    "/{portfolio_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a portfolio"
)
async def delete_portfolio(
    portfolio_id: int, session: SessionDep, principal: AuthenticatedDep
) -> Response:
    portfolio = await _load(session, portfolio_id, principal)
    await session.execute(delete(Portfolio).where(Portfolio.id == portfolio.id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/{portfolio_id}/holdings",
    response_model=PortfolioWriteOut,
    summary="Replace a portfolio's holdings",
)
async def put_holdings(
    portfolio_id: int,
    body: HoldingsIn,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> PortfolioWriteOut:
    """docs/07: `PUT /portfolios/{id}/holdings`."""
    portfolio = await _load(session, portfolio_id, principal)
    if len(body.holdings) > MAX_IMPORT_ROWS:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            f"A portfolio may hold at most {MAX_IMPORT_ROWS} names; "
            f"{len(body.holdings)} were sent.",
        )
    report = await _apply(session, portfolio, _rows_from_body(body.holdings))
    return PortfolioWriteOut(portfolio=await _portfolio_out(session, portfolio), report=report)


# ---------------------------------------------------------------------------
# The rebalance
# ---------------------------------------------------------------------------


@router.post(
    "/{portfolio_id}/rebalance",
    response_model=RebalanceOut,
    summary="Compute a rebalance against a screen",
)
async def rebalance(
    portfolio_id: int,
    body: RebalanceIn,
    session: SessionDep,
    principal: AuthenticatedDep,
    entitlements: EntitlementsDep,
) -> RebalanceOut:
    """docs/07 §"Portfolios & rebalance", and Prompt 14 §2's rank-buffer rule."""
    portfolio = await _load(session, portfolio_id, principal)
    screen = await _visible_screen(session, body.screen_public_id, principal)

    current = await current_data_version(session)
    if body.data_version is not None and body.data_version != current:
        raise stale_data_version(body.data_version, current)

    definition = ScreenDefinition.model_validate(screen.definition)
    # The screen's own gates still apply: the ₹0 tier's universe restriction, and historical
    # ranks when a past date is asked for.
    entitlements.require_universe(definition.index)
    outcome = await service.run_rebalance(
        session,
        portfolio_id=portfolio.id,
        definition=definition,
        top_n=body.top_n,
        hold_buffer=body.hold_buffer,
        requested_as_of=body.as_of or definition.historical_date,
    )
    if (
        outcome.resolution.requested is not None
        and outcome.resolution.as_of != outcome.resolution.latest_published
    ):
        entitlements.require(Feature.HISTORICAL_RANKS)

    payload = _payload(outcome, screen)
    stored: JsonObject = dict(payload.model_dump(mode="json"))
    row = await service.record_rebalance(
        session,
        portfolio_id=portfolio.id,
        screen_id=screen.id,
        outcome=outcome,
        payload=stored,
    )
    return RebalanceOut(id=row.id, created_at=row.created_at, **payload.model_dump())


async def _visible_screen(session: AsyncSession, public_id: str, principal: Principal) -> Screen:
    """The same visibility rule as ``routers.screens``: yours, or an example screen."""
    screen = (
        await session.execute(select(Screen).where(Screen.public_id == public_id))
    ).scalar_one_or_none()
    if screen is None or not (screen.user_id is None or screen.user_id == principal.user_id):
        raise not_found("screen", public_id)
    return screen


def _name_out(row: RebalanceRow) -> RebalanceNameOut:
    return RebalanceNameOut(
        instrument_id=row.instrument_id,
        symbol=row.symbol,
        name=row.name,
        rank=row.rank,
        reason=row.reason,
    )


def _weight_out(weight: TargetWeight) -> TargetWeightOut:
    return TargetWeightOut(
        instrument_id=weight.instrument_id,
        symbol=weight.symbol,
        name=weight.name,
        rank=weight.rank,
        weight=weight.weight,
        action=weight.action,
    )


def _payload(outcome: service.RebalanceOutcome, screen: Screen) -> RebalancePayload:
    """The response body, and — dumped in JSON mode — the stored history row.

    One model for both, so the record and the response cannot render a weight differently
    (CLAUDE.md house rule 8).
    """
    plan: RebalancePlan = outcome.plan
    return RebalancePayload(
        as_of=outcome.resolution.as_of,
        requested_as_of=outcome.resolution.requested,
        data_version=outcome.data_version,
        screen=RebalanceScreenOut(public_id=screen.public_id, name=screen.name),
        top_n=plan.top_n,
        hold_buffer=plan.hold_buffer,
        exits=[_name_out(row) for row in plan.exits],
        inside_wrh=[_name_out(row) for row in plan.inside_wrh],
        entries=[_name_out(row) for row in plan.entries],
        holds=[_name_out(row) for row in plan.holds],
        target_weights=[_weight_out(weight) for weight in plan.target_weights],
        holdings_count=outcome.holdings_count,
        screen_result_count=outcome.screen_result_count,
        delisted_count=outcome.delisted_count,
    )


@router.get(
    "/{portfolio_id}/rebalances",
    response_model=RebalanceHistoryPage,
    summary="Past rebalances of a portfolio",
)
async def list_rebalances(
    portfolio_id: int,
    session: SessionDep,
    principal: AuthenticatedDep,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> RebalanceHistoryPage:
    """Prompt 14 §4: "so a user can see what they were told and when"."""
    portfolio = await _load(session, portfolio_id, principal)
    statement = select(PortfolioRebalance).where(PortfolioRebalance.portfolio_id == portfolio.id)
    if cursor is not None:
        statement = statement.where(PortfolioRebalance.id < _cursor(cursor))
    rows = (
        (await session.execute(statement.order_by(PortfolioRebalance.id.desc()).limit(limit + 1)))
        .scalars()
        .all()
    )
    page = rows[:limit]
    return RebalanceHistoryPage(
        data=[_summary(row) for row in page],
        next_cursor=str(page[-1].id) if len(rows) > limit and page else None,
    )


@router.get(
    "/{portfolio_id}/rebalances/{rebalance_id}",
    response_model=RebalanceOut,
    summary="One past rebalance, exactly as it was served",
)
async def get_rebalance(
    portfolio_id: int,
    rebalance_id: int,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> RebalanceOut:
    """The stored payload, verbatim. Nothing is recomputed — that is the point of the record."""
    portfolio = await _load(session, portfolio_id, principal)
    row = (
        await session.execute(
            select(PortfolioRebalance).where(
                PortfolioRebalance.id == rebalance_id,
                PortfolioRebalance.portfolio_id == portfolio.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("rebalance", str(rebalance_id))
    return RebalanceOut.model_validate({**row.payload, "id": row.id, "created_at": row.created_at})


def _summary(row: PortfolioRebalance) -> RebalanceSummaryOut:
    payload = row.payload
    screen = payload.get("screen")
    screen_map = screen if isinstance(screen, Mapping) else {}
    return RebalanceSummaryOut(
        id=row.id,
        as_of=row.as_of,
        data_version=row.data_version,
        top_n=row.top_n,
        hold_buffer=row.hold_buffer,
        screen_name=_text(screen_map.get("name")),
        screen_public_id=_text(screen_map.get("public_id")),
        exits=_count(payload.get("exits")),
        inside_wrh=_count(payload.get("inside_wrh")),
        entries=_count(payload.get("entries")),
        created_at=row.created_at,
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _count(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def _cursor(cursor: str) -> int:
    try:
        return int(cursor)
    except ValueError as exc:
        raise Problem(
            ProblemType.NOT_FOUND, f"{cursor!r} is not a valid pagination cursor."
        ) from exc
