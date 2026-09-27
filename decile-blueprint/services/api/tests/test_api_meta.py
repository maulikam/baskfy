"""Contract tests for ``/meta/*`` — docs/07 §Metadata (Prompt 7 acceptance criterion 1)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Final

import httpx
import pytest
from api_helpers import assert_problem, bearer, errors_of, make_user, url
from screener_helpers import AS_OF, DATA_VERSION, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.live_prices import LiveQuote
from baskfy_api.routers import meta
from baskfy_api.screener import DATA_START_DATE
from baskfy_core.factor_registry import COLUMN_PICKER_KEYS, FACTORS, SORT_FACTOR_KEYS
from baskfy_core.market_hours_cb import IST
from baskfy_core.models import PipelineRun, TradingDay
from baskfy_core.ranking_presets import PRESET_SPECS
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.universes import UNIVERSES

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]


class TestFactors:
    async def test_it_returns_the_whole_registry(self, api: httpx.AsyncClient) -> None:
        response = await api.get(url("/meta/factors"))
        assert response.status_code == 200
        body = response.json()
        assert [row["key"] for row in body] == list(SORT_FACTOR_KEYS)

    async def test_each_entry_carries_the_documented_fields(self, api: httpx.AsyncClient) -> None:
        """docs/07's five, plus ``preference`` and C1's four registry fields (PLAN.md C6)."""
        body = (await api.get(url("/meta/factors"))).json()
        expected = {
            "key",
            "label",
            "family",
            "unit",
            "higher_is_better",
            "preference",
            "rankable",
            "weight_family",
            "validation_status",
            "definition",
        }
        assert all(set(row) == expected for row in body)

    async def test_the_ranking_fields_come_from_the_registry(self, api: httpx.AsyncClient) -> None:
        """Every C1 field is the registry's own value — the API keeps no second copy."""
        body = {row["key"]: row for row in (await api.get(url("/meta/factors"))).json()}
        for key, row in body.items():
            factor = FACTORS[key]
            assert row["preference"] == factor.preference.value
            assert row["rankable"] is factor.rankable
            assert row["weight_family"] == factor.weight_family.value
            assert row["validation_status"] == factor.validation_status.value
            assert row["definition"] == factor.definition
        assert any(row["rankable"] is False for row in body.values()), (
            "C1: filter-only factors exist and must reach the editor as unrankable"
        )

    async def test_it_needs_no_principal(self, api: httpx.AsyncClient) -> None:
        """Reference data like the rest of the static catalogue: anonymous callers get it."""
        response = await api.get(url("/meta/factors"))
        assert response.status_code == 200

    async def test_it_never_exposes_the_sql_expression(self, api: httpx.AsyncClient) -> None:
        """The registry's ``sql_expr`` is a server-side whitelist, not a client-side value.

        Publishing it would hand a caller the exact column names to probe with, and gains the UI
        nothing: it sorts by *key*.
        """
        text = (await api.get(url("/meta/factors"))).text
        assert "sql_expr" not in text
        assert FACTORS["avg_sharpe_12_6_3_1"].sql_expr not in text


class TestRankingPresets:
    async def test_it_returns_every_core_preset_with_its_status(
        self, api: httpx.AsyncClient
    ) -> None:
        """docs/ranking/PLAN.md C6 / C7: served from ``baskfy_core.ranking_presets``."""
        response = await api.get(url("/meta/ranking-presets"))
        assert response.status_code == 200
        body = response.json()
        assert [row["key"] for row in body] == list(PRESET_SPECS)
        for row in body:
            spec = PRESET_SPECS[row["key"]]
            assert set(row) == {"key", "label", "description", "status", "sort_by", "patch"}
            assert row["label"] == spec.label
            assert row["status"] == spec.status
            assert row["description"] == spec.description
            assert row["patch"] == spec.patch
            assert row["sort_by"] == spec.patch["sort_by"]

    async def test_nse_momentum_is_offered_with_c7s_label(self, api: httpx.AsyncClient) -> None:
        """PLAN correction 8 / C7: the NSE label, with NSE's universe, once the score exists."""
        body = (await api.get(url("/meta/ranking-presets"))).json()
        row = next(r for r in body if r["key"] == "nse_momentum")
        assert row["label"] == "NIFTY200 Momentum 30 score (NSE methodology)"
        assert row["patch"]["index"] == "nifty-200"

    async def test_it_needs_no_principal(self, api: httpx.AsyncClient) -> None:
        """Same posture as ``/meta/factors``: static catalogue, no auth, no database."""
        response = await api.get(url("/meta/ranking-presets"))
        assert response.status_code == 200


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
        assert body["live_quotes"] is False

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


class TestMarketOpen:
    """14 Sep 2026, an NSE holiday: the pill read "market open" because only the clock was asked.

    ``market_open`` is the calendar AND the clock: today (IST) is a trading day in
    ``trading_day``, and IST time is inside 09:15-15:30.
    """

    @staticmethod
    async def _calendar(
        session: AsyncSession, day: dt.date, *, trading: bool, source: str, name: str | None = None
    ) -> None:
        await session.merge(
            TradingDay(
                exchange_id=NSE_EXCHANGE_ID,
                date=day,
                is_trading_day=trading,
                holiday_name=name,
                source=source,
            )
        )
        await session.flush()

    @staticmethod
    async def _status_at(
        api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, moment: dt.datetime
    ) -> dict[str, object]:
        monkeypatch.setattr(meta, "_now", lambda: moment)
        body: dict[str, object] = (await api.get(url("/meta/status"))).json()
        return body

    async def test_a_holiday_during_session_hours_is_not_open(
        self,
        api: httpx.AsyncClient,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        day = dt.date(2026, 9, 14)
        await self._calendar(screener_session, day, trading=False, source="holiday", name="Holiday")
        body = await self._status_at(api, monkeypatch, dt.datetime(2026, 9, 14, 11, 0, tzinfo=IST))
        assert body["session_day"] is False
        assert body["market_open"] is False

    async def test_a_trading_day_during_session_hours_is_open(
        self,
        api: httpx.AsyncClient,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        day = dt.date(2026, 9, 15)
        await self._calendar(screener_session, day, trading=True, source="derived")
        body = await self._status_at(api, monkeypatch, dt.datetime(2026, 9, 15, 11, 0, tzinfo=IST))
        assert body["session_day"] is True
        assert body["market_open"] is True

    async def test_a_trading_day_after_the_close_is_not_open(
        self,
        api: httpx.AsyncClient,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        day = dt.date(2026, 9, 15)
        await self._calendar(screener_session, day, trading=True, source="derived")
        body = await self._status_at(api, monkeypatch, dt.datetime(2026, 9, 15, 16, 0, tzinfo=IST))
        assert body["session_day"] is True
        assert body["market_open"] is False

    async def test_a_weekend_is_not_open(
        self,
        api: httpx.AsyncClient,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        day = dt.date(2026, 9, 12)
        await self._calendar(screener_session, day, trading=False, source="weekend")
        body = await self._status_at(api, monkeypatch, dt.datetime(2026, 9, 12, 11, 0, tzinfo=IST))
        assert body["session_day"] is False
        assert body["market_open"] is False

    async def test_a_weekend_the_calendar_does_not_carry_is_not_open(
        self, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = await self._status_at(api, monkeypatch, dt.datetime(2099, 1, 3, 11, 0, tzinfo=IST))
        assert body["market_open"] is False

    async def test_a_weekday_the_calendar_does_not_carry_is_a_session(
        self, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``is_session_day``'s rule: an unloaded calendar must not silence the marker."""
        body = await self._status_at(api, monkeypatch, dt.datetime(2099, 1, 5, 11, 0, tzinfo=IST))
        assert body["session_day"] is True
        assert body["market_open"] is True


class TestLiveMarks:
    async def test_anonymous_callers_are_refused(self, api: httpx.AsyncClient) -> None:
        """A Kite quote batch is not a public read — it spends the operator session."""
        response = await api.get(url("/meta/live-marks"), params={"symbols": "RELIANCE"})
        assert response.status_code == 401


class TestLiveMarksOverlay:
    """Maulik, 21 Sep 2026: "screens should have live data".

    The spec: while the NSE session is open and a real Kite session exists, each requested name
    gets its live last price, the exchange's previous close and today's % change; otherwise the
    response is empty and says why, so the page keeps the close and labels it. Either way
    ``as_of`` is the published session — the overlay never moves it.
    """

    OPEN: Final = dt.datetime(2099, 1, 5, 11, 0, tzinfo=IST)  # a weekday the calendar lacks
    CLOSED: Final = dt.datetime(2099, 1, 5, 16, 0, tzinfo=IST)

    @staticmethod
    async def _get(
        api: httpx.AsyncClient,
        session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        moment: dt.datetime,
        symbols: str = "RELIANCE,TCS",
    ) -> dict[str, object]:
        monkeypatch.setattr(meta, "_now", lambda: moment)
        _, public_id = await make_user(session, "live-marks@example.com")
        response = await api.get(
            url("/meta/live-marks"), params={"symbols": symbols}, headers=bearer(public_id)
        )
        assert response.status_code == 200, response.text
        body: dict[str, object] = response.json()
        return body

    @staticmethod
    def _no_kite(monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_symbols: object) -> dict[str, LiveQuote]:
            raise AssertionError("Kite must not be asked for a quote here")

        monkeypatch.setattr(meta, "live_quote_details", _boom)

    async def test_a_closed_market_shows_the_close_and_never_asks_kite(
        self,
        api: httpx.AsyncClient,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(meta, "quotes_permitted", lambda: True)
        self._no_kite(monkeypatch)
        body = await self._get(api, screener_session, monkeypatch, self.CLOSED)
        assert body["live"] is False
        assert body["reason"] == "market_closed"
        assert body["market_open"] is False
        assert body["quotes"] == {}
        assert body["marks"] == {}
        assert body["as_of"] == AS_OF.isoformat()

    async def test_no_kite_session_is_named_as_the_reason(
        self,
        api: httpx.AsyncClient,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(meta, "quotes_permitted", lambda: False)
        self._no_kite(monkeypatch)
        body = await self._get(api, screener_session, monkeypatch, self.OPEN)
        assert body["live"] is False
        assert body["reason"] == "no_session"
        assert body["market_open"] is True
        assert body["quotes"] == {}
        assert body["as_of"] == AS_OF.isoformat()

    async def test_an_open_market_with_a_session_overlays_price_and_change(
        self,
        api: httpx.AsyncClient,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(meta, "quotes_permitted", lambda: True)
        monkeypatch.setattr(
            meta,
            "live_quote_details",
            lambda _symbols: {
                "RELIANCE": LiveQuote(Decimal("1520.40"), Decimal("1500.00")),
                "TCS": LiveQuote(Decimal("3000"), None),
            },
        )
        body = await self._get(api, screener_session, monkeypatch, self.OPEN)
        assert body["live"] is True
        assert body["reason"] is None
        assert body["live_overlay"] is True
        quotes = body["quotes"]
        assert isinstance(quotes, dict)
        # Decimal strings on the wire (house rule 9), change rounded to 0.01 (house rule 8).
        # `as_of` and `stale` joined the quote with LV1 (27 Sep 2026, DECISIONS-LV LV1.1): a quote
        # with no exchange timestamp is `as_of: None` and is judged fresh at the moment served.
        assert quotes["RELIANCE"] == {
            "last_price": "1520.40",
            "prev_close": "1500.00",
            "change_pct": "1.36",
            "as_of": None,
            "stale": False,
        }
        assert quotes["TCS"]["change_pct"] is None
        assert body["marks"] == {"RELIANCE": "1520.40", "TCS": "3000"}

    async def test_the_overlay_never_moves_the_published_as_of(
        self,
        api: httpx.AsyncClient,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(meta, "quotes_permitted", lambda: True)
        monkeypatch.setattr(
            meta,
            "live_quote_details",
            lambda _symbols: {"RELIANCE": LiveQuote(Decimal("1"), Decimal("1"))},
        )
        body = await self._get(api, screener_session, monkeypatch, self.OPEN)
        status = (await api.get(url("/meta/status"))).json()
        assert body["live"] is True
        assert body["as_of"] == status["as_of"] == AS_OF.isoformat()

    async def test_a_session_that_answers_nothing_is_unavailable(
        self,
        api: httpx.AsyncClient,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(meta, "quotes_permitted", lambda: True)
        monkeypatch.setattr(meta, "live_quote_details", lambda _symbols: {})
        body = await self._get(api, screener_session, monkeypatch, self.OPEN)
        assert body["live"] is False
        assert body["reason"] == "unavailable"
        assert body["quotes"] == {}

    def test_the_overlay_is_a_read_and_adds_no_execute_route(self) -> None:
        for route in meta.router.routes:
            path = getattr(route, "path", "")
            assert "execute" not in path
            if path.endswith("/live-marks"):
                assert getattr(route, "methods", set()) == {"GET"}
