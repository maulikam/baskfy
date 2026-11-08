"""``POST /screens/explain`` and ``POST /screens/selection`` (gate 2.G G3, docs/ranking/PLAN.md).

Explain is C4 step 6 over HTTP; selection is C5. What only this suite can prove is the join of
the pure core functions with the database: the row explained is the row the preview ranked, the
selection's candidates are the engine's ranks unmodified, a portfolio is read only for its owner,
and the correlation cap reads its returns from ``ohlcv_daily``.

Every expected number is derived by hand in the test's docstring from C4/C5, so an arithmetic
change in core fails here rather than being re-recorded. The last class holds the order-path
safety assertions and needs no database.
"""

from __future__ import annotations

import datetime as dt
import importlib
import inspect
import subprocess
import sys
from collections.abc import Mapping, Sequence
from decimal import Decimal

import httpx
import numpy as np
import pytest
from api_helpers import ORDER_PATH_TOKENS, assert_problem, bearer, make_user, url
from screener_helpers import (
    AS_OF,
    DATA_VERSION,
    add_factor_row,
    add_member,
    make_row,
    requires_db,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import screen_ranking
from baskfy_api.routers import screens as screens_router
from baskfy_api.schemas import ScreenSelectionOut
from baskfy_core import factor_registry, ranking_selection
from baskfy_core.models import DeskScoreDaily, OhlcvDaily, TradingDay
from baskfy_core.nse_momentum import NSE_MOMENTUM_VERSION
from baskfy_core.ranking_engine import RANKING_ENGINE_VERSION
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.universes import UNIVERSE_BY_SLUG

Json = dict[str, object]

MINIMAL: Json = {
    "index": "nifty-total-market",
    "sort_by": "avg_sharpe_12_6_3_1",
    "series": ["EQ", "BE"],
}
RANKED_MINIMAL: Json = {
    **MINIMAL,
    "ranking_mode": "single",
    "ranking_terms": [{"factor": "avg_sharpe_12_6_3_1", "preference": "higher"}],
}

#: A published trading day with no reference-export rows, so a test owns its whole universe.
SYNTHETIC = dt.date(2026, 8, 17)
NIFTY_500 = UNIVERSE_BY_SLUG["nifty-500"]
#: After every published date and inside the seeded calendar: docs/07's 422 no-trading-day.
FUTURE = "2026-12-01"

EXPLAIN_KEYS = {
    "as_of",
    "data_version",
    "instrument_id",
    "symbol",
    "rank",
    "total",
    "terms",
    "positives",
    "deductions",
    "eligibility",
    "data_quality",
    "desk",
    "provenance",
    "rank_history",
}


def obj(value: object) -> Json:
    assert isinstance(value, dict), value
    return value


def arr(value: object) -> list[Json]:
    assert isinstance(value, list), value
    return [obj(item) for item in value]


async def subscriber(session: AsyncSession, email: str) -> dict[str, str]:
    """Synthetic rows live on a past date, which is the ``historical_ranks`` feature."""
    _, public_id = await make_user(session, email, subscribed=True)
    return bearer(public_id)


def synthetic(**overrides: object) -> Json:
    payload: Json = {
        "index": NIFTY_500.slug,
        "sort_by": "ret_12m",
        "historical_date": SYNTHETIC.isoformat(),
    }
    payload.update(overrides)
    return payload


async def seed(session: AsyncSession, rows: Mapping[str, Mapping[str, object]]) -> dict[str, int]:
    return {
        symbol: await make_row(session, symbol, NIFTY_500.index_id, on=SYNTHETIC, values=values)
        for symbol, values in rows.items()
    }


async def sessions_up_to(session: AsyncSession, day: dt.date, count: int) -> list[dt.date]:
    dates = list(
        (
            await session.execute(
                select(TradingDay.date)
                .where(
                    TradingDay.exchange_id == NSE_EXCHANGE_ID,
                    TradingDay.is_trading_day.is_(True),
                    TradingDay.date <= day,
                )
                .order_by(TradingDay.date.desc())
                .limit(count)
            )
        ).scalars()
    )
    dates.reverse()
    return dates


async def add_bars(
    session: AsyncSession,
    instrument_id: int,
    days: Sequence[dt.date],
    closes: Sequence[Decimal],
    adj_factors: Sequence[Decimal] | None = None,
) -> None:
    for position, (day, close) in enumerate(zip(days, closes, strict=True)):
        session.add(
            OhlcvDaily(
                instrument_id=instrument_id,
                date=day,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=1000,
                close_raw=close,
                volume_raw=1000,
                adj_factor=adj_factors[position] if adj_factors is not None else Decimal(1),
                source="nse",
            )
        )
    await session.flush()


def prices(returns: Sequence[float]) -> list[Decimal]:
    level = 100.0
    out = [Decimal("100.0000")]
    for value in returns:
        level *= 1 + value
        out.append(Decimal(f"{level:.4f}"))
    return out


async def preview_ranks(api: httpx.AsyncClient) -> list[str]:
    body = obj(
        (await api.post(url("/screens/preview"), json={"definition": RANKED_MINIMAL})).json()
    )
    return [str(row["symbol"]) for row in arr(body["rows"])]


# ---------------------------------------------------------------------------
# POST /screens/explain
# ---------------------------------------------------------------------------


@pytest.mark.db
@pytest.mark.redis
@requires_db
class TestExplainRoute:
    async def test_explain_requires_authentication(self, api: httpx.AsyncClient) -> None:
        response = await api.post(
            url("/screens/explain"), json={"definition": RANKED_MINIMAL, "symbol": "CUPID"}
        )
        assert_problem(response, 401, "unauthenticated")

    async def test_explain_names_the_rank_the_preview_served(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """One engine path: the drawer's rank is the table's rank, and the envelope is C4's."""
        _, public_id = await make_user(screener_session, "explain.rank@example.com")
        order = await preview_ranks(api)
        symbol = order[9]
        response = await api.post(
            url("/screens/explain"),
            json={"definition": RANKED_MINIMAL, "symbol": symbol.lower()},
            headers=bearer(public_id),
        )
        assert response.status_code == 200, response.text
        body = obj(response.json())
        assert set(body) == EXPLAIN_KEYS
        assert body["symbol"] == symbol
        assert body["rank"] == 10
        assert body["total"] is None, "single mode has no composite total"
        assert body["as_of"] == AS_OF.isoformat()
        assert body["data_version"] == DATA_VERSION
        (term,) = arr(body["terms"])
        assert term["factor"] == "avg_sharpe_12_6_3_1"
        assert term["missing"] is False
        assert obj(body["eligibility"]) == {"passed": True, "failures": []}
        assert obj(body["provenance"]) == {
            "universe": "nifty-total-market",
            "as_of": AS_OF.isoformat(),
            "data_version": DATA_VERSION,
            "ranking_engine_version": RANKING_ENGINE_VERSION,
            "desk_score_version": None,
            "nse_momentum_version": NSE_MOMENTUM_VERSION,
            "scope": "filtered_results",
            "mode": "single",
        }
        # The session before AS_OF is SYNTHETIC, which carries no reference rows: nothing ranked.
        assert obj(body["rank_history"]) == {
            "today": 10,
            "previous": None,
            "previous_as_of": SYNTHETIC.isoformat(),
            "change": None,
        }

    async def test_explain_composite_terms_positives_deductions_and_eligibility(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """C4 steps 3-6 over four rows, two families at an equal share, one filter.

        ``min_return_1y=8`` fails EXPD (ret 5), so the scope set is A, B, C.
        ret_12m (higher): A=1, B=0.5, C=0. vol_12m (lower): A=0, B=1, C=0.5.
        B: contributions 100x0.5x0.5 = 25 and 100x0.5x1 = 50, total 75.00, rank 1; vol is a
        positive (>= 0.8). C: total 25.00, rank 3; ret is a deduction (<= 0.2). D: not ranked,
        ``min_return_1y`` named as its failure, no term scores (outside the scope set).
        No ``ohlcv_daily`` bar at all is the stalest price there is.
        """
        headers = await subscriber(screener_session, "explain.composite@example.com")
        await seed(
            screener_session,
            {
                "EXPA": {"ret_12m": Decimal("30"), "vol_12m": Decimal("0.30")},
                "EXPB": {"ret_12m": Decimal("20"), "vol_12m": Decimal("0.10")},
                "EXPC": {"ret_12m": Decimal("10"), "vol_12m": Decimal("0.20")},
                "EXPD": {"ret_12m": Decimal("5"), "vol_12m": Decimal("0.05")},
            },
        )
        definition = synthetic(
            min_return_1y="8",
            ranking_terms=[
                {"factor": "ret_12m", "preference": "higher"},
                {"factor": "vol_12m", "preference": "lower"},
            ],
        )
        ret_label = factor_registry.get("ret_12m").label
        vol_label = factor_registry.get("vol_12m").label

        async def explain(symbol: str) -> Json:
            response = await api.post(
                url("/screens/explain"),
                json={"definition": definition, "symbol": symbol},
                headers=headers,
            )
            assert response.status_code == 200, response.text
            return obj(response.json())

        b = await explain("EXPB")
        assert b["rank"] == 1
        assert b["total"] == 75.0
        assert [
            (t["factor"], t["transformed"], t["effective_weight"], t["contribution"])
            for t in arr(b["terms"])
        ] == [("ret_12m", 0.5, 0.5, 25.0), ("vol_12m", 1.0, 0.5, 50.0)]
        assert b["positives"] == [f"Top 20% on {vol_label} within the filtered results"]
        assert b["deductions"] == []
        assert b["desk"] is None
        assert obj(b["data_quality"]) == {
            "missing_factors": [],
            "insufficient_history": False,
            "stale_price": True,
            "recent_corporate_action": False,
        }
        assert obj(b["provenance"])["mode"] == "composite"
        assert obj(b["provenance"])["universe"] == NIFTY_500.slug

        c = await explain("EXPC")
        assert (c["rank"], c["total"]) == (3, 25.0)
        assert c["deductions"] == [f"Bottom 20% on {ret_label} within the filtered results"]

        d = await explain("EXPD")
        assert d["rank"] is None
        eligibility = obj(d["eligibility"])
        assert eligibility["passed"] is False
        (failure,) = arr(eligibility["failures"])
        assert failure["filter"] == "min_return_1y"
        assert str(failure["detail"]).endswith(">= 8")
        assert [t["transformed"] for t in arr(d["terms"])] == [None, None]

    async def test_explain_recent_corporate_action_is_point_in_time(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """C4's "adj_factor != 1 within 252 sessions", read without look-ahead (house rule 5).

        EXPF's bars all carry 0.5: an action after ``as_of`` scaled the whole window, which the
        date could not have known — not recent. EXPG's factor changes inside the window: an
        action went ex in it — recent. Both have a bar on ``as_of``, so neither price is stale.
        """
        headers = await subscriber(screener_session, "explain.ca@example.com")
        ids = await seed(
            screener_session,
            {"EXPF": {"ret_12m": Decimal("20")}, "EXPG": {"ret_12m": Decimal("10")}},
        )
        days = await sessions_up_to(screener_session, SYNTHETIC, 10)
        closes = [Decimal(100)] * len(days)
        await add_bars(screener_session, ids["EXPF"], days, closes, [Decimal("0.5")] * len(days))
        await add_bars(
            screener_session,
            ids["EXPG"],
            days,
            closes,
            [Decimal("0.5")] * 5 + [Decimal(1)] * (len(days) - 5),
        )
        definition = synthetic(ranking_terms=[{"factor": "ret_12m", "preference": "higher"}])
        flags: dict[str, Json] = {}
        for symbol in ("EXPF", "EXPG"):
            response = await api.post(
                url("/screens/explain"),
                json={"definition": definition, "symbol": symbol},
                headers=headers,
            )
            assert response.status_code == 200, response.text
            flags[symbol] = obj(obj(response.json())["data_quality"])
        assert flags["EXPF"]["recent_corporate_action"] is False
        assert flags["EXPG"]["recent_corporate_action"] is True
        assert flags["EXPF"]["stale_price"] is False

    async def test_explain_rank_history_ranks_the_previous_session_point_in_time(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """Previous rank = the same definition ranked on the session before, from that day's rows.

        Single ret_12m (higher), ``min_return_1y=8``, NIFTY 500 members only.
        Previous session P: HISA 10, HISB 30, HISD 25; HISC not a member that day.
        -> P ranks HISB 1, HISD 2, HISA 3.
        SYNTHETIC: HISA 30, HISB 20, HISC 10, HISD 5 (fails the filter).
        -> today HISA 1, HISB 2, HISC 3; HISD unranked.
        change = previous - today: HISA 3-1 = +2, HISB 1-2 = -1; HISC has no previous rank and
        HISD no rank today, so neither claims a change. Were today's rows re-read for P, HISA
        would be 1 on both days — the +2 is what proves the previous run read P's rows.
        """
        headers = await subscriber(screener_session, "explain.history@example.com")
        ids = await seed(
            screener_session,
            {
                "HISA": {"ret_12m": Decimal("30")},
                "HISB": {"ret_12m": Decimal("20")},
                "HISC": {"ret_12m": Decimal("10")},
                "HISD": {"ret_12m": Decimal("5")},
            },
        )
        previous, today = await sessions_up_to(screener_session, SYNTHETIC, 2)
        assert today == SYNTHETIC
        for symbol, value in (("HISA", "10"), ("HISB", "30"), ("HISD", "25")):
            await add_member(screener_session, NIFTY_500.index_id, ids[symbol], previous)
            await add_factor_row(
                screener_session,
                ids[symbol],
                previous,
                {"series": "EQ", "ret_12m": Decimal(value)},
            )
        definition = synthetic(
            min_return_1y="8", ranking_terms=[{"factor": "ret_12m", "preference": "higher"}]
        )
        histories: dict[str, Json] = {}
        for symbol in ("HISA", "HISB", "HISC", "HISD"):
            response = await api.post(
                url("/screens/explain"),
                json={"definition": definition, "symbol": symbol},
                headers=headers,
            )
            assert response.status_code == 200, response.text
            histories[symbol] = obj(obj(response.json())["rank_history"])
        on = previous.isoformat()
        assert histories == {
            "HISA": {"today": 1, "previous": 3, "previous_as_of": on, "change": 2},
            "HISB": {"today": 2, "previous": 1, "previous_as_of": on, "change": -1},
            "HISC": {"today": 3, "previous": None, "previous_as_of": on, "change": None},
            "HISD": {"today": None, "previous": 2, "previous_as_of": on, "change": None},
        }

    async def test_explain_desk_block_carries_the_stored_inputs_behind_each_grade(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """C2: the desk block is the ``desk_score_daily`` row as stored, grade by grade.

        The row stores one raw input, ``ext_over_20dma``, which ``score.score`` reads in A and
        F; B-E store none. Ranges are the formula's clips (A/B 0-25, C 0-20, D/E 0-10, F -10..0).
        """
        headers = await subscriber(screener_session, "explain.deskinputs@example.com")
        ids = await seed(screener_session, {"DSKA": {"ret_12m": Decimal("30")}})
        screener_session.add(
            DeskScoreDaily(
                instrument_id=ids["DSKA"],
                date=SYNTHETIC,
                score=Decimal("66.5"),
                score_rank=4,
                a_trend=Decimal("21.0000"),
                b_momentum=Decimal("18.2500"),
                c_sharpe=Decimal("14.0000"),
                d_consistency=Decimal("7.5000"),
                e_liquidity=Decimal("7.2500"),
                f_penalty=Decimal("-1.5000"),
                ext_over_20dma=Decimal("19.4000"),
                reject="",
                score_version="desk-score-test",
            )
        )
        await screener_session.flush()
        response = await api.post(
            url("/screens/explain"),
            json={
                "definition": synthetic(
                    ranking_terms=[{"factor": "ret_12m", "preference": "higher"}]
                ),
                "symbol": "DSKA",
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        desk = obj(obj(response.json())["desk"])
        assert (desk["score"], desk["rank"], desk["eligible"]) == (66.5, 4, True)
        assert (desk["ext_over_20dma"], desk["score_version"]) == (19.4, "desk-score-test")
        components = arr(desk["components"])
        assert [
            (c["grade"], c["key"], c["points"], c["min_points"], c["max_points"])
            for c in components
        ] == [
            ("A", "a_trend", 21.0, 0.0, 25.0),
            ("B", "b_momentum", 18.25, 0.0, 25.0),
            ("C", "c_sharpe", 14.0, 0.0, 20.0),
            ("D", "d_consistency", 7.5, 0.0, 10.0),
            ("E", "e_liquidity", 7.25, 0.0, 10.0),
            ("F", "f_penalty", -1.5, -10.0, 0.0),
        ]
        assert {
            str(c["grade"]): [(i["name"], i["value"]) for i in arr(c["inputs"])] for c in components
        } == {
            "A": [("ext_over_20dma", 19.4)],
            "B": [],
            "C": [],
            "D": [],
            "E": [],
            "F": [("ext_over_20dma", 19.4)],
        }

    async def test_explain_a_symbol_outside_the_universe_is_not_found(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, public_id = await make_user(screener_session, "explain.404@example.com")
        response = await api.post(
            url("/screens/explain"),
            json={"definition": RANKED_MINIMAL, "symbol": "NOSUCHNAME"},
            headers=bearer(public_id),
        )
        assert_problem(response, 404, "not-found")

    async def test_explain_an_as_of_after_the_published_range_is_422(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await subscriber(screener_session, "explain.422@example.com")
        response = await api.post(
            url("/screens/explain"),
            json={"definition": RANKED_MINIMAL, "symbol": "CUPID", "as_of": FUTURE},
            headers=headers,
        )
        assert_problem(response, 422, "no-trading-day")

    async def test_explain_refuses_a_definition_without_ranking_terms(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, public_id = await make_user(screener_session, "explain.legacy@example.com")
        response = await api.post(
            url("/screens/explain"),
            json={"definition": MINIMAL, "symbol": "CUPID"},
            headers=bearer(public_id),
        )
        assert_problem(response, 400, "invalid-screen-definition")

    async def test_explain_refuses_a_malformed_body(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, public_id = await make_user(screener_session, "explain.body@example.com")
        response = await api.post(
            url("/screens/explain"),
            json={"definition": RANKED_MINIMAL, "symbol": "", "extra": 1},
            headers=bearer(public_id),
        )
        assert_problem(response, 400, "invalid-screen-definition")


# ---------------------------------------------------------------------------
# POST /screens/selection
# ---------------------------------------------------------------------------


@pytest.mark.db
@pytest.mark.redis
@requires_db
class TestSelectionRoute:
    async def test_selection_requires_authentication(self, api: httpx.AsyncClient) -> None:
        response = await api.post(
            url("/screens/selection"), json={"definition": RANKED_MINIMAL, "holdings": []}
        )
        assert_problem(response, 401, "unauthenticated")

    async def test_selection_refuses_a_portfolio_the_caller_does_not_own(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """Not-yours is a 404, as on ``/portfolios`` — a 403 would confirm the id exists."""
        _, alice = await make_user(screener_session, "alice.sel@example.com")
        _, bob = await make_user(screener_session, "bob.sel@example.com")
        created = await api.post(
            url("/portfolios"),
            json={"name": "Alice's", "holdings": [{"symbol": "CUPID", "quantity": "10"}]},
            headers=bearer(alice),
        )
        assert created.status_code == 201, created.text
        portfolio_id = obj(obj(created.json())["portfolio"])["id"]
        for headers, pid in ((bearer(bob), portfolio_id), (bearer(alice), 999_999_999)):
            response = await api.post(
                url("/screens/selection"),
                json={"definition": RANKED_MINIMAL, "portfolio_id": pid},
                headers=headers,
            )
            assert_problem(response, 404, "not-found")

    async def test_selection_over_an_owned_portfolio(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """C5 rules 1 and 3 with the defaults (15 names, entry 15, retention 60 — VALIDATION.md §6).

        Held: rank 1 (kept), rank 100 (exits, outside retention), rank 2 with no quantity
        (not a position C5 can count — named, not held). Entrants are ranks 2..15, all 14 of
        which fit beside the one kept name. No capital, so capacity is not checked.
        """
        _, public_id = await make_user(screener_session, "owner.sel@example.com")
        headers = bearer(public_id)
        order = await preview_ranks(api)
        created = await api.post(
            url("/portfolios"),
            json={
                "name": "Core",
                "holdings": [
                    {"symbol": order[0], "quantity": "10"},
                    {"symbol": order[99], "quantity": "4"},
                    {"symbol": order[1]},
                ],
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        portfolio_id = obj(obj(created.json())["portfolio"])["id"]

        response = await api.post(
            url("/screens/selection"),
            json={"definition": RANKED_MINIMAL, "portfolio_id": portfolio_id},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = obj(response.json())
        assert body["informational_only"] is True
        assert "plan_id" not in body
        assert body["portfolio_id"] == portfolio_id
        assert body["holdings_without_quantity"] == [order[1]]
        assert obj(body["provenance"])["ranking_engine_version"] == RANKING_ENGINE_VERSION
        assert (body["as_of"], body["data_version"]) == (AS_OF.isoformat(), DATA_VERSION)
        summary = obj(body["summary"])
        assert (summary["kept"], summary["exits"], summary["entries"], summary["skips"]) == (
            1,
            1,
            14,
            0,
        )
        assert [note["code"] for note in arr(body["notes"])] == ["CAPACITY_NOT_CHECKED"]
        rows = {str(row["symbol"]): row for row in arr(body["rows"])}
        assert (rows[order[0]]["action"], rows[order[0]]["reasons"]) == (
            "hold",
            ["RANK_WITHIN_RETENTION"],
        )
        assert rows[order[0]]["current_quantity"] == "10.0000", "stored precision (rule 8)"
        assert (rows[order[99]]["action"], rows[order[99]]["reasons"]) == (
            "exit",
            ["RANK_OUTSIDE_RETENTION"],
        )
        assert rows[order[99]]["quality_rank"] == 100
        entering = [row for row in arr(body["rows"]) if row["action"] == "enter"]
        assert [row["symbol"] for row in entering] == order[1:15]
        assert [row["quality_rank"] for row in entering] == list(range(2, 16))

    async def test_selection_inline_holdings_hold_exit_enter_and_skip(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """C5 over five hand-ranked rows (single ret_12m): A1 B2 C3 D4 E5.

        Holdings D (10) and E (5); max_names 2, entry 2, retention 4, capital 1,00,000.
        D rank 4 <= 4 holds; E rank 5 exits. Slot = 100000 / 2 = 50000.00.
        A: participation 50000 / 1,00,00,000 x 100 = 0.5000% <= 1%, qty floor(50000/1000) = 50,
        enters. B: 50000 / 10,00,000 x 100 = 5.0000% > 1% (CAPACITY) and D + A fill the book
        (FULL): skip with both, in C5's order. C is ranked outside entry and never considered.
        """
        headers = await subscriber(screener_session, "inline.sel@example.com")
        await seed(
            screener_session,
            {
                "SELA": {
                    "ret_12m": Decimal("50"),
                    "median_vol_12m": 10_000_000,
                    "close_raw": Decimal("1000"),
                },
                "SELB": {
                    "ret_12m": Decimal("40"),
                    "median_vol_12m": 1_000_000,
                    "close_raw": Decimal("250"),
                },
                "SELC": {"ret_12m": Decimal("30")},
                "SELD": {"ret_12m": Decimal("20")},
                "SELE": {"ret_12m": Decimal("10")},
            },
        )
        response = await api.post(
            url("/screens/selection"),
            json={
                "definition": synthetic(
                    ranking_mode="single",
                    ranking_terms=[{"factor": "ret_12m", "preference": "higher"}],
                ),
                "holdings": [
                    {"symbol": "seld", "quantity": "10"},
                    {"symbol": "SELE", "quantity": "5"},
                ],
                "constraints": {
                    "max_names": 2,
                    "entry_rank": 2,
                    "retention_rank": 4,
                    "capital_inr": "100000",
                },
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = obj(response.json())
        rows = arr(body["rows"])
        assert [(r["symbol"], r["action"], r["reasons"]) for r in rows] == [
            ("SELA", "enter", ["RANK_WITHIN_ENTRY"]),
            ("SELB", "skip", ["CAPACITY", "FULL"]),
            ("SELD", "hold", ["RANK_WITHIN_RETENTION"]),
            ("SELE", "exit", ["RANK_OUTSIDE_RETENTION"]),
        ]
        a, b, d, _ = rows
        assert (a["proposed_value_inr"], a["proposed_qty"], a["adv_participation_pct"]) == (
            "50000.00",
            50,
            "0.5000",
        )
        assert b["adv_participation_pct"] == "5.0000"
        assert [r["quality_rank"] for r in rows] == [1, 2, 4, 5]
        assert a["score"] == 50.0, "single mode reports the first term's raw value, unmodified"
        assert d["current_quantity"] == "10"
        assert body["portfolio_id"] is None
        summary = obj(body["summary"])
        assert (summary["turnover_used"], summary["unfilled_slots"]) == (2, 0)
        assert obj(body["constraints"])["capital_inr"] == "100000"

    async def test_selection_correlation_cap_reads_returns_from_ohlcv_daily(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """C5's CORRELATION over 60 daily returns loaded from ``ohlcv_daily``.

        CORD is held (rank 4, retention 4). CORA's closes are CORD's, so their returns correlate
        at 1.0 > 0.5: skip. CORB alternates +1% / -1%, which against CORD's pattern is well
        under 0.5 (computed below from the constructed returns): enter. The refused CORA is not a
        peer, so CORB is measured against CORD alone.
        """
        headers = await subscriber(screener_session, "corr.sel@example.com")
        ids = await seed(
            screener_session,
            {
                "CORA": {"ret_12m": Decimal("50")},
                "CORB": {"ret_12m": Decimal("40")},
                "CORC": {"ret_12m": Decimal("30")},
                "CORD": {"ret_12m": Decimal("20")},
            },
        )
        window = 60
        days = await sessions_up_to(screener_session, SYNTHETIC, window + 1)
        assert len(days) == window + 1
        pattern = [0.01 * (((t * 7) % 5) - 2) for t in range(window)]
        alternating = [0.01 if t % 2 == 0 else -0.01 for t in range(window)]
        expected_b = float(np.corrcoef(alternating, pattern)[0, 1])
        assert abs(expected_b) < 0.3, "the fixture must separate the two cases clearly"
        await add_bars(screener_session, ids["CORA"], days, prices(pattern))
        await add_bars(screener_session, ids["CORD"], days, prices(pattern))
        await add_bars(screener_session, ids["CORB"], days, prices(alternating))

        response = await api.post(
            url("/screens/selection"),
            json={
                "definition": synthetic(
                    ranking_mode="single",
                    ranking_terms=[{"factor": "ret_12m", "preference": "higher"}],
                ),
                "holdings": [{"symbol": "CORD", "quantity": "10"}],
                "constraints": {
                    "max_names": 3,
                    "entry_rank": 2,
                    "retention_rank": 4,
                    "max_correlation": 0.5,
                    "correlation_window": window,
                },
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        rows = {str(row["symbol"]): row for row in arr(obj(response.json())["rows"])}
        cora, corb = rows["CORA"], rows["CORB"]
        assert (cora["action"], cora["reasons"]) == ("skip", ["CORRELATION"])
        assert (cora["max_correlation"], cora["correlation_peer"]) == (1.0, "CORD")
        assert corb["action"] == "enter"
        assert corb["correlation_peer"] == "CORD"
        assert corb["max_correlation"] == pytest.approx(expected_b, abs=0.01)
        assert corb["flags"] == []

    async def test_selection_an_as_of_after_the_published_range_is_422(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await subscriber(screener_session, "sel.422@example.com")
        response = await api.post(
            url("/screens/selection"),
            json={"definition": RANKED_MINIMAL, "holdings": [], "as_of": FUTURE},
            headers=headers,
        )
        assert_problem(response, 422, "no-trading-day")

    @pytest.mark.parametrize(
        "extra",
        [
            pytest.param({}, id="neither-source"),
            pytest.param({"portfolio_id": 1, "holdings": []}, id="both-sources"),
            pytest.param(
                {"holdings": [], "constraints": {"entry_rank": 20, "retention_rank": 10}},
                id="retention-inside-entry",
            ),
            pytest.param({"holdings": [{"symbol": "CUPID", "quantity": "0"}]}, id="zero-qty"),
            pytest.param(
                {"holdings": [], "constraints": {"correlation_window": 10}}, id="short-window"
            ),
        ],
    )
    async def test_selection_refuses_a_malformed_request(
        self, api: httpx.AsyncClient, screener_session: AsyncSession, extra: Json
    ) -> None:
        _, public_id = await make_user(screener_session, "sel.body@example.com")
        response = await api.post(
            url("/screens/selection"),
            json={"definition": RANKED_MINIMAL, **extra},
            headers=bearer(public_id),
        )
        assert_problem(response, 400, "invalid-screen-definition")

    async def test_selection_refuses_an_unknown_inline_symbol(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, public_id = await make_user(screener_session, "sel.unknown@example.com")
        response = await api.post(
            url("/screens/selection"),
            json={
                "definition": RANKED_MINIMAL,
                "holdings": [{"symbol": "NOTALISTEDNAME", "quantity": "5"}],
            },
            headers=bearer(public_id),
        )
        body = assert_problem(response, 400, "bad-request")
        assert "NOTALISTEDNAME" in str(body)

    async def test_selection_refuses_a_definition_without_ranking_terms(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, public_id = await make_user(screener_session, "sel.legacy@example.com")
        response = await api.post(
            url("/screens/selection"),
            json={"definition": MINIMAL, "holdings": []},
            headers=bearer(public_id),
        )
        assert_problem(response, 400, "invalid-screen-definition")


# ---------------------------------------------------------------------------
# The order path (no database)
# ---------------------------------------------------------------------------


class TestSelectionReachesNoOrderPath:
    """Non-negotiable #1 and the second law for C5: a proposal, never a plan or an order.

    The same token list ``test_api_portfolios_tree.TestNoOrderPath`` holds the rebalance router
    to, plus a transitive check: the pure selection module and its I/O half must not even load
    an execution module. (``routers/screens.py`` is scanned by source only — like every router it
    imports ``baskfy_api.auth``, whose broker-connection imports load the Kite client.)
    """

    @pytest.mark.parametrize(
        "module", [ranking_selection, screen_ranking, screens_router], ids=lambda m: m.__name__
    )
    def test_selection_sources_name_no_order_path(self, module: object) -> None:
        source = inspect.getsource(importlib.import_module(getattr(module, "__name__", "")))
        for forbidden in ORDER_PATH_TOKENS:
            assert forbidden not in source, f"{getattr(module, '__name__', module)}: {forbidden}"

    def test_selection_modules_import_nothing_from_the_execution_path(self) -> None:
        code = (
            "import sys\n"
            "import baskfy_core.ranking_selection, baskfy_api.screen_ranking\n"
            "print(sorted(m for m in sys.modules "
            "if m.split('.')[0] in ('baskfy_execution', 'kiteconnect')))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        assert result.stdout.strip() == "[]", result.stdout

    def test_selection_adds_no_execute_or_order_route(self) -> None:
        for route in screens_router.router.routes:
            path = str(getattr(route, "path", "")).lower()
            assert "execute" not in path
            assert "/order" not in path

    def test_selection_response_carries_no_plan(self) -> None:
        assert "plan_id" not in ScreenSelectionOut.model_fields
        assert "informational_only" in ScreenSelectionOut.model_fields
