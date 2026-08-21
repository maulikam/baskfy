"""``EXPLAIN ANALYZE`` for every hot query, and a baseline CI can regress against.

Prompt 16 deliverable 2:

    "Query tuning: EXPLAIN ANALYZE every hot query, add or adjust indexes, and add a CI check
     that fails if any hot query's plan changes to a sequential scan on the seeded dataset."

What "hot" means here is docs/03 §"Request path for a screen run (the hot path)" plus the
surfaces docs/11 §"Performance budgets" puts a number on: the screen run, the dashboard, the
factsheet, the CSV export's row source, and the breadth history. Every one of them is defined
below as the statement the service actually issues, built from the same code the request handler
uses wherever that is possible — a plan for a hand-written approximation of the query would tune
something we do not run.

Why a *baseline*, not a blanket ban on sequential scans
-------------------------------------------------------
On the seeded dataset (docs/13's 271-row export) PostgreSQL is right to scan sequentially: with
one page in the table an index adds a level of indirection to save nothing, and forcing an index
with ``enable_seqscan = off`` would measure a plan production never runs. So the check is a
*change detector*, which is exactly what the deliverable asks for — "fails if any hot query's
plan **changes to** a sequential scan". Each relation's scan node is recorded in
``query_plan_baseline.json``; a relation that was index-scanned and is now sequentially scanned
fails, and a new query with no baseline entry fails until someone records it deliberately.

The honest limitation is written down here rather than left to be discovered: a plan captured
against 271 rows tells you almost nothing about the plan against 8.5M (docs/04 §"Retention & size
estimates"). What this catches is a *lost index* — a migration that drops one, a predicate that
stops being sargable because a column got wrapped in a function — which is the failure mode that
otherwise reaches production and is invisible until the table is big.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sqlalchemy import Select, create_mock_engine, func, select
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text

from decile_api.db import session_scope
from decile_api.market_data import ListingQuery, listings_statement
from decile_api.screener import latest_published_date
from decile_core.models import (
    FactorDaily,
    IndexDef,
    IndexMemberDaily,
    IndexSnapshotDaily,
    MarketHealthDaily,
    OhlcvDaily,
)
from decile_core.screen_definition import ScreenDefinition
from decile_core.screener import build_screen_query
from decile_core.seed_data import EXAMPLE_SCREENS
from decile_core.universes import UNIVERSE_BY_SLUG

#: A rendering-only PostgreSQL dialect. ``create_mock_engine`` is the typed way to get one — the
#: dialect classes themselves have untyped constructors, the same reason
#: ``services/api/tests/test_screener_performance.py`` builds its dialect this way.
PG_DIALECT: Dialect = create_mock_engine("postgresql+asyncpg://", lambda *args: None).dialect

#: The relations whose scan strategy is worth guarding. These are the tables docs/04 sizes in
#: millions of rows, plus ``instrument``, which every hot query joins.
GUARDED_RELATIONS: Final[frozenset[str]] = frozenset(
    {
        "factor_daily",
        "ohlcv_daily",
        "index_member_daily",
        "index_snapshot_daily",
        "market_health_daily",
        "instrument",
    }
)

#: Where the recorded plan shapes live. Beside the test that reads them.
BASELINE_PATH: Final = Path(__file__).resolve().parents[2] / "tests" / "query_plan_baseline.json"

#: The screen definition the hot-path plan is taken from. The first seeded example (docs/01 §1's
#: "Investing 001") rather than a definition invented here: it is the one the benchmark measures,
#: the one the publish step warms (docs/06 §Caching), and the one the reference export in
#: ``fixtures/`` was produced by — so its plan is the plan that matters.
HOT_SCREEN: Final[ScreenDefinition] = EXAMPLE_SCREENS[0].definition

#: A second definition with the decile bucket and the marketcap range turned on, because
#: docs/06 §step 3's bucketing is the part of the statement that reads ``factor_daily`` twice and
#: is therefore the part an index change shows up in first.
BUCKETED_SCREEN: Final[ScreenDefinition] = HOT_SCREEN.model_copy(
    update={"apply_filters_on": "decile_1"}
)


@dataclass(frozen=True, slots=True)
class HotQuery:
    """One statement we are prepared to be held to the plan of."""

    name: str
    sql: str
    why: str


@dataclass(frozen=True, slots=True)
class ScanNode:
    """One relation, and how the planner chose to read it."""

    relation: str
    node_type: str

    @property
    def is_sequential(self) -> bool:
        return self.node_type == "Seq Scan"


def _compile[Row: tuple[object, ...]](statement: Select[Row]) -> str:
    """Render a SQLAlchemy statement with its parameters inlined, for ``EXPLAIN``.

    Generic over the row shape rather than widened with an escape hatch: this function does not
    care what the columns are, only that there are some, and CLAUDE.md house rule 3 rules out the
    obvious shortcut.

    ``literal_binds`` rather than a prepared statement with parameters: ``EXPLAIN`` on a
    parameterised statement plans against *generic* parameter values, which is a different plan
    from the one the request gets. The values come from this module, never from a caller, so the
    "no user string ever reaches a SQL expression" rule in docs/11 §Security is untouched.
    """
    return str(statement.compile(dialect=PG_DIALECT, compile_kwargs={"literal_binds": True}))


def hot_queries(as_of: dt.date) -> tuple[HotQuery, ...]:
    """Every query docs/11 §"Performance budgets" puts a number on, as SQL."""
    universe = UNIVERSE_BY_SLUG["nifty-500"]
    floor = as_of - dt.timedelta(days=30)

    screen = build_screen_query(HOT_SCREEN, as_of)
    bucketed = build_screen_query(BUCKETED_SCREEN, as_of)

    dashboard = (
        select(
            IndexDef.id,
            IndexDef.slug,
            IndexSnapshotDaily.level,
            IndexSnapshotDaily.change_pct,
        )
        .join(IndexSnapshotDaily, IndexSnapshotDaily.index_id == IndexDef.id)
        .where(IndexSnapshotDaily.date <= as_of, IndexSnapshotDaily.date >= floor)
        .order_by(IndexSnapshotDaily.change_pct.desc().nullslast())
    )

    factsheet_fact = select(FactorDaily).where(
        FactorDaily.instrument_id == 1, FactorDaily.date == as_of
    )
    factsheet_bar = (
        select(OhlcvDaily)
        .where(OhlcvDaily.instrument_id == 1, OhlcvDaily.date <= as_of)
        .order_by(OhlcvDaily.date.desc())
        .limit(1)
    )
    factsheet_membership = select(IndexMemberDaily.index_id).where(
        IndexMemberDaily.date == as_of, IndexMemberDaily.instrument_id == 1
    )
    universe_size = select(func.count()).where(
        IndexMemberDaily.date == as_of, IndexMemberDaily.index_id == universe.index_id
    )
    breadth_history = (
        select(MarketHealthDaily)
        .where(
            MarketHealthDaily.index_id == universe.index_id,
            MarketHealthDaily.date >= as_of - dt.timedelta(days=365),
            MarketHealthDaily.date <= as_of,
        )
        .order_by(MarketHealthDaily.date)
    )
    # The register's real statement, built by the handler's own builder so the two cannot drift.
    listings = listings_statement(ListingQuery(limit=100))

    return (
        HotQuery(
            "screen_run",
            _compile(screen.statement),
            "docs/03 §'Request path' step 4 — the one statement a cold screen run issues.",
        ),
        HotQuery(
            "screen_run_decile_bucket",
            _compile(bucketed.statement),
            "docs/06 §step 3 — the decile bucketing pass over factor_daily.",
        ),
        HotQuery("dashboard", _compile(dashboard), "docs/11: dashboard (145 indices) < 500 ms."),
        HotQuery(
            "factsheet_fact_row",
            _compile(factsheet_fact),
            "docs/11: instrument factsheet TTFB < 300 ms — the fact row it opens with.",
        ),
        HotQuery(
            "factsheet_latest_bar",
            _compile(factsheet_bar),
            "The factsheet's header price (docs/01 §5 block 1).",
        ),
        HotQuery(
            "factsheet_memberships",
            _compile(factsheet_membership),
            "Which universes this instrument was in on the day (docs/10a §4).",
        ),
        HotQuery(
            "universe_size",
            _compile(universe_size),
            "The denominator of every percentile bar on the factsheet.",
        ),
        HotQuery(
            "breadth_history",
            _compile(breadth_history),
            "docs/08 §'Market Health' — a year of the four breadth series.",
        ),
        HotQuery(
            "listings_page",
            _compile(listings),
            "docs/01 §7's listings register, first page.",
        ),
    )


def scan_nodes(plan: Mapping[str, object]) -> tuple[ScanNode, ...]:
    """Every relation-touching node in an ``EXPLAIN (FORMAT JSON)`` tree, depth-first."""
    found: list[ScanNode] = []
    _walk(plan, found)
    # Sorted and de-duplicated so the recorded shape does not depend on the planner's node order.
    unique = {(node.relation, node.node_type) for node in found}
    return tuple(ScanNode(relation, node_type) for relation, node_type in sorted(unique))


def _walk(node: Mapping[str, object], found: list[ScanNode]) -> None:
    relation = node.get("Relation Name")
    node_type = node.get("Node Type")
    if isinstance(relation, str) and isinstance(node_type, str):
        found.append(ScanNode(_base_relation(relation), node_type))
    children = node.get("Plans")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, Mapping):
                _walk(child, found)


def _base_relation(relation: str) -> str:
    """Map a TimescaleDB chunk back to its hypertable.

    A hypertable's chunks are named ``_hyper_3_11_chunk``; the plan names the chunk, not
    ``factor_daily``. Recording chunk names would make the baseline depend on how many chunks the
    seed happened to create, which changes with the fixture's date range.
    """
    if relation.startswith("_hyper_") or relation.startswith("compress_hyper_"):
        return _HYPERTABLE_FOR_CHUNK.get(relation, relation)
    return relation


#: Filled in by :func:`resolve_chunk_names` before a plan is read, because the mapping is a
#: database fact rather than a naming convention we may rely on.
_HYPERTABLE_FOR_CHUNK: dict[str, str] = {}


async def resolve_chunk_names(session: AsyncSession) -> None:
    """Learn which hypertable each chunk belongs to, so plans can be read in table terms."""
    rows = await session.execute(
        text(
            "SELECT chunk_name, hypertable_name FROM timescaledb_information.chunks "
            "WHERE hypertable_schema = 'public'"
        )
    )
    _HYPERTABLE_FOR_CHUNK.clear()
    for chunk_name, hypertable_name in rows.all():
        _HYPERTABLE_FOR_CHUNK[str(chunk_name)] = str(hypertable_name)


async def explain(session: AsyncSession, sql: str, *, analyze: bool = True) -> Mapping[str, object]:
    """``EXPLAIN (ANALYZE, FORMAT JSON)`` one statement and return its root plan node."""
    prefix = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " if analyze else "EXPLAIN (FORMAT JSON) "
    raw = (await session.execute(text(prefix + sql))).scalar_one()
    document = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(document, list) or not document:  # pragma: no cover - PostgreSQL invariant
        raise RuntimeError("EXPLAIN (FORMAT JSON) returned no plan")
    root = document[0]
    if not isinstance(root, Mapping):  # pragma: no cover - PostgreSQL invariant
        raise RuntimeError("EXPLAIN (FORMAT JSON) returned an unexpected shape")
    plan = root["Plan"]
    if not isinstance(plan, Mapping):  # pragma: no cover - PostgreSQL invariant
        raise RuntimeError("EXPLAIN (FORMAT JSON) returned no root node")
    return plan


async def observe(session: AsyncSession, as_of: dt.date) -> dict[str, dict[str, str]]:
    """The current plan shape of every hot query: ``{query: {relation: node type}}``."""
    await resolve_chunk_names(session)
    observed: dict[str, dict[str, str]] = {}
    for query in hot_queries(as_of):
        plan = await explain(session, query.sql)
        shape: dict[str, str] = {}
        for node in scan_nodes(plan):
            if node.relation not in GUARDED_RELATIONS:
                continue
            # A hypertable read as several chunks reports one node per chunk. If *any* of them
            # went sequential, that is the shape worth recording — the pessimistic one.
            if shape.get(node.relation) != "Seq Scan":
                shape[node.relation] = node.node_type
        observed[query.name] = shape
    return observed


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, dict[str, str]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    queries = document["queries"]
    if not isinstance(queries, dict):  # pragma: no cover - the file is ours
        raise RuntimeError(f"{path} has no 'queries' object")
    return {
        str(name): {str(rel): str(node) for rel, node in shape.items()}
        for name, shape in queries.items()
    }


def write_baseline(observed: Mapping[str, Mapping[str, str]], path: Path = BASELINE_PATH) -> None:
    document = {
        "_comment": (
            "Generated by `python -m decile_api.query_plans --write` against the seeded "
            "dataset. Prompt 16 deliverable 2: a hot query whose relation regresses from an "
            "index scan to a sequential scan fails CI. Re-record only with a reason."
        ),
        "queries": {name: dict(shape) for name, shape in sorted(observed.items())},
    }
    path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")


@dataclass(frozen=True, slots=True)
class Regression:
    query: str
    relation: str
    was: str
    now: str

    def __str__(self) -> str:
        return f"{self.query}: {self.relation} was {self.was}, is now {self.now}"


def regressions(
    baseline: Mapping[str, Mapping[str, str]], observed: Mapping[str, Mapping[str, str]]
) -> tuple[Regression, ...]:
    """Every guarded relation that has *become* a sequential scan since the baseline.

    A relation that has *stopped* being sequentially scanned is an improvement and is not a
    failure — but it does leave the baseline pessimistic, which the test says out loud.
    """
    found: list[Regression] = []
    for query, shape in observed.items():
        recorded = baseline.get(query, {})
        for relation, node_type in shape.items():
            was = recorded.get(relation)
            if node_type == "Seq Scan" and was is not None and was != "Seq Scan":
                found.append(Regression(query, relation, was, node_type))
    return tuple(found)


def unrecorded(
    baseline: Mapping[str, Mapping[str, str]], observed: Iterable[str]
) -> tuple[str, ...]:
    """Hot queries with no baseline entry. A new one must be recorded deliberately."""
    return tuple(name for name in sorted(observed) if name not in baseline)


async def _main(argv: Sequence[str]) -> int:  # pragma: no cover - a developer CLI
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="record the observed plans")
    parser.add_argument("--date", type=dt.date.fromisoformat, default=None)
    args = parser.parse_args(argv)

    async with session_scope() as session:
        as_of = args.date or await latest_published_date(session)
        if as_of is None:
            print("no published pipeline run; run `make seed` first")
            return 2
        for query in hot_queries(as_of):
            plan = await explain(session, query.sql)
            print(f"\n=== {query.name} — {query.why}")
            print(json.dumps(plan, indent=2)[:4000])
        observed = await observe(session, as_of)
        if args.write:
            write_baseline(observed)
            print(f"\nwrote {BASELINE_PATH}")
        else:
            print("\n" + json.dumps(observed, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - a developer CLI
    import asyncio
    import sys

    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
