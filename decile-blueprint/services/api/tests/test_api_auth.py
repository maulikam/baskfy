"""Contract and security tests for `/auth/*` and `/me` — docs/07, docs/11 §Security (Prompt 12).

Prompt 12's first acceptance criterion lives in `TestSecurity`:

* brute force triggers a lockout,
* refresh-token reuse is detected and revokes the family,
* a token signed with the wrong secret is rejected.

The second — "no password or token value is ever written to logs" — is
`services/api/tests/test_log_redaction.py`, because it is about the logging subsystem rather than
about any one endpoint.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Final

import httpx
import pytest
from api_helpers import TEST_JWT_SECRET, assert_problem, url
from screener_helpers import requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.auth import encode_token
from baskfy_api.csrf import CSRF_COOKIE, CSRF_HEADER, REFRESH_COOKIE
from baskfy_api.email import Mailer
from baskfy_api.email.templates import Message
from baskfy_core.models import AppUser, AuthLockout, AuthToken, ConsentRecord, RefreshToken

pytestmark = [
    pytest.mark.db,
    pytest.mark.redis,
    requires_db,
    # Per-request cookies are exactly what these tests need: the reuse-detection case has to
    # present a *specific* superseded token, which a client-level cookie jar would have replaced.
    pytest.mark.filterwarnings("ignore:Setting per-request cookies:DeprecationWarning"),
]

JsonMap = dict[str, object]

EMAIL: Final = "person@example.com"
PASSWORD: Final = "correct horse battery"
OTHER_PASSWORD: Final = "a different one entirely"


def body_of(response: httpx.Response) -> JsonMap:
    parsed: JsonMap = json.loads(response.text)
    return parsed


def as_map(value: object) -> JsonMap:
    assert isinstance(value, dict), value
    return value


class Outbox:
    """A `Transport` that keeps what it was asked to send.

    Substituted for the process mailer so a test can read the OTP the way a user reads their
    inbox — which is the only way to exercise the code path end to end without a live SMTP server.
    """

    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def send(self, message: Message) -> None:
        self.messages.append(message)

    def last_for(self, to: str) -> Message:
        for message in reversed(self.messages):
            if message.to.lower() == to.lower():
                return message
        raise AssertionError(f"no message was sent to {to}: {[m.to for m in self.messages]}")

    def codes_in(self, message: Message) -> str:
        """The six-digit code out of the plain-text body."""
        for line in message.text.splitlines():
            stripped = line.strip()
            if stripped.isdigit() and len(stripped) == 6:
                return stripped
        raise AssertionError(f"no code in:\n{message.text}")

    def token_in(self, message: Message) -> str:
        for line in message.text.splitlines():
            if "token=" in line:
                return line.split("token=", maxsplit=1)[1].strip()
        raise AssertionError(f"no token link in:\n{message.text}")


@pytest.fixture
def outbox(api: httpx.AsyncClient) -> Outbox:
    """Swap the app's mailer for one that keeps the messages.

    Reaching through the transport is the only way to get at the ASGI app an `httpx.AsyncClient`
    was built around, and building a second app for these tests would mean a second lifespan and a
    second engine.
    """
    box = Outbox()
    transport = api._transport
    app = getattr(transport, "app", None)
    assert app is not None, "the api fixture must be built on an ASGITransport"
    app.state.mailer = Mailer(box)
    return box


async def register(
    api: httpx.AsyncClient, email: str = EMAIL, password: str | None = PASSWORD
) -> httpx.Response:
    payload: JsonMap = {"email": email, "accept_terms": True}
    if password is not None:
        payload["password"] = password
    return await api.post(url("/auth/register"), json=payload)


async def login(
    api: httpx.AsyncClient, email: str = EMAIL, password: str = PASSWORD
) -> httpx.Response:
    return await api.post(url("/auth/login"), json={"email": email, "password": password})


class TestRegistration:
    async def test_it_creates_an_account_and_sends_a_confirmation(
        self, api: httpx.AsyncClient, outbox: Outbox, screener_session: AsyncSession
    ) -> None:
        response = await register(api)
        assert response.status_code == 202
        assert body_of(response)["status"] == "accepted"

        user = (
            await screener_session.execute(select(AppUser).where(AppUser.email == EMAIL))
        ).scalar_one()
        assert user.password_hash is not None
        assert user.password_hash.startswith("$argon2id$"), "docs/11 names Argon2id"
        assert PASSWORD not in user.password_hash

        message = outbox.last_for(EMAIL)
        assert "Confirm" in message.subject
        assert message.text and message.html, "plain-text alternative is required (Prompt 12 §3)"

    async def test_registering_a_known_address_says_the_same_thing(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        """docs/11's PII inventory makes the customer list PII, so this form is not a
        membership check."""
        first = await register(api)
        second = await register(api)
        assert first.status_code == second.status_code == 202
        assert body_of(first)["detail"] == body_of(second)["detail"]
        # The owner of the address is told, though.
        assert "sign-in code" in outbox.last_for(EMAIL).subject

    async def test_it_requires_the_terms_to_be_accepted(self, api: httpx.AsyncClient) -> None:
        """docs/11 §Compliance: "DPDP Act: consent record"."""
        response = await api.post(
            url("/auth/register"), json={"email": "x@example.com", "accept_terms": False}
        )
        assert_problem(response, 400, "invalid-screen-definition")

    async def test_it_records_the_consent(
        self, api: httpx.AsyncClient, outbox: Outbox, screener_session: AsyncSession
    ) -> None:
        del outbox
        await register(api)
        rows = (
            await screener_session.execute(select(AppUser).where(AppUser.email == EMAIL))
        ).scalar_one()
        consents = (
            (
                await screener_session.execute(
                    select(ConsentRecord).where(ConsentRecord.user_id == rows.id)
                )
            )
            .scalars()
            .all()
        )
        assert {row.kind for row in consents} == {"terms", "privacy"}
        assert all(row.document_version for row in consents)

    async def test_a_password_is_optional(
        self, api: httpx.AsyncClient, outbox: Outbox, screener_session: AsyncSession
    ) -> None:
        """docs/11: "OTP login as the default path, **password optional**"."""
        del outbox
        assert (await register(api, password=None)).status_code == 202
        user = (
            await screener_session.execute(select(AppUser).where(AppUser.email == EMAIL))
        ).scalar_one()
        assert user.password_hash is None

    async def test_a_short_password_is_refused(self, api: httpx.AsyncClient) -> None:
        response = await api.post(
            url("/auth/register"),
            json={"email": EMAIL, "password": "short", "accept_terms": True},
        )
        assert_problem(response, 400, "invalid-screen-definition")


class TestPasswordLogin:
    async def test_it_returns_an_access_token_and_sets_the_cookies(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        del outbox
        await register(api)
        response = await login(api)
        assert response.status_code == 200

        payload = body_of(response)
        assert payload["token_type"] == "Bearer"
        assert payload["email"] == EMAIL
        assert isinstance(payload["access_token"], str)

        # docs/11: the refresh token is an httpOnly cookie and nowhere else.
        assert REFRESH_COOKIE in response.cookies
        assert CSRF_COOKIE in response.cookies
        assert "refresh" not in response.text.lower()

    async def test_the_access_token_works_against_the_rest_of_the_api(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        del outbox
        await register(api)
        token = str(body_of(await login(api))["access_token"])
        me = await api.get(url("/me"), headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert as_map(body_of(me))["email"] == EMAIL

    async def test_a_wrong_password_is_a_flat_401(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        del outbox
        await register(api)
        assert_problem(await login(api, password=OTHER_PASSWORD), 401, "unauthenticated")

    async def test_an_unknown_address_answers_identically(self, api: httpx.AsyncClient) -> None:
        known = await login(api, email="nobody@example.com", password=OTHER_PASSWORD)
        body = assert_problem(known, 401, "unauthenticated")
        assert "not valid" in str(body["detail"]).lower()
        assert "no such" not in str(body["detail"]).lower()


class TestOtp:
    async def test_the_default_path_signs_in_with_a_code(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        """docs/11: "OTP login as the default path"."""
        await register(api, password=None)
        accepted = await api.post(url("/auth/request-otp"), json={"email": EMAIL})
        assert accepted.status_code == 202

        code = outbox.codes_in(outbox.last_for(EMAIL))
        response = await api.post(url("/auth/verify-otp"), json={"email": EMAIL, "code": code})
        assert response.status_code == 200
        assert body_of(response)["email"] == EMAIL

    async def test_a_code_works_once(self, api: httpx.AsyncClient, outbox: Outbox) -> None:
        await register(api, password=None)
        await api.post(url("/auth/request-otp"), json={"email": EMAIL})
        code = outbox.codes_in(outbox.last_for(EMAIL))
        assert (
            await api.post(url("/auth/verify-otp"), json={"email": EMAIL, "code": code})
        ).status_code == 200
        assert_problem(
            await api.post(url("/auth/verify-otp"), json={"email": EMAIL, "code": code}),
            401,
            "unauthenticated",
        )

    async def test_an_unknown_address_sends_nothing_and_says_the_same(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        response = await api.post(url("/auth/request-otp"), json={"email": "ghost@example.com"})
        assert response.status_code == 202
        assert outbox.messages == []

    async def test_the_code_is_stored_hashed(
        self, api: httpx.AsyncClient, outbox: Outbox, screener_session: AsyncSession
    ) -> None:
        await register(api, password=None)
        await api.post(url("/auth/request-otp"), json={"email": EMAIL})
        code = outbox.codes_in(outbox.last_for(EMAIL))
        rows = (
            (
                await screener_session.execute(
                    select(AuthToken).where(AuthToken.email == EMAIL, AuthToken.purpose == "otp")
                )
            )
            .scalars()
            .all()
        )
        assert rows
        assert all(code not in row.token_hash for row in rows)

    async def test_signing_in_by_code_verifies_the_address(
        self, api: httpx.AsyncClient, outbox: Outbox, screener_session: AsyncSession
    ) -> None:
        """A correct code proves control of the inbox, which is what verification asserts."""
        await register(api, password=None)
        await api.post(url("/auth/request-otp"), json={"email": EMAIL})
        code = outbox.codes_in(outbox.last_for(EMAIL))
        await api.post(url("/auth/verify-otp"), json={"email": EMAIL, "code": code})

        user = (
            await screener_session.execute(select(AppUser).where(AppUser.email == EMAIL))
        ).scalar_one()
        await screener_session.refresh(user)
        assert user.email_verified_at is not None


class TestRefreshAndLogout:
    async def _sign_in(self, api: httpx.AsyncClient) -> tuple[str, str]:
        response = await login(api)
        return response.cookies[REFRESH_COOKIE], response.cookies[CSRF_COOKIE]

    async def test_refresh_rotates_the_token(self, api: httpx.AsyncClient, outbox: Outbox) -> None:
        del outbox
        await register(api)
        refresh_cookie, csrf = await self._sign_in(api)

        rotated = await api.post(
            url("/auth/refresh"),
            headers={CSRF_HEADER: csrf},
            cookies={REFRESH_COOKIE: refresh_cookie, CSRF_COOKIE: csrf},
        )
        assert rotated.status_code == 200
        assert rotated.cookies[REFRESH_COOKIE] != refresh_cookie, "docs/11 says *rotating*"

    async def test_refresh_without_the_csrf_header_is_refused(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        """docs/11: "CSRF protection on all cookie-authenticated mutations"."""
        del outbox
        await register(api)
        refresh_cookie, csrf = await self._sign_in(api)
        assert_problem(
            await api.post(
                url("/auth/refresh"),
                cookies={REFRESH_COOKIE: refresh_cookie, CSRF_COOKIE: csrf},
            ),
            401,
            "unauthenticated",
        )

    async def test_a_mismatched_csrf_header_is_refused(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        del outbox
        await register(api)
        refresh_cookie, csrf = await self._sign_in(api)
        assert_problem(
            await api.post(
                url("/auth/refresh"),
                headers={CSRF_HEADER: "not-the-cookie"},
                cookies={REFRESH_COOKIE: refresh_cookie, CSRF_COOKIE: csrf},
            ),
            401,
            "unauthenticated",
        )

    async def test_logout_revokes_the_token(self, api: httpx.AsyncClient, outbox: Outbox) -> None:
        del outbox
        await register(api)
        refresh_cookie, csrf = await self._sign_in(api)

        out = await api.post(
            url("/auth/logout"),
            headers={CSRF_HEADER: csrf},
            cookies={REFRESH_COOKIE: refresh_cookie, CSRF_COOKIE: csrf},
        )
        assert out.status_code == 204

        assert_problem(
            await api.post(
                url("/auth/refresh"),
                headers={CSRF_HEADER: csrf},
                cookies={REFRESH_COOKIE: refresh_cookie, CSRF_COOKIE: csrf},
            ),
            401,
            "unauthenticated",
        )


class TestSecurity:
    """Prompt 12's first acceptance criterion, in three parts."""

    async def test_brute_force_triggers_a_lockout(
        self, api: httpx.AsyncClient, outbox: Outbox, screener_session: AsyncSession
    ) -> None:
        """docs/11: "account lockout after 10 failures with email notification"."""
        await register(api)
        statuses = [(await login(api, password=OTHER_PASSWORD)).status_code for _ in range(10)]
        assert statuses[:9] == [401] * 9, statuses
        # The tenth failure trips it; the request that follows is refused as rate-limited.
        locked = await login(api, password=OTHER_PASSWORD)
        assert locked.status_code == 429, locked.text
        assert "Retry-After" in locked.headers

        # Even the *correct* password is refused while the lockout stands.
        assert (await login(api)).status_code == 429

        row = (
            await screener_session.execute(
                select(AuthLockout).where(AuthLockout.identifier == EMAIL)
            )
        ).scalar_one()
        assert row.failures >= 10
        assert row.locked_until is not None

        # ...and the account's owner was told, exactly once.
        notices = [m for m in outbox.messages if "Unusual sign-in" in m.subject]
        assert len(notices) == 1, [m.subject for m in outbox.messages]

    async def test_refresh_reuse_is_detected_and_revokes_the_family(
        self, api: httpx.AsyncClient, outbox: Outbox, screener_session: AsyncSession
    ) -> None:
        """RFC 9700 §4.14.2, which is what "rotating refresh" is for."""
        del outbox
        await register(api)
        signed_in = await login(api)
        first = signed_in.cookies[REFRESH_COOKIE]
        csrf = signed_in.cookies[CSRF_COOKIE]

        rotated = await api.post(
            url("/auth/refresh"),
            headers={CSRF_HEADER: csrf},
            cookies={REFRESH_COOKIE: first, CSRF_COOKIE: csrf},
        )
        assert rotated.status_code == 200
        second = rotated.cookies[REFRESH_COOKIE]

        # Replay the *first* token — the one a thief would have copied.
        replay = await api.post(
            url("/auth/refresh"),
            headers={CSRF_HEADER: csrf},
            cookies={REFRESH_COOKIE: first, CSRF_COOKIE: csrf},
        )
        assert_problem(replay, 401, "unauthenticated")

        # The legitimate successor is dead too: the whole family was revoked.
        after = await api.post(
            url("/auth/refresh"),
            headers={CSRF_HEADER: csrf},
            cookies={REFRESH_COOKIE: second, CSRF_COOKIE: csrf},
        )
        assert_problem(after, 401, "unauthenticated")

        live = (
            (
                await screener_session.execute(
                    select(RefreshToken).where(RefreshToken.revoked_at.is_(None))
                )
            )
            .scalars()
            .all()
        )
        assert live == [], "no token in the family may survive a detected reuse"

    async def test_a_token_signed_with_the_wrong_secret_is_rejected(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        del outbox
        await register(api)
        public_id = str(body_of(await login(api))["public_id"])

        forged = encode_token(public_id, "a-different-secret-that-is-long-enough-32")
        assert_problem(
            await api.get(url("/me"), headers={"Authorization": f"Bearer {forged}"}),
            401,
            "unauthenticated",
        )
        # The genuine one still works, so the test is not passing for the wrong reason.
        genuine = encode_token(public_id, TEST_JWT_SECRET)
        assert (
            await api.get(url("/me"), headers={"Authorization": f"Bearer {genuine}"})
        ).status_code == 200

    async def test_an_expired_access_token_is_rejected(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        del outbox
        await register(api)
        public_id = str(body_of(await login(api))["public_id"])
        stale = encode_token(
            public_id,
            TEST_JWT_SECRET,
            lifetime_seconds=60,
            issued_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=2),
        )
        assert_problem(
            await api.get(url("/me"), headers={"Authorization": f"Bearer {stale}"}),
            401,
            "unauthenticated",
        )


class TestPasswordReset:
    async def test_the_full_reset_flow(self, api: httpx.AsyncClient, outbox: Outbox) -> None:
        await register(api)
        assert (
            await api.post(url("/auth/forgot-password"), json={"email": EMAIL})
        ).status_code == 202

        token = outbox.token_in(outbox.last_for(EMAIL))
        response = await api.post(
            url("/auth/reset-password"), json={"token": token, "password": OTHER_PASSWORD}
        )
        assert response.status_code == 200, response.text

        assert (await login(api, password=OTHER_PASSWORD)).status_code == 200
        assert_problem(await login(api, password=PASSWORD), 401, "unauthenticated")

    async def test_a_reset_link_works_once(self, api: httpx.AsyncClient, outbox: Outbox) -> None:
        await register(api)
        await api.post(url("/auth/forgot-password"), json={"email": EMAIL})
        token = outbox.token_in(outbox.last_for(EMAIL))
        await api.post(
            url("/auth/reset-password"), json={"token": token, "password": OTHER_PASSWORD}
        )
        assert_problem(
            await api.post(
                url("/auth/reset-password"), json={"token": token, "password": "third password"}
            ),
            400,
            "invalid-screen-definition",
        )

    async def test_a_reset_ends_every_other_session(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        await register(api)
        signed_in = await login(api)
        refresh_cookie = signed_in.cookies[REFRESH_COOKIE]
        csrf = signed_in.cookies[CSRF_COOKIE]

        await api.post(url("/auth/forgot-password"), json={"email": EMAIL})
        token = outbox.token_in(outbox.last_for(EMAIL))
        await api.post(
            url("/auth/reset-password"), json={"token": token, "password": OTHER_PASSWORD}
        )

        assert_problem(
            await api.post(
                url("/auth/refresh"),
                headers={CSRF_HEADER: csrf},
                cookies={REFRESH_COOKIE: refresh_cookie, CSRF_COOKIE: csrf},
            ),
            401,
            "unauthenticated",
        )

    async def test_an_unknown_address_answers_the_same(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        response = await api.post(url("/auth/forgot-password"), json={"email": "ghost@example.com"})
        assert response.status_code == 202
        assert outbox.messages == []


class TestMe:
    async def _token(self, api: httpx.AsyncClient) -> dict[str, str]:
        return {"Authorization": f"Bearer {body_of(await login(api))['access_token']}"}

    async def test_it_carries_the_profile_and_the_entitlements(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        """docs/07: `GET /me` -> "profile + entitlements"."""
        del outbox
        await register(api)
        body = body_of(await api.get(url("/me"), headers=await self._token(api)))
        assert body["email"] == EMAIL
        assert body["has_password"] is True
        entitlements = as_map(body["entitlements"])
        assert set(entitlements) == {
            "screener",
            "export_csv",
            "custom_columns",
            "historical_ranks",
            "backtests",
            "api_access",
            "max_screens",
        }

    async def test_it_needs_authentication(self, api: httpx.AsyncClient) -> None:
        assert_problem(await api.get(url("/me")), 401, "unauthenticated")

    async def test_patch_updates_the_name(self, api: httpx.AsyncClient, outbox: Outbox) -> None:
        del outbox
        await register(api)
        headers = await self._token(api)
        body = body_of(await api.patch(url("/me"), json={"name": "Renamed"}, headers=headers))
        assert body["name"] == "Renamed"

    async def test_change_password_requires_the_current_one(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        del outbox
        await register(api)
        headers = await self._token(api)
        assert_problem(
            await api.post(
                url("/me/change-password"),
                json={"current_password": "wrong", "new_password": OTHER_PASSWORD},
                headers=headers,
            ),
            401,
            "unauthenticated",
        )

    async def test_change_password_works_and_ends_other_sessions(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        del outbox
        await register(api)
        signed_in = await login(api)
        headers = {"Authorization": f"Bearer {body_of(signed_in)['access_token']}"}
        refresh_cookie = signed_in.cookies[REFRESH_COOKIE]
        csrf = signed_in.cookies[CSRF_COOKIE]

        changed = await api.post(
            url("/me/change-password"),
            json={"current_password": PASSWORD, "new_password": OTHER_PASSWORD},
            headers=headers,
        )
        assert changed.status_code == 200

        assert (await login(api, password=OTHER_PASSWORD)).status_code == 200
        assert_problem(
            await api.post(
                url("/auth/refresh"),
                headers={CSRF_HEADER: csrf},
                cookies={REFRESH_COOKIE: refresh_cookie, CSRF_COOKIE: csrf},
            ),
            401,
            "unauthenticated",
        )

    async def test_an_otp_only_account_can_set_a_password(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        """docs/11: "password optional" — setting the first one supplies no current password."""
        await register(api, password=None)
        await api.post(url("/auth/request-otp"), json={"email": EMAIL})
        code = outbox.codes_in(outbox.last_for(EMAIL))
        signed_in = await api.post(url("/auth/verify-otp"), json={"email": EMAIL, "code": code})
        headers = {"Authorization": f"Bearer {body_of(signed_in)['access_token']}"}

        response = await api.post(
            url("/me/change-password"), json={"new_password": PASSWORD}, headers=headers
        )
        assert response.status_code == 200
        assert body_of(response)["has_password"] is True
        assert (await login(api)).status_code == 200


class TestDpdp:
    """docs/11 §Compliance: "DPDP Act: consent record, data export and deletion endpoints"."""

    async def _token(self, api: httpx.AsyncClient) -> dict[str, str]:
        return {"Authorization": f"Bearer {body_of(await login(api))['access_token']}"}

    async def test_export_returns_everything_the_account_owns(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        del outbox
        await register(api)
        body = body_of(await api.get(url("/me/export"), headers=await self._token(api)))
        assert set(body) == {
            "exported_at",
            "profile",
            "screens",
            "consents",
            "subscriptions",
            "payments",
        }
        assert as_map(body["profile"])["email"] == EMAIL
        consents = body["consents"]
        assert isinstance(consents, list) and consents

    async def test_deleting_schedules_erasure_and_deactivates_immediately(
        self, api: httpx.AsyncClient, outbox: Outbox, screener_session: AsyncSession
    ) -> None:
        await register(api)
        headers = await self._token(api)
        response = await api.request("DELETE", url("/me"), json={"email": EMAIL}, headers=headers)
        assert response.status_code == 200
        assert body_of(response)["status"] == "scheduled"

        user = (
            await screener_session.execute(select(AppUser).where(AppUser.email == EMAIL))
        ).scalar_one()
        await screener_session.refresh(user)
        assert user.deleted_at is not None

        # Deactivated: the password no longer signs in.
        assert_problem(await login(api), 401, "unauthenticated")
        assert "deleted" in outbox.last_for(EMAIL).subject.lower()

    async def test_deleting_requires_the_address_back(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        del outbox
        await register(api)
        headers = await self._token(api)
        assert_problem(
            await api.request(
                "DELETE", url("/me"), json={"email": "someone.else@example.com"}, headers=headers
            ),
            400,
            "invalid-screen-definition",
        )

    async def test_signing_in_again_cancels_the_deletion(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        """Prompt 12 §5's soft-delete window is only soft if it can be undone."""
        await register(api)
        await api.request(
            "DELETE", url("/me"), json={"email": EMAIL}, headers=await self._token(api)
        )

        await api.post(url("/me/restore"), json={"email": EMAIL})
        assert (await login(api)).status_code == 200
