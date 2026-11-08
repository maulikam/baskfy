"""Error handling, authentication and rate limiting — docs/07 §"Error catalogue", docs/11 §Security.

Prompt 7 deliverables 1, 3 and 5. Every response this module asserts is RFC 9457
``application/problem+json``: docs/07 says errors follow it, and a service that answers problem+json
for the failures it anticipated and HTML for the ones it did not has not implemented the contract.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import httpx
import jwt
import pytest
from api_helpers import (
    PROBLEM_MEDIA_TYPE,
    TEST_JWT_SECRET,
    api_settings,
    assert_problem,
    bearer,
    make_user,
    problem,
    url,
)
from redis.asyncio import Redis
from screener_helpers import requires_db
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.app import create_app
from baskfy_api.auth import encode_token
from baskfy_api.db import get_session
from baskfy_api.logging import REQUEST_ID_HEADER
from baskfy_api.problems import ProblemType
from baskfy_api.ratelimit import KEY_PREFIX
from baskfy_core.models import PipelineRun
from baskfy_core.seed_data import EXAMPLE_SCREENS

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]

EXAMPLE_ID = EXAMPLE_SCREENS[0].public_id


class TestProblemDocuments:
    async def test_an_unrouted_path_is_problem_json_not_html(self, api: httpx.AsyncClient) -> None:
        """The framework's own 404 goes through the same handler as ours."""
        response = await api.get(url("/nothing-here"))
        assert response.status_code == 404
        body = problem(response)
        assert body["type"] == ProblemType.NOT_FOUND.value

    async def test_every_problem_carries_the_instance_and_the_request_id(
        self, api: httpx.AsyncClient
    ) -> None:
        """
        THE REQUEST-ID HALF ASSERTED NOTHING UNTIL 12 Sep 2026. The line was

            assert response.headers[REQUEST_ID_HEADER] == body.get("instance", "") or True

        ``X or True`` is ``True`` for every ``X``, so the comparison was evaluated and thrown
        away — and the comparison was nonsense anyway: ``instance`` is the request *path*
        (RFC 9457) and the request id is a uuid4 hex. They were never meant to be equal, which is
        presumably why ``or True`` was appended instead of the assertion being deleted or fixed.
        What was left, ``REQUEST_ID_HEADER in response.headers``, checks the header exists and
        nothing about whether it is usable.

        The property that matters is **correlation**: a support conversation starts with "here is
        the id from the error page", so the id must be a real value, and a client that supplies
        its own must get that one back rather than a fresh one (``app.py``'s middleware:
        ``request.headers.get(REQUEST_ID_HEADER) or new_request_id()``). Both halves below.
        """
        response = await api.get(url("/screens/unknown-id"))
        body = problem(response)
        assert body["instance"] == url("/screens/unknown-id")

        # A request id is generated when the client sends none, and it is a real one.
        generated = response.headers[REQUEST_ID_HEADER]
        assert generated
        assert generated.strip() == generated
        uuid.UUID(hex=generated)

        # Two requests do not share an id, or it correlates nothing.
        other = await api.get(url("/screens/unknown-id"))
        assert other.headers[REQUEST_ID_HEADER] != generated

        # A client-supplied id is echoed back, which is what makes the id in a bug report usable.
        supplied = uuid.uuid4().hex
        echoed = await api.get(url("/screens/unknown-id"), headers={REQUEST_ID_HEADER: supplied})
        assert echoed.headers[REQUEST_ID_HEADER] == supplied

    async def test_an_unhandled_exception_becomes_a_500_problem(
        self, seeded_url: str, screener_session: AsyncSession
    ) -> None:
        """docs/07's catalogue has no 500 row; a service promising problem+json still needs one.

        The stack trace goes to the log and the client gets four fields and nothing else.
        """
        app = create_app(api_settings(seeded_url))

        async def _session_override() -> object:
            yield screener_session

        app.dependency_overrides[get_session] = _session_override

        @app.get("/api/v1/boom")
        async def boom() -> None:
            raise RuntimeError("deliberate")

        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.get(url("/boom"))
        assert response.status_code == 500
        body = problem(response)
        assert body["type"] == "internal-error"
        assert "deliberate" not in str(body["detail"]), "a client must not see the internal error"


class TestAuthentication:
    async def test_no_token_is_anonymous_not_an_error(self, api: httpx.AsyncClient) -> None:
        """docs/07 rate-limits anonymous traffic, so anonymous traffic must be allowed."""
        assert (await api.get(url("/meta/factors"))).status_code == 200

    async def test_a_garbage_token_is_401(self, api: httpx.AsyncClient) -> None:
        response = await api.get(url("/screens"), headers={"Authorization": "Bearer not-a-token"})
        assert_problem(response, 401, "unauthenticated")

    async def test_a_token_signed_with_the_wrong_key_is_401(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, public_id = await make_user(screener_session, "wrongkey@example.com")
        headers = bearer(public_id, secret="a-different-secret-of-the-same-length!!")
        assert_problem(await api.get(url("/screens"), headers=headers), 401, "unauthenticated")

    async def test_an_expired_token_is_401(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, public_id = await make_user(screener_session, "expired@example.com")
        stale = encode_token(
            public_id,
            TEST_JWT_SECRET,
            lifetime_seconds=60,
            issued_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=2),
        )
        assert_problem(
            await api.get(url("/screens"), headers={"Authorization": f"Bearer {stale}"}),
            401,
            "unauthenticated",
        )

    async def test_an_over_long_lifetime_is_refused(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/11 §Security: "15-min access". A year-long token against the same secret is not
        a valid access token, however correctly it is signed."""
        _, public_id = await make_user(screener_session, "forever@example.com")
        headers = bearer(public_id, lifetime_seconds=365 * 24 * 3600)
        assert_problem(await api.get(url("/screens"), headers=headers), 401, "unauthenticated")

    async def test_an_unsigned_token_is_refused(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The ``alg: none`` attack. The verifier accepts one algorithm and does not negotiate."""
        _, public_id = await make_user(screener_session, "algnone@example.com")
        now = dt.datetime.now(tz=dt.UTC)
        forged = jwt.encode(
            {
                "sub": public_id,
                "iat": int(now.timestamp()),
                "exp": int((now + dt.timedelta(minutes=5)).timestamp()),
            },
            key="",
            algorithm="none",
        )
        assert_problem(
            await api.get(url("/screens"), headers={"Authorization": f"Bearer {forged}"}),
            401,
            "unauthenticated",
        )

    async def test_a_token_for_an_unknown_account_is_401(self, api: httpx.AsyncClient) -> None:
        assert_problem(
            await api.get(url("/screens"), headers=bearer("ghost0000000")), 401, "unauthenticated"
        )

    async def test_a_valid_token_authenticates(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, public_id = await make_user(screener_session, "real@example.com")
        response = await api.post(
            url("/screens"),
            json={"name": "Real", "definition": {"index": "nifty-500", "sort_by": "ret_12m"}},
            headers=bearer(public_id),
        )
        assert response.status_code == 201


class TestRequestIds:
    async def test_every_response_carries_one(self, api: httpx.AsyncClient) -> None:
        response = await api.get(url("/meta/universes"))
        assert len(response.headers[REQUEST_ID_HEADER]) == 32

    async def test_an_inbound_id_is_preserved(self, api: httpx.AsyncClient) -> None:
        """So a trace that started at the edge keeps one id end to end."""
        response = await api.get(
            url("/meta/universes"), headers={REQUEST_ID_HEADER: "edge-request-1"}
        )
        assert response.headers[REQUEST_ID_HEADER] == "edge-request-1"

    async def test_two_requests_get_different_ids(self, api: httpx.AsyncClient) -> None:
        first = (await api.get(url("/meta/universes"))).headers[REQUEST_ID_HEADER]
        second = (await api.get(url("/meta/universes"))).headers[REQUEST_ID_HEADER]
        assert first != second


class TestPipelineDegraded:
    async def test_an_unpublished_database_answers_503(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/07: "503 `pipeline-degraded`".

        docs/11 §Reliability wants a failed run to keep serving "the last good `data_version`
        with a banner", and it does — ``/meta/status`` reports ``degraded`` and the screen still
        runs. This is the other case: there is no good version to fall back to.
        """
        await screener_session.execute(update(PipelineRun).values(data_version=None))
        response = await api.post(url(f"/screens/{EXAMPLE_ID}/run"), json={})
        assert_problem(response, 503, "pipeline-degraded")

    async def test_a_running_run_reports_in_progress_not_a_failure(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """M76. Maulik asked for exactly this: "if data pull is in progress it should say so".

        `/meta/status` had two states — published or `degraded` — and the freshness pill read the
        second as "The last pipeline run did not publish". On 1 Sep 2026 that sentence was on
        screen while a run WAS in progress: the nightly now takes about an hour, because the SME
        universe tripled the instrument count, so the in-flight window is long and visible.

        The failure being reported in that window belongs to the PREVIOUS run. Blaming a finished
        failure while its replacement is working is the more misleading of the two, so `running`
        wins.
        """
        await screener_session.execute(update(PipelineRun).values(status="running"))
        body = (await api.get(url("/meta/status"))).json()
        assert body["pipeline_running"] is True

    async def test_a_failed_run_still_reports_degraded(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The other direction, so the new state cannot swallow the old one."""
        await screener_session.execute(update(PipelineRun).values(status="failed"))
        body = (await api.get(url("/meta/status"))).json()
        assert body["degraded"] is True
        assert body["pipeline_running"] is False

    async def test_status_still_answers_when_nothing_is_published(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The banner has to be able to say why the screen is empty."""
        await screener_session.execute(update(PipelineRun).values(data_version=None))
        body = (await api.get(url("/meta/status"))).json()
        assert body["as_of"] is None
        assert body["data_version"] == 0


async def _flush_rate_limits() -> None:
    """Buckets outlive a test; a leftover one would make the next assertion count from the wrong
    place."""
    client: Redis = Redis.from_url(os.environ.get("BASKFY_REDIS_URL", "redis://localhost:6380/0"))
    keys = [key async for key in client.scan_iter(match=f"{KEY_PREFIX}*")]
    if keys:
        await client.delete(*keys)
    await client.aclose()


class TestRateLimiting:
    """docs/07 §Conventions: "60 req/min authenticated, 10 req/min anonymous"."""

    async def test_anonymous_traffic_is_capped_at_ten_a_minute(
        self, seeded_url: str, screener_session: AsyncSession
    ) -> None:
        await _flush_rate_limits()
        app = create_app(
            api_settings(
                seeded_url,
                rate_limit_anonymous_per_minute=10,
                rate_limit_authenticated_per_minute=60,
            )
        )

        async def _session_override() -> object:
            yield screener_session

        app.dependency_overrides[get_session] = _session_override
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                statuses = [
                    (await client.get(url("/meta/universes"))).status_code for _ in range(12)
                ]
                refused = await client.get(url("/meta/universes"))

        assert statuses[:10] == [200] * 10
        assert statuses[10:] == [429, 429]
        body = assert_problem(refused, 429, "rate-limited")
        assert body["limit_per_minute"] == 10
        assert int(refused.headers["retry-after"]) >= 1
        assert refused.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)

    async def test_an_authenticated_caller_gets_the_higher_limit(
        self, seeded_url: str, screener_session: AsyncSession
    ) -> None:
        """Same bucket algorithm, a different quota — and a different key, per user not per IP."""
        await _flush_rate_limits()
        _, public_id = await make_user(screener_session, "busy@example.com")
        app = create_app(
            api_settings(
                seeded_url,
                rate_limit_anonymous_per_minute=2,
                rate_limit_authenticated_per_minute=40,
            )
        )

        async def _session_override() -> object:
            yield screener_session

        app.dependency_overrides[get_session] = _session_override
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                headers = bearer(public_id)
                statuses = [
                    (await client.get(url("/meta/universes"), headers=headers)).status_code
                    for _ in range(12)
                ]
        assert statuses == [200] * 12, "an authenticated caller must not hit the anonymous cap"
