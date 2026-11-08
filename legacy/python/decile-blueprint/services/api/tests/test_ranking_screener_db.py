"""Screen runs through the ranking engine, against a real PostgreSQL (gate 2.G G1, PLAN.md C4/C6).

The engine is asserted on hand-built frames in ``packages/core/tests/test_ranking_engine.py`` and
the frame statement on SQLite in ``test_ranking_frame_query.py``. What only this suite can prove is
the join of the two on the production engine: that ``baskfy_api.screener.run_screen`` sends a
definition with ``ranking_terms`` through ``build_ranking_frame_query`` and ``rank_frame``, that
the ranks it serves are the ones C4 specifies, and that ``desk_score`` comes from
``desk_score_daily`` with the book's rejected rows nowhere in the result.

Every expectation below is computed by hand from C4's formulas, in the docstring of its test, so a
change in the engine's arithmetic fails here rather than being re-recorded.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal

import pytest
from screener_helpers import DATA_VERSION, add_member, make_row, requires_db
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.screener import LEGACY_RANKING_ENGINE_VERSION, run_screen
from baskfy_core.models import DeskScoreDaily, IndexDef
from baskfy_core.ranking_engine import RANKING_ENGINE_VERSION
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_core.universes import UNIVERSE_BY_SLUG

pytestmark = [pytest.mark.db, requires_db]

#: A trading day inside the published range that carries no reference-export rows, so each test
#: owns its whole universe. The same date ``test_screener_db`` uses for its synthetic rows.
SYNTHETIC = dt.date(2026, 8, 17)
NIFTY_500 = UNIVERSE_BY_SLUG["nifty-500"]
DESK_VERSION = "desk-score-test"


def ranked(**overrides: object) -> ScreenDefinition:
    payload: dict[str, object] = {
        "index": NIFTY_500.slug,
        "sort_by": "ret_12m",
        "historical_date": SYNTHETIC.isoformat(),
    }
    payload.update(overrides)
    return ScreenDefinition.model_validate(payload)


async def seed(session: AsyncSession, rows: Mapping[str, Mapping[str, object]]) -> dict[str, int]:
    """One NIFTY 500 member per symbol on ``SYNTHETIC``, with only the given factor values set."""
    ids: dict[str, int] = {}
    for symbol, values in rows.items():
        ids[symbol] = await make_row(
            session, symbol, NIFTY_500.index_id, on=SYNTHETIC, values=values
        )
    return ids


async def run(
    session: AsyncSession, definition: ScreenDefinition, columns: Sequence[str] = ()
) -> dict[str, object]:
    """The response body, decoded with ``Decimal`` so a 2 dp score stays a 2 dp score."""
    outcome = await run_screen(session, definition, columns=columns)
    body = json.loads(outcome.payload, parse_float=Decimal)
    assert isinstance(body, dict)
    return body


def symbols(body: Mapping[str, object]) -> list[str]:
    rows = body["rows"]
    assert isinstance(rows, list)
    return [str(row["symbol"]) for row in rows]


def column(body: Mapping[str, object], name: str) -> list[object]:
    rows = body["rows"]
    assert isinstance(rows, list)
    return [row[name] for row in rows]


def columns_of(body: Mapping[str, object]) -> list[str]:
    value = body["columns"]
    assert isinstance(value, list)
    return [str(name) for name in value]


def provenance(body: Mapping[str, object]) -> Mapping[str, object]:
    value = body["provenance"]
    assert isinstance(value, dict)
    return value


async def ensure_index(session: AsyncSession, slug: str) -> int:
    """The ``index_def`` id for a sector slug, registering it past the used ids when absent."""
    existing = (
        await session.execute(select(IndexDef.id).where(IndexDef.slug == slug))
    ).scalar_one_or_none()
    if existing is not None:
        return int(existing)
    next_id = int((await session.execute(select(func.max(IndexDef.id)))).scalar_one()) + 1
    session.add(IndexDef(id=max(next_id, 200), slug=slug, name=slug.upper(), is_universe=False))
    await session.flush()
    return max(next_id, 200)


async def add_desk_row(
    session: AsyncSession, instrument_id: int, *, score: Decimal | None, reject: str = ""
) -> None:
    session.add(
        DeskScoreDaily(
            instrument_id=instrument_id,
            date=SYNTHETIC,
            score=score,
            score_rank=None,
            a_trend=Decimal("20.0000") if score is not None else None,
            reject=reject,
            score_version=DESK_VERSION,
        )
    )
    await session.flush()


class TestRankingEngineComposite:
    async def test_composite_ranking_scores_by_family_weighted_percentiles(
        self, screener_session: AsyncSession
    ) -> None:
        """C4 steps 3-4 over three rows, two families at an equal share.

        ret_12m (momentum, higher): A=1, B=0.5, C=0. vol_12m (risk_execution, lower): A=0, B=1,
        C=0.5. composite = 100 x (0.5 x ret + 0.5 x vol): A=50.00, B=75.00, C=25.00.
        """
        await seed(
            screener_session,
            {
                "CMPA": {"ret_12m": Decimal("30"), "vol_12m": Decimal("0.30")},
                "CMPB": {"ret_12m": Decimal("20"), "vol_12m": Decimal("0.10")},
                "CMPC": {"ret_12m": Decimal("10"), "vol_12m": Decimal("0.20")},
            },
        )
        body = await run(
            screener_session,
            ranked(
                ranking_terms=[
                    {"factor": "ret_12m", "preference": "higher"},
                    {"factor": "vol_12m", "preference": "lower"},
                ]
            ),
        )
        assert symbols(body) == ["CMPB", "CMPA", "CMPC"]
        assert column(body, "rank") == [1, 2, 3]
        assert column(body, "composite_score") == [
            Decimal("75.00"),
            Decimal("50.00"),
            Decimal("25.00"),
        ]
        assert "composite_score" in columns_of(body)
        # sorting_factor is the first term's raw value, under the first term's label.
        assert column(body, "sorting_factor") == [Decimal("20"), Decimal("30"), Decimal("10")]
        assert body["sorting_factor"] == {"key": "ret_12m", "label": "ABSOLUTE RETURN 1 YEAR"}

        stamp = provenance(body)
        assert stamp["ranking_engine_version"] == RANKING_ENGINE_VERSION
        assert stamp["mode"] == "composite"
        assert stamp["scope"] == "filtered_results"
        assert stamp["universe"] == NIFTY_500.slug
        assert stamp["universe_label"] == NIFTY_500.name
        assert stamp["as_of"] == SYNTHETIC.isoformat()
        assert stamp["data_version"] == DATA_VERSION
        assert stamp["desk_score_version"] is None


class TestRankingEngineSequential:
    async def test_sequential_ranking_orders_by_values_not_by_a_blend(
        self, screener_session: AsyncSession
    ) -> None:
        """Sequential: ret_12m first, sharpe_12m breaks the tie -> C (30), B (20, 2), A (20, 1).

        The same two terms as a composite give B=62.50, C=50.00, A=37.50 (ret: A=B=0.25 on the
        average tie, C=1; sharpe: A=0.5, B=1, C=0), which is a different order — so this is not a
        composite that happens to agree.
        """
        await seed(
            screener_session,
            {
                "SEQA": {"ret_12m": Decimal("20"), "sharpe_12m": Decimal("1")},
                "SEQB": {"ret_12m": Decimal("20"), "sharpe_12m": Decimal("2")},
                "SEQC": {"ret_12m": Decimal("30"), "sharpe_12m": Decimal("0")},
            },
        )
        terms = [
            {"factor": "ret_12m", "preference": "higher"},
            {"factor": "sharpe_12m", "preference": "higher"},
        ]
        sequential = await run(
            screener_session, ranked(ranking_mode="sequential", ranking_terms=terms)
        )
        assert symbols(sequential) == ["SEQC", "SEQB", "SEQA"]
        assert "composite_score" not in columns_of(sequential)
        assert provenance(sequential)["mode"] == "sequential"

        composite = await run(screener_session, ranked(ranking_terms=terms))
        assert symbols(composite) == ["SEQB", "SEQC", "SEQA"]
        assert column(composite, "composite_score") == [
            Decimal("62.50"),
            Decimal("50.00"),
            Decimal("37.50"),
        ]


class TestRankingEngineScope:
    async def test_fixed_universe_and_filtered_results_rank_differently(
        self, screener_session: AsyncSession
    ) -> None:
        """The price filter drops X. The scope decides whether X still counts toward percentiles.

        filtered_results (A, B only): ret A=1, B=0; sharpe A=0, B=1 -> both 50.00, and the tie goes
        to the higher first-term raw value -> A, B.
        fixed_universe (A, B, X): ret A=1, B=0.5, X=0; sharpe A=0, B=1, X=0.5 -> A=50.00,
        B=75.00 -> B, A.
        """
        await seed(
            screener_session,
            {
                "SCPA": {
                    "ret_12m": Decimal("3"),
                    "sharpe_12m": Decimal("1"),
                    "close": Decimal("100"),
                    "close_raw": Decimal("100"),
                },
                "SCPB": {
                    "ret_12m": Decimal("2"),
                    "sharpe_12m": Decimal("3"),
                    "close": Decimal("100"),
                    "close_raw": Decimal("100"),
                },
                "SCPX": {
                    "ret_12m": Decimal("1"),
                    "sharpe_12m": Decimal("2"),
                    "close": Decimal("900"),
                    "close_raw": Decimal("900"),
                },
            },
        )
        terms = [
            {"factor": "ret_12m", "preference": "higher"},
            {"factor": "sharpe_12m", "preference": "higher"},
        ]
        price = {"to": "200"}

        filtered = await run(
            screener_session,
            ranked(ranking_terms=terms, ranking_scope="filtered_results", price=price),
        )
        fixed = await run(
            screener_session,
            ranked(ranking_terms=terms, ranking_scope="fixed_universe", price=price),
        )

        assert symbols(filtered) == ["SCPA", "SCPB"]
        assert column(filtered, "composite_score") == [Decimal("50.00"), Decimal("50.00")]
        assert symbols(fixed) == ["SCPB", "SCPA"]
        assert column(fixed, "composite_score") == [Decimal("75.00"), Decimal("50.00")]
        assert "SCPX" not in symbols(fixed), "fixed_universe scores against X but never serves it"
        assert provenance(fixed)["scope"] == "fixed_universe"

    async def test_within_sector_ranks_each_sector_on_its_own(
        self, screener_session: AsyncSession
    ) -> None:
        """within_sector percentiles are per point-in-time sector; no sector is ``unclassified``.

        BANK: A (10) = 0, B (20) = 1. IT: C (30) = 0, D (40) = 1. E has no sector and is alone in
        ``unclassified`` -> 1. Composite desc, ties by ret_12m desc: D, B, E (100.00), C, A (0.00).
        Across the whole universe D, C, B, A, E would be the order instead.
        """
        ids = await seed(
            screener_session,
            {
                "SECA": {"ret_12m": Decimal("10")},
                "SECB": {"ret_12m": Decimal("20")},
                "SECC": {"ret_12m": Decimal("30")},
                "SECD": {"ret_12m": Decimal("40")},
                "SECE": {"ret_12m": Decimal("5")},
            },
        )
        bank = await ensure_index(screener_session, "nifty-bank")
        it = await ensure_index(screener_session, "nifty-it")
        for symbol in ("SECA", "SECB"):
            await add_member(screener_session, bank, ids[symbol], SYNTHETIC)
        for symbol in ("SECC", "SECD"):
            await add_member(screener_session, it, ids[symbol], SYNTHETIC)

        terms = [{"factor": "ret_12m", "preference": "higher"}]
        body = await run(
            screener_session, ranked(ranking_terms=terms, ranking_scope="within_sector")
        )
        assert symbols(body) == ["SECD", "SECB", "SECE", "SECC", "SECA"]
        assert column(body, "composite_score") == [
            Decimal("100.00"),
            Decimal("100.00"),
            Decimal("100.00"),
            Decimal("0.00"),
            Decimal("0.00"),
        ]
        assert provenance(body)["scope"] == "within_sector"

        universe = await run(
            screener_session, ranked(ranking_terms=terms, ranking_scope="fixed_universe")
        )
        assert symbols(universe) == ["SECD", "SECC", "SECB", "SECA", "SECE"]


class TestRankingEngineFactorRanges:
    async def test_factor_ranges_gate_the_ranking_results(
        self, screener_session: AsyncSession
    ) -> None:
        """ret_6m in [0, 10], inclusive, NULL never inside: A (5) and D (0) survive, by ret_12m.

        B is above the range and C has no ret_6m. The legacy statement honours the same range, so
        both paths serve the same two names.
        """
        await seed(
            screener_session,
            {
                "RNGA": {"ret_12m": Decimal("10"), "ret_6m": Decimal("5")},
                "RNGB": {"ret_12m": Decimal("30"), "ret_6m": Decimal("15")},
                "RNGC": {"ret_12m": Decimal("20"), "ret_6m": None},
                "RNGD": {"ret_12m": Decimal("5"), "ret_6m": Decimal("0")},
            },
        )
        ranges = [{"factor": "ret_6m", "min": 0.0, "max": 10.0}]
        engine = await run(
            screener_session,
            ranked(
                ranking_mode="single",
                ranking_terms=[{"factor": "ret_12m", "preference": "higher"}],
                factor_ranges=ranges,
            ),
        )
        assert symbols(engine) == ["RNGA", "RNGD"]
        assert provenance(engine)["ranking_engine_version"] == RANKING_ENGINE_VERSION

        legacy = await run(screener_session, ranked(factor_ranges=ranges))
        assert symbols(legacy) == ["RNGA", "RNGD"]
        assert provenance(legacy)["ranking_engine_version"] == LEGACY_RANKING_ENGINE_VERSION


class TestRankingEngineDeskScore:
    """C2: ``desk_score`` is read from ``desk_score_daily``; a rejected row is never ranked."""

    async def _seed_book(self, session: AsyncSession) -> None:
        """A=70 and D=80 scored; B rejected (and the best on ret_12m); C never scanned."""
        ids = await seed(
            session,
            {
                "DSKA": {"ret_12m": Decimal("10")},
                "DSKB": {"ret_12m": Decimal("90")},
                "DSKC": {"ret_12m": Decimal("50")},
                "DSKD": {"ret_12m": Decimal("20")},
            },
        )
        await add_desk_row(session, ids["DSKA"], score=Decimal("70.0"))
        await add_desk_row(session, ids["DSKB"], score=None, reject="extended;illiquid")
        await add_desk_row(session, ids["DSKD"], score=Decimal("80.0"))

    async def test_desk_score_ranks_from_desk_score_daily_without_rejected_rows(
        self, screener_session: AsyncSession
    ) -> None:
        await self._seed_book(screener_session)
        body = await run(
            screener_session,
            ranked(
                sort_by="desk_score",
                ranking_mode="single",
                ranking_terms=[{"factor": "desk_score", "preference": "higher"}],
            ),
        )
        assert symbols(body) == ["DSKD", "DSKA"]
        assert column(body, "sorting_factor") == [Decimal("80.0"), Decimal("70.0")]
        assert column(body, "desk_eligible") == [True, True]
        assert column(body, "desk_reject") == ["", ""]
        assert column(body, "desk_a_trend") == [Decimal("20.0000"), Decimal("20.0000")]
        stamp = provenance(body)
        assert stamp["desk_score_version"] == DESK_VERSION
        assert stamp["ranking_engine_version"] == RANKING_ENGINE_VERSION

    async def test_a_desk_score_composite_never_ranks_a_rejected_row(
        self, screener_session: AsyncSession
    ) -> None:
        """B has the best ret_12m in the universe; the book rejected it, so it is not a result.

        Over A and D (filtered_results): desk D=1, A=0; ret D=1, A=0 -> D=100.00, A=0.00.
        """
        await self._seed_book(screener_session)
        body = await run(
            screener_session,
            ranked(
                sort_by="desk_score",
                ranking_terms=[
                    {"factor": "desk_score", "preference": "higher"},
                    {"factor": "ret_12m", "preference": "higher"},
                ],
            ),
        )
        assert symbols(body) == ["DSKD", "DSKA"]
        assert column(body, "composite_score") == [Decimal("100.00"), Decimal("0.00")]
        assert "DSKB" not in symbols(body)
        assert "DSKC" not in symbols(body)

    async def test_legacy_desk_score_reads_the_table_and_attaches_the_stored_breakdown(
        self, screener_session: AsyncSession
    ) -> None:
        """No ranking_terms: docs/06's SQL ranks the stored SCORE; nothing re-scores survivors."""
        await self._seed_book(screener_session)
        body = await run(screener_session, ranked(sort_by="desk_score"))
        assert symbols(body) == ["DSKD", "DSKA"]
        assert column(body, "sorting_factor") == [Decimal("80.0"), Decimal("70.0")]
        assert column(body, "desk_eligible") == [True, True]
        assert column(body, "desk_a_trend") == [Decimal("20.0000"), Decimal("20.0000")]
        stamp = provenance(body)
        assert stamp["ranking_engine_version"] == LEGACY_RANKING_ENGINE_VERSION
        assert stamp["desk_score_version"] == DESK_VERSION
