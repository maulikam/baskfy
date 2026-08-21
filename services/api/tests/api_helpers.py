"""Shared setup for the HTTP contract tests (Prompt 7).

Separate from ``conftest.py`` so the test modules can import it, and separate from
``screener_helpers`` because that module is about seeding a database and this one is about driving
an application over HTTP.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Final

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from decile_api.app import create_app
from decile_api.auth import encode_token
from decile_api.db import get_session
from decile_api.settings import Settings
from decile_core.models import AppUser, Subscription
from decile_core.seed_data import PLANS

#: 32 bytes, because RFC 7518 §3.2 requires an HS256 key at least as long as the hash output and
#: PyJWT warns when it is not. The value is arbitrary; the length is not.
TEST_JWT_SECRET: Final = "decile-test-secret-0123456789abc"

#: docs/07: "Base: `/api/v1`".
PREFIX: Final = "/api/v1"

PROBLEM_MEDIA_TYPE: Final = "application/problem+json"


def url(path: str) -> str:
    return f"{PREFIX}{path}"


def api_settings(database_url: str, **overrides: object) -> Settings:
    """Test settings: the seeded database, a real Redis, and limits that do not bite.

    The documented rate limits (10/min anonymous) would exhaust inside a single contract module,
    so the default here is generous and ``test_api_errors`` builds its own app with the real
    numbers. Everything else — the limiter, the cache, the token verifier — is the production path.
    """
    base = Settings(
        database_url=database_url,
        # Argon2id at production cost would put ~20 MiB and a few hundred milliseconds on every
        # fixture that logs in. These are the *smallest* parameters argon2-cffi accepts, which is
        # correct for a suite that is testing the flow rather than the KDF — `test_security.py`
        # asserts the production defaults separately.
        argon2_time_cost=1,
        argon2_memory_kib=8,
        argon2_parallelism=1,
        # The suite drives HTTP, not HTTPS, so a Secure cookie would never come back.
        cookie_secure=False,
        rate_limit_auth_per_minute=100_000,
        redis_url=os.environ.get("DECILE_REDIS_URL", "redis://localhost:6380/0"),
        environment="test",
        jwt_secret=TEST_JWT_SECRET,
        rate_limit_anonymous_per_minute=100_000,
        rate_limit_authenticated_per_minute=100_000,
        log_json=False,
        log_level="WARNING",
    )
    return base.model_copy(update=dict(overrides)) if overrides else base


@asynccontextmanager
async def running_app(
    settings: Settings,
    session: AsyncSession,
    *,
    razorpay: object | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """The real application, with only the session dependency swapped.

    The lifespan runs, so the engine, the Redis client and the rate limiter are built exactly as
    they are in production. ``get_session`` is overridden to hand back the test's transaction —
    which is never committed — so a contract test can create screens without leaving any behind.
    """
    app = create_app(settings)

    async def _session_override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _session_override
    async with app.router.lifespan_context(app):
        if razorpay is not None:
            # Prompt 13: the gateway is read from `app.state`, so a suite that must never reach
            # Razorpay attaches one driven by an `httpx.MockTransport`.
            app.state.razorpay = razorpay
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


def errors_of(body: Mapping[str, object]) -> list[Mapping[str, object]]:
    """The ``errors[]`` extension docs/07 attaches to `invalid-screen-definition`."""
    errors = body["errors"]
    assert isinstance(errors, list)
    return [error for error in errors if isinstance(error, Mapping)]


async def make_user(
    session: AsyncSession, email: str, *, subscribed: bool = False
) -> tuple[int, str]:
    """An ``app_user`` row, optionally with an active subscription. Returns ``(id, public_id)``.

    The subscription is what the Prompt 7 stub entitlement service reads, so ``subscribed=True``
    is the difference between a 200 and a 402 on every gated endpoint.
    """
    public_id = email.split("@", maxsplit=1)[0].replace(".", "")[:12].ljust(12, "0")
    user = AppUser(public_id=public_id, email=email, name=email.split("@", maxsplit=1)[0])
    session.add(user)
    await session.flush()

    if subscribed:
        session.add(
            Subscription(
                user_id=user.id,
                plan_id=PLANS[0].id,
                status="active",
                started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                current_period_end=dt.datetime(2027, 1, 1, tzinfo=dt.UTC),
            )
        )
        await session.flush()
    return user.id, public_id


def bearer(
    public_id: str, *, secret: str = TEST_JWT_SECRET, lifetime_seconds: int = 900
) -> dict[str, str]:
    """docs/07: "Authorization: Bearer <JWT> issued by the Next.js app"."""
    token = encode_token(public_id, secret, lifetime_seconds=lifetime_seconds)
    return {"Authorization": f"Bearer {token}"}


def problem(response: httpx.Response) -> dict[str, object]:
    """Assert the response *is* a problem document, and return it."""
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE), (
        f"{response.status_code} was not problem+json: {response.headers.get('content-type')}"
    )
    body: dict[str, object] = response.json()
    for member in ("type", "title", "status", "detail"):
        assert member in body, f"RFC 9457 requires {member!r}: {body}"
    assert body["status"] == response.status_code
    return body


def assert_problem(response: httpx.Response, status: int, type_: str) -> dict[str, object]:
    assert response.status_code == status, f"{response.status_code}: {response.text[:400]}"
    body = problem(response)
    assert body["type"] == type_, body
    return body


def analytics_envelope(body: dict[str, object]) -> None:
    """docs/07 §Conventions: "Every analytics response includes `as_of` and `data_version`"."""
    assert "as_of" in body, body
    assert "data_version" in body, body


def row_values(body: dict[str, object], column: str) -> Sequence[object]:
    rows = body["rows"]
    assert isinstance(rows, list)
    return [row[column] for row in rows if isinstance(row, dict)]
