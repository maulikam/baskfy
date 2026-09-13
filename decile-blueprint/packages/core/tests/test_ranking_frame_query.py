"""Screener SQL for docs/ranking/PLAN.md Phase 2 (gate 2.D G6).

Five things are asserted here:

(a) legacy ``sequential`` orders by factor *values* — factor two decides a tie in factor one's
    value, which ordering by ``r1`` (a unique ROW_NUMBER) could never do;
(b) :func:`build_ranking_frame_query` returns the post-bucket, **pre-filter** universe with
    ``passes_filters``, one ``fail__*`` flag per clause, the point-in-time narrowest sector, the
    ``desk_score_daily`` row and the NSE inputs (C4 "Input");
(c) ``factor_ranges`` and ``regime_in`` are real clauses, in the legacy statement and the frame;
(d) ``sort_by=desk_score`` on the legacy path reads ``desk_score_daily`` and never ranks a row the
    book rejected (C2);
(e) every legacy definition renders exactly the SQL it rendered at 999bf37, once the
    ``factor_daily`` columns added since are taken out of the text.

The behavioural assertions execute the statements against an in-memory SQLite database holding
only the tables the statements read. SQLite is not the production engine — PostgreSQL is, and the
API's DB suite (gate 2.G G1) runs these paths there — but it does evaluate the same
``ORDER BY``/``CASE``/window SQL, which is the point: an ordering test that only inspects the text
of its own ORDER BY proves nothing about which row comes first.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import statistics
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Column, Engine, MetaData, Select, Table, create_engine, event, insert
from sqlalchemy.pool import StaticPool

from baskfy_core import ranking_engine
from baskfy_core.models import (
    Base,
    DeskScoreDaily,
    FactorDaily,
    IndexDef,
    IndexMemberDaily,
    Instrument,
    OhlcvDaily,
)
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_core.screener import (
    _POSTGRES,
    ScreenQueryError,
    build_export_query,
    build_ranking_frame_query,
    build_screen_query,
)
from baskfy_core.universes import UNIVERSE_BY_SLUG

AS_OF = dt.date(2026, 8, 18)
FIXTURE = Path(__file__).parent / "fixtures" / "legacy-screen-sql-999bf37.json"

NIFTY_500 = UNIVERSE_BY_SLUG["nifty-500"]
NIFTY_200 = UNIVERSE_BY_SLUG["nifty-200"]
NIFTY_FNO = UNIVERSE_BY_SLUG["nifty-fno"]
#: Dashboard-only sector indices get ids past the 14 universes.
NIFTY_BANK_ID = 101
NIFTY_FIN_ID = 102


def definition(**overrides: object) -> ScreenDefinition:
    payload: dict[str, object] = {"index": "nifty-500", "sort_by": "ret_12m"}
    payload.update(overrides)
    return ScreenDefinition.model_validate(payload)


# ---------------------------------------------------------------------------
# SQLite harness
# ---------------------------------------------------------------------------


class _StddevPop:
    """PostgreSQL's ``stddev_pop`` for SQLite, which has no standard deviation aggregate."""

    def __init__(self) -> None:
        self.values: list[float] = []

    def step(self, value: float | None) -> None:
        if value is not None:
            self.values.append(float(value))

    def finalize(self) -> float | None:
        return statistics.pstdev(self.values) if self.values else None


def _bare_copy(table: Table, metadata: MetaData) -> Table:
    """The table's columns by name and type only — no server defaults SQLite cannot parse."""
    return Table(
        table.name,
        metadata,
        *[Column(column.name, column.type, primary_key=column.primary_key) for column in table.c],
    )


@pytest.fixture
def db() -> Iterator[Engine]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _register(connection: sqlite3.Connection, _record: object) -> None:
        # Through getattr because typeshed types an aggregate's ``finalize`` as returning int,
        # and a standard deviation is a float.
        getattr(connection, "create_aggregate")("stddev_pop", 1, _StddevPop)  # noqa: B009

    metadata = MetaData()
    for name in (
        "factor_daily",
        "index_member_daily",
        "index_def",
        "instrument",
        "desk_score_daily",
        "ohlcv_daily",
    ):
        _bare_copy(Base.metadata.tables[name], metadata)
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def _fact(instrument_id: int, **values: object) -> dict[str, object]:
    row: dict[str, object] = {
        "instrument_id": instrument_id,
        "date": AS_OF,
        "series": "EQ",
        "marketcap_cr": 10_000 - instrument_id,
        "universe_mask": 0,
        "top_beta_mask": 0,
        "top_volatility_mask": 0,
    }
    row.update(values)
    return row


def _uniform(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Every row with the same keys (NULL where absent), as one executemany needs."""
    keys = list(dict.fromkeys(key for row in rows for key in row))
    return [{key: row.get(key) for key in keys} for row in rows]


def _seed(
    engine: Engine,
    facts: Sequence[Mapping[str, object]],
    *,
    members: Mapping[int, Sequence[int]] | None = None,
    desk: Sequence[Mapping[str, object]] = (),
    bars: Sequence[Mapping[str, object]] = (),
) -> None:
    """Instruments + facts, every instrument in NIFTY 500 unless ``members`` says otherwise."""
    ids = [int(str(fact["instrument_id"])) for fact in facts]
    membership = dict(members) if members is not None else {}
    membership.setdefault(NIFTY_500.index_id, ids)
    with engine.begin() as connection:
        connection.execute(
            insert(IndexDef),
            [
                {"id": NIFTY_500.index_id, "slug": "nifty-500"},
                {"id": NIFTY_200.index_id, "slug": "nifty-200"},
                {"id": NIFTY_FNO.index_id, "slug": "nifty-fno"},
                {"id": NIFTY_BANK_ID, "slug": "nifty-bank"},
                {"id": NIFTY_FIN_ID, "slug": "nifty-financial-services"},
            ],
        )
        connection.execute(
            insert(Instrument),
            [{"id": i, "symbol": f"S{i}", "name": f"Stock {i}"} for i in ids],
        )
        connection.execute(insert(FactorDaily), _uniform(facts))
        connection.execute(
            insert(IndexMemberDaily),
            [
                {"index_id": index_id, "date": AS_OF, "instrument_id": instrument_id}
                for index_id, instrument_ids in membership.items()
                for instrument_id in instrument_ids
            ],
        )
        if desk:
            connection.execute(insert(DeskScoreDaily), _uniform(desk))
        if bars:
            connection.execute(insert(OhlcvDaily), _uniform(bars))


def _rows(engine: Engine, statement: Select[tuple[object]]) -> list[dict[str, object]]:
    with engine.connect() as connection:
        return [dict(row._mapping) for row in connection.execute(statement)]


def _symbols(engine: Engine, statement: Select[tuple[object]]) -> list[str]:
    return [str(row["symbol"]) for row in _rows(engine, statement)]


def _num(value: object) -> float:
    assert isinstance(value, (int, float, Decimal, str)), value
    return float(value)


# ---------------------------------------------------------------------------
# (a) sequential orders by values
# ---------------------------------------------------------------------------


class TestSequentialOrdersByValues:
    def test_factor_two_decides_a_tie_in_factor_ones_value(self, db: Engine) -> None:
        """S1 and S2 tie on 1Y return; S2 has the lower volatility, so S2 ranks first.

        Under the old ``ORDER BY r1, r2, r3`` the tie went to the lower instrument_id (S1),
        because r1 — a ROW_NUMBER — is unique and r2 was never consulted.
        """
        _seed(
            db,
            [
                _fact(1, ret_12m=Decimal("20.00"), vol_12m=Decimal("0.30")),
                _fact(2, ret_12m=Decimal("20.00"), vol_12m=Decimal("0.10")),
                _fact(3, ret_12m=Decimal("10.00"), vol_12m=Decimal("0.05")),
            ],
        )
        screen = definition(
            ranking_mode="sequential",
            factor_two={"enabled": True, "sort_by": "vol_12m", "sort_direction": "asc"},
        )
        rows = _rows(db, build_screen_query(screen, AS_OF).statement)
        assert [row["symbol"] for row in rows] == ["S2", "S1", "S3"]
        assert [row["rank"] for row in rows] == [1, 2, 3]

    def test_factor_three_decides_when_one_and_two_both_tie(self, db: Engine) -> None:
        _seed(
            db,
            [
                _fact(1, ret_12m=Decimal("20"), vol_12m=Decimal("0.1"), ret_6m=Decimal("1")),
                _fact(2, ret_12m=Decimal("20"), vol_12m=Decimal("0.1"), ret_6m=Decimal("9")),
            ],
        )
        screen = definition(
            ranking_mode="sequential",
            factor_two={"enabled": True, "sort_by": "vol_12m", "sort_direction": "asc"},
            factor_three={"enabled": True, "sort_by": "ret_6m", "sort_direction": "desc"},
        )
        assert _symbols(db, build_screen_query(screen, AS_OF).statement) == ["S2", "S1"]

    def test_a_missing_value_sorts_last_then_instrument_id_breaks_a_full_tie(
        self, db: Engine
    ) -> None:
        _seed(
            db,
            [
                _fact(1, ret_12m=None, vol_12m=Decimal("0.1")),
                _fact(2, ret_12m=Decimal("5"), vol_12m=Decimal("0.2")),
                _fact(3, ret_12m=Decimal("5"), vol_12m=Decimal("0.2")),
            ],
        )
        screen = definition(
            ranking_mode="sequential",
            factor_two={"enabled": True, "sort_by": "vol_12m", "sort_direction": "asc"},
        )
        assert _symbols(db, build_screen_query(screen, AS_OF).statement) == ["S2", "S3", "S1"]

    def test_fixed_universe_sequential_also_orders_by_values(self, db: Engine) -> None:
        _seed(
            db,
            [
                _fact(1, ret_12m=Decimal("20"), vol_12m=Decimal("0.3")),
                _fact(2, ret_12m=Decimal("20"), vol_12m=Decimal("0.1")),
            ],
        )
        screen = definition(
            ranking_mode="sequential",
            ranking_scope="fixed_universe",
            factor_two={"enabled": True, "sort_by": "vol_12m", "sort_direction": "asc"},
        )
        assert _symbols(db, build_screen_query(screen, AS_OF).statement) == ["S2", "S1"]

    def test_the_csv_export_agrees_with_the_screen(self, db: Engine) -> None:
        _seed(
            db,
            [
                _fact(1, ret_12m=Decimal("20"), vol_12m=Decimal("0.3")),
                _fact(2, ret_12m=Decimal("20"), vol_12m=Decimal("0.1")),
            ],
        )
        screen = definition(
            ranking_mode="sequential",
            factor_two={"enabled": True, "sort_by": "vol_12m", "sort_direction": "asc"},
        )
        assert _symbols(db, build_export_query(screen, AS_OF)) == ["S2", "S1"]


# ---------------------------------------------------------------------------
# (b) the ranking frame
# ---------------------------------------------------------------------------


class TestRankingFrame:
    REQUIRED = (
        ranking_engine.INSTRUMENT_ID,
        ranking_engine.SYMBOL,
        ranking_engine.PASSES_FILTERS,
        ranking_engine.SECTOR,
        ranking_engine.DESK_SCORE_KEY,
        ranking_engine.DESK_SCORE_RANK,
        ranking_engine.DESK_REJECT,
        *ranking_engine.DESK_COMPONENTS,
        ranking_engine.DESK_F_PENALTY,
        ranking_engine.NSE_MR6,
        ranking_engine.NSE_MR12,
        ranking_engine.IN_NIFTY_200,
        ranking_engine.IS_FNO,
        *ranking_engine.NSE_POPULATION_COLUMNS,
        ranking_engine.LAST_BAR_DATE,
    )

    def test_projects_every_column_the_engine_contract_names(self) -> None:
        query = build_ranking_frame_query(definition(min_return_1y=Decimal("10")), AS_OF)
        names = [column.name for column in query.statement.selected_columns]
        for required in self.REQUIRED:
            assert required in names, required
        for column in Base.metadata.tables["factor_daily"].c:
            assert column.name in names, column.name
        assert "fail__min_return_1y" in names
        assert len(names) == len(set(names))

    def test_no_filter_reaches_a_where_clause(self) -> None:
        """Pre-filter: the predicates are projected as flags, never used to drop rows."""
        query = build_ranking_frame_query(
            definition(min_return_1y=Decimal("10"), regime_in=["BULL"]), AS_OF
        )
        sql = query.sql()
        flagged = sql[sql.index("flagged AS") : sql.index("sector_members AS")]
        assert "WHERE" not in flagged
        assert "CASE WHEN (bucketed.ret_12m >=" in flagged
        assert "LIMIT" not in sql

    def test_expression_terms_are_projected_by_key(self) -> None:
        query = build_ranking_frame_query(
            definition(
                sort_by="avg_sharpe_12_6_3_1",
                ranking_terms=[
                    {"factor": "avg_sharpe_12_6_3_1", "preference": "higher"},
                    {"factor": "ret_12m", "preference": "higher"},
                    {"factor": "desk_score", "preference": "higher"},
                ],
            ),
            AS_OF,
        )
        assert query.term_columns == ("avg_sharpe_12_6_3_1",)
        names = [column.name for column in query.statement.selected_columns]
        assert names.count("avg_sharpe_12_6_3_1") == 1
        assert names.count("desk_score") == 1

    def test_the_pre_filter_universe_with_flags_sector_desk_and_nse_inputs(
        self, db: Engine
    ) -> None:
        facts = [
            # Passes. In NIFTY BANK (2 members) and FIN SERVICES (3): the narrower wins.
            _fact(
                1,
                ret_12m=Decimal("30"),
                regime="BULL",
                nse_mr6=Decimal("1.0"),
                nse_mr12=Decimal("2.0"),
            ),
            # Fails the return filter; also only in FIN SERVICES.
            _fact(
                2,
                ret_12m=Decimal("5"),
                regime="BULL",
                nse_mr6=Decimal("3.0"),
                nse_mr12=Decimal("4.0"),
            ),
            # NULL return: a NULL comparison fails (docs/06 step 4). Fails the regime filter too.
            _fact(3, ret_12m=None, regime="BEAR", nse_mr6=Decimal("5.0"), nse_mr12=None),
            # Passes, in no sector index, not scanned by the desk.
            _fact(4, ret_12m=Decimal("50"), regime="BULL"),
            # Eligible for NSE's population but outside the screen's universe.
            _fact(9, ret_12m=Decimal("1"), nse_mr6=Decimal("8.0"), nse_mr12=Decimal("6.0")),
        ]
        _seed(
            db,
            facts,
            members={
                NIFTY_500.index_id: [1, 2, 3, 4],
                NIFTY_200.index_id: [1, 2, 3, 9],
                NIFTY_FNO.index_id: [1, 3, 9],
                NIFTY_BANK_ID: [1, 3],
                NIFTY_FIN_ID: [1, 2, 3],
            },
            desk=[
                {
                    "instrument_id": 1,
                    "date": AS_OF,
                    "score": Decimal("71.5"),
                    "score_rank": 1,
                    "a_trend": Decimal("20"),
                    "f_penalty": Decimal("0"),
                    "reject": "",
                    "score_version": "v",
                },
                {
                    "instrument_id": 2,
                    "date": AS_OF,
                    "score": None,
                    "score_rank": None,
                    "reject": "below_20dma",
                    "score_version": "v",
                },
            ],
            bars=[
                {
                    "instrument_id": 1,
                    "date": AS_OF,
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 1,
                    "close_raw": 1,
                    "volume_raw": 1,
                    "adj_factor": 1,
                    "source": "nse",
                },
                {
                    "instrument_id": 2,
                    "date": dt.date(2026, 8, 14),
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 1,
                    "close_raw": 1,
                    "volume_raw": 1,
                    "adj_factor": 1,
                    "source": "nse",
                },
            ],
        )
        screen = definition(min_return_1y=Decimal("10"), regime_in=["BULL", "NEUTRAL"])
        query = build_ranking_frame_query(screen, AS_OF)
        rows = {row["instrument_id"]: row for row in _rows(db, query.statement)}

        assert sorted(rows, key=_num) == [1, 2, 3, 4], "the pre-filter universe, and only it"
        assert list(query.clause_details) == ["min_return_1y", "series", "regime_in"]

        def truthy(value: object) -> bool:
            return bool(value)

        assert truthy(rows[1]["passes_filters"]) is True
        assert truthy(rows[4]["passes_filters"]) is True
        assert truthy(rows[2]["passes_filters"]) is False
        assert truthy(rows[2]["fail__min_return_1y"]) is True
        assert truthy(rows[2]["fail__regime_in"]) is False
        assert truthy(rows[3]["passes_filters"]) is False
        assert truthy(rows[3]["fail__min_return_1y"]) is True, "NULL never satisfies"
        assert truthy(rows[3]["fail__regime_in"]) is True
        assert truthy(rows[1]["fail__series"]) is False

        assert rows[1]["sector"] == "nifty-bank"
        assert rows[2]["sector"] == "nifty-financial-services"
        assert rows[3]["sector"] == "nifty-bank"
        assert rows[4]["sector"] is None

        assert _num(rows[1]["desk_score"]) == 71.5
        assert rows[1]["desk_score_rank"] == 1
        assert rows[1]["desk_reject"] == ""
        assert rows[2]["desk_score"] is None
        assert rows[2]["desk_reject"] == "below_20dma"
        assert rows[4]["desk_score"] is None
        assert rows[4]["desk_reject"] is None

        assert [truthy(rows[i]["in_nifty_200"]) for i in (1, 2, 3, 4)] == [
            True,
            True,
            True,
            False,
        ]
        assert [truthy(rows[i]["is_fno"]) for i in (1, 2, 3, 4)] == [True, False, True, False]
        # NSE's eligible set is NIFTY 200 ∩ F&O with both ratios: instruments 1 and 9 — 9 is
        # outside the screen's universe and still counts; 3 lacks nse_mr12; 2 lacks F&O.
        assert _num(rows[1]["nse_mr6_mean"]) == pytest.approx(4.5)
        assert _num(rows[1]["nse_mr6_std"]) == pytest.approx(3.5)
        assert _num(rows[4]["nse_mr12_mean"]) == pytest.approx(4.0)
        assert _num(rows[4]["nse_mr12_std"]) == pytest.approx(2.0)

        assert str(rows[1]["last_bar_date"]) == AS_OF.isoformat()
        assert str(rows[2]["last_bar_date"]) == "2026-08-14"
        assert rows[3]["last_bar_date"] is None

    def test_the_bucket_is_applied_before_the_frame(self, db: Engine) -> None:
        """Post-bucket: ``top_50`` keeps the 50 largest by marketcap, and nothing else."""
        _seed(db, [_fact(i, ret_12m=Decimal("1")) for i in range(1, 61)])
        query = build_ranking_frame_query(definition(apply_filters_on="top_50"), AS_OF)
        ids = [row["instrument_id"] for row in _rows(db, query.statement)]
        assert ids == list(range(1, 51))

    def test_no_active_clause_means_every_row_passes(self, db: Engine) -> None:
        _seed(db, [_fact(1, series="BE")])
        query = build_ranking_frame_query(definition(series=[]), AS_OF)
        assert query.clause_details == {}
        rows = _rows(db, query.statement)
        assert bool(rows[0]["passes_filters"]) is True

    def test_risk_flags_are_clauses_of_the_frame(self, db: Engine) -> None:
        _seed(
            db,
            [
                _fact(1, ret_12m=Decimal("1"), top_beta_mask=NIFTY_500.mask_value),
                _fact(2, ret_12m=Decimal("1")),
            ],
        )
        query = build_ranking_frame_query(
            definition(ignore_top_beta={"enabled": True, "count": 10}), AS_OF
        )
        rows = {row["instrument_id"]: row for row in _rows(db, query.statement)}
        assert bool(rows[1]["fail__ignore_top_beta"]) is True
        assert bool(rows[1]["passes_filters"]) is False
        assert bool(rows[2]["passes_filters"]) is True

    def test_the_frame_feeds_the_ranking_engine(self, db: Engine) -> None:
        """End to end: SQL frame → rank_frame. A desk-rejected row is never ranked."""
        _seed(
            db,
            [
                _fact(1, ret_12m=Decimal("30")),
                _fact(2, ret_12m=Decimal("40")),
                _fact(3, ret_12m=Decimal("50")),
                _fact(4, ret_12m=Decimal("2")),
            ],
            desk=[
                {
                    "instrument_id": 1,
                    "date": AS_OF,
                    "score": Decimal("60.0"),
                    "reject": "",
                    "score_version": "v",
                },
                {
                    "instrument_id": 2,
                    "date": AS_OF,
                    "score": Decimal("80.0"),
                    "reject": "",
                    "score_version": "v",
                },
                {
                    "instrument_id": 3,
                    "date": AS_OF,
                    "score": None,
                    "reject": "extended",
                    "score_version": "v",
                },
                {
                    "instrument_id": 4,
                    "date": AS_OF,
                    "score": Decimal("99.0"),
                    "reject": "",
                    "score_version": "v",
                },
            ],
        )
        screen = definition(
            sort_by="desk_score",
            min_return_1y=Decimal("10"),
            ranking_terms=[{"factor": "desk_score", "preference": "higher"}],
        )
        query = build_ranking_frame_query(screen, AS_OF)
        frame = pd.DataFrame(_rows(db, query.statement))
        spec = ranking_engine.RankingSpec(
            terms=(
                ranking_engine.TermSpec(
                    key="desk_score",
                    label="Desk SCORE",
                    weight_family="momentum",
                    preference="higher",
                ),
            ),
            mode="single",
            scope="fixed_universe",
        )
        result = ranking_engine.rank_frame(frame, spec)
        assert list(result.ranked["instrument_id"]) == [2, 1]


# ---------------------------------------------------------------------------
# (c) factor_ranges and regime_in
# ---------------------------------------------------------------------------


def _filtered_where(sql: str) -> str:
    start = sql.index("filtered AS")
    body = sql[start : sql.index("relative AS")]
    return body[body.index("WHERE") :]


class TestFactorRangesAndRegime:
    def test_a_range_on_an_expression_and_a_regime_are_where_clauses(self) -> None:
        sql = build_screen_query(
            definition(
                factor_ranges=[{"factor": "avg_sharpe_12_6_3_1", "min": 1.0, "max": 3.0}],
                regime_in=["BULL"],
            ),
            AS_OF,
        ).sql()
        where = _filtered_where(sql)
        assert "((sharpe_12m + sharpe_6m + sharpe_3m + sharpe_1m) / 4.0) >=" in where
        assert "((sharpe_12m + sharpe_6m + sharpe_3m + sharpe_1m) / 4.0) <=" in where
        assert "regime IN" in where

    def test_values_are_bound_not_interpolated(self) -> None:
        query = build_screen_query(
            definition(factor_ranges=[{"factor": "ret_6m", "min": 12.375}]), AS_OF
        )
        assert "12.375" not in query.sql()
        assert 12.375 in query.params().values()

    def test_a_disabled_range_emits_nothing(self) -> None:
        plain = build_screen_query(definition(), AS_OF).sql()
        disabled = build_screen_query(
            definition(factor_ranges=[{"factor": "ret_6m", "min": 1.0, "enabled": False}]), AS_OF
        ).sql()
        assert disabled == plain

    def test_ranges_and_regime_select_the_right_rows(self, db: Engine) -> None:
        _seed(
            db,
            [
                _fact(1, ret_12m=Decimal("10"), ret_6m=Decimal("5"), regime="BULL"),
                _fact(2, ret_12m=Decimal("20"), ret_6m=Decimal("15"), regime="BULL"),
                _fact(3, ret_12m=Decimal("30"), ret_6m=Decimal("5"), regime="BEAR"),
                _fact(4, ret_12m=Decimal("40"), ret_6m=None, regime="NEUTRAL"),
                _fact(5, ret_12m=Decimal("50"), ret_6m=Decimal("0"), regime=None),
            ],
        )
        screen = definition(
            factor_ranges=[{"factor": "ret_6m", "min": 0.0, "max": 10.0}],
            regime_in=["BULL", "NEUTRAL"],
        )
        # 2 is outside the range, 3 is BEAR, 4 has no ret_6m, 5 has no regime.
        assert _symbols(db, build_screen_query(screen, AS_OF).statement) == ["S1"]

    def test_the_frame_flags_each_range_separately(self, db: Engine) -> None:
        _seed(db, [_fact(1, ret_6m=Decimal("5"), ret_3m=Decimal("50"))])
        query = build_ranking_frame_query(
            definition(
                series=[],
                factor_ranges=[
                    {"factor": "ret_6m", "max": 10.0},
                    {"factor": "ret_3m", "max": 10.0},
                ],
            ),
            AS_OF,
        )
        assert list(query.clause_details) == ["factor_range_1_ret_6m", "factor_range_2_ret_3m"]
        row = _rows(db, query.statement)[0]
        assert bool(row["fail__factor_range_1_ret_6m"]) is False
        assert bool(row["fail__factor_range_2_ret_3m"]) is True
        assert bool(row["passes_filters"]) is False


# ---------------------------------------------------------------------------
# (d) desk_score on the legacy path
# ---------------------------------------------------------------------------


class TestDeskScoreLegacyPath:
    def test_reads_desk_score_daily_and_never_rescores(self) -> None:
        sql = build_screen_query(definition(sort_by="desk_score"), AS_OF).sql()
        desk = sql[sql.index("desk_scored AS") : sql.index("filtered AS")]
        assert "JOIN desk_score_daily ON" in desk
        assert "desk_score_daily.score IS NOT NULL" in desk
        assert "desk_score_daily.score AS desk_score" in desk
        assert "row_number() OVER (ORDER BY desk_score DESC NULLS LAST" in sql

    def test_rejected_and_unscanned_rows_are_never_ranked(self, db: Engine) -> None:
        _seed(
            db,
            [_fact(i, ret_12m=Decimal("20")) for i in (1, 2, 3, 4, 5)],
            desk=[
                {
                    "instrument_id": 1,
                    "date": AS_OF,
                    "score": Decimal("55.5"),
                    "reject": "",
                    "score_version": "v",
                },
                {
                    "instrument_id": 2,
                    "date": AS_OF,
                    "score": None,
                    "reject": "extended",
                    "score_version": "v",
                },
                {
                    "instrument_id": 3,
                    "date": AS_OF,
                    "score": Decimal("90.0"),
                    "reject": "",
                    "score_version": "v",
                },
                # A score from another day is not today's score.
                {
                    "instrument_id": 4,
                    "date": dt.date(2026, 8, 14),
                    "score": Decimal("99.0"),
                    "reject": "",
                    "score_version": "v",
                },
            ],
        )
        rows = _rows(db, build_screen_query(definition(sort_by="desk_score"), AS_OF).statement)
        assert [row["symbol"] for row in rows] == ["S3", "S1"]
        assert [_num(row["sorting_factor"]) for row in rows] == [90.0, 55.5]

    def test_filters_still_apply(self, db: Engine) -> None:
        _seed(
            db,
            [_fact(1, ret_12m=Decimal("5")), _fact(2, ret_12m=Decimal("50"))],
            desk=[
                {
                    "instrument_id": 1,
                    "date": AS_OF,
                    "score": Decimal("90"),
                    "reject": "",
                    "score_version": "v",
                },
                {
                    "instrument_id": 2,
                    "date": AS_OF,
                    "score": Decimal("40"),
                    "reject": "",
                    "score_version": "v",
                },
            ],
        )
        screen = definition(sort_by="desk_score", min_return_1y=Decimal("10"))
        assert _symbols(db, build_screen_query(screen, AS_OF).statement) == ["S2"]

    def test_the_export_ranks_desk_score_the_same_way(self, db: Engine) -> None:
        _seed(
            db,
            [_fact(1), _fact(2), _fact(3)],
            desk=[
                {
                    "instrument_id": 1,
                    "date": AS_OF,
                    "score": Decimal("10"),
                    "reject": "",
                    "score_version": "v",
                },
                {
                    "instrument_id": 2,
                    "date": AS_OF,
                    "score": None,
                    "reject": "x",
                    "score_version": "v",
                },
                {
                    "instrument_id": 3,
                    "date": AS_OF,
                    "score": Decimal("20"),
                    "reject": "",
                    "score_version": "v",
                },
            ],
        )
        assert _symbols(db, build_export_query(definition(sort_by="desk_score"), AS_OF)) == [
            "S3",
            "S1",
        ]

    def test_ranking_terms_and_nse_momentum_are_refused_by_the_legacy_sql(self) -> None:
        with pytest.raises(ScreenQueryError, match="ranking_terms"):
            build_screen_query(
                definition(ranking_terms=[{"factor": "ret_12m", "preference": "higher"}]), AS_OF
            )
        with pytest.raises(ScreenQueryError, match="nse_momentum_score"):
            build_screen_query(definition(sort_by="nse_momentum_score"), AS_OF)


# ---------------------------------------------------------------------------
# (e) legacy SQL unchanged since 999bf37
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LegacyFixture:
    as_of: dt.date
    columns: tuple[str, ...]
    factor_daily_columns: frozenset[str]
    #: case → (definition payload, screen SQL, export SQL) as rendered at 999bf37.
    cases: Mapping[str, tuple[Mapping[str, object], str, str]]


def _fixture() -> LegacyFixture:
    loaded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    cases: dict[str, tuple[Mapping[str, object], str, str]] = {}
    for name, case in loaded["cases"].items():
        assert isinstance(case["definition"], dict)
        assert isinstance(case["screen_sql"], str)
        assert isinstance(case["export_sql"], str)
        cases[str(name)] = (case["definition"], case["screen_sql"], case["export_sql"])
    return LegacyFixture(
        as_of=dt.date.fromisoformat(str(loaded["as_of"])),
        columns=tuple(str(column) for column in loaded["columns"]),
        factor_daily_columns=frozenset(str(column) for column in loaded["factor_daily_columns"]),
        cases=cases,
    )


def _without_columns(sql: str, columns: Sequence[str]) -> str:
    """Remove the projection of each ``column`` from every CTE and from the export's SELECT.

    CTEs project ``<source>.<col> AS <col>``; the export projects ``ranked.<col>``. Only a
    comma-separated projection item is removed, so a predicate naming the column would survive —
    and none of the added columns is used by a legacy definition.
    """
    for column in columns:
        name = re.escape(column)
        sql = re.sub(rf", \w+\.{name} AS {name}\b", "", sql)
        sql = re.sub(rf", ranked\.{name}(?=[, \n])(?! AS)", "", sql)
    return sql


LEGACY_CASES = sorted(_fixture().cases)


@pytest.mark.parametrize("case", LEGACY_CASES)
def test_legacy_sql_is_unchanged_since_999bf37(case: str) -> None:
    fixture = _fixture()
    added = [
        column.name
        for column in Base.metadata.tables["factor_daily"].c
        if column.name not in fixture.factor_daily_columns
    ]
    assert added, "the fixture predates the Phase-2 factor columns; the stripping must be live"
    payload, expected_screen_sql, expected_export_sql = fixture.cases[case]
    screen = ScreenDefinition.model_validate(payload)
    screen_sql = build_screen_query(screen, fixture.as_of, columns=fixture.columns).sql()
    export_sql = str(build_export_query(screen, fixture.as_of).compile(dialect=_POSTGRES))
    assert _without_columns(screen_sql, added) == expected_screen_sql
    assert _without_columns(export_sql, added) == expected_export_sql


def test_the_fixture_covers_the_three_legacy_shapes() -> None:
    assert {"minimal", "single-fixed-universe", "three-factor-composite-with-filters"} <= set(
        _fixture().cases
    )
