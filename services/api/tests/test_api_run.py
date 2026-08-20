"""Contract tests for running a screen — docs/07 §"Running a screen" (Prompt 7).

Carries two of Prompt 7's named acceptance criteria:

* "A test asserts /screens/{id}/run with a historical_date in the future returns 422
  no-trading-day."
* "A test asserts a stale data_version request returns 409."
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal

import httpx
import pytest
from api_helpers import (
    analytics_envelope,
    assert_problem,
    bearer,
    errors_of,
    make_user,
    url,
)
from screener_helpers import AS_OF, DATA_VERSION, export_symbols_in_file_order, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from decile_core.reference_export import EXPORT_COLUMNS
from decile_core.screener import DEFAULT_RESULT_COLUMNS, IDENTITY_COLUMNS
from decile_core.seed_data import EXAMPLE_SCREENS

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]

EXAMPLE_ID = EXAMPLE_SCREENS[0].public_id
MINIMAL = {"index": "nifty-total-market", "sort_by": "avg_sharpe_12_6_3_1", "series": ["EQ", "BE"]}


async def subscriber(session: AsyncSession, email: str = "paid@example.com") -> dict[str, str]:
    _, public_id = await make_user(session, email, subscribed=True)
    return bearer(public_id)


async def free_user(session: AsyncSession, email: str = "free@example.com") -> dict[str, str]:
    _, public_id = await make_user(session, email)
    return bearer(public_id)


class TestRunningASavedScreen:
    async def test_it_returns_the_documented_envelope(self, api: httpx.AsyncClient) -> None:
        """docs/07 §"Running a screen" — field for field."""
        response = await api.post(url(f"/screens/{EXAMPLE_ID}/run"), json={})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        body = response.json()
        assert set(body) == {
            "as_of",
            "data_version",
            "result_count",
            "sorting_factor",
            "columns",
            "rows",
        }
        analytics_envelope(body)
        assert body["as_of"] == AS_OF.isoformat()
        assert body["data_version"] == DATA_VERSION
        assert body["sorting_factor"] == {
            "key": "avg_sharpe_12_6_3_1",
            "label": "AVERAGE SHARPE RETURN 12 6 3 1 MONTHS",
        }
        assert body["columns"][:3] == list(IDENTITY_COLUMNS)
        assert set(DEFAULT_RESULT_COLUMNS) <= set(body["columns"])

    async def test_it_reproduces_the_reference_export(self, api: httpx.AsyncClient) -> None:
        """The docs/13 answer key, delivered over HTTP."""
        body = (await api.post(url(f"/screens/{EXAMPLE_ID}/run"), json={})).json()
        assert body["result_count"] == 271
        assert {row["symbol"] for row in body["rows"]} == set(export_symbols_in_file_order())
        assert [row["rank"] for row in body["rows"]] == list(range(1, 272))

    async def test_stored_precision_survives_the_wire(self, api: httpx.AsyncClient) -> None:
        """CLAUDE.md house rule 8: "the API, the UI and the CSV export can never disagree".

        The payload is the bytes ``ScreenResult.to_json`` produced, so a value stored at two
        decimal places arrives with two decimal places. Re-serialising through ``float`` would
        turn ``13.00`` into ``13.0`` and quietly break the contract.
        """
        text = (await api.post(url(f"/screens/{EXAMPLE_ID}/run"), json={})).text
        body = (await api.post(url(f"/screens/{EXAMPLE_ID}/run"), json={})).json()
        cupid = next(row for row in body["rows"] if row["symbol"] == "CUPID")
        rendered = f'"ret_12m":{cupid["ret_12m"]}'
        assert rendered in text
        assert Decimal(str(cupid["ret_12m"])).as_tuple().exponent == -2

    async def test_an_override_definition_is_honoured(self, api: httpx.AsyncClient) -> None:
        """docs/07: `{ "as_of": …, "override_definition": {…} | null }`."""
        body = (
            await api.post(
                url(f"/screens/{EXAMPLE_ID}/run"),
                json={"override_definition": {**MINIMAL, "sort_by": "ret_12m"}},
            )
        ).json()
        assert body["sorting_factor"]["key"] == "ret_12m"

    async def test_a_malformed_override_is_a_400(self, api: httpx.AsyncClient) -> None:
        response = await api.post(
            url(f"/screens/{EXAMPLE_ID}/run"),
            json={"override_definition": {"index": "nifty-500", "sort_by": "drop table"}},
        )
        assert_problem(response, 400, "invalid-screen-definition")

    async def test_an_unknown_screen_is_404(self, api: httpx.AsyncClient) -> None:
        assert_problem(await api.post(url("/screens/nope/run"), json={}), 404, "not-found")

    async def test_the_run_is_recorded_for_the_audit_trail(self, api: httpx.AsyncClient) -> None:
        """docs/04 calls ``screen_run`` "audit + historical ranks cache"."""
        await api.post(url(f"/screens/{EXAMPLE_ID}/run"), json={})
        body = (await api.get(url(f"/screens/{EXAMPLE_ID}/runs"))).json()
        assert len(body["data"]) == 1
        entry = body["data"][0]
        assert entry["as_of"] == AS_OF.isoformat()
        assert entry["result_count"] == 271
        assert len(entry["definition_hash"]) == 64


class TestTheFutureDateCriterion:
    """Prompt 7: "run with a historical_date in the future returns 422 no-trading-day"."""

    async def test_a_future_as_of_is_422_no_trading_day(self, api: httpx.AsyncClient) -> None:
        response = await api.post(url(f"/screens/{EXAMPLE_ID}/run"), json={"as_of": "2030-01-01"})
        body = assert_problem(response, 422, "no-trading-day")
        assert body["requested"] == "2030-01-01"
        assert body["latest_available"] == AS_OF.isoformat()

    async def test_a_future_historical_date_in_the_definition_is_also_422(
        self, api: httpx.AsyncClient
    ) -> None:
        """The date can arrive on the definition rather than the request (docs/01 §2.13)."""
        response = await api.post(
            url(f"/screens/{EXAMPLE_ID}/run"),
            json={"override_definition": {**MINIMAL, "historical_date": "2030-01-01"}},
        )
        assert_problem(response, 422, "no-trading-day")

    async def test_a_date_before_the_data_start_is_422(self, api: httpx.AsyncClient) -> None:
        """docs/07: "422 `no-trading-day` — `as_of` before the data start date"."""
        response = await api.post(url(f"/screens/{EXAMPLE_ID}/run"), json={"as_of": "2020-01-01"})
        assert_problem(response, 422, "no-trading-day")

    async def test_a_weekend_snaps_backwards_rather_than_failing(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/06 §step 1 — the difference between "not a trading day" and "out of range".

        A Sunday inside the servable range is answered for the Friday before it and the response
        says so. Run as a subscriber because the resolved date is not the latest published one,
        which makes it a historical rank (docs/01 §2.13).
        """
        headers = await subscriber(screener_session, "weekend@example.com")
        body = (
            await api.post(
                url(f"/screens/{EXAMPLE_ID}/run"), json={"as_of": "2026-08-16"}, headers=headers
            )
        ).json()
        assert body["as_of"] == "2026-08-14"


class TestTheStaleDataVersionCriterion:
    """Prompt 7: "A test asserts a stale data_version request returns 409"."""

    async def test_an_old_data_version_is_409(self, api: httpx.AsyncClient) -> None:
        response = await api.post(
            url(f"/screens/{EXAMPLE_ID}/run"), json={"data_version": DATA_VERSION - 1}
        )
        body = assert_problem(response, 409, "stale-data-version")
        assert body["sent_data_version"] == DATA_VERSION - 1
        assert body["current_data_version"] == DATA_VERSION

    async def test_a_data_version_that_never_existed_is_409(self, api: httpx.AsyncClient) -> None:
        assert_problem(
            await api.post(url(f"/screens/{EXAMPLE_ID}/run"), json={"data_version": 9999}),
            409,
            "stale-data-version",
        )

    async def test_the_current_data_version_is_accepted(self, api: httpx.AsyncClient) -> None:
        response = await api.post(
            url(f"/screens/{EXAMPLE_ID}/run"), json={"data_version": DATA_VERSION}
        )
        assert response.status_code == 200

    async def test_omitting_it_is_accepted(self, api: httpx.AsyncClient) -> None:
        assert (await api.post(url(f"/screens/{EXAMPLE_ID}/run"), json={})).status_code == 200

    async def test_preview_checks_it_too(self, api: httpx.AsyncClient) -> None:
        response = await api.post(
            url("/screens/preview"), json={"definition": MINIMAL, "data_version": 0}
        )
        assert_problem(response, 409, "stale-data-version")


class TestPreview:
    async def test_it_runs_an_unsaved_definition(self, api: httpx.AsyncClient) -> None:
        """docs/07: "run an unsaved definition (the edit form's live preview)"."""
        response = await api.post(url("/screens/preview"), json={"definition": MINIMAL})
        assert response.status_code == 200
        body = response.json()
        analytics_envelope(body)
        assert body["result_count"] == 271

    async def test_it_persists_nothing(self, api: httpx.AsyncClient) -> None:
        before = (await api.get(url("/screens"))).json()
        await api.post(url("/screens/preview"), json={"definition": MINIMAL})
        after = (await api.get(url("/screens"))).json()
        assert len(before["data"]) == len(after["data"])

    async def test_a_bad_definition_is_a_400(self, api: httpx.AsyncClient) -> None:
        response = await api.post(
            url("/screens/preview"),
            json={"definition": {"index": "atlantis", "sort_by": "ret_12m"}},
        )
        body = assert_problem(response, 400, "invalid-screen-definition")
        assert any("index" in str(error["field"]) for error in errors_of(body))


class TestEntitlements:
    """Prompt 7 §3 — the three gated features, enforced server-side (docs/07 §Entitlements)."""

    async def test_csv_export_is_refused_without_a_subscription(
        self, api: httpx.AsyncClient
    ) -> None:
        response = await api.get(url(f"/screens/{EXAMPLE_ID}/csv"))
        body = assert_problem(response, 402, "payment-required")
        assert body["upgrade_url"] == "/pricing"
        assert body["feature"] == "export_csv"

    async def test_csv_export_is_allowed_with_a_subscription(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await subscriber(screener_session)
        response = await api.get(url(f"/screens/{EXAMPLE_ID}/csv"), headers=headers)
        assert response.status_code == 200

    async def test_a_free_account_is_still_refused(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await free_user(screener_session)
        assert_problem(
            await api.get(url(f"/screens/{EXAMPLE_ID}/csv"), headers=headers),
            402,
            "payment-required",
        )

    async def test_historical_ranks_are_gated(self, api: httpx.AsyncClient) -> None:
        """docs/01 §2.13 — running the screen as of a past date."""
        response = await api.post(url(f"/screens/{EXAMPLE_ID}/run"), json={"as_of": "2026-08-17"})
        body = assert_problem(response, 402, "payment-required")
        assert body["feature"] == "historical_ranks"

    async def test_the_latest_date_is_not_a_historical_rank(self, api: httpx.AsyncClient) -> None:
        """Asking explicitly for today is not the paid feature."""
        response = await api.post(
            url(f"/screens/{EXAMPLE_ID}/run"), json={"as_of": AS_OF.isoformat()}
        )
        assert response.status_code == 200

    async def test_historical_ranks_are_allowed_with_a_subscription(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await subscriber(screener_session, "paid2@example.com")
        response = await api.post(
            url(f"/screens/{EXAMPLE_ID}/run"), json={"as_of": "2026-08-17"}, headers=headers
        )
        assert response.status_code == 200
        assert response.json()["as_of"] == "2026-08-17"

    async def test_custom_columns_are_gated(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await free_user(screener_session, "free2@example.com")
        created = (
            await api.post(
                url("/screens"),
                json={"name": "Wide", "definition": MINIMAL, "columns": ["rsi_12m", "high_ath"]},
                headers=headers,
            )
        ).json()
        response = await api.post(
            url(f"/screens/{created['public_id']}/run"), json={}, headers=headers
        )
        body = assert_problem(response, 402, "payment-required")
        assert body["feature"] == "custom_columns"

    async def test_the_default_columns_are_not_custom(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await free_user(screener_session, "free3@example.com")
        created = (
            await api.post(
                url("/screens"), json={"name": "Plain", "definition": MINIMAL}, headers=headers
            )
        ).json()
        response = await api.post(
            url(f"/screens/{created['public_id']}/run"), json={}, headers=headers
        )
        assert response.status_code == 200


class TestCsvExport:
    async def test_it_starts_with_a_bom_and_the_column_header(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """Prompt 7 §6: "a UTF-8 BOM for Excel". docs/13 §1 records the same on the reference."""
        headers = await subscriber(screener_session, "csv1@example.com")
        response = await api.get(url(f"/screens/{EXAMPLE_ID}/csv"), headers=headers)
        assert response.status_code == 200
        assert response.content.startswith(b"\xef\xbb\xbf")
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]
        assert "investing-001-2026-08-18.csv" in response.headers["content-disposition"]

    async def test_it_carries_the_reference_export_columns(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/13 §5 step 6 and Prompt 9 §6 — all 93, not the screen's chosen columns.

        Prompt 7 shipped the screen's column set; docs/13 settles it the other way, because the
        reference product exports everything regardless of what the user has chosen to see. The
        header is diffed against the committed file in `test_csv_export.py`; this asserts the
        route serves it.
        """
        headers = await subscriber(screener_session, "csv2@example.com")
        response = await api.get(url(f"/screens/{EXAMPLE_ID}/csv"), headers=headers)
        text = response.content.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        assert rows[0] == list(EXPORT_COLUMNS)
        assert len(rows) - 1 == 271

    async def test_the_values_match_the_json_response_exactly(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """CLAUDE.md house rule 8 again — one rounding, three surfaces.

        The two payloads name their columns differently (docs/13 §1's export names against
        `factor_daily`'s), which is exactly why this is worth asserting: the same stored value has
        to come out identically through both.
        """
        headers = await subscriber(screener_session, "csv3@example.com")
        body = (await api.post(url(f"/screens/{EXAMPLE_ID}/run"), json={}, headers=headers)).json()
        text = (await api.get(url(f"/screens/{EXAMPLE_ID}/csv"), headers=headers)).content.decode(
            "utf-8-sig"
        )
        rows = list(csv.DictReader(io.StringIO(text)))

        first_json = body["rows"][0]
        first_csv = rows[0]
        # The export's row order is the screen's order, so row one of each is the same instrument.
        assert first_csv["symbol"] == first_json["symbol"]
        assert first_csv["absolute_return_one_year"] == str(first_json["ret_12m"])
        assert first_csv["ma_200"] == str(first_json["ma_200"])

    async def test_the_response_is_chunked_not_content_length(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """A buffered body would carry a ``Content-Length``; a streamed one cannot.

        Chunk *counting* is not asserted here on purpose: httpx's ASGI transport collects the
        parts into one ``bytes`` before handing them over, so it could only ever see one chunk no
        matter how the application produced them. The generator itself is asserted in
        ``test_csv_export.py``.
        """
        headers = await subscriber(screener_session, "csv4@example.com")
        async with api.stream(
            "GET", url(f"/screens/{EXAMPLE_ID}/csv"), headers=headers
        ) as response:
            assert response.status_code == 200
            assert "content-length" not in response.headers
            await response.aread()


class TestRunHistory:
    async def test_it_paginates_with_a_cursor(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/07 §Conventions: `{ "data": [...], "next_cursor": "…" }`."""
        headers = await subscriber(screener_session, "hist@example.com")
        for as_of in ("2026-08-18", "2026-08-17", "2026-08-14"):
            await api.post(
                url(f"/screens/{EXAMPLE_ID}/run"), json={"as_of": as_of}, headers=headers
            )

        first = (
            await api.get(url(f"/screens/{EXAMPLE_ID}/runs"), params={"limit": 2}, headers=headers)
        ).json()
        assert len(first["data"]) == 2
        assert first["next_cursor"] is not None

        second = (
            await api.get(
                url(f"/screens/{EXAMPLE_ID}/runs"),
                params={"limit": 2, "cursor": first["next_cursor"]},
                headers=headers,
            )
        ).json()
        assert len(second["data"]) == 1
        assert second["next_cursor"] is None

        seen = [row["as_of"] for row in first["data"] + second["data"]]
        assert len(set(seen)) == 3

    async def test_an_empty_history_is_an_empty_page(self, api: httpx.AsyncClient) -> None:
        body = (await api.get(url(f"/screens/{EXAMPLE_ID}/runs"))).json()
        assert body == {"data": [], "next_cursor": None}

    async def test_a_bad_cursor_is_refused(self, api: httpx.AsyncClient) -> None:
        response = await api.get(
            url(f"/screens/{EXAMPLE_ID}/runs"), params={"cursor": "not-a-cursor"}
        )
        assert_problem(response, 404, "not-found")
