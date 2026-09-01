"""Contract tests for the market-data surfaces — docs/01 §6 and §7, docs/07 (Prompt 11).

Two of Prompt 11's three acceptance criteria are here:

* **breadth is verified against a hand-computed fixture** — `TestBreadthArithmetic` recomputes all
  four gauges for all twelve universes directly from
  `tests/fixtures/reference-screen-export-2026-08-18.csv`, in Python, with no SQL and no shared
  code, and compares against what the API serves;
* **listings pagination is stable under a cursor walk** — `TestCursorStability` pages the whole
  register and asserts no duplicate and no skip.

The third (the dashboard rendering 145 rows without jank) is a browser concern and lives in
`apps/web/e2e/dashboard.spec.ts`.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Final

import httpx
import pytest
from api_helpers import assert_problem, url
from screener_helpers import AS_OF, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.breadth import ATH_PROXIMITY_PCT
from baskfy_core.models import Instrument
from baskfy_core.reference_export import to_rows
from baskfy_core.universes import (
    DASHBOARD_UNIVERSES,
    MARKET_HEALTH_SLUGS,
    UNIVERSE_BY_SLUG,
)

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]

JsonMap = dict[str, object]


def body_of(response: httpx.Response) -> JsonMap:
    """Parse with ``Decimal``: the API serialises through ``canonical_json`` (house rule 8)."""
    assert response.status_code == 200, response.text
    parsed: JsonMap = json.loads(response.text, parse_float=Decimal)
    return parsed


def as_rows(value: object) -> list[JsonMap]:
    assert isinstance(value, list), value
    for item in value:
        assert isinstance(item, dict), item
    return value


def as_map(value: object) -> JsonMap:
    assert isinstance(value, dict), value
    return value


def number(value: object) -> Decimal:
    assert isinstance(value, (Decimal, int, str)), value
    return Decimal(value)


# ---------------------------------------------------------------------------
# The hand-computed breadth fixture (acceptance criterion 1)
# ---------------------------------------------------------------------------


def hand_computed_breadth(slug: str) -> dict[str, Decimal | int | None]:
    """docs/01 §6's four gauges for one universe, computed here from the committed CSV.

    Deliberately naive: a list comprehension per gauge over the export's own rows, with no SQL,
    no `baskfy_core.breadth`, and no rounding cleverness. If this agreed with the API because both
    called the same function the check would be worthless — the point is that two independent
    readings of docs/01 §6 land on the same numbers.

    docs/01 §6's definitions, in order:
        Above 200 DMA       close > ma_200
        Above 50 DMA        close > ma_50
        Within 10% of ATH   abs(away_high_ath) <= 10
        1Y Return > 0%      ret_12m > 0
    """
    data = to_rows()
    members = {m.symbol for m in data.memberships if m.universe_slug == slug}
    rows = [row for row in data.factors if str(row["symbol"]) in members]

    def share(hits: int, considered: int) -> Decimal | None:
        if considered == 0:
            return None
        return round(Decimal(hits) * 100 / Decimal(considered), 4)

    def gauge(predicate: str) -> Decimal | None:
        considered = 0
        hits = 0
        for row in rows:
            close = row["close"]
            if predicate == "pct_above_200dma":
                left, right = close, row["ma_200"]
            elif predicate == "pct_above_50dma":
                left, right = close, row["ma_50"]
            elif predicate == "pct_within_10pct_ath":
                away = row["away_high_ath"]
                if away is None:
                    continue
                considered += 1
                assert isinstance(away, Decimal)
                hits += abs(away) <= ATH_PROXIMITY_PCT
                continue
            else:
                value = row["ret_12m"]
                if value is None:
                    continue
                considered += 1
                assert isinstance(value, Decimal)
                hits += value > 0
                continue
            if right is None:
                continue
            considered += 1
            assert isinstance(left, Decimal) and isinstance(right, Decimal)
            hits += left > right
        return share(hits, considered)

    return {
        "pct_above_200dma": gauge("pct_above_200dma"),
        "pct_above_50dma": gauge("pct_above_50dma"),
        "pct_within_10pct_ath": gauge("pct_within_10pct_ath"),
        "pct_ret_1y_positive": gauge("pct_ret_1y_positive"),
        "constituent_count": len(rows),
    }


#: What the fixture yields for NIFTY 50, pinned so a change in the CSV or the arithmetic is a
#: failing test rather than two agreeing recomputations.
NIFTY_50_EXPECTED: Final[dict[str, str | int]] = {
    "pct_above_200dma": "83.3333",
    "pct_above_50dma": "72.2222",
    "pct_within_10pct_ath": "61.1111",
    "pct_ret_1y_positive": "100.0000",
    "constituent_count": 18,
}


class TestBreadthArithmetic:
    """Acceptance criterion 1."""

    def test_the_fixture_reproduces_its_own_pinned_values(self) -> None:
        """The hand computation is pinned, so it cannot drift alongside the code it checks."""
        computed = hand_computed_breadth("nifty-50")
        for key, expected in NIFTY_50_EXPECTED.items():
            wanted = expected if key == "constituent_count" else Decimal(str(expected))
            assert computed[key] == wanted, key

    @pytest.mark.parametrize("slug", MARKET_HEALTH_SLUGS)
    async def test_every_gauge_matches_the_hand_computation(
        self, api: httpx.AsyncClient, slug: str
    ) -> None:
        expected = hand_computed_breadth(slug)
        body = body_of(await api.get(url("/market-health"), params={"universe": slug}))

        assert body["constituent_count"] == expected["constituent_count"], slug
        served = {str(gauge["key"]): gauge["value"] for gauge in as_rows(body["gauges"])}
        for key in (
            "pct_above_200dma",
            "pct_above_50dma",
            "pct_within_10pct_ath",
            "pct_ret_1y_positive",
        ):
            wanted = expected[key]
            if wanted is None:
                assert served[key] is None, (slug, key)
            else:
                assert number(served[key]) == wanted, (slug, key)

    async def test_the_export_is_a_momentum_screen_so_every_return_is_positive(
        self, api: httpx.AsyncClient
    ) -> None:
        """A property of the seeded dataset, asserted so nobody reads 100% as a bug.

        `docs/13`'s export is the *output of a momentum screen*, so every row in it has a positive
        1-year return by construction. On real market data this gauge would not be 100%.
        """
        body = body_of(await api.get(url("/market-health"), params={"universe": "nifty-500"}))
        served = {str(g["key"]): g["value"] for g in as_rows(body["gauges"])}
        assert number(served["pct_ret_1y_positive"]) == Decimal("100.0000")


class TestMarketHealth:
    async def test_it_names_the_four_gauges_in_the_documented_order(
        self, api: httpx.AsyncClient
    ) -> None:
        """docs/01 §6, verbatim wording and order."""
        body = body_of(await api.get(url("/market-health"), params={"universe": "nifty-500"}))
        assert [gauge["label"] for gauge in as_rows(body["gauges"])] == [
            "Above 200 DMA",
            "Above 50 DMA",
            "Within 10% of ATH",
            "1Y Return > 0%",
        ]

    async def test_it_carries_as_of_and_data_version(self, api: httpx.AsyncClient) -> None:
        """docs/07 §Conventions."""
        body = body_of(await api.get(url("/market-health")))
        assert body["as_of"] == AS_OF.isoformat()
        assert number(body["data_version"]) >= 1

    async def test_data_available_from_is_the_earliest_stored_row(
        self, api: httpx.AsyncClient
    ) -> None:
        """docs/01 §6's "Data available from 1st Nov 2024", read rather than hard-coded."""
        body = body_of(await api.get(url("/market-health"), params={"universe": "nifty-50"}))
        assert body["data_available_from"] == AS_OF.isoformat()

    async def test_it_names_the_universe_it_answered_for(self, api: httpx.AsyncClient) -> None:
        body = body_of(
            await api.get(url("/market-health"), params={"universe": "nifty-microcap-250"})
        )
        assert as_map(body["universe"])["name"] == "NIFTY MICROCAP 250"

    async def test_the_two_non_breadth_universes_are_refused(self, api: httpx.AsyncClient) -> None:
        """docs/01 §6's selector lists twelve; `nifty-fno` and `etf` are not among them."""
        for slug in ("nifty-fno", "etf"):
            assert_problem(
                await api.get(url("/market-health"), params={"universe": slug}),
                400,
                "invalid-screen-definition",
            )

    async def test_an_unknown_universe_is_refused(self, api: httpx.AsyncClient) -> None:
        assert_problem(
            await api.get(url("/market-health"), params={"universe": "nifty-nope"}),
            400,
            "invalid-screen-definition",
        )


class TestMarketHealthHistory:
    async def test_it_returns_the_four_series_in_date_order(self, api: httpx.AsyncClient) -> None:
        body = body_of(
            await api.get(url("/market-health/history"), params={"universe": "nifty-50"})
        )
        points = as_rows(body["points"])
        assert points, "the seeded dataset has one breadth date"
        dates = [str(point["date"]) for point in points]
        assert dates == sorted(dates)
        for key in (
            "pct_above_200dma",
            "pct_above_50dma",
            "pct_within_10pct_ath",
            "pct_ret_1y_positive",
        ):
            assert key in points[0]

    async def test_each_point_carries_the_universes_own_index_level(
        self, api: httpx.AsyncClient
    ) -> None:
        """docs/08 §"Market Health": the breadth chart has the index level overlaid."""
        body = body_of(
            await api.get(url("/market-health/history"), params={"universe": "nifty-50"})
        )
        point = as_rows(body["points"])[-1]
        assert point["index_level"] is not None
        assert number(point["index_level"]) > 0

    async def test_the_window_defaults_to_a_year_and_is_reported(
        self, api: httpx.AsyncClient
    ) -> None:
        body = body_of(await api.get(url("/market-health/history")))
        assert body["to"] == AS_OF.isoformat()
        assert body["from"] == (AS_OF - dt.timedelta(days=365)).isoformat()

    async def test_a_reversed_range_is_a_400(self, api: httpx.AsyncClient) -> None:
        assert_problem(
            await api.get(
                url("/market-health/history"),
                params={"from": "2026-08-18", "to": "2026-01-01"},
            ),
            400,
            "invalid-screen-definition",
        )


class TestIndexDashboard:
    async def test_it_serves_every_index_with_a_snapshot(self, api: httpx.AsyncClient) -> None:
        """docs/01 §7 — "~145 index rows". The fixture carries 117; see `docs/11a` §1."""
        body = body_of(await api.get(url("/indices/dashboard")))
        rows = as_rows(body["data"])
        assert len(rows) == 117
        assert len({row["slug"] for row in rows}) == len(rows)

    async def test_it_is_sorted_by_percent_change_descending(self, api: httpx.AsyncClient) -> None:
        """docs/01 §7: "Sorted by % change descending"."""
        rows = as_rows(body_of(await api.get(url("/indices/dashboard")))["data"])
        changes = [number(row["change_pct"]) for row in rows if row["change_pct"] is not None]
        assert changes == sorted(changes, reverse=True)

    async def test_every_universe_with_a_published_level_is_flagged_as_such(
        self, api: httpx.AsyncClient
    ) -> None:
        """This compared against every universe, which held while all of them were indices.

        M59 added `nse-sme-emerge`, a trading PLATFORM rather than an index: it has members and
        constituents, but NSE computes no daily level for it, so it has no `index_snapshot_daily`
        row and cannot appear on a dashboard built from that table. Its absence is correct, and
        `Universe.has_index_level` is where the catalog now says so.
        """
        rows = as_rows(body_of(await api.get(url("/indices/dashboard")))["data"])
        universes = {str(row["slug"]) for row in rows if row["is_universe"]}
        assert universes == {u.slug for u in DASHBOARD_UNIVERSES}

    async def test_an_index_with_no_fundamentals_serves_null_not_zero(
        self, api: httpx.AsyncClient
    ) -> None:
        """docs/01 §7: "`India VIX` → PE/PB/DivYield shown as `-`". NULL is what `-` renders."""
        rows = as_rows(body_of(await api.get(url("/indices/dashboard")))["data"])
        vix = next(row for row in rows if row["slug"] == "india-vix")
        assert vix["pe"] is None
        assert vix["pb"] is None
        assert vix["div_yield"] is None

    async def test_every_row_carries_a_thirty_day_sparkline(self, api: httpx.AsyncClient) -> None:
        """docs/08 §Dashboard: "a sparkline per index (30-day)", served with the page."""
        rows = as_rows(body_of(await api.get(url("/indices/dashboard")))["data"])
        for row in rows:
            series = row["sparkline"]
            assert isinstance(series, list)
            assert len(series) == 30, row["slug"]

    async def test_it_carries_as_of_and_data_version(self, api: httpx.AsyncClient) -> None:
        body = body_of(await api.get(url("/indices/dashboard")))
        assert body["as_of"] == AS_OF.isoformat()
        assert number(body["data_version"]) >= 1


class TestListings:
    async def test_it_is_newest_first(self, api: httpx.AsyncClient) -> None:
        """docs/07: "NSE listings, newest first"."""
        rows = as_rows(body_of(await api.get(url("/listings"), params={"limit": 50}))["data"])
        dated = [str(row["listed_on"]) for row in rows if row["listed_on"] is not None]
        assert dated == sorted(dated, reverse=True)

    async def test_it_pages_at_the_documented_size(self, api: httpx.AsyncClient) -> None:
        """docs/07 §Conventions: `?limit=100&cursor=…` -> `{data, next_cursor}`."""
        body = body_of(await api.get(url("/listings")))
        assert len(as_rows(body["data"])) == 100
        assert body["next_cursor"]

    async def test_the_series_filter_narrows_the_register(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        screener_session.add(
            Instrument(
                exchange_id=1,
                symbol="BEONE",
                name="BE SERIES ONE LIMITED",
                series="BE",
                instrument_type="EQ",
                is_active=True,
                listed_on=dt.date(2026, 8, 1),
            )
        )
        await screener_session.flush()

        rows = as_rows(body_of(await api.get(url("/listings"), params={"series": "BE"}))["data"])
        # The reference export already contains BE-series instruments, so the assertion is that
        # the filter is exclusive, not that the register is empty but for the row just added.
        assert {row["series"] for row in rows} == {"BE"}
        assert "BEONE" in {row["symbol"] for row in rows}

    async def test_search_matches_symbol_or_name(self, api: httpx.AsyncClient) -> None:
        rows = as_rows(body_of(await api.get(url("/listings"), params={"search": "CUPID"}))["data"])
        assert [row["symbol"] for row in rows] == ["CUPID"]

    async def test_a_forged_cursor_is_a_400(self, api: httpx.AsyncClient) -> None:
        assert_problem(
            await api.get(url("/listings"), params={"cursor": "not-a-cursor"}),
            400,
            "invalid-screen-definition",
        )


class TestCursorStability:
    """Acceptance criterion 3: no duplicates, no skips, across the whole register."""

    async def _walk(self, api: httpx.AsyncClient, limit: int) -> list[str]:
        symbols: list[str] = []
        cursor: str | None = None
        for _ in range(50):  # a bound, so a broken cursor loops finitely rather than forever
            params: dict[str, str | int] = {"limit": limit}
            if cursor is not None:
                params["cursor"] = cursor
            body = body_of(await api.get(url("/listings"), params=params))
            symbols.extend(str(row["symbol"]) for row in as_rows(body["data"]))
            next_cursor = body["next_cursor"]
            if next_cursor is None:
                return symbols
            assert isinstance(next_cursor, str)
            cursor = next_cursor
        raise AssertionError("the cursor never terminated")

    async def test_a_full_walk_visits_every_row_exactly_once(self, api: httpx.AsyncClient) -> None:
        walked = await self._walk(api, limit=25)
        assert len(walked) == len(set(walked)), "a symbol appeared on two pages"

        total = len({str(row["symbol"]) for row in to_rows().factors})
        assert len(walked) >= total, "the walk skipped rows the register contains"

    async def test_the_page_size_does_not_change_the_set_of_rows(
        self, api: httpx.AsyncClient
    ) -> None:
        """The strongest statement of "no duplicates, no skips": the boundary is not the cause.

        The 271 seeded instruments all share a NULL listing date and the 40 fixture instruments
        all share one, so almost every page boundary falls *inside* a tie. A cursor that used only
        the date would repeat or skip here; one that uses the whole sort key cannot.
        """
        assert await self._walk(api, limit=25) == await self._walk(api, limit=7)

    async def test_the_walk_is_in_the_same_order_as_a_single_page(
        self, api: httpx.AsyncClient
    ) -> None:
        walked = await self._walk(api, limit=13)
        single = as_rows(body_of(await api.get(url("/listings"), params={"limit": 500}))["data"])
        assert walked[: len(single)] == [str(row["symbol"]) for row in single]
