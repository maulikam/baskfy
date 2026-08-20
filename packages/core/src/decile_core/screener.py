"""The screener query engine — docs/06-screener-semantics.md (Prompt 6 deliverable 1).

    "The order of operations is load-bearing. Getting it wrong changes results silently."

This module turns a validated :class:`~decile_core.screen_definition.ScreenDefinition` plus an
as-of date into **one** parameterised SQL statement, following docs/06 §"Reference SQL skeleton"
step for step. It is pure: it builds a SQLAlchemy ``Select`` and hands it back. Nothing here opens
a connection, reads a clock or touches Redis — docs/02 §"Repo layout" keeps ``packages/core``
I/O-free, so execution and caching live in ``decile_api.screener``.

Why a SQLAlchemy construct rather than a formatted string
---------------------------------------------------------
docs/06: "`<factorN_expr>` is produced by a **whitelisted** factor registry that maps a factor key
to a SQL expression — never string-interpolated from user input." Every *value* a user supplies
(thresholds, series codes, dates) becomes a bind parameter by construction; every *identifier* a
user supplies (a factor key, a custom-filter operand, a column) is looked up in the registry
before any SQL exists, and the lookup raises on a miss. The only strings that reach the SQL text
are the ``sql_expr`` values written by hand in ``decile_core.factor_registry``.

Four places where this file departs from the letter of docs/06, and why
-----------------------------------------------------------------------
All four are written up in ``docs/06a-screener-implementation-notes.md``.

1. **``QUALIFY`` does not exist in PostgreSQL.** The skeleton's
   ``QUALIFY PERCENT_RANK() OVER (…) <= :bucket_pct`` is Snowflake/DuckDB syntax; docs/02 locks
   PostgreSQL 16. The window function is therefore computed in the ``bucketed`` CTE and filtered
   in the ``selected`` CTE immediately after it — identical semantics, two CTEs instead of one.
2. **``NULLS LAST`` on the marketcap ordering.** docs/06 writes ``ORDER BY f.marketcap_cr DESC``.
   PostgreSQL sorts NULLs *first* under ``DESC``, which would place every instrument with an
   unknown marketcap in the top decile — the exact opposite of docs/06 §step 4's "NULLs never
   satisfy a predicate". Explicit ``DESC NULLS LAST``.
3. **A tie-breaker inside every ``ROW_NUMBER``.** docs/06 §"Determinism guarantee" requires
   byte-identical results for the same inputs, but ``ROW_NUMBER() OVER (ORDER BY f DESC)`` leaves
   the order *within* a tie unspecified, so two runs can hand the same two rows opposite ranks.
   Every ranking window therefore orders by ``(<factor> <dir> NULLS LAST, instrument_id ASC)``.
   Ties still receive consecutive integers, which is what docs/06 §step 5 asks for; they now
   receive the *same* consecutive integers every time.
4. **Only active filters are emitted.** The skeleton parameterises the on/off switch too
   (``:min_ret_1y IS NULL OR ret_12m >= :min_ret_1y``). Emitting the clause only when the filter
   is active keeps the sentinel logic (docs/01 §2.4-§2.10) in one place — the
   ``is_active`` predicates on ``ScreenDefinition`` — instead of restating it in SQL, and gives
   the planner a WHERE clause it can actually use an index on. The values are still bound
   parameters, and the statement is still one statement.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Protocol

from sqlalchemy import Select, create_mock_engine, literal, literal_column, select
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.sql import ColumnElement, func
from sqlalchemy.sql.elements import KeyedColumnElement, Label
from sqlalchemy.sql.selectable import CTE

from decile_core import factor_registry
from decile_core.factor_registry import COLUMN_PICKER_KEYS, CUSTOM_FILTER_OPERANDS, Factor
from decile_core.models import FactorDaily, IndexMemberDaily, Instrument, OhlcvDaily
from decile_core.screen_definition import ScreenDefinition
from decile_core.universes import DECILE_RANK_KEY, UNIVERSE_BY_SLUG

#: docs/06 §step 3 — the bucket table. Deciles are a fraction of the universe; ``top_50`` and
#: ``top_100`` are row counts ("or LIMIT n" in the skeleton).
BUCKET_FRACTIONS: Final[Mapping[str, Decimal]] = {
    "decile_1": Decimal("0.10"),
    "decile_2": Decimal("0.20"),
    "decile_3": Decimal("0.30"),
    "decile_4": Decimal("0.40"),
    "decile_5": Decimal("0.50"),
}
BUCKET_ROW_LIMITS: Final[Mapping[str, int]] = {"top_50": 50, "top_100": 100}

#: docs/07 §"Running a screen" — the first three entries of the response's ``columns`` array. The
#: ``#`` column of docs/01 §4 is the row's ``rank`` and is always present, so it is not listed.
IDENTITY_COLUMNS: Final[tuple[str, ...]] = ("symbol", "name", "sorting_factor")

#: docs/01 §4 "Default columns", minus the identity ones: Last Close, Series, Marketcap,
#: 1Yr Return, 1Yr Sharpe, 1Yr Volatility, Beta, MA 200. "Last Close" is ``close_raw`` — the
#: exchange print, per CLAUDE.md house rule 6 and docs/07's own example row.
DEFAULT_RESULT_COLUMNS: Final[tuple[str, ...]] = (
    "close_raw",
    "series",
    "marketcap_cr",
    "ret_12m",
    "sharpe_12m",
    "vol_12m",
    "beta_12m",
    "ma_200",
)

#: docs/03 §"Request path": "Result rows (<= 4,000)".
MAX_RESULT_ROWS: Final = 4000

#: docs/06 §Caching: "Key: `screen:{sha256(definition_canonical_json)}:{as_of}:{data_version}`".
#: The prefix is shared with ``decile_worker.tasks.publish``, which purges by it.
CACHE_NAMESPACE: Final = "screen:"

#: A PostgreSQL dialect with no connection behind it, for rendering a statement to text. Built
#: through ``create_mock_engine`` because that is the typed way to obtain one — the dialect
#: classes themselves have untyped constructors, and CLAUDE.md house rule 3 forbids the
#: ``type: ignore`` that calling one would need.
_POSTGRES: Final[Dialect] = create_mock_engine("postgresql+asyncpg://", lambda *args: None).dialect

_MA_LENGTHS: Final[tuple[int, ...]] = (200, 100, 50, 20)
_WINDOW_FIELDS: Final[Mapping[str, int]] = {"m12": 12, "m9": 9, "m6": 6, "m3": 3, "m1": 1}


class ColumnLookup(Protocol):
    """Anything that can hand back a column by name.

    SQLAlchemy's ``ReadOnlyColumnCollection`` is not a ``Mapping``, and a CTE's ``.c`` is exactly
    what the filter builders need, so the contract is stated structurally rather than by naming a
    concrete collection type.
    """

    def __getitem__(self, key: str) -> KeyedColumnElement[object]: ...


class ScreenQueryError(ValueError):
    """A definition that cannot be turned into SQL.

    Raised *before* any statement is constructed, which is the property Prompt 6's first
    acceptance criterion asks for: a malicious factor key or custom-filter operand never reaches
    query construction, let alone the SQL text.
    """


# ---------------------------------------------------------------------------
# Validation — the whitelist gate (deliverable 3)
# ---------------------------------------------------------------------------


def _require_factor(key: str, *, where: str) -> Factor:
    try:
        return factor_registry.get(key)
    except KeyError as exc:
        raise ScreenQueryError(
            f"{where}: {key!r} is not a factor-registry key, so no SQL will be built for it"
        ) from exc


def _require_operand(key: str, *, where: str) -> str:
    """docs/01 §2.14's operand list, which is a strict subset of the registry.

    A blend or a beta-guarded ratio is a legitimate *sort* key but not a legitimate custom-filter
    operand — comparing ``avg_sharpe_12_6_3_1 >= ma_200`` is meaningless — so the narrower list
    from the registry is what gates this, not the 64-key one.
    """
    if key not in CUSTOM_FILTER_OPERANDS:
        raise ScreenQueryError(
            f"{where}: {key!r} is not a custom-filter operand "
            f"(docs/01 §2.14 lists {len(CUSTOM_FILTER_OPERANDS)})"
        )
    return key


def validate_definition(definition: ScreenDefinition) -> None:
    """Reject every user-supplied identifier that is not in the registry.

    Called at the top of :func:`build_screen_query`, and callable on its own by the API layer so
    a bad definition fails validation (422) rather than query construction (500).
    """
    _require_factor(definition.sort_by, where="sort_by")
    extras = (("factor_two", definition.factor_two), ("factor_three", definition.factor_three))
    for name, extra in extras:
        if extra.is_active() and extra.sort_by is not None:
            _require_factor(extra.sort_by, where=f"{name}.sort_by")
    for position, custom in enumerate(definition.custom_filters, start=1):
        if not custom.is_active():
            continue
        _require_operand(custom.left, where=f"custom_filters[{position}].left")
        _require_operand(custom.right, where=f"custom_filters[{position}].right")
    if definition.apply_filters_on not in ("all", *BUCKET_FRACTIONS, *BUCKET_ROW_LIMITS):
        raise ScreenQueryError(f"apply_filters_on: unknown bucket {definition.apply_filters_on!r}")


def resolve_columns(saved: Sequence[str] = ()) -> tuple[str, ...]:
    """docs/06 §step 7: "Default columns … plus any of the 34 optional columns the user saved".

    Order is identity, then the defaults in docs/01 §4's order, then the saved extras in the
    user's own order. Duplicates collapse; an unknown key is refused rather than ignored, because
    silently dropping a column the user picked is a bug they cannot see.
    """
    ordered: list[str] = [*IDENTITY_COLUMNS, *DEFAULT_RESULT_COLUMNS]
    seen = set(ordered)
    for key in saved:
        if key in seen:
            continue
        if key not in COLUMN_PICKER_KEYS:
            raise ScreenQueryError(
                f"columns: {key!r} is not one of the {len(COLUMN_PICKER_KEYS)} selectable result "
                "columns (docs/01 §4)"
            )
        ordered.append(key)
        seen.add(key)
    return tuple(ordered)


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RankingFactor:
    """One of the up-to-three factors whose ranks are summed (docs/06 §step 5-6)."""

    position: int
    key: str
    label: str
    direction: str


@dataclass(frozen=True, slots=True)
class ScreenQuery:
    """One parameterised statement, plus what the caller needs to read its rows."""

    statement: Select[tuple[object]]
    as_of: dt.date
    index_id: int
    universe_slug: str
    #: Result column names in projection order (docs/07's ``columns``).
    columns: tuple[str, ...]
    #: Factor one — docs/06: "The `Sorting Factor` column always displays the value of factor one".
    sorting_factor: RankingFactor
    ranking_factors: tuple[RankingFactor, ...]
    definition_hash: str

    def sql(self) -> str:
        """The rendered PostgreSQL text, values still bound.

        Used by the injection tests (nothing user-supplied may appear here) and by the EXPLAIN
        test. Rendering with ``literal_binds`` is deliberately *not* offered: there is no
        supported way to ask for it by accident.
        """
        return str(self.statement.compile(dialect=_POSTGRES))

    def params(self) -> Mapping[str, object]:
        return dict(self.statement.compile(dialect=_POSTGRES).params)


def _bucket_order(column: KeyedColumnElement[object]) -> ColumnElement[object]:
    """docs/06 §step 3: "Rank the universe by **marketcap descending**" — see departure 2."""
    return column.desc().nullslast()


def _scalar_clauses(definition: ScreenDefinition, cols: ColumnLookup) -> list[ColumnElement[bool]]:
    """The single-threshold and range filters of docs/06 §step 4's table."""
    clauses: list[ColumnElement[bool]] = []

    if definition.min_return_1y is not None:
        clauses.append(cols["ret_12m"] >= definition.min_return_1y)

    if definition.median_volume_1y is not None:
        clauses.append(cols["median_vol_12m"] >= definition.median_volume_1y)

    if definition.marketcap.from_ is not None:
        clauses.append(cols["marketcap_cr"] >= definition.marketcap.from_)
    if definition.marketcap.to is not None:
        clauses.append(cols["marketcap_cr"] <= definition.marketcap.to)

    if definition.pe.is_active():
        # docs/06: "`pe BETWEEN a AND b AND pe IS NOT NULL`". The IS NOT NULL is redundant under
        # three-valued logic and is kept because docs/01 §2.8 makes it a promise to the user:
        # switching the P/E filter on excludes loss-making companies, whose P/E is undefined.
        clauses.append(cols["pe"].is_not(None))
        if definition.pe.from_ is not None:
            clauses.append(cols["pe"] >= definition.pe.from_)
        if definition.pe.to is not None:
            clauses.append(cols["pe"] <= definition.pe.to)

    if definition.series:
        clauses.append(cols["series"].in_(list(definition.series)))

    if definition.ignore_above_beta_is_active():
        clauses.append(cols["beta_12m"] <= definition.ignore_above_beta)

    if definition.price.from_ is not None:
        clauses.append(cols["close_raw"] >= definition.price.from_)
    if definition.price.to is not None:
        clauses.append(cols["close_raw"] <= definition.price.to)

    return clauses


def _moving_average_clauses(
    definition: ScreenDefinition, cols: ColumnLookup
) -> list[ColumnElement[bool]]:
    """docs/01 §2.3's eight independent switches, behind one group switch."""
    moving_average = definition.moving_average
    if not moving_average.is_active():
        return []
    clauses: list[ColumnElement[bool]] = []
    for length in _MA_LENGTHS:
        if getattr(moving_average, f"above_{length}"):
            clauses.append(cols["close"] > cols[f"ma_{length}"])
        if getattr(moving_average, f"below_{length}"):
            clauses.append(cols["close"] < cols[f"ma_{length}"])
    return clauses


def _windowed_clauses(
    definition: ScreenDefinition, cols: ColumnLookup
) -> list[ColumnElement[bool]]:
    """The per-window filters: away-from-high, positive days, circuits."""
    clauses: list[ColumnElement[bool]] = []

    away = definition.away_from_high
    if away.ath_is_active():
        clauses.append(func.abs(cols["away_high_ath"]) <= away.ath)
    if away.one_year_is_active():
        clauses.append(func.abs(cols["away_high_1y"]) <= away.one_year)

    positive_days = definition.positive_days
    for window in positive_days.active_windows():
        column = cols[f"pos_days_{_WINDOW_FIELDS[window]}m"]
        clauses.append(column >= getattr(positive_days, window))

    circuits = definition.circuits
    for window in circuits.active_windows():
        column = cols[f"circuits_{_WINDOW_FIELDS[window]}m"]
        clauses.append(column <= getattr(circuits, window))

    return clauses


def _custom_filter_clauses(
    definition: ScreenDefinition, cols: ColumnLookup
) -> list[ColumnElement[bool]]:
    """docs/01 §2.14's three field-to-field slots, operands re-checked against the registry."""
    clauses: list[ColumnElement[bool]] = []
    for position, custom in enumerate(definition.custom_filters, start=1):
        if not custom.is_active():
            continue
        left = cols[_require_operand(custom.left, where=f"custom_filters[{position}].left")]
        right = cols[_require_operand(custom.right, where=f"custom_filters[{position}].right")]
        if custom.op == ">=":
            clauses.append(left >= right)
        elif custom.op == "<=":
            clauses.append(left <= right)
        else:
            clauses.append(left == right)
    return clauses


def _filter_clauses(definition: ScreenDefinition, cols: ColumnLookup) -> list[ColumnElement[bool]]:
    """docs/06 §step 4's table, in the document's own order. All AND-combined.

    Every clause relies on SQL's own three-valued logic for docs/06's NULL rule — "NULLs never
    satisfy a predicate". ``NULL >= 5`` is NULL, not TRUE, so ``WHERE`` drops the row. There is
    deliberately no ``COALESCE`` anywhere below: one would silently readmit exactly the young
    listings docs/06 says must be excluded.
    """
    return [
        *_scalar_clauses(definition, cols),
        *_moving_average_clauses(definition, cols),
        *_windowed_clauses(definition, cols),
        *_custom_filter_clauses(definition, cols),
    ]


def _risk_clauses(
    definition: ScreenDefinition, cols: ColumnLookup, mask_bit: int
) -> list[ColumnElement[bool]]:
    """docs/06 §step 4 ⚠ and Prompt 6 §2b — the precomputed per-universe flags.

    Not a windowed exclusion over the survivors: docs/13 §2 finding 10 proves the cut is taken
    over the whole universe nightly (within every universe the flagged rows' minimum beta strictly
    exceeds the unflagged rows' maximum), so the screener tests one bit. ``mask_bit`` selects the
    bit for the screen's *current* universe out of ``factor_daily.top_beta_mask`` /
    ``.top_volatility_mask`` (``decile_core.universes.Universe.mask_value``).
    """
    clauses: list[ColumnElement[bool]] = []
    if definition.ignore_top_beta.is_active():
        clauses.append(cols["top_beta_mask"].op("&")(mask_bit) == 0)
    if definition.ignore_top_volatility.is_active():
        clauses.append(cols["top_volatility_mask"].op("&")(mask_bit) == 0)
    return clauses


def _factor_expression(key: str) -> ColumnElement[object]:
    """The registry's hand-written SQL for ``key``, as an unqualified expression.

    ``literal_column`` embeds the string verbatim, which is safe here and only here: the string
    came out of ``decile_core.factor_registry``, never off the wire, and :func:`validate_definition`
    has already refused anything that is not a registry key. The expression names bare columns
    (``sharpe_12m``), so it may only be used in a SELECT with exactly one FROM entry — which is
    why ranking happens in its own single-source CTE.
    """
    return literal_column(factor_registry.sql_for(key))


def _ranking_factors(definition: ScreenDefinition) -> tuple[RankingFactor, ...]:
    return tuple(
        RankingFactor(position, key, factor_registry.get(key).label, direction)
        for position, (key, direction) in enumerate(definition.ranking_factors(), start=1)
    )


def _bucketed_universe(definition: ScreenDefinition, universe_cte: CTE, as_of: dt.date) -> CTE:
    """Steps 2-3: the universe on ``as_of``, cut down to the ``apply_filters_on`` bucket.

    Two CTEs rather than the skeleton's one, because PostgreSQL has no ``QUALIFY`` and a window
    function cannot appear in the ``WHERE`` of the select that computes it. The window column is
    dropped on the way out, so nothing downstream can accidentally rank or filter on it.
    """
    facts = FactorDaily.__table__
    fraction = BUCKET_FRACTIONS.get(definition.apply_filters_on)
    row_limit = BUCKET_ROW_LIMITS.get(definition.apply_filters_on)

    marketcap_order = _bucket_order(facts.c[DECILE_RANK_KEY])
    if fraction is not None:
        bucket_windows = [
            func.percent_rank().over(order_by=marketcap_order).label("bucket_percent_rank")
        ]
    elif row_limit is not None:
        bucket_windows = [
            func.row_number().over(order_by=marketcap_order).label("bucket_row_number")
        ]
    else:
        bucket_windows = []

    bucketed = (
        select(facts, *bucket_windows)
        .select_from(
            facts.join(universe_cte, facts.c.instrument_id == universe_cte.c.instrument_id)
        )
        .where(facts.c.date == as_of)
        .cte("bucketed")
    )
    if not bucket_windows:
        return bucketed

    kept = [bucketed.c[column.name] for column in facts.c]
    cut = (
        bucketed.c.bucket_percent_rank <= fraction
        if fraction is not None
        else bucketed.c.bucket_row_number <= row_limit
    )
    return select(*kept).where(cut).cte("selected")


@dataclass(frozen=True, slots=True)
class RankedPipeline:
    """Steps 1-6 of docs/06, ready to be projected.

    Split out so that the CSV export (docs/13's 93 columns) and the JSON response (docs/01 §4's
    column picker) can project *the same rows* differently. They must agree about which
    instruments qualify and in what order — an export that disagrees with the table above it is
    worse than no export — so there is one pipeline and two projections, not two pipelines.
    """

    ranked: CTE
    combined: Label[int]
    order_by: list[ColumnElement[object]]
    ranking: tuple[RankingFactor, ...]


def _ranked_pipeline(definition: ScreenDefinition, as_of: dt.date) -> RankedPipeline:
    universe = UNIVERSE_BY_SLUG[definition.index]

    # --- Step 2: point-in-time universe --------------------------------------
    # docs/06: "Never resolve a historical universe from today's membership — that is the single
    # most common source of backtest look-ahead bias." The join key is (index_id, date), and
    # `index_member_daily` is the normalised point-in-time source of truth. `nifty-allcap` and
    # `etf` are rule-derived rather than file-derived (docs/06 §step 2), but the nightly
    # `refresh_index_membership` step materialises them as rows with source='derived', so there
    # is one read path for all fourteen.
    universe_cte = (
        select(IndexMemberDaily.instrument_id)
        .where(
            IndexMemberDaily.index_id == universe.index_id,
            IndexMemberDaily.date == as_of,
        )
        .cte("universe")
    )

    selected = _bucketed_universe(definition, universe_cte, as_of)

    # --- Step 4: every filter except sort_by / sort_direction ----------------
    filtered = select(selected).where(*_filter_clauses(definition, selected.c)).cte("filtered")
    relative = (
        select(filtered)
        .where(*_risk_clauses(definition, filtered.c, universe.mask_value))
        .cte("relative")
    )

    # --- Steps 5-6: rank per factor, sum, sort ascending ---------------------
    ranking = _ranking_factors(definition)
    rank_columns: list[Label[int]] = []
    for slot in range(1, 4):
        label = f"r{slot}"
        if slot > len(ranking):
            rank_columns.append(literal(0).label(label))
            continue
        factor = ranking[slot - 1]
        expression = _factor_expression(factor.key)
        ordering = expression.desc() if factor.direction == "desc" else expression.asc()
        rank_columns.append(
            func.row_number()
            .over(order_by=[ordering.nullslast(), relative.c.instrument_id.asc()])
            .label(label)
        )

    ranked = select(
        relative,
        _factor_expression(ranking[0].key).label("sorting_factor"),
        *rank_columns,
    ).cte("ranked")

    combined = (ranked.c.r1 + ranked.c.r2 + ranked.c.r3).label("combined_rank")
    return RankedPipeline(
        ranked=ranked,
        combined=combined,
        order_by=[combined.asc(), ranked.c.r1.asc()],
        ranking=ranking,
    )


def build_screen_query(
    definition: ScreenDefinition,
    as_of: dt.date,
    *,
    columns: Sequence[str] = (),
    limit: int = MAX_RESULT_ROWS,
) -> ScreenQuery:
    """docs/06's seven-step pipeline as one statement.

    ``as_of`` must already be resolved (docs/06 §step 1 is a database question — which dates are
    trading days, which runs are published — and therefore lives in ``decile_api.screener``).
    """
    validate_definition(definition)
    projection = resolve_columns(columns)
    universe = UNIVERSE_BY_SLUG[definition.index]
    pipeline = _ranked_pipeline(definition, as_of)
    ranked = pipeline.ranked
    combined = pipeline.combined
    final_order = pipeline.order_by
    ranking = pipeline.ranking

    # --- Step 7: projection --------------------------------------------------
    projected = [
        func.row_number().over(order_by=final_order).label("rank"),
        ranked.c.instrument_id,
        combined,
        ranked.c.r1,
        ranked.c.r2,
        ranked.c.r3,
        # ``symbol`` and ``name`` are the instrument's, not the fact row's (docs/03 §"Request
        # path": "factor_daily ⋈ index_member_daily ⋈ instrument").
        *[
            Instrument.symbol.label("symbol")
            if name == "symbol"
            else Instrument.name.label("name")
            if name == "name"
            else ranked.c[name].label(name)
            for name in projection
        ],
    ]

    statement = (
        select(*projected)
        .select_from(ranked.join(Instrument, Instrument.id == ranked.c.instrument_id))
        .order_by(*final_order)
        .limit(limit)
    )

    return ScreenQuery(
        statement=statement,
        as_of=as_of,
        index_id=universe.index_id,
        universe_slug=universe.slug,
        columns=projection,
        sorting_factor=ranking[0],
        ranking_factors=ranking,
        definition_hash=definition.definition_hash(),
    )


# ---------------------------------------------------------------------------
# Caching key and canonical serialisation (deliverable 5, docs/06 §Determinism)
# ---------------------------------------------------------------------------


def cache_key(
    definition: ScreenDefinition,
    as_of: dt.date,
    data_version: int,
    *,
    columns: Sequence[str] = (),
) -> str:
    """docs/06 §Caching: ``screen:{sha256(definition_canonical_json)}:{as_of}:{data_version}``.

    **The digest covers the projection as well as the definition**, which docs/06's formula does
    not say. It has to: the cached value is the *response body*, and the response body carries
    ``columns`` and one member per column (docs/07 §"Running a screen"). Two requests can share a
    definition and differ in columns — that is exactly what the columns editor does, and what
    ``POST /screens/preview`` accepts as a separate field — so hashing the definition alone serves
    the second request the first one's columns. Found by the browser suite; see ``docs/09a`` §3.

    The key's *shape* is unchanged, and ``screen_run.definition_hash`` (docs/04) still uses
    :meth:`ScreenDefinition.definition_hash`, because that column answers "what did the user run",
    not "what bytes did we send".
    """
    projection = ",".join(resolve_columns(columns))
    # \x1f is the ASCII unit separator: it cannot occur in canonical JSON or in a column key, so
    # no definition-and-columns pair can collide with a different one by concatenation.
    payload = f"{definition.canonical_json()}\x1f{projection}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{CACHE_NAMESPACE}{digest}:{as_of.isoformat()}:{data_version}"


def canonical_json(payload: object) -> str:
    """A byte-stable JSON encoding that keeps ``Decimal`` exact.

    docs/06 §"Determinism guarantee": "Given the same `data_version`, the same definition and the
    same `as_of`, results are byte-identical." ``json.dumps`` cannot do this on its own — it has
    no ``Decimal`` support, and routing through ``float`` would both violate CLAUDE.md house rule
    9 ("money and prices are `numeric`, never `float`") and lose the storage precision docs/13 §4
    calls the contract: ``Decimal("13.00")`` would render as ``13.0``.

    So decimals are emitted as their own string form, unquoted — a valid JSON number, and exactly
    the ``"sharpe_12m": 13.00`` docs/07 §"Running a screen" shows in its example row.
    """
    parts: list[str] = []
    _encode(payload, parts)
    return "".join(parts)


def _encode(value: object, out: list[str]) -> None:
    if isinstance(value, Decimal):
        out.append(_decimal_token(value))
    elif isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        out.append(json.dumps(value))
    elif isinstance(value, str):
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, dt.date):
        out.append(json.dumps(value.isoformat()))
    elif isinstance(value, Mapping):
        out.append("{")
        for index, key in enumerate(value):
            if index:
                out.append(",")
            out.append(json.dumps(str(key), ensure_ascii=False))
            out.append(":")
            _encode(value[key], out)
        out.append("}")
    elif isinstance(value, Iterable):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _encode(item, out)
        out.append("]")
    else:
        raise TypeError(f"cannot canonically encode {type(value).__name__}")


def _decimal_token(value: Decimal) -> str:
    """``Decimal("13.00")`` -> ``13.00``; ``Decimal("1E+3")`` -> ``1000``.

    Exponent notation is legal JSON but is not what the reference export writes, and two equal
    values must not produce two different bytes, so the exponent is always expanded.
    """
    if not value.is_finite():
        raise ValueError(f"{value} is not a finite decimal and has no JSON representation")
    text = format(value, "f")
    return "-0" if text == "-0" else text


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScreenResultRow:
    """One result row. ``values`` is keyed by :attr:`ScreenResult.columns`."""

    rank: int
    instrument_id: int
    combined_rank: int
    ranks: tuple[int, int, int]
    values: Mapping[str, object]

    @property
    def symbol(self) -> str:
        return str(self.values["symbol"])


@dataclass(frozen=True, slots=True)
class ScreenResult:
    """The payload of docs/07 §"Running a screen"."""

    as_of: dt.date
    data_version: int
    sorting_factor: RankingFactor
    columns: tuple[str, ...]
    rows: tuple[ScreenResultRow, ...]
    #: docs/06 §step 1: "tell the client which date was actually used".
    requested_as_of: dt.date | None = None
    truncated: bool = False
    ranking_factors: tuple[RankingFactor, ...] = field(default_factory=tuple)

    @property
    def result_count(self) -> int:
        return len(self.rows)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(row.symbol for row in self.rows)

    def payload(self) -> Mapping[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "data_version": self.data_version,
            "result_count": self.result_count,
            "sorting_factor": {
                "key": self.sorting_factor.key,
                "label": self.sorting_factor.label,
            },
            "columns": list(self.columns),
            "rows": [{"rank": row.rank, **dict(row.values)} for row in self.rows],
        }

    def to_json(self) -> str:
        """The cached bytes, and the determinism guarantee's unit of comparison."""
        return canonical_json(self.payload())


# ---------------------------------------------------------------------------
# The reference CSV export (docs/13 §1)
# ---------------------------------------------------------------------------

#: The four export columns that live on the daily bar rather than on the fact row
#: (``decile_core.reference_export.UNMAPPED_EXPORT_COLUMNS``).
BAR_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "volume_shares")


def build_export_query(
    definition: ScreenDefinition,
    as_of: dt.date,
    *,
    limit: int = MAX_RESULT_ROWS,
) -> Select[tuple[object]]:
    """The same rows as :func:`build_screen_query`, projected for docs/13's 93-column export.

    docs/13 §5 step 6: "Assert our CSV export reproduces this file's exact column names, order,
    quoting and BOM." That file carries every stored factor and all 42 universe flags, not the
    user's chosen columns — so the export is a *different projection of the same pipeline*, which
    is why ``_ranked_pipeline`` exists.

    ``ohlcv_daily`` is **left**-joined. The export's `open`/`high`/`low`/`volume_shares` are the
    exchange's prints, and a database seeded from the reference export alone has factor rows and
    no bars. A left join renders those four columns empty rather than dropping every row, which is
    the honest answer: the rest of the export is real.
    """
    pipeline = _ranked_pipeline(definition, as_of)
    ranked = pipeline.ranked
    bars = OhlcvDaily.__table__

    # Built as one literal so that mypy joins the element types itself; annotating the list would
    # require every entry to be the same `ColumnElement[T]`, and they are deliberately not.
    projected = [
        func.row_number().over(order_by=pipeline.order_by).label("rank"),
        Instrument.symbol.label("symbol"),
        Instrument.name.label("name"),
        bars.c.open.label("open"),
        bars.c.high.label("high"),
        bars.c.low.label("low"),
        bars.c.volume_raw.label("volume_shares"),
        *[ranked.c[column.name] for column in FactorDaily.__table__.c],
    ]

    return (
        select(*projected)
        .select_from(
            ranked.join(Instrument, Instrument.id == ranked.c.instrument_id).outerjoin(
                bars,
                (bars.c.instrument_id == ranked.c.instrument_id) & (bars.c.date == as_of),
            )
        )
        .order_by(*pipeline.order_by)
        .limit(limit)
    )
