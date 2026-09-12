"""Contract tests for ``/meta/*`` — docs/07 §Metadata (Prompt 7 acceptance criterion 1)."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from api_helpers import assert_problem, errors_of, url
from screener_helpers import AS_OF, DATA_VERSION, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.screener import DATA_START_DATE
from baskfy_core.models import PipelineRun
from baskfy_core.factor_registry import COLUMN_PICKER_KEYS, FACTORS, SORT_FACTOR_KEYS
from baskfy_core.universes import UNIVERSES

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]


class TestFactors:
    async def test_it_returns_the_whole_registry(self, api: httpx.AsyncClient) -> None:
        response = await api.get(url("/meta/factors"))
        assert response.status_code == 200
        body = response.json()
        assert [row["key"] for row in body] == list(SORT_FACTOR_KEYS)

    async def test_each_entry_carries_the_documented_fields(self, api: httpx.AsyncClient) -> None:
        """docs/07: "(key, label, family, unit, higher_is_better)"."""
        body = (await api.get(url("/meta/factors"))).json()
        assert set(body[0]) == {"key", "label", "family", "unit", "higher_is_better"}

    async def test_it_never_exposes_the_sql_expression(self, api: httpx.AsyncClient) -> None:
        """The registry's ``sql_expr`` is a server-side whitelist, not a client-side value.

        Publishing it would hand a caller the exact column names to probe with, and gains the UI
        nothing: it sorts by *key*.
        """
        text = (await api.get(url("/meta/factors"))).text
        assert "sql_expr" not in text
        assert FACTORS["avg_sharpe_12_6_3_1"].sql_expr not in text


class TestColumns:
    async def test_it_returns_the_column_picker(self, api: httpx.AsyncClient) -> None:
        body = (await api.get(url("/meta/columns"))).json()
        assert [row["key"] for row in body] == list(COLUMN_PICKER_KEYS)

    async def test_display_only_columns_are_marked(self, api: httpx.AsyncClient) -> None:
        """``series`` and the circuit counts are columns but not ranking factors."""
        body = {row["key"]: row for row in (await api.get(url("/meta/columns"))).json()}
        assert body["series"]["is_factor"] is False
        assert body["circuits_12m"]["is_factor"] is False
        assert body["ret_12m"]["is_factor"] is True
        assert body["series"]["label"] == "SERIES"


class TestUniverses:
    async def test_it_returns_the_fourteen_in_ui_order(self, api: httpx.AsyncClient) -> None:
        """docs/01 §2.1's order, which differs from the mask-bit order (see universes.py)."""
        body = (await api.get(url("/meta/universes"))).json()
        assert len(body) == len(UNIVERSES)
        assert [row["sort_order"] for row in body] == sorted(u.ui_order for u in UNIVERSES)
        assert body[0]["slug"] == "nifty-50"

    async def test_no_dashboard_index_leaks_into_the_dropdown(self, api: httpx.AsyncClient) -> None:
        """docs/07: "index_def rows where is_universe" — the ~145 dashboard indices are not."""
        body = (await api.get(url("/meta/universes"))).json()
        assert all(row["index_id"] < 100 for row in body)


class TestTradingDays:
    async def test_it_lists_trading_days_in_the_range(self, api: httpx.AsyncClient) -> None:
        response = await api.get(
            url("/meta/trading-days"), params={"from": "2026-08-10", "to": "2026-08-20"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["from"] == "2026-08-10"
        assert "2026-08-15" not in body["dates"], "a Saturday is not a trading day"
        assert "2026-08-17" in body["dates"]

    async def test_an_inverted_range_is_refused(self, api: httpx.AsyncClient) -> None:
        response = await api.get(
            url("/meta/trading-days"), params={"from": "2026-08-20", "to": "2026-08-10"}
        )
        assert_problem(response, 422, "no-trading-day")

    async def test_an_unbounded_range_is_refused(self, api: httpx.AsyncClient) -> None:
        response = await api.get(
            url("/meta/trading-days"), params={"from": "1990-01-01", "to": "2030-01-01"}
        )
        assert_problem(response, 422, "no-trading-day")

    async def test_a_missing_bound_is_a_validation_problem(self, api: httpx.AsyncClient) -> None:
        """docs/07: a schema violation is 400 `invalid-screen-definition` with `errors[]`."""
        body = assert_problem(
            await api.get(url("/meta/trading-days")), 400, "invalid-screen-definition"
        )
        fields = {error["field"] for error in errors_of(body)}
        assert fields == {"from", "to"}


class TestStatus:
    async def test_it_reports_the_published_snapshot(self, api: httpx.AsyncClient) -> None:
        """docs/07: "{ as_of, data_version, last_pipeline_run }"."""
        body = (await api.get(url("/meta/status"))).json()
        assert body["as_of"] == AS_OF.isoformat()
        assert body["data_version"] == DATA_VERSION
        assert body["last_pipeline_run"]["status"] == "succeeded"
        assert body["degraded"] is False
        assert body["data_start_date"] == DATA_START_DATE.isoformat()

    async def test_a_retry_that_published_is_not_degraded(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """A date can hold several runs, and the newest attempt is the one that describes it.

        This is the box's own 2026-09-11: three runs failed the quality gate, a fourth passed it
        and published. Ordering on ``trade_date`` alone left the tie to the planner, so the
        endpoint served ``degraded: true`` — a stale-data banner over a day whose data was
        published and current.
        """
        screener_session.add(
            PipelineRun(
                trade_date=AS_OF,
                status="failed",
                started_at=dt.datetime(2026, 8, 18, 13, 0, tzinfo=dt.UTC),
                finished_at=dt.datetime(2026, 8, 18, 13, 30, tzinfo=dt.UTC),
                data_version=None,
            )
        )
        await screener_session.flush()

        body = (await api.get(url("/meta/status"))).json()
        assert body["last_pipeline_run"]["status"] == "succeeded"
        assert body["last_pipeline_run"]["data_version"] == DATA_VERSION
        assert body["degraded"] is False

    async def test_a_failure_after_a_publish_still_raises_the_banner(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The other direction, so "newest wins" is pinned rather than "success wins".

        docs/11 §Reliability: a failed run serves the last good ``data_version`` *with a banner*.
        The published version keeps answering — ``as_of`` does not move — but the reader is told.
        """
        screener_session.add(
            PipelineRun(
                trade_date=AS_OF,
                status="failed",
                started_at=dt.datetime(2026, 8, 18, 15, 0, tzinfo=dt.UTC),
                finished_at=dt.datetime(2026, 8, 18, 15, 30, tzinfo=dt.UTC),
                data_version=None,
            )
        )
        await screener_session.flush()

        body = (await api.get(url("/meta/status"))).json()
        assert body["last_pipeline_run"]["status"] == "failed"
        assert body["degraded"] is True
        assert body["as_of"] == AS_OF.isoformat()
        assert body["data_version"] == DATA_VERSION

    async def test_the_date_picker_bounds_agree_with_the_run_endpoint(
        self, api: httpx.AsyncClient
    ) -> None:
        """The UI reads these two numbers to decide what a user may ask for.

        If ``/meta/status`` advertised a range the run endpoint refuses, every date picker in the
        product would offer dates that 422.
        """
        status = (await api.get(url("/meta/status"))).json()
        latest = dt.date.fromisoformat(status["as_of"])
        response = await api.post(
            url("/screens/exmpl0000001/run"), json={"as_of": latest.isoformat()}
        )
        assert response.status_code == 200
