"""Contract tests for ``/portfolios`` — docs/07 §"Portfolios & rebalance" (Prompt 14).

The second acceptance criterion lives in :class:`TestMessyImport`: the file it uploads has the
extra columns, the whitespace, the lowercase symbols, the BSE-style code and the blank row that
PROMPTS.md names, and the assertions are about the *report* rather than about what survived.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import httpx
import pytest
import screener_helpers
from api_helpers import assert_problem, bearer, make_user, url
from screener_helpers import AS_OF, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import Instrument, SymbolAlias
from baskfy_core.portfolio_csv import SAMPLE_CSV
from baskfy_core.seed_data import EXAMPLE_SCREENS, NSE_EXCHANGE_ID

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]

#: One decoded JSON object. ``object`` rather than ``Any`` — CLAUDE.md house rule 3 — with
#: :func:`obj` and :func:`arr` doing the narrowing that an ``Any`` would have skipped.
Json = dict[str, object]

SCREEN_ID = EXAMPLE_SCREENS[0].public_id

#: The acceptance criterion's messy input, as a file.
MESSY_CSV = (
    "Symbol , Quantity ,Avg Price,Broker Note,ISIN\r\n"
    "  cupid , 100 , 284.56 ,bought the dip,INE509A01011\r\n"
    "\r\n"
    "hfcl,250,89.10,,\r\n"
    "532540,10,1000,a BSE scrip code,\r\n"
    "WELCORP , 40 , 912.40 , ,\r\n"
    "NOTALISTEDNAME,5,1,,\r\n"
)


def obj(value: object) -> Json:
    assert isinstance(value, dict), value
    return value


def arr(value: object) -> list[Json]:
    assert isinstance(value, list), value
    return [obj(item) for item in value]


def body_of(response: httpx.Response) -> Json:
    return obj(response.json())


def portfolio_of(body: Json) -> Json:
    """The ``portfolio`` member of a create / import / holdings response."""
    return obj(body["portfolio"])


def report_of(body: Json) -> Json:
    return obj(body["report"])


def holdings_of(body: Json) -> list[Json]:
    return arr(portfolio_of(body)["holdings"])


def symbols_of(rows: list[Json]) -> list[object]:
    return [row["symbol"] for row in rows]


def pid(body: Json) -> int:
    value = portfolio_of(body)["id"]
    assert isinstance(value, int)
    return value


def count(value: object) -> int:
    assert isinstance(value, int)
    return value


async def owner(session: AsyncSession, email: str = "port@example.com") -> dict[str, str]:
    _, public_id = await make_user(session, email)
    return bearer(public_id)


async def make_portfolio(
    api: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    name: str = "Core",
    holdings: list[Json] | None = None,
) -> Json:
    response = await api.post(
        url("/portfolios"),
        json={"name": name, "holdings": holdings or []},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return obj(response.json())


async def upload(
    api: httpx.AsyncClient, headers: dict[str, str], text: str, **params: str
) -> httpx.Response:
    return await api.post(
        url("/portfolios/import-csv"),
        files={"file": ("holdings.csv", text.encode("utf-8"), "text/csv")},
        params=params,
        headers=headers,
    )


async def ranked_symbols(api: httpx.AsyncClient, headers: dict[str, str]) -> list[str]:
    """The example screen's output in rank order — the answer the rebalance is measured against."""
    response = await api.post(url(f"/screens/{SCREEN_ID}/run"), json={}, headers=headers)
    assert response.status_code == 200, response.text
    return [str(row["symbol"]) for row in arr(body_of(response)["rows"])]


class TestSampleCsv:
    async def test_it_is_served_as_a_downloadable_file(self, api: httpx.AsyncClient) -> None:
        """docs/01 §8: "(sample CSV provided)"."""
        response = await api.get(url("/portfolios/sample-csv"))
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]
        assert response.text == SAMPLE_CSV

    async def test_the_sample_imports_cleanly_against_the_seeded_data(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """A sample that does not work is worse than no sample."""
        headers = await owner(screener_session, "sample@example.com")
        response = await upload(api, headers, SAMPLE_CSV)
        assert response.status_code == 201, response.text
        report = report_of(body_of(response))
        assert report["unmatched"] == 0
        assert report["ambiguous"] == 0
        assert report["matched"] == 5
        assert report["imported"] == 5


class TestCrud:
    async def test_it_requires_authentication(self, api: httpx.AsyncClient) -> None:
        assert_problem(await api.get(url("/portfolios")), 401, "unauthenticated")

    async def test_create_list_rename_delete(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await owner(screener_session, "crud@example.com")
        created = await make_portfolio(api, headers, holdings=[{"symbol": "CUPID"}])
        portfolio_id = portfolio_of(created)["id"]

        listing = body_of(await api.get(url("/portfolios"), headers=headers))
        rows = arr(listing["data"])
        assert [row["id"] for row in rows] == [portfolio_id]
        assert rows[0]["holdings_count"] == 1

        renamed = await api.patch(
            url(f"/portfolios/{portfolio_id}"), json={"name": "Renamed"}, headers=headers
        )
        assert renamed.status_code == 200
        assert body_of(renamed)["name"] == "Renamed"

        assert (
            await api.delete(url(f"/portfolios/{portfolio_id}"), headers=headers)
        ).status_code == 204
        assert_problem(
            await api.get(url(f"/portfolios/{portfolio_id}"), headers=headers), 404, "not-found"
        )

    async def test_one_user_never_sees_anothers_portfolio(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        alice = await owner(screener_session, "alice.p@example.com")
        bob = await owner(screener_session, "bob.p@example.com")
        created = await make_portfolio(api, alice, name="Alice only")
        assert_problem(
            await api.get(url(f"/portfolios/{portfolio_of(created)['id']}"), headers=bob),
            404,
            "not-found",
        )

    async def test_holdings_are_replaced_not_merged(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """``PUT`` means replacement; a merge leaves no way to sell a position."""
        headers = await owner(screener_session, "put@example.com")
        created = await make_portfolio(
            api, headers, holdings=[{"symbol": "CUPID"}, {"symbol": "HFCL"}]
        )
        portfolio_id = portfolio_of(created)["id"]
        response = await api.put(
            url(f"/portfolios/{portfolio_id}/holdings"),
            json={"holdings": [{"symbol": "HFCL", "quantity": "5"}]},
            headers=headers,
        )
        assert response.status_code == 200
        assert symbols_of(holdings_of(body_of(response))) == ["HFCL"]

    async def test_added_on_survives_a_replacement_that_keeps_a_name(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await owner(screener_session, "addedon@example.com")
        created = await make_portfolio(api, headers, holdings=[{"symbol": "CUPID"}])
        portfolio_id = portfolio_of(created)["id"]
        original = holdings_of(created)[0]["added_on"]

        await api.put(
            url(f"/portfolios/{portfolio_id}/holdings"),
            json={"holdings": [{"symbol": "CUPID", "quantity": "9"}]},
            headers=headers,
        )
        fetched = body_of(await api.get(url(f"/portfolios/{portfolio_id}"), headers=headers))
        held = arr(fetched["holdings"])[0]
        assert held["added_on"] == original
        assert held["quantity"] == "9.0000"

    async def test_quantities_are_exact_decimals_not_floats(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """CLAUDE.md house rule 9: money and quantities are `numeric`, never float."""
        headers = await owner(screener_session, "decimal@example.com")
        created = await make_portfolio(
            api, headers, holdings=[{"symbol": "CUPID", "avg_price": "284.56"}]
        )
        assert holdings_of(created)[0]["avg_price"] == "284.5600"

    async def test_an_idempotency_key_does_not_create_two_portfolios(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/07 §Conventions: honoured on all POSTs that create resources."""
        headers = {**await owner(screener_session, "idem@example.com"), "Idempotency-Key": "abc-1"}
        first = await api.post(url("/portfolios"), json={"name": "Once"}, headers=headers)
        second = await api.post(url("/portfolios"), json={"name": "Once"}, headers=headers)
        assert first.status_code == 201
        assert second.json()["portfolio"]["id"] == first.json()["portfolio"]["id"]
        listing = (await api.get(url("/portfolios"), headers=headers)).json()
        assert len(listing["data"]) == 1


class TestMessyImport:
    """PROMPTS.md Prompt 14 acceptance criterion 2, end to end."""

    @pytest.fixture
    async def report(self, api: httpx.AsyncClient, screener_session: AsyncSession) -> Json:
        headers = await owner(screener_session, "messy@example.com")
        response = await upload(api, headers, MESSY_CSV, name="Messy")
        assert response.status_code == 201, response.text
        return obj(obj(response.json())["report"])

    def row(self, report: Json, symbol: str) -> Json:
        return next(row for row in arr(report["rows"]) if row["symbol"] == symbol)

    async def test_lowercase_and_whitespace_resolve(self, report: Json) -> None:
        cupid = self.row(report, "CUPID")
        assert cupid["status"] == "matched"
        assert cupid["raw_symbol"] == "cupid"
        assert cupid["quantity"] == "100"

    async def test_extra_columns_are_reported_as_ignored(self, report: Json) -> None:
        assert report["ignored_columns"] == ["Broker Note", "ISIN"]

    async def test_the_blank_row_is_reported_not_dropped(self, report: Json) -> None:
        assert [row["reason"] for row in arr(report["skipped_rows"])] == ["blank"]

    async def test_the_bse_code_is_unmatched_with_that_reason(self, report: Json) -> None:
        """Guessing an NSE symbol from a BSE scrip code would be worse than saying so."""
        assert self.row(report, "532540")["reason"] == "bse_code"

    async def test_an_unknown_symbol_says_so(self, report: Json) -> None:
        assert self.row(report, "NOTALISTEDNAME")["reason"] == "unknown_symbol"

    async def test_the_counts_add_up_to_the_file(self, report: Json) -> None:
        assert report["matched"] == 3
        assert report["unmatched"] == 2
        assert report["ambiguous"] == 0
        assert report["skipped"] == 1
        assert report["imported"] == 3
        assert report["total_lines"] == report["matched"] + report["unmatched"] + report["skipped"]

    async def test_only_the_matched_rows_became_holdings(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await owner(screener_session, "messy2@example.com")
        response = await upload(api, headers, MESSY_CSV)
        holdings = response.json()["portfolio"]["holdings"]
        assert {row["symbol"] for row in holdings} == {"CUPID", "HFCL", "WELCORP"}


class TestAmbiguity:
    async def test_a_symbol_on_two_series_is_ambiguous_not_guessed(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """``instrument`` is unique on (exchange, symbol, series), so this is a real state."""
        for series in ("EQ", "BE"):
            screener_session.add(
                Instrument(
                    exchange_id=NSE_EXCHANGE_ID,
                    symbol="TWINCO",
                    name=f"TWIN COMPANY {series}",
                    series=series,
                    instrument_type="EQ",
                    is_active=True,
                )
            )
        await screener_session.flush()

        headers = await owner(screener_session, "ambig@example.com")
        created = await make_portfolio(api, headers, holdings=[{"symbol": "TWINCO"}])
        report = report_of(created)
        assert report["ambiguous"] == 1
        assert report["imported"] == 0
        row = arr(report["rows"])[0]
        assert row["status"] == "ambiguous"
        candidates = arr(row["candidates"])
        assert sorted(str(candidate["series"]) for candidate in candidates) == ["BE", "EQ"]

    async def test_a_renamed_symbol_resolves_through_its_alias(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/04's ``symbol_alias``: "NSE renames symbols; keep the history mapped"."""
        instrument_id = await screener_helpers.add_instrument(screener_session, "NEWNAME")
        screener_session.add(
            SymbolAlias(
                instrument_id=instrument_id, old_symbol="OLDNAME", changed_on=dt.date(2026, 1, 1)
            )
        )
        await screener_session.flush()

        headers = await owner(screener_session, "alias@example.com")
        created = await make_portfolio(api, headers, holdings=[{"symbol": "OLDNAME"}])
        row = arr(report_of(created)["rows"])[0]
        assert row["status"] == "matched"
        assert row["matched_via_alias"] is True
        assert holdings_of(created)[0]["symbol"] == "NEWNAME"


class TestRebalance:
    async def test_the_three_lists_follow_the_buffer_rule(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The rule, against real seeded ranks rather than a fixture of them."""
        headers = await owner(screener_session, "reb@example.com")
        ranked = await ranked_symbols(api, headers)
        top_n, buffer = 5, 3
        # rank 1 (hold), rank top_n+buffer (inside band, hold), rank top_n+buffer+1 (exit)
        held = [ranked[0], ranked[top_n + buffer - 1], ranked[top_n + buffer]]
        created = await make_portfolio(
            api, headers, holdings=[{"symbol": symbol} for symbol in held]
        )
        portfolio_id = portfolio_of(created)["id"]

        response = await api.post(
            url(f"/portfolios/{portfolio_id}/rebalance"),
            json={"screen_public_id": SCREEN_ID, "top_n": top_n, "hold_buffer": buffer},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = body_of(response)

        assert symbols_of(arr(body["holds"])) == [ranked[0]]
        assert symbols_of(arr(body["inside_wrh"])) == [ranked[top_n + buffer - 1]]
        assert symbols_of(arr(body["exits"])) == [ranked[top_n + buffer]]
        assert arr(body["exits"])[0]["reason"] == "rank_outside_buffer"
        assert symbols_of(arr(body["entries"])) == ranked[1:top_n]
        assert body["as_of"] == AS_OF.isoformat()
        assert count(body["data_version"]) >= 1

    async def test_target_weights_are_exact_and_sum_to_one(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await owner(screener_session, "weights@example.com")
        created = await make_portfolio(api, headers, holdings=[])
        response = await api.post(
            url(f"/portfolios/{pid(created)}/rebalance"),
            json={"screen_public_id": SCREEN_ID, "top_n": 7, "hold_buffer": 3},
            headers=headers,
        )
        weights = arr(body_of(response)["target_weights"])
        assert len(weights) == 7
        assert sum(Decimal(str(row["weight"])) for row in weights) == Decimal(1)
        assert all(row["action"] == "enter" for row in weights)

    async def test_a_holding_outside_the_screens_universe_exits_as_not_in_screen(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The acceptance criterion's fourth case: a symbol not in our universe.

        An instrument we know about, which this screen never returns.
        """
        await screener_helpers.add_instrument(screener_session, "OFFUNIVERSE")
        headers = await owner(screener_session, "offuni@example.com")
        created = await make_portfolio(api, headers, holdings=[{"symbol": "OFFUNIVERSE"}])
        response = await api.post(
            url(f"/portfolios/{pid(created)}/rebalance"),
            json={"screen_public_id": SCREEN_ID, "top_n": 5, "hold_buffer": 3},
            headers=headers,
        )
        exits = arr(body_of(response)["exits"])
        assert symbols_of(exits) == ["OFFUNIVERSE"]
        assert exits[0]["reason"] == "not_in_screen"
        assert exits[0]["rank"] is None

    async def test_a_delisted_holding_exits_with_that_reason(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await owner(screener_session, "delisted@example.com")
        ranked = await ranked_symbols(api, headers)
        created = await make_portfolio(api, headers, holdings=[{"symbol": ranked[0]}])

        instrument_id = holdings_of(created)[0]["instrument_id"]
        instrument = await screener_session.get(Instrument, instrument_id)
        assert instrument is not None
        instrument.delisted_on = dt.date(2026, 8, 17)
        await screener_session.flush()

        response = await api.post(
            url(f"/portfolios/{pid(created)}/rebalance"),
            json={"screen_public_id": SCREEN_ID, "top_n": 5, "hold_buffer": 3},
            headers=headers,
        )
        body = body_of(response)
        assert arr(body["exits"])[0]["reason"] == "delisted"
        assert body["delisted_count"] == 1

    async def test_an_unknown_screen_is_a_404(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await owner(screener_session, "noscreen@example.com")
        created = await make_portfolio(api, headers)
        assert_problem(
            await api.post(
                url(f"/portfolios/{pid(created)}/rebalance"),
                json={"screen_public_id": "nope", "top_n": 5, "hold_buffer": 0},
                headers=headers,
            ),
            404,
            "not-found",
        )

    async def test_a_stale_data_version_is_a_409(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/07: "409 stale-data-version — client sent a data_version that no longer exists"."""
        headers = await owner(screener_session, "stale@example.com")
        created = await make_portfolio(api, headers)
        assert_problem(
            await api.post(
                url(f"/portfolios/{pid(created)}/rebalance"),
                json={
                    "screen_public_id": SCREEN_ID,
                    "top_n": 5,
                    "hold_buffer": 0,
                    "data_version": 999_999,
                },
                headers=headers,
            ),
            409,
            "stale-data-version",
        )

    @pytest.mark.parametrize(
        "body",
        [
            {"screen_public_id": SCREEN_ID, "top_n": 0, "hold_buffer": 0},
            {"screen_public_id": SCREEN_ID, "top_n": 5, "hold_buffer": -1},
        ],
    )
    async def test_the_rule_inputs_are_validated(
        self, api: httpx.AsyncClient, screener_session: AsyncSession, body: Json
    ) -> None:
        headers = await owner(screener_session, "bounds@example.com")
        created = await make_portfolio(api, headers)
        response = await api.post(
            url(f"/portfolios/{pid(created)}/rebalance"), json=body, headers=headers
        )
        assert response.status_code == 400


class TestHistory:
    async def test_each_rebalance_is_persisted_and_readable(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """Prompt 14 §4: "so a user can see what they were told and when"."""
        headers = await owner(screener_session, "hist@example.com")
        ranked = await ranked_symbols(api, headers)
        created = await make_portfolio(api, headers, holdings=[{"symbol": ranked[0]}])
        portfolio_id = pid(created)

        first = await api.post(
            url(f"/portfolios/{portfolio_id}/rebalance"),
            json={"screen_public_id": SCREEN_ID, "top_n": 5, "hold_buffer": 3},
            headers=headers,
        )
        assert first.status_code == 200

        history = body_of(
            await api.get(url(f"/portfolios/{portfolio_id}/rebalances"), headers=headers)
        )
        summaries = arr(history["data"])
        assert len(summaries) == 1
        summary = summaries[0]
        assert summary["top_n"] == 5
        assert summary["screen_public_id"] == SCREEN_ID
        assert summary["entries"] == len(arr(body_of(first)["entries"]))

        stored = await api.get(
            url(f"/portfolios/{portfolio_id}/rebalances/{summary['id']}"), headers=headers
        )
        assert stored.status_code == 200
        assert stored.json() == first.json()

    async def test_the_record_survives_the_screen_being_deleted(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """A record of advice given must outlive the screen that gave it."""
        headers = await owner(screener_session, "histdel@example.com")
        mine = await api.post(
            url("/screens"),
            json={"name": "Temporary", "definition": {"index": "nifty-500", "sort_by": "ret_12m"}},
            headers=headers,
        )
        screen_id = body_of(mine)["public_id"]
        created = await make_portfolio(api, headers)
        portfolio_id = pid(created)
        rebalanced = await api.post(
            url(f"/portfolios/{portfolio_id}/rebalance"),
            json={"screen_public_id": screen_id, "top_n": 5, "hold_buffer": 3},
            headers=headers,
        )
        assert rebalanced.status_code == 200

        assert (await api.delete(url(f"/screens/{screen_id}"), headers=headers)).status_code == 204

        history = body_of(
            await api.get(url(f"/portfolios/{portfolio_id}/rebalances"), headers=headers)
        )
        summaries = arr(history["data"])
        assert len(summaries) == 1
        assert summaries[0]["screen_name"] == "Temporary"
