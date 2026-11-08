"""Running a screen against the database, with the Redis result cache (Prompt 6, deliverables 1,
5 and 6 — the ``services/api`` half).

``baskfy_core.screener`` builds the statement and knows nothing else; everything here is the I/O
``packages/core`` is not allowed to do (docs/02 §"Repo layout"): resolving the as-of date against
the trading calendar and the published pipeline runs, executing the statement, and the cache.

The cache contract, from docs/06 §Caching and §"Determinism guarantee"
----------------------------------------------------------------------
    Key: `screen:{sha256(definition_canonical_json)}:{as_of}:{data_version}`.
    TTL: until the next `data_version` bump. Invalidate the whole namespace on publish.

The cached value is the response body itself — the exact bytes :func:`payload_with_provenance`
produces (``ScreenResult.payload()`` plus C6's ``provenance``, canonically encoded) — so a hit
and a miss are indistinguishable to the client, which is what makes the determinism guarantee
testable: run the same screen twice and compare the strings.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
from collections.abc import Awaitable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Protocol, runtime_checkable

import pandas as pd
from pydantic import ValidationError
from sqlalchemy import Row, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.metrics import observe_cache
from baskfy_api.telemetry import annotate_current_span
from baskfy_core import factor_registry, ranking_engine
from baskfy_core.models import (
    DeskScoreDaily,
    FactorDaily,
    PipelineRun,
    Screen,
    ScreenRun,
    TradingDay,
)
from baskfy_core.ranking import DESK_SCORE_EXPLAIN_COLUMNS, is_desk_score_factor
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_core.screener import (
    CACHE_NAMESPACE,
    DESK_FRAME_COLUMNS,
    MAX_RESULT_ROWS,
    RankingFactor,
    RankingFrameQuery,
    ScreenQueryError,
    ScreenResult,
    ScreenResultRow,
    build_ranking_frame_query,
    build_screen_query,
    cache_key,
    canonical_json,
    resolve_columns,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.universes import UNIVERSE_BY_SLUG

#: A safety net, not the contract. docs/06 ties the lifetime of a cached result to the next
#: ``data_version`` bump, and ``publish`` purges the namespace to enforce it. This expiry only
#: decides how long orphaned keys survive if a publish never happens — a stuck pipeline should
#: cost memory for a weekend, not forever. The key already carries ``data_version``, so an expiry
#: can never serve a stale result; it can only force a recompute.
SCREEN_CACHE_FALLBACK_TTL_SECONDS: Final = 48 * 60 * 60

#: docs/06 §Caching: "Warm the top 200 most-run screen definitions after each publish."
WARM_CACHE_SCREEN_LIMIT: Final = 200

#: docs/01 §2.13: "Site states historical data is available **from 1 Nov 2024**." docs/07 makes
#: an ``as_of`` "before the data start date" a ``422 no-trading-day``, and this is the date it
#: means. A constant rather than ``min(factor_daily.date)`` because it is a product promise, not
#: an observation — and because a scan of the fact table on every request to discover it would be
#: an odd way to enforce a published policy.
DATA_START_DATE: Final = dt.date(2024, 11, 1)

#: ``provenance.ranking_engine_version`` for a definition without ``ranking_terms``: docs/06's SQL
#: row-number ranking, not :data:`baskfy_core.ranking_engine.RANKING_ENGINE_VERSION`.
LEGACY_RANKING_ENGINE_VERSION: Final = "legacy-sql"


class AsOfOutOfRange(ValueError):
    """The requested ``as_of`` is outside the range the service can serve.

    docs/07 §"Error catalogue" maps this to ``422 no-trading-day``, described there as "`as_of`
    before the data start date". Prompt 7's acceptance criteria extend it to the other end: "a
    historical_date in the future returns 422 no-trading-day".

    This replaces the clamping that Prompt 6 implemented (``docs/06a`` §11 originally said a
    future date was pulled back to the latest published day). Clamping is right for a *weekend* —
    docs/06 §step 1 asks for exactly that, and the response says which date was used — but wrong
    for a date the service has no data for at all: it turns a client bug into a plausible-looking
    answer for a different date than the one asked for.
    """

    def __init__(self, requested: dt.date, earliest: dt.date, latest: dt.date) -> None:
        super().__init__(
            f"{requested.isoformat()} is outside the range this service can serve "
            f"({earliest.isoformat()} to {latest.isoformat()})"
        )
        self.requested = requested
        self.earliest = earliest
        self.latest = latest


class NoPublishedData(RuntimeError):
    """No ``pipeline_run`` has ever been published, so there is no date a screen may run for.

    docs/06 §step 1 defines the default as-of as "the latest `date` in `factor_daily` that belongs
    to a published `pipeline_run` (**never** a half-written day)". Before the first successful
    nightly run that set is empty, and answering with the newest ``factor_daily`` row anyway would
    be exactly the half-written day the document rules out.
    """


@runtime_checkable
class ScreenCache(Protocol):
    """The slice of redis-py this module uses.

    ``runtime_checkable`` so that the pipeline's ``publish`` step — which is handed whatever cache
    client the deployment configured, or none — can narrow it with ``isinstance`` instead of
    reaching for a cast or a ``type: ignore`` (CLAUDE.md house rule 3).

    Narrow on purpose, matching ``baskfy_providers.ratelimit.RedisLike``: it keeps the cache
    injectable (and ``baskfy-api`` free of a hard redis dependency) without pulling the whole
    client surface into these signatures. Parameters are positional-only so that redis-py's own
    naming (``name`` rather than ``key``) satisfies the protocol structurally.

    **Async.** Every caller of this protocol is already inside a coroutine — a FastAPI request
    handler or the nightly ``publish`` step — and a synchronous Redis round trip inside an event
    loop stops every other in-flight request for its duration. On a warm screen that round trip
    *is* the request (docs/11 budgets 150 ms p95 warm), so serialising the whole process on it
    would be the difference between a cache and a queue.

    The methods return ``Awaitable[object]`` rather than being declared ``async def``: redis-py
    types every command with one signature covering both its sync and async clients, so an
    ``async def`` here would not match ``redis.asyncio.Redis`` structurally. ``object`` rather than
    a narrower type for the same reason; :func:`_cached_payload` narrows what we actually get.
    """

    def get(self, key: str, /) -> Awaitable[object]: ...

    def set(self, key: str, value: str, ex: int | None = None, /) -> Awaitable[object]: ...


def _cached_payload(value: object) -> str | None:
    """Narrow one cache read to the JSON body we stored, or ``None`` for a miss."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    raise TypeError(f"cache returned {type(value).__name__}, not a stored screen payload")


@dataclass(frozen=True, slots=True)
class AsOfResolution:
    """docs/06 §step 1: "tell the client which date was actually used"."""

    requested: dt.date | None
    as_of: dt.date
    latest_published: dt.date
    #: True when ``requested`` was not a trading day and was snapped backwards.
    snapped: bool


@dataclass(frozen=True, slots=True)
class ScreenRunResult:
    """What :func:`run_screen` returns: the response bytes, and how they were obtained."""

    payload: str
    resolution: AsOfResolution
    data_version: int
    cache_hit: bool
    #: ``None`` on a cache hit — the cached bytes are the response, and re-deriving the object
    #: graph from them would only invite the two to drift.
    result: ScreenResult | None = None


# ---------------------------------------------------------------------------
# Step 1 — as-of resolution
# ---------------------------------------------------------------------------


async def current_data_version(session: AsyncSession) -> int:
    """The version the site is serving: the highest ever published. ``0`` before the first run.

    Shared with ``baskfy_worker.tasks.publish``, which writes it. One implementation, so the
    reader and the writer cannot disagree about what "current" means.
    """
    value = (
        await session.execute(
            select(func.max(PipelineRun.data_version)).where(PipelineRun.data_version.is_not(None))
        )
    ).scalar_one_or_none()
    return int(value) if value is not None else 0


async def latest_published_date(session: AsyncSession) -> dt.date | None:
    """The newest trade date covered by a published run (docs/06 §step 1)."""
    return (
        await session.execute(
            select(func.max(PipelineRun.trade_date)).where(PipelineRun.data_version.is_not(None))
        )
    ).scalar_one_or_none()


async def snap_backward_to_trading_day(session: AsyncSession, date: dt.date) -> dt.date | None:
    """docs/06 §step 1: "snap **backwards** to the previous trading day"."""
    return (
        await session.execute(
            select(func.max(TradingDay.date)).where(
                TradingDay.exchange_id == NSE_EXCHANGE_ID,
                TradingDay.is_trading_day.is_(True),
                TradingDay.date <= date,
            )
        )
    ).scalar_one_or_none()


async def resolve_as_of(
    session: AsyncSession,
    requested: dt.date | None,
    *,
    data_start: dt.date = DATA_START_DATE,
) -> AsOfResolution:
    """Resolve docs/06 §step 1 in full.

    ``historical_date`` if set, else the latest published date. A date inside the servable range
    that is not a trading day snaps **backwards**, and the resolution records which date was
    actually used. A date outside the range — later than anything published, or earlier than the
    data start — raises :class:`AsOfOutOfRange` rather than being quietly moved.
    """
    latest = await latest_published_date(session)
    if latest is None:
        raise NoPublishedData(
            "no pipeline_run has been published, so factor_daily has no trustworthy as-of date"
        )
    if requested is None:
        return AsOfResolution(requested=None, as_of=latest, latest_published=latest, snapped=False)

    if requested > latest or requested < data_start:
        raise AsOfOutOfRange(requested, data_start, latest)

    snapped_to = await snap_backward_to_trading_day(session, requested)
    if snapped_to is None or snapped_to < data_start:
        raise AsOfOutOfRange(requested, data_start, latest)
    return AsOfResolution(
        requested=requested,
        as_of=snapped_to,
        latest_published=latest,
        snapped=snapped_to != requested,
    )


# ---------------------------------------------------------------------------
# Steps 2-7 — execution
# ---------------------------------------------------------------------------


def _as_int(value: object) -> int:
    """Narrow one result cell to ``int``. Rank columns are ``bigint``; nothing else reaches here."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise TypeError(f"expected an integer rank column, got {value!r} ({type(value).__name__})")


def _row_to_result(row: Row[tuple[object]], columns: Sequence[str]) -> ScreenResultRow:
    mapping = row._mapping
    return ScreenResultRow(
        rank=_as_int(mapping["rank"]),
        instrument_id=_as_int(mapping["instrument_id"]),
        combined_rank=_as_int(mapping["combined_rank"]),
        ranks=(_as_int(mapping["r1"]), _as_int(mapping["r2"]), _as_int(mapping["r3"])),
        values={name: mapping[name] for name in columns},
    )


def _cell(value: object) -> object:
    """One ranked-frame cell as the payload carries it.

    The frame is built with ``dtype=object`` so a ``numeric`` column keeps its ``Decimal`` and its
    storage precision (house rules 8 and 9); only the engine's own outputs arrive as numpy/pandas
    scalars, and a missing one arrives as NaN or ``pd.NA``.
    """
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (Decimal, bool, int, str, dt.date)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else value
    item = getattr(value, "item", None)
    if callable(item):
        return _cell(item())
    return value


def _composite_cell(value: object) -> Decimal | None:
    """C4 step 4: ``composite_score`` is a 2 dp number, so it goes out as one."""
    cell = _cell(value)
    if cell is None:
        return None
    if isinstance(cell, (int, float, Decimal)) and not isinstance(cell, bool):
        return Decimal(repr(cell) if isinstance(cell, float) else cell).quantize(Decimal("0.01"))
    raise TypeError(f"composite_score must be numeric, got {type(cell).__name__}")


def _term_direction(preference: str) -> str:
    """``RankingFactor.direction`` for a term: best-first is descending only for ``higher``."""
    return "desc" if preference == "higher" else "asc"


def uses_desk_score(definition: ScreenDefinition) -> bool:
    """True when the book's SCORE decides the order — as ``sort_by`` or as any ranking term."""
    return is_desk_score_factor(definition.sort_by) or any(
        is_desk_score_factor(term.factor) for term in definition.ranking_terms
    )


def ranking_spec_for(definition: ScreenDefinition) -> ranking_engine.RankingSpec:
    """The ranking engine's spec for a definition with ``ranking_terms`` (C3 -> C4).

    Label, weight family and null policy come from the registry, so a term cannot claim a family
    its factor does not belong to.
    """
    if not definition.ranking_terms:
        raise ScreenQueryError("ranking_spec_for needs a definition with ranking_terms")
    terms: list[ranking_engine.TermSpec] = []
    for term in definition.ranking_terms:
        factor = factor_registry.get(term.factor)
        terms.append(
            ranking_engine.TermSpec(
                key=term.factor,
                label=factor.label,
                weight_family=str(factor.weight_family),
                preference=term.preference,
                weight=term.weight,
                target_min=term.target_min,
                target_max=term.target_max,
                null_policy=str(factor.null_policy),
            )
        )
    return ranking_engine.RankingSpec(
        terms=tuple(terms),
        mode=definition.ranking_mode,
        scope=definition.ranking_scope,
        missing_data=definition.missing_data,
        family_weights=(
            None if definition.family_weights is None else definition.family_weights.as_mapping()
        ),
    )


def _desk_explain_values(
    score: object, reject: object, parts: dict[str, object]
) -> dict[str, object]:
    """The A-F explain columns the results table's peek drawer reads, from ``desk_score_daily``."""
    reject_text = reject if isinstance(reject, str) else ""
    values = {name: _cell(parts.get(name)) for name in DESK_SCORE_EXPLAIN_COLUMNS}
    values["desk_reject"] = reject_text
    values["desk_eligible"] = reject_text == "" and _cell(score) is not None
    return values


async def _attach_desk_columns(
    session: AsyncSession, as_of: dt.date, rows: Sequence[ScreenResultRow]
) -> tuple[ScreenResultRow, ...]:
    """Legacy ``sort_by=desk_score``: add the stored A-F breakdown to each ranked row.

    Read from ``desk_score_daily`` for the rows the SQL already ranked — never re-scored (C2).
    """
    if not rows:
        return ()
    desk = DeskScoreDaily.__table__
    stored = (
        await session.execute(
            select(desk).where(
                desk.c.date == as_of,
                desk.c.instrument_id.in_([row.instrument_id for row in rows]),
            )
        )
    ).all()
    by_instrument = {int(item._mapping["instrument_id"]): item._mapping for item in stored}
    attached: list[ScreenResultRow] = []
    for row in rows:
        mapping = by_instrument.get(row.instrument_id)
        parts: dict[str, object] = {}
        score: object = None
        reject: object = None
        if mapping is not None:
            parts = {
                label: mapping[column]
                for column, label in DESK_FRAME_COLUMNS.items()
                if label in DESK_SCORE_EXPLAIN_COLUMNS
            }
            score = mapping["score"]
            reject = mapping["reject"]
        attached.append(
            ScreenResultRow(
                rank=row.rank,
                instrument_id=row.instrument_id,
                combined_rank=row.combined_rank,
                ranks=row.ranks,
                values={**row.values, **_desk_explain_values(score, reject, parts)},
            )
        )
    return tuple(attached)


@dataclass(frozen=True, slots=True)
class RankedFrame:
    """One definition ranked by C4 over its pre-filter frame — shared by run, explain, selection.

    ``result`` is ``None`` when the selected universe has no rows on ``as_of``: there is nothing
    to rank, and :func:`baskfy_core.ranking_engine.rank_frame` is not asked to rank nothing.
    """

    query: RankingFrameQuery
    spec: ranking_engine.RankingSpec
    result: ranking_engine.RankingResult | None


async def rank_definition(
    session: AsyncSession, definition: ScreenDefinition, as_of: dt.date
) -> RankedFrame:
    """``build_ranking_frame_query`` -> one round trip -> ``rank_frame``. The only engine path.

    :func:`execute_screen` serves ``result.ranked``; ``POST /screens/explain`` reads
    ``result.scored`` (every row, ranked or not) and ``POST /screens/selection`` reads the whole
    of ``result.ranked`` rather than a page of it, so all three rank one frame the same way.
    """
    spec = ranking_spec_for(definition)
    query = build_ranking_frame_query(definition, as_of)
    rows = (await session.execute(query.statement)).all()
    if not rows:
        return RankedFrame(query=query, spec=spec, result=None)
    frame = pd.DataFrame([dict(row._mapping) for row in rows], dtype=object)
    try:
        result = ranking_engine.rank_frame(frame, spec)
    except ValueError as exc:
        # e.g. family_weights that give every family the terms use a zero share: the
        # definition validated, but nothing can rank, which is the caller's 400 not our 500.
        raise ScreenQueryError(f"ranking_terms cannot be ranked: {exc}") from exc
    return RankedFrame(query=query, spec=spec, result=result)


async def _execute_ranking_screen(  # noqa: PLR0913 - mirrors execute_screen inputs
    session: AsyncSession,
    definition: ScreenDefinition,
    *,
    as_of: dt.date,
    data_version: int,
    columns: Sequence[str] = (),
    requested_as_of: dt.date | None = None,
    limit: int = MAX_RESULT_ROWS,
) -> ScreenResult:
    """``ranking_terms`` screens: the pre-filter frame in one statement, ranked by C4 in-process.

    The frame is the whole selected universe (``build_ranking_frame_query``), because
    ``fixed_universe`` and ``within_sector`` score against rows that are not results; the engine
    returns only the rows that pass every filter, ranked ``1..n``. ``desk_score`` is the joined
    ``desk_score_daily`` value, and a row the book rejected or never scored is never ranked.
    """
    ranked_frame = await rank_definition(session, definition, as_of)
    spec = ranked_frame.spec
    projection = resolve_columns(columns)
    first = spec.terms[0]
    extra: list[str] = []
    if spec.mode == "composite" and ranking_engine.COMPOSITE_SCORE not in projection:
        extra.append(ranking_engine.COMPOSITE_SCORE)
    desk_terms = any(is_desk_score_factor(term.key) for term in spec.terms)
    if desk_terms:
        extra.extend(name for name in DESK_SCORE_EXPLAIN_COLUMNS if name not in projection)
    result_columns = (*projection, *extra)
    ranking_factors = tuple(
        RankingFactor(position, term.key, term.label, _term_direction(term.preference))
        for position, term in enumerate(spec.terms, start=1)
    )

    kept_rows: list[ScreenResultRow] = []
    truncated = False
    if ranked_frame.result is not None:
        ranked = ranked_frame.result.ranked
        truncated = len(ranked) > limit
        for record in ranked.head(limit).to_dict("records"):
            rank = int(str(record[ranking_engine.RANK]))
            values: dict[str, object] = {}
            for name in projection:
                source = first.key if name == "sorting_factor" else name
                values[name] = _cell(record.get(source))
            if ranking_engine.COMPOSITE_SCORE in extra:
                values[ranking_engine.COMPOSITE_SCORE] = _composite_cell(
                    record.get(ranking_engine.COMPOSITE_SCORE)
                )
            if desk_terms:
                parts = {name: record.get(name) for name in DESK_SCORE_EXPLAIN_COLUMNS}
                values.update(
                    _desk_explain_values(
                        record.get(ranking_engine.DESK_SCORE_KEY),
                        record.get(ranking_engine.DESK_REJECT),
                        parts,
                    )
                )
            kept_rows.append(
                ScreenResultRow(
                    rank=rank,
                    instrument_id=int(str(record[ranking_engine.INSTRUMENT_ID])),
                    combined_rank=rank,
                    ranks=(rank, 0, 0),
                    values=values,
                )
            )

    return ScreenResult(
        as_of=as_of,
        data_version=data_version,
        sorting_factor=ranking_factors[0],
        ranking_factors=ranking_factors,
        columns=result_columns,
        rows=tuple(kept_rows),
        requested_as_of=requested_as_of,
        truncated=truncated,
    )


async def execute_screen(  # noqa: PLR0913 - as_of, data_version and the projection are all inputs
    session: AsyncSession,
    definition: ScreenDefinition,
    *,
    as_of: dt.date,
    data_version: int,
    columns: Sequence[str] = (),
    requested_as_of: dt.date | None = None,
    limit: int = MAX_RESULT_ROWS,
) -> ScreenResult:
    """One statement, one round trip (docs/03 §"Request path" step 4).

    A definition with ``ranking_terms`` is ranked by :mod:`baskfy_core.ranking_engine` over
    :func:`~baskfy_core.screener.build_ranking_frame_query` (docs/ranking/PLAN.md C4); every other
    definition takes the legacy docs/06 statement, unchanged. ``sort_by=desk_score`` on the legacy
    path ranks the stored ``desk_score_daily.score`` in SQL and attaches the stored A-F breakdown —
    the Phase 1.2 re-scoring of the filtered survivors is gone (C2: the screener never re-scores).
    """
    if definition.ranking_terms:
        return await _execute_ranking_screen(
            session,
            definition,
            as_of=as_of,
            data_version=data_version,
            columns=columns,
            requested_as_of=requested_as_of,
            limit=limit,
        )
    query = build_screen_query(definition, as_of, columns=columns, limit=limit + 1)
    rows = (await session.execute(query.statement)).all()
    truncated = len(rows) > limit
    kept = tuple(_row_to_result(row, query.columns) for row in rows[:limit])
    result_columns = query.columns
    if is_desk_score_factor(definition.sort_by):
        kept = await _attach_desk_columns(session, as_of, kept)
        result_columns = (
            *query.columns,
            *(name for name in DESK_SCORE_EXPLAIN_COLUMNS if name not in query.columns),
        )
    return ScreenResult(
        as_of=as_of,
        data_version=data_version,
        sorting_factor=query.sorting_factor,
        ranking_factors=query.ranking_factors,
        columns=result_columns,
        rows=kept,
        requested_as_of=requested_as_of,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Provenance (docs/ranking/PLAN.md C6)
# ---------------------------------------------------------------------------


async def desk_score_version(session: AsyncSession, as_of: dt.date) -> str | None:
    """The ``score_version`` of the ``desk_score_daily`` rows for ``as_of``; ``None`` if absent.

    One nightly writes one version per date. ``max`` only makes the read deterministic should a
    re-score ever leave two on the same date.
    """
    return (
        await session.execute(
            select(func.max(DeskScoreDaily.score_version)).where(DeskScoreDaily.date == as_of)
        )
    ).scalar_one_or_none()


async def screen_provenance(
    session: AsyncSession, definition: ScreenDefinition, as_of: dt.date, data_version: int
) -> dict[str, object]:
    """C6: what produced this payload — universe, date, data, engine, desk formula, scope, mode.

    ``desk_score_version`` is only looked up when the SCORE decides the order; a screen that never
    reads ``desk_score_daily`` reports ``None`` rather than a version it did not use.
    """
    universe = UNIVERSE_BY_SLUG[definition.index]
    return {
        "universe": universe.slug,
        "universe_label": universe.name,
        "as_of": as_of.isoformat(),
        "data_version": data_version,
        "ranking_engine_version": (
            ranking_engine.RANKING_ENGINE_VERSION
            if definition.ranking_terms
            else LEGACY_RANKING_ENGINE_VERSION
        ),
        "desk_score_version": (
            await desk_score_version(session, as_of) if uses_desk_score(definition) else None
        ),
        "scope": definition.ranking_scope,
        "mode": definition.ranking_mode,
    }


def payload_with_provenance(result: ScreenResult, provenance: Mapping[str, object]) -> str:
    """The response body: ``ScreenResult.payload()`` plus ``provenance``, canonically encoded."""
    return canonical_json({**result.payload(), "provenance": dict(provenance)})


def _predates_provenance(payload: str) -> bool:
    """A cached run envelope written before C6 — served once, it would lack ``provenance``.

    ``publish`` purges the namespace, but a deploy between publishes would otherwise keep serving
    the old shape until the next one. Only a real envelope (it has ``rows``) is judged.
    """
    return '"rows":' in payload and '"provenance":' not in payload


async def record_run(
    session: AsyncSession, screen_id: int, result: ScreenResult, definition_hash: str
) -> None:
    """``screen_run`` — docs/04 calls it "audit + historical ranks cache".

    Public, and here rather than in the router, because Prompt 20's nightly alert dispatch writes
    the same row (``baskfy_api.alerts.ensure_run``): an alert diffs ``screen_run`` rows, and a
    second copy of this upsert would be a second place for the audit trail's shape to drift.

    Only written when the result was computed rather than served from Redis: docs/04's row stores
    ``[{rank, instrument_id, factor_value}]``, and a cached payload carries no ``instrument_id``
    to store. A cache hit therefore leaves the earlier row for that (screen, as_of, definition)
    exactly as it was, which is the row it would have written anyway.
    """
    rows = [
        {
            "rank": row.rank,
            "instrument_id": row.instrument_id,
            "factor_value": json_number(row.values.get("sorting_factor")),
        }
        for row in result.rows
    ]
    statement = insert(ScreenRun).values(
        screen_id=screen_id,
        as_of=result.as_of,
        definition_hash=definition_hash,
        result_count=result.result_count,
        results=rows,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ScreenRun.screen_id, ScreenRun.as_of, ScreenRun.definition_hash],
            set_={
                "result_count": statement.excluded.result_count,
                "results": statement.excluded.results,
            },
        )
    )


def json_number(value: object) -> str | None:
    """A factor value as it goes into ``screen_run.results`` (JSONB).

    The exact decimal **as a string**. asyncpg serialises JSONB with ``json.dumps``, which has no
    ``Decimal`` support, and routing through ``float`` would put ``13.0`` in the audit trail where
    the API returned ``13.00`` — the disagreement CLAUDE.md house rule 8 exists to prevent. A
    string keeps every digit, and the column is an internal audit record rather than a wire
    format. Recorded in docs/07a §6.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


class SingleFlight:
    """Collapse concurrent misses on one cache key into a single query, per process.

    The problem this solves is a cache stampede, and it is not hypothetical: fifty concurrent
    callers running the *same* screen the moment after a publish all miss the same key and all
    issue the same statement. Measured over Prompt 16's load test (50 concurrent runs of one
    definition), the cold p95 was 503 ms against a 400 ms criterion; with the misses collapsed it
    is a fraction of that, because forty-nine of the fifty queries never happened.

    docs/06 §Caching's warm-up — "warm the top 200 most-run screen definitions after each publish"
    — is the *other* half of the same fix, and the more important one: a warmed key is never
    missed at all. This covers the definitions nobody warmed.

    **Per process, deliberately.** A cross-process lock means a Redis ``SET NX`` with a lease, a
    fencing token and a story for what happens when the leader dies holding it. With one uvicorn
    worker per core (docs/11 §"Cost envelope" sizes the box at 8) the stampede is reduced from N
    callers to 8 — which is a query per core, not a query per request, and that is where the cost
    stops mattering. Written down rather than left as a surprise: this is not a distributed lock
    and must not be relied on as one.
    """

    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Event] = {}

    def claim(self, key: str) -> tuple[bool, asyncio.Event]:
        """``(leading, event)``. The leader computes and signals; a follower waits on the event.

        No lock: every statement here is synchronous, so the whole method runs without the event
        loop getting a chance to interleave another coroutine into the middle of it.
        """
        existing = self._waiters.get(key)
        if existing is not None:
            return False, existing
        event = asyncio.Event()
        self._waiters[key] = event
        return True, event

    def release(self, key: str, event: asyncio.Event) -> None:
        """Signal every follower — whether the leader succeeded or raised.

        Waking followers after a *failure* is deliberate: they re-read the cache, find nothing,
        and compute for themselves. Leaving them blocked until the timeout would turn one failed
        query into fifty slow ones.
        """
        self._waiters.pop(key, None)
        event.set()

    def in_flight(self) -> int:
        """How many distinct keys are being computed. For tests and for a future metric."""
        return len(self._waiters)


#: One per process. Module-level for the same reason the engine is: a per-request instance would
#: collapse nothing.
SCREEN_FLIGHT: Final = SingleFlight()

#: How long a follower waits for the leader before giving up and querying for itself. Above the
#: 800 ms docs/11 budgets for a cold screen run, so a healthy leader is always waited for; low
#: enough that a leader wedged on a lost connection costs one request's latency, not a hang.
SINGLE_FLIGHT_TIMEOUT_SECONDS: Final = 5.0


async def run_screen(  # noqa: PLR0913 - the request, the projection and the cache are separate concerns
    session: AsyncSession,
    definition: ScreenDefinition,
    *,
    requested_as_of: dt.date | None = None,
    columns: Sequence[str] = (),
    cache: ScreenCache | None = None,
    ttl_seconds: int | None = SCREEN_CACHE_FALLBACK_TTL_SECONDS,
) -> ScreenRunResult:
    """docs/03 §"Request path for a screen run" steps 2-5, cache included.

    ``requested_as_of`` overrides ``definition.historical_date``; passing neither means "latest".
    """
    requested = requested_as_of if requested_as_of is not None else definition.historical_date
    resolution = await resolve_as_of(session, requested)
    data_version = await current_data_version(session)
    key = cache_key(definition, resolution.as_of, data_version, columns=columns)
    # PROMPTS.md Prompt 17 §1 names the screen definition hash as a span attribute. It is
    # `definition_hash()`, not the cache key's digest: the cache key also covers the projection
    # (`docs/09a` §3), so two traces for the same screen with different columns would otherwise
    # look like two different screens.
    annotate_current_span(
        {
            "baskfy.screen_definition_hash": definition.definition_hash(),
            "baskfy.as_of": resolution.as_of.isoformat(),
            "baskfy.data_version": data_version,
        }
    )

    leading = False
    flight: asyncio.Event | None = None
    if cache is not None:
        cached = _cached_payload(await cache.get(key))
        if cached is not None and not _predates_provenance(cached):
            return _observed(
                ScreenRunResult(
                    payload=cached,
                    resolution=resolution,
                    data_version=data_version,
                    cache_hit=True,
                )
            )
        leading, flight = SCREEN_FLIGHT.claim(key)
        if not leading:
            # Someone in this process is already computing this exact key. Wait for them rather
            # than issuing a second identical query — see :class:`SingleFlight`.
            with suppress(TimeoutError):
                await asyncio.wait_for(flight.wait(), SINGLE_FLIGHT_TIMEOUT_SECONDS)
            cached = _cached_payload(await cache.get(key))
            if cached is not None and not _predates_provenance(cached):
                # A follower that waited for the leader. Counted as a hit: it was served from the
                # cache and issued no query, which is what the hit-rate metric is measuring.
                return _observed(
                    ScreenRunResult(
                        payload=cached,
                        resolution=resolution,
                        data_version=data_version,
                        cache_hit=True,
                    )
                )

    try:
        result = await execute_screen(
            session,
            definition,
            as_of=resolution.as_of,
            data_version=data_version,
            columns=columns,
            requested_as_of=requested,
        )
        provenance = await screen_provenance(session, definition, resolution.as_of, data_version)
        payload = payload_with_provenance(result, provenance)
        if cache is not None:
            await cache.set(key, payload, ttl_seconds)
    finally:
        if leading and flight is not None:
            SCREEN_FLIGHT.release(key, flight)
    return _observed(
        ScreenRunResult(
            payload=payload,
            resolution=resolution,
            data_version=data_version,
            cache_hit=False,
            result=result,
        )
    )


def _observed(result: ScreenRunResult) -> ScreenRunResult:
    """Record the cache outcome and annotate the span, then hand the result straight back.

    One function at every exit of :func:`run_screen` rather than a counter increment per branch:
    there are three ways out and a missed one is a hit rate that silently lies.
    """
    observe_cache(hit=result.cache_hit)
    annotate_current_span(
        {
            "baskfy.cache_hit": result.cache_hit,
            "baskfy.rows_out": 0 if result.result is None else len(result.result.rows),
        }
    )
    return result


# ---------------------------------------------------------------------------
# Cache warming (deliverable 5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WarmTarget:
    """A screen worth warming, and why it earned the slot."""

    screen_id: int
    public_id: str
    definition: ScreenDefinition
    columns: tuple[str, ...]
    run_count: int


@dataclass(frozen=True, slots=True)
class WarmPlan:
    """What the warm-up will attempt, and what it could not even read.

    Separating the two matters: a saved definition that no longer validates — one persisted
    before the factor-registry gate existed, say — must cost that one screen its warm cache and
    nothing else. Folding it into ``targets`` would either crash the listing or hide the row.
    """

    targets: tuple[WarmTarget, ...]
    unreadable: tuple[str, ...] = ()


async def top_screen_definitions(
    session: AsyncSession, limit: int = WARM_CACHE_SCREEN_LIMIT
) -> WarmPlan:
    """The ``limit`` most-run screens, newest-first among never-run ones.

    docs/06 says "the top 200 most-run screen definitions"; ``screen_run`` is the only record of
    how often a definition has been run (docs/04: it is the audit trail). Screens that have never
    run sort after those that have, so that a brand-new deployment — where ``screen_run`` is
    empty — still warms the example screens rather than warming nothing at all.
    """
    runs = (
        select(ScreenRun.screen_id, func.count().label("run_count"))
        .group_by(ScreenRun.screen_id)
        .subquery()
    )
    statement = (
        select(Screen, func.coalesce(runs.c.run_count, 0).label("run_count"))
        .outerjoin(runs, runs.c.screen_id == Screen.id)
        .order_by(
            func.coalesce(runs.c.run_count, 0).desc(),
            Screen.updated_at.desc(),
            Screen.id.asc(),
        )
        .limit(limit)
    )
    targets: list[WarmTarget] = []
    unreadable: list[str] = []
    for screen, run_count in (await session.execute(statement)).all():
        try:
            definition = ScreenDefinition.model_validate(screen.definition)
        except ValidationError as exc:
            unreadable.append(f"{screen.public_id}: {exc.error_count()} validation error(s)")
            continue
        targets.append(
            WarmTarget(
                screen_id=screen.id,
                public_id=screen.public_id,
                definition=definition,
                columns=tuple(screen.columns),
                run_count=int(run_count),
            )
        )
    return WarmPlan(tuple(targets), tuple(unreadable))


@dataclass(frozen=True, slots=True)
class WarmCacheResult:
    warmed: int
    skipped: int
    failures: tuple[str, ...]


async def warm_screen_cache(
    session: AsyncSession,
    cache: ScreenCache,
    *,
    limit: int = WARM_CACHE_SCREEN_LIMIT,
    ttl_seconds: int | None = SCREEN_CACHE_FALLBACK_TTL_SECONDS,
) -> WarmCacheResult:
    """Recompute and cache the hottest screens for the current ``data_version``.

    Called by step 10 (``publish``) once the quality gate has passed. A screen that fails to run
    is recorded and skipped rather than aborting the warm-up: a single malformed saved definition
    must not deprive every other screen of a warm cache.

    **Every computed run is also stored in ``screen_run``** (14 Sep 2026). The warm-up already
    paid for the query; before this it kept the result only in Redis, so a stock's factsheet
    could not say which screens it is in without running them all again. The row is the same
    idempotent upsert the run route writes, so a second publish of the same session changes
    nothing. A cache hit stores nothing, for the reason :func:`record_run` gives.
    """
    plan = await top_screen_definitions(session, limit)
    warmed = 0
    skipped = 0
    failures: list[str] = list(plan.unreadable)
    for target in plan.targets:
        try:
            outcome = await run_screen(
                session,
                target.definition,
                columns=target.columns,
                cache=cache,
                ttl_seconds=ttl_seconds,
            )
        except (ValueError, NoPublishedData) as exc:
            failures.append(f"{target.public_id}: {exc}")
            continue
        if outcome.result is not None:
            await record_run(
                session, target.screen_id, outcome.result, target.definition.definition_hash()
            )
        if outcome.cache_hit:
            skipped += 1
        else:
            warmed += 1
    return WarmCacheResult(warmed=warmed, skipped=skipped, failures=tuple(failures))


async def purge_screen_cache(cache: object) -> int:
    """Delete every ``screen:*`` key — docs/06: "Invalidate the whole namespace on publish".

    Duck-typed rather than declared on :class:`ScreenCache`: scanning and deleting a namespace is
    an operator capability, not something a read path needs, and a client that cannot do it is
    reported by the caller rather than refused here.
    """
    scanner = getattr(cache, "scan_iter", None)
    deleter = getattr(cache, "delete", None)
    if not callable(scanner) or not callable(deleter):
        return 0
    keys = [key async for key in scanner(match=f"{CACHE_NAMESPACE}*")]
    if not keys:
        return 0
    await deleter(*keys)
    return len(keys)


async def latest_factor_date(session: AsyncSession) -> dt.date | None:
    """The newest ``factor_daily`` date, published or not — for diagnostics only.

    Deliberately *not* used to resolve an as-of date: see :class:`NoPublishedData`.
    """
    return (await session.execute(select(func.max(FactorDaily.date)))).scalar_one_or_none()
