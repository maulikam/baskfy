"""The HTTP caching layer — Prompt 16 deliverable 3.

    "plus HTTP caching: ETags derived from `data_version`, stale-while-revalidate on RSC
     fetches, and cache warming after publish."

Two halves. The pure half (tag construction, ``If-None-Match`` comparison, which paths are in
scope) needs no database and is asserted directly. The wired half — that a real analytics response
carries a validator, that presenting it back gets a 304 with no body, and that the routes with
per-user mutable state do *not* get one — runs against the seeded database, because a validator
computed from the wrong snapshot is exactly the bug that would survive a mock.
"""

from __future__ import annotations

import httpx
import pytest
from api_helpers import bearer, make_user, url
from screener_helpers import AS_OF, DATA_VERSION, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from decile_api.http_cache import (
    AS_OF_HEADER,
    DATA_VERSION_HEADER,
    cache_control,
    compute_etag,
    etag_matches,
    is_cacheable_path,
    snapshot_headers,
)
from decile_api.settings import API_PREFIX
from decile_core.seed_data import EXAMPLE_SCREENS


class TestEtagConstruction:
    def test_the_tag_names_the_data_version(self) -> None:
        """docs/06 §"Determinism guarantee" ties an answer to its version; so does the tag."""
        tag = compute_etag(b'{"data":[]}', "7")
        assert tag.startswith('W/"v7-')

    def test_a_different_version_is_a_different_tag_for_the_same_bytes(self) -> None:
        """A publish must invalidate every validator, even where the body happens to be identical.

        `/listings` does not change on a night when nothing listed. Serving the same ETag across
        a version bump would let a client hold a response that predates the snapshot the rest of
        the page is rendered from.
        """
        body = b'{"data":[]}'
        assert compute_etag(body, "7") != compute_etag(body, "8")

    def test_a_different_body_is_a_different_tag_for_the_same_version(self) -> None:
        assert compute_etag(b'{"a":1}', "7") != compute_etag(b'{"a":2}', "7")

    def test_a_versionless_route_still_gets_a_tag(self) -> None:
        """`/meta/factors` is a static catalogue with no snapshot behind it."""
        assert compute_etag(b"[]", None).startswith('W/"')

    def test_the_tag_is_weak(self) -> None:
        """We control the body we hand to the transport, not the encoding a proxy applies."""
        assert compute_etag(b"[]", "1").startswith("W/")


class TestIfNoneMatch:
    def test_an_exact_match(self) -> None:
        tag = compute_etag(b"[]", "1")
        assert etag_matches(tag, tag)

    def test_a_list_matches_on_any_member(self) -> None:
        tag = compute_etag(b"[]", "1")
        assert etag_matches(f'W/"other", {tag}', tag)

    def test_a_star_matches_anything(self) -> None:
        assert etag_matches("*", compute_etag(b"[]", "1"))

    def test_comparison_ignores_the_weak_prefix(self) -> None:
        """RFC 9110 §13.1.2: a GET revalidation uses the weak comparison function."""
        tag = compute_etag(b"[]", "1")
        assert etag_matches(tag.removeprefix("W/"), tag)

    def test_a_missing_header_never_matches(self) -> None:
        assert not etag_matches(None, compute_etag(b"[]", "1"))

    def test_a_stale_tag_does_not_match(self) -> None:
        assert not etag_matches(compute_etag(b"[]", "1"), compute_etag(b"[]", "2"))


class TestScope:
    @pytest.mark.parametrize(
        "path",
        [
            "/meta/status",
            "/meta/factors",
            "/instruments/CUPID",
            "/instruments/CUPID/history",
            "/indices/dashboard",
            "/market-health",
            "/market-health/history",
            "/listings",
        ],
    )
    def test_the_published_analytics_reads_are_in_scope(self, path: str) -> None:
        assert is_cacheable_path(f"{API_PREFIX}{path}")

    @pytest.mark.parametrize(
        "path",
        [
            "/screens",
            "/screens/abc/run",
            "/me",
            "/invoices",
            "/portfolios",
            "/backtests",
            "/plans",
        ],
    )
    def test_per_user_state_is_not(self, path: str) -> None:
        """A validator here would be a cached answer to a question whose answer is per user."""
        assert not is_cacheable_path(f"{API_PREFIX}{path}")

    def test_a_streaming_export_is_never_buffered(self) -> None:
        """docs/11 budgets a 4,000-row CSV at 2 s; hashing it would mean buffering all of it."""
        assert not is_cacheable_path(f"{API_PREFIX}/instruments/CUPID/csv")

    def test_an_unversioned_path_is_out_of_scope(self) -> None:
        assert not is_cacheable_path("/healthz")


class TestCacheControl:
    def test_it_is_private(self) -> None:
        """These responses are entitlement-filtered; a shared cache would leak one across users."""
        assert "private" in cache_control(60)

    def test_it_revalidates_and_allows_a_stale_answer_while_it_does(self) -> None:
        directives = cache_control(60)
        assert "max-age=0" in directives
        assert "must-revalidate" in directives
        assert "stale-while-revalidate=60" in directives


class TestSnapshotHeaders:
    def test_both_headers_when_both_are_known(self) -> None:
        headers = snapshot_headers(AS_OF, DATA_VERSION)
        assert headers[AS_OF_HEADER] == AS_OF.isoformat()
        assert headers[DATA_VERSION_HEADER] == str(DATA_VERSION)

    def test_nothing_when_neither_is(self) -> None:
        assert snapshot_headers(None, None) == {}


@pytest.mark.db
@requires_db
class TestOverHttp:
    """The wired behaviour, against the seeded database."""

    async def test_the_dashboard_carries_a_versioned_validator(
        self, api: httpx.AsyncClient
    ) -> None:
        response = await api.get(url("/indices/dashboard"))
        assert response.status_code == 200
        assert response.headers[DATA_VERSION_HEADER] == str(DATA_VERSION)
        assert response.headers["ETag"].startswith(f'W/"v{DATA_VERSION}-')
        assert "stale-while-revalidate" in response.headers["Cache-Control"]

    async def test_presenting_the_validator_back_is_a_304_with_no_body(
        self, api: httpx.AsyncClient
    ) -> None:
        first = await api.get(url("/indices/dashboard"))
        again = await api.get(
            url("/indices/dashboard"), headers={"If-None-Match": first.headers["ETag"]}
        )
        assert again.status_code == 304
        assert again.content == b""
        assert again.headers["ETag"] == first.headers["ETag"]
        # RFC 9110 §15.4.5: the freshness directives are repeated on the 304, because they are
        # what the client stores against the copy it already has.
        assert again.headers["Cache-Control"] == first.headers["Cache-Control"]

    async def test_a_stale_validator_gets_the_body(self, api: httpx.AsyncClient) -> None:
        response = await api.get(
            url("/indices/dashboard"), headers={"If-None-Match": 'W/"v0-deadbeef"'}
        )
        assert response.status_code == 200
        assert response.content

    async def test_the_factsheet_carries_its_own_as_of(self, api: httpx.AsyncClient) -> None:
        response = await api.get(url("/instruments/CUPID"))
        assert response.status_code == 200
        assert response.headers[AS_OF_HEADER] == AS_OF.isoformat()
        assert response.headers[DATA_VERSION_HEADER] == str(DATA_VERSION)
        assert "ETag" in response.headers

    async def test_two_instruments_do_not_share_a_validator(self, api: httpx.AsyncClient) -> None:
        """The version says which snapshot; the digest says which answer within it."""
        first = await api.get(url("/instruments/CUPID"))
        second = await api.get(url("/instruments/RELIANCE"))
        if second.status_code != 200:  # pragma: no cover - depends on the fixture's symbols
            pytest.skip("RELIANCE is not in the reference export")
        assert first.headers["ETag"] != second.headers["ETag"]

    async def test_the_validator_is_stable_across_calls(self, api: httpx.AsyncClient) -> None:
        """It has to be: docs/06 promises byte-identical results for the same snapshot."""
        first = await api.get(url("/listings"))
        second = await api.get(url("/listings"))
        assert first.headers["ETag"] == second.headers["ETag"]

    async def test_the_response_varies_on_authorization(self, api: httpx.AsyncClient) -> None:
        """These payloads are entitlement-filtered. Without this a shared cache is a data leak."""
        response = await api.get(url("/indices/dashboard"))
        assert "authorization" in response.headers["Vary"].lower()

    async def test_a_screen_run_gets_no_validator(self, api: httpx.AsyncClient) -> None:
        """It is a POST with a Redis cache behind it; a browser would never revalidate it."""
        response = await api.post(url(f"/screens/{EXAMPLE_SCREENS[0].public_id}/run"), json={})
        assert response.status_code == 200
        assert "ETag" not in response.headers

    async def test_a_csv_export_still_streams(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """Buffering it to hash it would defeat the point of streaming it.

        docs/11 budgets a 4,000-row export at 2 s and `decile_api.csv_export` meets it by never
        holding the file in memory. The export is entitlement-gated (docs/07 §"Error catalogue",
        402), so this drives it as a subscriber — an anonymous 402 would prove nothing about the
        streaming path.
        """
        _, public_id = await make_user(screener_session, "csv-etag@example.com", subscribed=True)
        response = await api.get(
            url(f"/screens/{EXAMPLE_SCREENS[0].public_id}/csv"), headers=bearer(public_id)
        )
        assert response.status_code == 200, response.text[:300]
        assert response.headers["content-type"].startswith("text/csv")
        assert "ETag" not in response.headers
