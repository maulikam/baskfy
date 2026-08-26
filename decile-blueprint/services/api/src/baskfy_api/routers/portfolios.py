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
from pydantic import BaseModel, ConfigDict, Field
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
    PortfolioOut,
    PortfolioSummaryOut,
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
from baskfy_core.allocation_ledger import PortfolioKind, PortfolioSource
from baskfy_core.models import (
    BrokerAccount,
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
from baskfy_core.portfolio_graph import (
    MAX_DEPTH,
    Holding,
    PortfolioGraphError,
    PortfolioNode,
    Totals,
    TreeNode,
    assemble_tree,
    check_move,
    depth_of,
    descendant_ids,
    rollup_by_broker,
)
from baskfy_core.rank_buffer import RebalancePlan, RebalanceRow, TargetWeight
from baskfy_core.screen_definition import ScreenDefinition

log = logging.getLogger(__name__)

router = APIRouter(prefix="/portfolios", tags=["portfolios"])

#: An upload larger than this is not a portfolio. 500 rows of `SYMBOL,qty,price` is well under
#: 32 KiB; the margin is for broker exports with fifteen columns of their own.
MAX_UPLOAD_BYTES = 1_048_576

CSV_MEDIA_TYPE = "text/csv; charset=utf-8"

#: The 400 every tree refusal answers with — a cycle, a self-parent, a breach of
#: :data:`~baskfy_core.portfolio_graph.MAX_DEPTH`.
#:
#: The tree-5 contract calls this member ``VALIDATION``. There is no such member:
#: ``docs/07 §"Error catalogue"`` has exactly one 400 and ``baskfy_api.problems.ProblemType``
#: spells it ``INVALID_SCREEN_DEFINITION``. Adding a member would change the wire contract of
#: every route at once (the generated TypeScript unions the type strings), so the refusals reuse
#: the 400 this router already answers with when a portfolio is sent more names than it may hold.
#: Named here so that the day the catalogue gains a portfolio-shaped 400, one line moves.
TREE_VALIDATION = ProblemType.INVALID_SCREEN_DEFINITION


# ---------------------------------------------------------------------------
# The tree — request and response models (0019: ``parent_id``, ``broker_account_id``)
# ---------------------------------------------------------------------------


class PortfolioCreateIn(PortfolioCreate):
    """``POST /portfolios``, plus the two columns migration 0019 added.

    Both are optional and both default to the pre-0019 behaviour: no parent (a root) and no
    declared broker account (a node that spans brokers). A client that has never heard of the
    tree keeps working unchanged.
    """

    #: The portfolio this one sits inside. A parent that is not the caller's own is answered
    #: with ``NOT_FOUND``, never ``FORBIDDEN`` — see :func:`_parent_for`.
    parent_id: int | None = None
    #: The broker account everything under this portfolio is attributable to. ``None`` — the
    #: default — means it spans brokers.
    broker_account_id: int | None = None


class PortfolioPatchIn(BaseModel):
    """``PATCH /portfolios/{id}`` — rename, **move**, re-attribute, or any combination.

    Every field is optional, and "absent" and "null" are different requests: omitting
    ``parent_id`` leaves the parent alone, while sending ``null`` promotes the portfolio to a
    root. The two are told apart by :attr:`pydantic.BaseModel.model_fields_set`, which is the
    only thing that can tell them apart — a default of ``None`` cannot.

    A move lives here rather than on a ``/{id}/move`` path because re-parenting is a partial
    update of the row, and because every path this API serves is enumerated in
    ``services/api/tests/test_api_artifacts.py``: a new path is a contract change, and this is
    not one.
    """

    #: ``extra="forbid"`` spelled out rather than inherited from ``schemas.PortfolioRename``:
    #: every field here is optional, and a subclass that widened ``name`` from ``str`` to
    #: ``str | None`` would be narrowing a base class's contract in the wrong direction.
    #: docs/07 §Screens' rule — "Unknown keys are rejected" — still holds.
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    parent_id: int | None = None
    broker_account_id: int | None = None


class PortfolioNodeOut(PortfolioSummaryOut):
    """One portfolio in the tree ``GET /portfolios`` returns, with its children under it.

    Every field the flat summary had is inherited unchanged — ``holdings_count`` still counts
    **this** portfolio's rows and not its subtree's, because a count that silently changed
    meaning the day someone added a child would be worse than one that never moved. The
    subtree's numbers are the roll-up's job (``GET /portfolios/{id}/holdings``).
    """

    parent_id: int | None = None
    broker_account_id: int | None = None
    #: 1 for a root. Inside ``orphans`` this is depth *within the fragment* — the rows above it
    #: are not the caller's, so its true depth is not knowable from this response.
    depth: int
    children: list[PortfolioNodeOut] = Field(default_factory=list)


class PortfolioForestOut(BaseModel):
    """``GET /portfolios``: the caller's portfolios, nested.

    ``data`` holds the roots. ``orphans`` holds fragments whose ``parent_id`` names a portfolio
    that is not in the caller's set — impossible through this API, which refuses a foreign
    parent, and therefore a symptom of damage. They are reported rather than dropped, because a
    dropped fragment is holdings that vanish from a list without anyone being told.
    """

    data: list[PortfolioNodeOut]
    orphans: list[PortfolioNodeOut] = Field(default_factory=list)


class PortfolioDetailOut(PortfolioOut):
    """``GET /portfolios/{id}``: the portfolio, its holdings, and where it sits in the tree."""

    parent_id: int | None = None
    broker_account_id: int | None = None
    depth: int
    #: Immediate children, in the listing's order. Ids only: the nesting belongs to
    #: ``GET /portfolios``, and repeating a whole subtree here would make a rename a two-place
    #: edit for every client.
    child_ids: list[int] = Field(default_factory=list)


class PortfolioWriteDetailOut(BaseModel):
    """What a create, an import or a holdings replacement answers with."""

    portfolio: PortfolioDetailOut
    report: ImportReportOut


class TotalsOut(BaseModel):
    """A summed line of the roll-up. ``Decimal`` throughout — house rule 9.

    ``cost`` is ``quantity * avg_price`` summed, exactly, and is **not** rounded: the response's
    invariant is that ``total == sum(by_broker) + unattributed`` to the last digit, and rounding
    each line independently is precisely what breaks it. Both inputs are stored at four decimal
    places, so every value here terminates.

    ``cost`` is money put in, never a valuation: this endpoint reads no quote.
    """

    quantity: Decimal
    cost: Decimal
    #: Rows behind the line. A zero ``quantity`` with a non-zero count is a real state — a
    #: closed position nobody deleted, or a holding imported without a quantity column.
    holdings: int


class BrokerLineOut(BaseModel):
    """One broker account's share of a subtree."""

    broker_account_id: int
    #: The catalog id (``zerodha``, …) and the user's label for the account. ``None`` when the
    #: account is not one of the caller's, which cannot happen through this API and would be
    #: damage; the line is still reported, unnamed, rather than another tenant's label leaking.
    broker_id: str | None = None
    label: str | None = None
    totals: TotalsOut


class SubtreeHoldingOut(BaseModel):
    """One holding row inside the subtree, with the portfolio and account it belongs to."""

    portfolio_id: int
    instrument_id: int
    symbol: str
    name: str
    broker_account_id: int
    quantity: Decimal | None = None
    avg_price: Decimal | None = None
    added_on: dt.date


class PortfolioRollupOut(BaseModel):
    """``GET /portfolios/{id}/holdings``: a subtree's holdings, split by broker, with a total."""

    portfolio_id: int
    #: What the root portfolio *claims*: ``None`` means it declares itself as spanning brokers.
    declared_broker_account_id: int | None = None
    #: The subtree, root first, breadth-first — so a caller can see what was summed.
    subtree_portfolio_ids: list[int]
    by_broker: list[BrokerLineOut]
    #: Holdings whose account could not be named. Structurally empty since 0019 made the column
    #: ``NOT NULL``; kept because a total that can hide unattributed money inside a broker's
    #: line is the failure this split exists to prevent.
    unattributed: TotalsOut
    total: TotalsOut
    #: True when the money sits in more than one nameable place — unattributed money counts as
    #: one of them.
    spans_brokers: bool
    #: True when the root declares one account but its subtree's money is not all there.
    declaration_conflicts: bool
    rows: list[SubtreeHoldingOut]


PortfolioNodeOut.model_rebuild()


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


def _refuse(exc: PortfolioGraphError) -> Problem:
    """Turn a graph refusal into the documented 400.

    The graph's messages already name the offending rows — ``portfolio parentage forms a cycle:
    7 -> 9 -> 7``, ``portfolio 9 would sit at depth 7, deeper than the limit of 6`` — which is
    what the contract asks for and what a user can act on. They name ids of the caller's own
    portfolios only: every id in the message came out of a set this router built from
    ``WHERE user_id = <caller>``.
    """
    return Problem(TREE_VALIDATION, str(exc))


async def _graph_rows(
    session: AsyncSession, user_id: int
) -> tuple[list[PortfolioNode], dict[int, dt.datetime]]:
    """Every portfolio the caller owns, as graph nodes, plus their creation timestamps.

    One query. The ``user_id`` filter is the whole of tenant isolation for the tree: nothing
    outside this set can become a parent, be moved, or be summed, because nothing outside it is
    ever handed to :mod:`baskfy_core.portfolio_graph`.

    Ordered newest first, matching what ``GET /portfolios`` has always returned; the graph
    preserves input order, so roots and children come back in it.
    """
    rows = (
        await session.execute(
            select(
                Portfolio.id,
                Portfolio.name,
                Portfolio.parent_id,
                Portfolio.broker_account_id,
                Portfolio.created_at,
            )
            .where(Portfolio.user_id == user_id)
            .order_by(Portfolio.created_at.desc(), Portfolio.id.desc())
        )
    ).all()
    nodes = [
        PortfolioNode(
            id=row.id,
            name=row.name,
            parent_id=row.parent_id,
            broker_account_id=row.broker_account_id,
        )
        for row in rows
    ]
    return nodes, {row.id: row.created_at for row in rows}


async def _holdings_counts(session: AsyncSession, user_id: int) -> dict[int, int]:
    """``portfolio_id -> rows held``, for the caller's portfolios only."""
    rows = (
        await session.execute(
            select(PortfolioHolding.portfolio_id, func.count().label("holdings"))
            .join(Portfolio, Portfolio.id == PortfolioHolding.portfolio_id)
            .where(Portfolio.user_id == user_id)
            .group_by(PortfolioHolding.portfolio_id)
        )
    ).all()
    return {row.portfolio_id: int(row.holdings) for row in rows}


async def _parent_for(
    session: AsyncSession, parent_id: int | None, principal: Principal
) -> int | None:
    """Validate a proposed parent id, or ``None``.

    A parent belonging to another user answers ``NOT_FOUND``, which is the point: a 403 would
    confirm that the id exists and whose it is. :func:`_load` already answers not-yours and
    does-not-exist identically, so this is one call and no branch.
    """
    if parent_id is None:
        return None
    parent = await _load(session, parent_id, principal)
    return parent.id


async def _broker_account_for(
    session: AsyncSession, broker_account_id: int | None, principal: Principal
) -> int | None:
    """Validate a proposed ``broker_account_id``, or ``None``. Not-yours is ``NOT_FOUND`` too."""
    if broker_account_id is None:
        return None
    owned = await session.scalar(
        select(BrokerAccount.id).where(
            BrokerAccount.id == broker_account_id,
            BrokerAccount.user_id == principal.require_user(),
        )
    )
    if owned is None:
        raise not_found("broker account", str(broker_account_id))
    return int(owned)


def _node_out(
    node: TreeNode, counts: Mapping[int, int], created: Mapping[int, dt.datetime]
) -> PortfolioNodeOut:
    return PortfolioNodeOut(
        id=node.portfolio.id,
        name=node.portfolio.name,
        created_at=created[node.portfolio.id],
        parent_id=node.portfolio.parent_id,
        broker_account_id=node.portfolio.broker_account_id,
        depth=node.depth,
        holdings_count=counts.get(node.portfolio.id, 0),
        children=[_node_out(child, counts, created) for child in node.children],
    )


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


async def _portfolio_out(session: AsyncSession, portfolio: Portfolio) -> PortfolioDetailOut:
    """One portfolio, its holdings, and its place in the caller's tree.

    ``depth`` is computed rather than stored, so it cannot drift out of step with ``parent_id``.
    A stored tree that is damaged — a cycle, or a parent belonging to nobody — surfaces as the
    documented 400 naming the damage, not as a 500 and not as a silently wrong number.
    """
    nodes, _ = await _graph_rows(session, portfolio.user_id)
    try:
        depth = depth_of(nodes, portfolio.id)
    except PortfolioGraphError as exc:
        raise _refuse(exc) from exc
    return PortfolioDetailOut(
        id=portfolio.id,
        name=portfolio.name,
        created_at=portfolio.created_at,
        holdings=await _holdings_out(session, portfolio.id),
        parent_id=portfolio.parent_id,
        broker_account_id=portfolio.broker_account_id,
        depth=depth,
        child_ids=[node.id for node in nodes if node.parent_id == portfolio.id],
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

    imported = await service.replace_holdings(session, portfolio, matched, added_on=dt.date.today())
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


@router.get("", response_model=PortfolioForestOut, summary="List portfolios, nested")
async def list_portfolios(session: SessionDep, principal: AuthenticatedDep) -> PortfolioForestOut:
    """The caller's portfolios as a forest: a child appears under its parent, never beside it.

    ``data`` holds the roots, newest first, each with its children nested underneath. Before
    migration 0019 every portfolio was a root, so a client that only reads ``data[*]`` sees
    exactly what it saw then — the nesting is additive.
    """
    user_id = principal.require_user()
    nodes, created = await _graph_rows(session, user_id)
    counts = await _holdings_counts(session, user_id)
    try:
        forest = assemble_tree(nodes)
    except PortfolioGraphError as exc:
        raise _refuse(exc) from exc
    return PortfolioForestOut(
        data=[_node_out(root, counts, created) for root in forest.roots],
        orphans=[_node_out(orphan.subtree, counts, created) for orphan in forest.orphans],
    )


@router.post(
    "",
    response_model=PortfolioWriteDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a portfolio",
)
async def create_portfolio(
    request: Request,
    body: PortfolioCreateIn,
    session: SessionDep,
    principal: AuthenticatedDep,
    idempotency_key: Annotated[str | None, Header(alias=idempotency.HEADER)] = None,
) -> PortfolioWriteDetailOut:
    """docs/07: `POST /portfolios { name, holdings:[{symbol, quantity?, avg_price?}] }`.

    Since 0019 the body may also carry ``parent_id`` and ``broker_account_id``. A parent that is
    not the caller's own is ``NOT_FOUND``; a parent already at :data:`MAX_DEPTH` is the
    documented 400, because the child would sit one level deeper than the product supports.
    No cycle is reachable here — a row that does not exist yet cannot be in anybody's chain —
    so the cycle rule belongs to the move, on ``PATCH``.
    """
    user_id = principal.require_user()
    if len(body.holdings) > MAX_IMPORT_ROWS:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            f"A portfolio may hold at most {MAX_IMPORT_ROWS} names; "
            f"{len(body.holdings)} were sent.",
        )
    # The replay is answered before the parent is validated, so a retry still replays after the
    # parent has been deleted: the first attempt already created the portfolio, and answering a
    # retry with 404 would be a worse lie than the one `_replayed` already argues against.
    scope = f"portfolio:create:{user_id}"
    replayed = await _replayed(session, principal, _cache(request), scope, idempotency_key)
    if replayed is not None:
        return replayed

    parent_id = await _parent_for(session, body.parent_id, principal)
    broker_account_id = await _broker_account_for(session, body.broker_account_id, principal)
    if parent_id is not None:
        nodes, _ = await _graph_rows(session, user_id)
        try:
            depth = depth_of(nodes, parent_id) + 1
        except PortfolioGraphError as exc:
            raise _refuse(exc) from exc
        if depth > MAX_DEPTH:
            raise Problem(
                TREE_VALIDATION,
                f"A portfolio under {parent_id} would sit at depth {depth}, "
                f"deeper than the limit of {MAX_DEPTH}.",
            )

    portfolio = Portfolio(
        user_id=user_id,
        name=body.name,
        parent_id=parent_id,
        broker_account_id=broker_account_id,
        # 0021/0022 made these NOT NULL with no server default, on purpose: §4.1 makes the kind
        # an arithmetic decision, not a detail to inherit silently. A portfolio created through
        # this route is one the user assembled, so HOLDING_GROUP is the true source (§3) and
        # §5.2 then gives it the most conservative metric. CAPITAL because it holds real money
        # and must sum into net worth; a monitoring view is created deliberately, never by
        # default. `started_on` is today because that is the day this grouping begins, and every
        # §5.2 metric is measured from it.
        kind=PortfolioKind.CAPITAL.value,
        source=PortfolioSource.HOLDING_GROUP.value,
        started_on=dt.date.today(),
    )
    session.add(portfolio)
    await session.flush()
    report = await _apply(session, portfolio, _rows_from_body(body.holdings))
    await idempotency.remember(_cache(request), scope, idempotency_key, str(portfolio.id))
    return PortfolioWriteDetailOut(
        portfolio=await _portfolio_out(session, portfolio), report=report
    )


async def _replayed(
    session: AsyncSession,
    principal: Principal,
    cache: Redis | None,
    scope: str,
    key: str | None,
) -> PortfolioWriteDetailOut | None:
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
    return PortfolioWriteDetailOut(
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
    response_model=PortfolioWriteDetailOut,
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
) -> PortfolioWriteDetailOut:
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
        portfolio = Portfolio(
            user_id=user_id,
            name=name or _name_from(file.filename),
            # Same reasoning as the create route above: an imported CSV is a holding group the
            # user assembled, and it holds real money.
            kind=PortfolioKind.CAPITAL.value,
            source=PortfolioSource.HOLDING_GROUP.value,
            started_on=dt.date.today(),
        )
        session.add(portfolio)
        await session.flush()
        await idempotency.remember(_cache(request), scope, idempotency_key, str(portfolio.id))

    report = await _apply(session, portfolio, parsed)
    return PortfolioWriteDetailOut(
        portfolio=await _portfolio_out(session, portfolio), report=report
    )


def _name_from(filename: str | None) -> str:
    stem = (filename or "").rsplit("/", maxsplit=1)[-1]
    stem = stem[: -len(".csv")] if stem.lower().endswith(".csv") else stem
    return stem.strip()[:120] or "Imported portfolio"


@router.get("/{portfolio_id}", response_model=PortfolioDetailOut, summary="Fetch one portfolio")
async def get_portfolio(
    portfolio_id: int, session: SessionDep, principal: AuthenticatedDep
) -> PortfolioDetailOut:
    return await _portfolio_out(session, await _load(session, portfolio_id, principal))


@router.patch(
    "/{portfolio_id}",
    response_model=PortfolioDetailOut,
    summary="Rename, move or re-attribute a portfolio",
)
async def update_portfolio(
    portfolio_id: int,
    body: PortfolioPatchIn,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> PortfolioDetailOut:
    """Rename it, **move** it to a new parent, or change which broker account it names.

    Only the fields present in the request are touched, so a move never renames and a rename
    never moves. ``"parent_id": null`` is a request — promote this portfolio to a root — and is
    distinct from omitting the key.

    The move is judged by :func:`~baskfy_core.portfolio_graph.check_move` over the caller's own
    rows, which refuses the two shapes a naive check misses: re-parenting under one's own
    descendant, and a legal parent plus a legal subtree that are illegal *together* because
    their combined height breaches :data:`MAX_DEPTH`. Every refusal is the documented 400 and
    names the rows involved; a parent that is not the caller's is ``NOT_FOUND`` instead, so the
    existence of another tenant's portfolio never leaks.

    Changing ``broker_account_id`` re-labels the **container** and moves no shares: holdings
    already written keep the account they were written at, which the roll-up then reports as
    ``declaration_conflicts``. Silently rewriting them would be this router claiming that stock
    had moved between brokers, which is an assertion about the world that no HTTP request can
    make true.
    """
    portfolio = await _load(session, portfolio_id, principal)
    fields = body.model_fields_set

    if "broker_account_id" in fields:
        portfolio.broker_account_id = await _broker_account_for(
            session, body.broker_account_id, principal
        )
    if "parent_id" in fields:
        parent_id = await _parent_for(session, body.parent_id, principal)
        nodes, _ = await _graph_rows(session, portfolio.user_id)
        try:
            check_move(nodes, portfolio.id, parent_id)
        except PortfolioGraphError as exc:
            raise _refuse(exc) from exc
        portfolio.parent_id = parent_id
    if body.name is not None:
        portfolio.name = body.name

    await session.flush()
    return await _portfolio_out(session, portfolio)


@router.delete(
    "/{portfolio_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a portfolio"
)
async def delete_portfolio(
    portfolio_id: int, session: SessionDep, principal: AuthenticatedDep
) -> Response:
    """Delete one portfolio. Its children are **promoted to roots**, not deleted with it.

    ``portfolio.parent_id`` is ``ON DELETE SET NULL`` (migration 0019), deliberately: deleting a
    grouping node must not silently delete the money underneath it. The children stay the
    caller's own, visibly detached at the top of ``GET /portfolios``, with their holdings
    untouched. This portfolio's *own* holdings go with it — ``portfolio_holding.portfolio_id``
    is ``ON DELETE CASCADE``, which is the row being deleted, not somebody else's.
    """
    portfolio = await _load(session, portfolio_id, principal)
    await session.execute(delete(Portfolio).where(Portfolio.id == portfolio.id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/{portfolio_id}/holdings",
    response_model=PortfolioWriteDetailOut,
    summary="Replace a portfolio's holdings",
)
async def put_holdings(
    portfolio_id: int,
    body: HoldingsIn,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> PortfolioWriteDetailOut:
    """docs/07: `PUT /portfolios/{id}/holdings`.

    Every row written names its broker account — see
    :func:`baskfy_api.portfolios.resolve_broker_account`. The body has no broker column, so it
    describes one row per instrument and the stored rows are made to say exactly that.
    """
    portfolio = await _load(session, portfolio_id, principal)
    if len(body.holdings) > MAX_IMPORT_ROWS:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            f"A portfolio may hold at most {MAX_IMPORT_ROWS} names; "
            f"{len(body.holdings)} were sent.",
        )
    report = await _apply(session, portfolio, _rows_from_body(body.holdings))
    return PortfolioWriteDetailOut(
        portfolio=await _portfolio_out(session, portfolio), report=report
    )


@router.get(
    "/{portfolio_id}/holdings",
    response_model=PortfolioRollupOut,
    summary="A subtree's holdings, split by broker account",
)
async def holdings_rollup(
    portfolio_id: int, session: SessionDep, principal: AuthenticatedDep
) -> PortfolioRollupOut:
    """Whose money is where: every holding in this portfolio's **subtree**, summed per account.

    Read-only, and the only thing it does with a broker is name one. It reaches no broker
    session, places nothing, and marks nothing to market — ``cost`` is what was put in, because
    this endpoint receives no quote and is structurally incapable of valuing anything.

    The subtree is computed from the caller's own rows, so a sibling's money — or another
    tenant's — cannot enter the total: nothing outside ``WHERE user_id = <caller>`` is ever
    handed to :func:`~baskfy_core.portfolio_graph.rollup_by_broker`.

    A holding with no ``quantity`` or no ``avg_price`` (both columns are nullable — an import
    may carry symbols and nothing else) contributes zero to the sums and one to the line's
    ``holdings`` count, which is what tells "nothing held" apart from "nothing known".
    """
    user_id = principal.require_user()
    portfolio = await _load(session, portfolio_id, principal)
    nodes, _ = await _graph_rows(session, user_id)
    try:
        inside = descendant_ids(nodes, portfolio.id)
    except PortfolioGraphError as exc:
        raise _refuse(exc) from exc

    rows = (
        await session.execute(
            select(
                PortfolioHolding.portfolio_id,
                PortfolioHolding.instrument_id,
                PortfolioHolding.broker_account_id,
                PortfolioHolding.quantity,
                PortfolioHolding.avg_price,
                PortfolioHolding.added_on,
                Instrument.symbol,
                Instrument.name,
            )
            .join(Instrument, Instrument.id == PortfolioHolding.instrument_id)
            .where(PortfolioHolding.portfolio_id.in_(inside))
            .order_by(
                PortfolioHolding.portfolio_id,
                Instrument.symbol,
                PortfolioHolding.broker_account_id,
            )
        )
    ).all()

    holdings = [
        Holding(
            portfolio_id=row.portfolio_id,
            instrument_id=row.instrument_id,
            broker_account_id=row.broker_account_id,
            quantity=row.quantity if row.quantity is not None else Decimal(0),
            avg_price=row.avg_price if row.avg_price is not None else Decimal(0),
        )
        for row in rows
    ]
    try:
        rollup = rollup_by_broker(nodes, holdings, portfolio.id)
    except PortfolioGraphError as exc:
        raise _refuse(exc) from exc

    accounts = await _account_names(session, user_id)
    return PortfolioRollupOut(
        portfolio_id=rollup.portfolio_id,
        declared_broker_account_id=rollup.declared_broker_account_id,
        subtree_portfolio_ids=list(inside),
        by_broker=[
            BrokerLineOut(
                broker_account_id=line.broker_account_id,
                broker_id=accounts.get(line.broker_account_id, (None, None))[0],
                label=accounts.get(line.broker_account_id, (None, None))[1],
                totals=_totals_out(line.totals),
            )
            for line in rollup.by_broker
        ],
        unattributed=_totals_out(rollup.unattributed),
        total=_totals_out(rollup.total),
        spans_brokers=rollup.spans_brokers,
        declaration_conflicts=rollup.declaration_conflicts,
        rows=[
            SubtreeHoldingOut(
                portfolio_id=row.portfolio_id,
                instrument_id=row.instrument_id,
                symbol=row.symbol,
                name=row.name,
                broker_account_id=row.broker_account_id,
                quantity=row.quantity,
                avg_price=row.avg_price,
                added_on=row.added_on,
            )
            for row in rows
        ],
    )


def _totals_out(totals: Totals) -> TotalsOut:
    return TotalsOut(quantity=totals.quantity, cost=totals.cost, holdings=totals.holdings)


async def _account_names(
    session: AsyncSession, user_id: int
) -> dict[int, tuple[str | None, str | None]]:
    """``broker_account_id -> (broker_id, label)`` for the caller's own accounts only.

    Scoped to the caller so that a roll-up can never render another tenant's label, even if a
    holding somehow named their account. A line whose account is missing from this map is
    reported with no name rather than dropped — the money is real either way.
    """
    rows = (
        await session.execute(
            select(BrokerAccount.id, BrokerAccount.broker_id, BrokerAccount.label).where(
                BrokerAccount.user_id == user_id
            )
        )
    ).all()
    return {row.id: (row.broker_id, row.label) for row in rows}


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
