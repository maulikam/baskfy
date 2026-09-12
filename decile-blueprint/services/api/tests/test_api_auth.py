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

import api_helpers
import httpx
import pytest
from api_helpers import (
    STUB_GOOGLE_EMAIL,
    TEST_JWT_SECRET,
    StubGoogle,
    assert_problem,
    google_token,
    url,
)
from screener_helpers import requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.auth import encode_token
from baskfy_api.csrf import CSRF_COOKIE, CSRF_HEADER, REFRESH_COOKIE
from baskfy_api.email import Mailer
from baskfy_api.email.templates import Message
from baskfy_core.models import AppUser, AuthIdentity, ConsentRecord, RefreshToken

pytestmark = [
    pytest.mark.db,
    pytest.mark.redis,
    requires_db,
    # Per-request cookies are exactly what these tests need: the reuse-detection case has to
    # present a *specific* superseded token, which a client-level cookie jar would have replaced.
    pytest.mark.filterwarnings("ignore:Setting per-request cookies:DeprecationWarning"),
]

JsonMap = dict[str, object]

EMAIL: Final = STUB_GOOGLE_EMAIL


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


@pytest.fixture(autouse=True)
def google(api: httpx.AsyncClient) -> StubGoogle:
    """Swap the app's verifier for the double. Same seam as :func:`outbox`.

    ``autouse`` because every test in this module signs in, and a test that forgot to ask for the
    double would reach the *real* verifier, fail its JWKS fetch, and report a confusing 401
    instead of the thing it was actually asserting.
    """
    stub = StubGoogle()
    transport = api._transport
    app = getattr(transport, "app", None)
    assert app is not None, "the api fixture must be built on an ASGITransport"
    app.state.google_verifier = stub
    return stub


def _with_stub_google(client: httpx.AsyncClient) -> httpx.AsyncClient:
    """Install the Google double on an app this module built itself.

    The module-level `google` fixture is autouse, but it patches the shared `api` fixture's app.
    A test that stands up its OWN app via `api_helpers.running_app` — as the allowlist tests must,
    because they need non-default settings — does not get it, and reaches the real verifier
    instead. That is the exact trap the fixture's docstring warns about, and it bit both allowlist
    tests: one failed outright, and the other PASSED FOR THE WRONG REASON, expecting a 401 from
    the allowlist and getting one from a failed JWKS fetch.
    """
    # Private attribute on purpose: the fixture reaches for the same one.
    app = getattr(client._transport, "app", None)
    assert app is not None, "running_app must be built on an ASGITransport"
    app.state.google_verifier = StubGoogle()
    return client


async def sign_in(
    api: httpx.AsyncClient,
    *,
    subject: str = "google-sub-1",
    email: str = EMAIL,
    name: str = "",
) -> httpx.Response:
    """What `register` + `login` used to be, in one call — because it now is one call.

    A first sign-in creates the account and a later one signs into it; there is no separate
    registration step to perform first (`docs/DECISIONS-MERGE.md` M46).
    """
    return await api.post(
        url("/auth/google"), json={"id_token": google_token(subject, email, name)}
    )


class TestGoogleSignIn:
    """The only way in, so this is where the front door is tested.

    The *verification* half — signature, audience, issuer, `email_verified` — is
    `test_auth_google.py`, against real RSA. What is here is everything that happens once a token
    has been believed: which account it resolves to, what is created, and what is recorded.
    """

    async def test_a_first_sign_in_creates_the_account_and_a_session(
        self, api: httpx.AsyncClient, google: StubGoogle
    ) -> None:
        del google
        response = await sign_in(api, name="Asha Rao")
        assert response.status_code == 200, response.text

        body = body_of(response)
        assert body["email"] == EMAIL
        assert body["name"] == "Asha Rao"
        assert body["access_token"]
        # docs/11: the refresh token is a cookie and never the body.
        assert "refresh_token" not in body
        assert REFRESH_COOKIE in response.cookies
        assert CSRF_COOKIE in response.cookies

    async def test_the_address_is_verified_without_an_email_being_sent(
        self, api: httpx.AsyncClient, google: StubGoogle, outbox: Outbox
    ) -> None:
        """The whole point of the change.

        Verification used to mean a link in a mail this service sent — and when SES refused to
        deliver it, the account was unreachable. Google asserted the address before we ever saw
        it, so the account is verified on arrival and no mail is involved.
        """
        del google
        assert body_of(await sign_in(api))["email_verified"] is True
        assert outbox.messages == [], [m.subject for m in outbox.messages]

    async def test_signing_in_twice_reuses_the_account(
        self, api: httpx.AsyncClient, google: StubGoogle, screener_session: AsyncSession
    ) -> None:
        """House rule 7 applied to identity: re-running produces identical rows, not new ones."""
        del google
        first = body_of(await sign_in(api))
        second = body_of(await sign_in(api))
        assert first["public_id"] == second["public_id"]

        users = (
            (await screener_session.execute(select(AppUser).where(AppUser.email == EMAIL)))
            .scalars()
            .all()
        )
        assert len(users) == 1
        identities = (await screener_session.execute(select(AuthIdentity))).scalars().all()
        assert len(identities) == 1, "a second sign-in must not accumulate an identity row"

    async def test_the_subject_identifies_the_account_not_the_address(
        self, api: httpx.AsyncClient, google: StubGoogle, screener_session: AsyncSession
    ) -> None:
        """A Workspace rename must not create a second account.

        This is why `auth_identity` is keyed on Google's `sub` and not on the email: the address
        is a display fact that can change, and the subject is the identity that cannot.
        """
        del google
        first = body_of(await sign_in(api, subject="sub-stable", email=EMAIL))
        renamed = body_of(await sign_in(api, subject="sub-stable", email="asha@example.com"))
        assert renamed["public_id"] == first["public_id"]

        users = (await screener_session.execute(select(AppUser))).scalars().all()
        assert len(users) == 1, "the rename created a second account"

    async def test_a_different_subject_with_the_same_address_is_refused(
        self, api: httpx.AsyncClient, google: StubGoogle
    ) -> None:
        """The inverse, and the dangerous direction.

        Google does not hand one verified address to two subjects, so this should be unreachable.
        If it ever happens, adopting the account would let the second subject inherit the first
        one's holdings — so it refuses rather than guesses.
        """
        del google
        assert (await sign_in(api, subject="sub-one")).status_code == 200
        assert_problem(await sign_in(api, subject="sub-two"), 401, "unauthenticated")

    async def test_consent_is_recorded_once_at_the_first_sign_in(
        self, api: httpx.AsyncClient, google: StubGoogle, screener_session: AsyncSession
    ) -> None:
        """docs/11 §Compliance, DPDP.

        The mandatory checkbox is gone (`docs/DECISIONS-MERGE.md` M46.2) — the sign-in page states
        the agreement rather than gating on it. The *record* is not gone, and it must not
        accumulate a row per sign-in either: it says what was agreed to and when, once.
        """
        del google
        await sign_in(api)
        await sign_in(api)

        kinds = sorted(
            row.kind
            for row in (await screener_session.execute(select(ConsentRecord))).scalars().all()
        )
        assert kinds == ["privacy", "terms"], kinds

    async def test_a_token_google_did_not_sign_is_a_flat_401(
        self, api: httpx.AsyncClient, google: StubGoogle
    ) -> None:
        google.rejects.add(google_token())
        assert_problem(await sign_in(api), 401, "unauthenticated")

    async def test_the_failure_says_nothing_about_which_check_refused(
        self, api: httpx.AsyncClient, google: StubGoogle
    ) -> None:
        """A caller who learns *why* their forged token failed learns how to forge a better one."""
        google.rejects.add(google_token())
        refused = body_of(await sign_in(api))
        forged = body_of(
            await api.post(url("/auth/google"), json={"id_token": "not-even-the-right-shape"})
        )
        assert refused["detail"] == forged["detail"]

    async def test_no_account_is_created_by_a_refused_token(
        self, api: httpx.AsyncClient, google: StubGoogle, screener_session: AsyncSession
    ) -> None:
        google.rejects.add(google_token())
        await sign_in(api)
        users = (await screener_session.execute(select(AppUser))).scalars().all()
        assert users == []

    async def test_the_endpoint_accepts_no_identity_field_at_all(
        self, api: httpx.AsyncClient, google: StubGoogle
    ) -> None:
        """The security property the whole design rests on, enforced twice by the schema.

        `GoogleSignInIn` has one field, so there is nothing to send but the token — and because
        `_In` sets ``extra="forbid"``, an email smuggled alongside it is not quietly ignored, it
        is a 400. The weaker version of this endpoint takes the caller's word for who they are;
        this one cannot be asked the question at all.
        """
        del google
        refused = await api.post(
            url("/auth/google"),
            json={"id_token": google_token(email=EMAIL), "email": "attacker@example.com"},
        )
        assert_problem(refused, 400, "invalid-screen-definition")

        # ...and the token on its own still works, so the refusal is about the extra key.
        assert body_of(await sign_in(api))["email"] == EMAIL


class TestRefreshAndLogout:
    async def _sign_in(self, api: httpx.AsyncClient) -> tuple[str, str]:
        response = await sign_in(api)
        return response.cookies[REFRESH_COOKIE], response.cookies[CSRF_COOKIE]

    async def test_refresh_rotates_the_token(self, api: httpx.AsyncClient, outbox: Outbox) -> None:
        del outbox
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

    async def test_refresh_reuse_is_detected_and_revokes_the_family(
        self, api: httpx.AsyncClient, outbox: Outbox, screener_session: AsyncSession
    ) -> None:
        """RFC 9700 §4.14.2, which is what "rotating refresh" is for."""
        del outbox
        signed_in = await sign_in(api)
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
        public_id = str(body_of(await sign_in(api))["public_id"])

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
        public_id = str(body_of(await sign_in(api))["public_id"])
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


class TestMe:
    async def _token(self, api: httpx.AsyncClient) -> dict[str, str]:
        return {"Authorization": f"Bearer {body_of(await sign_in(api))['access_token']}"}

    async def test_it_carries_the_profile_and_the_entitlements(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        """docs/07: `GET /me` -> "profile + entitlements"."""
        del outbox
        await sign_in(api)
        body = body_of(await api.get(url("/me"), headers=await self._token(api)))
        assert body["email"] == EMAIL
        assert body["email_verified"] is True
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
        await sign_in(api)
        headers = await self._token(api)
        body = body_of(await api.patch(url("/me"), json={"name": "Renamed"}, headers=headers))
        assert body["name"] == "Renamed"


class TestDpdp:
    """docs/11 §Compliance: "DPDP Act: consent record, data export and deletion endpoints"."""

    async def _token(self, api: httpx.AsyncClient) -> dict[str, str]:
        return {"Authorization": f"Bearer {body_of(await sign_in(api))['access_token']}"}

    async def test_export_returns_everything_the_account_owns(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        del outbox
        await sign_in(api)
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
        """Prompt 12 §5: erasure is scheduled, and the live session ends now.

        This used to assert a second thing as well — that the *password* stopped signing in. It
        cannot any more, and not because the check was dropped: with one sign-in path, and that
        path being the one Prompt 12 §5 says cancels a pending deletion, signing in again is
        `test_signing_in_again_cancels_the_deletion` rather than a rejection. What "deactivates
        immediately" means here is therefore the session, which is what the epoch bump does and
        what the assertion below reads.
        """
        signed_in = body_of(await sign_in(api))
        before = signed_in["session_epoch"]
        headers = await self._token(api)
        response = await api.request("DELETE", url("/me"), json={"email": EMAIL}, headers=headers)
        assert response.status_code == 200
        assert body_of(response)["status"] == "scheduled"

        user = (
            await screener_session.execute(select(AppUser).where(AppUser.email == EMAIL))
        ).scalar_one()
        await screener_session.refresh(user)
        assert user.deleted_at is not None

        # Deactivated: every web session issued before this moment fails the generation check the
        # gated layout makes on its next navigation.
        assert isinstance(before, int)
        assert user.session_epoch > before
        assert "deleted" in outbox.last_for(EMAIL).subject.lower()

    async def test_deleting_requires_the_address_back(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        del outbox
        await sign_in(api)
        headers = await self._token(api)
        assert_problem(
            await api.request(
                "DELETE", url("/me"), json={"email": "someone.else@example.com"}, headers=headers
            ),
            400,
            "bad-request",
        )

    async def test_signing_in_again_cancels_the_deletion(
        self, api: httpx.AsyncClient, outbox: Outbox
    ) -> None:
        """Prompt 12 §5's soft-delete window is only soft if it can be undone."""
        await sign_in(api)
        await api.request(
            "DELETE", url("/me"), json={"email": EMAIL}, headers=await self._token(api)
        )

        mail = outbox.last_for(EMAIL)
        # AUDIT 2.4: restore requires the signed code from the deletion e-mail, not email alone.
        assert "this code: " in mail.text
        code = mail.text.rsplit("this code: ", 1)[1].strip().split()[0]
        bare = await api.post(url("/me/restore"), json={"email": EMAIL})
        assert bare.status_code in {400, 422}
        assert bare.status_code != 200
        assert_problem(
            await api.post(
                url("/me/restore"),
                json={"email": EMAIL, "code": "not-a-real-restore-code-xxxxx"},
            ),
            401,
            "unauthenticated",
        )

        restored = await api.post(url("/me/restore"), json={"email": EMAIL, "code": code})
        assert restored.status_code == 200
        assert (await sign_in(api)).status_code == 200

class TestSessionEpoch:
    """``app_user.session_epoch`` — the only thing that can end a *web* session.

    Why revoking refresh tokens is not enough, and never was: the web app has never held one. Its
    session is an Auth.js JWT cookie, it mints access tokens from the shared secret, and until
    AUDIT 0.8 / 2.2 ``current_principal`` checked only that ``sub`` named a row — so revoking rows
    nobody reads left the session that actually mattered alive for the rest of its thirty days
    (``NEEDS-MAULIK.md`` §22, now fixed: bearer carries ``epoch``, refused when stale). The epoch
    is what the web app compares against, so these assert the number, not the rows.

    **What bumps it changed with M46.** It used to be a password change or a reset, and both are
    gone. Account deletion and restore are what remain (``auth_service.revoke_all_for_user``), and
    they are what these now assert against — the mechanism is unchanged, only its trigger is.
    """

    async def test_a_fresh_account_starts_at_a_known_generation(
        self, api: httpx.AsyncClient
    ) -> None:
        """Zero, and stated rather than implied: every comparison downstream is against this."""
        await sign_in(api)
        assert body_of(await sign_in(api))["session_epoch"] == 0

    async def test_signing_in_reports_the_generation_the_session_must_carry(
        self, api: httpx.AsyncClient
    ) -> None:
        """On ``SessionOut`` so a sign-in learns it in one round trip, not two."""
        signed_in = await sign_in(api)
        headers = {"Authorization": f"Bearer {body_of(signed_in)['access_token']}"}

        me = body_of(await api.get(url("/me"), headers=headers))
        assert me["session_epoch"] == body_of(signed_in)["session_epoch"]

    async def test_deleting_the_account_moves_the_generation_on(
        self, api: httpx.AsyncClient, google: StubGoogle
    ) -> None:
        """The eviction path, now that a password change is not one.

        A session issued before the deletion must fail the comparison the gated layout makes, or
        somebody who asked to be erased keeps browsing their holdings for thirty days.
        """
        del google
        signed_in = body_of(await sign_in(api))
        before = signed_in["session_epoch"]
        headers = {"Authorization": f"Bearer {signed_in['access_token']}"}

        deleted = await api.request("DELETE", url("/me"), json={"email": EMAIL}, headers=headers)
        assert deleted.status_code == 200, deleted.text

        # AUDIT 0.8 / 2.2: the pre-deletion bearer is dead — epoch + deleted_at, not only the layout.
        assert_problem(await api.get(url("/me"), headers=headers), 401, "unauthenticated")

        # Signing in again cancels the deletion (Prompt 12 §5) and reports the new generation.
        after = body_of(await sign_in(api))["session_epoch"]
        assert isinstance(before, int) and isinstance(after, int)
        assert after > before, "a deletion must invalidate every cookie issued before it"

    async def test_the_generation_never_goes_backwards(
        self, api: httpx.AsyncClient, google: StubGoogle
    ) -> None:
        """It is a generation counter, not a count of anything.

        Reuse of a number would resurrect a session that was meant to die, so the only property
        that matters is monotonicity — asserted here rather than left to the ``+ 1``.
        """
        del google
        seen: list[int] = []
        for _ in range(3):
            signed_in = body_of(await sign_in(api))
            epoch = signed_in["session_epoch"]
            assert isinstance(epoch, int)
            seen.append(epoch)
            headers = {"Authorization": f"Bearer {signed_in['access_token']}"}
            await api.request("DELETE", url("/me"), json={"email": EMAIL}, headers=headers)

        assert seen == sorted(seen), seen
        assert len(set(seen)) == len(seen), f"a generation was reused: {seen}"


class TestTheLoginAllowlist:
    """M60: `BASKFY_LOGIN_ALLOWLIST` makes a deployment single-tenant by configuration.

    Authorisation, not authentication — Google has already proved the address by the time the
    list is consulted. What these pin is that an empty list stays open (so the setting cannot
    quietly lock an existing deployment out), that a barred address gets the *same* 401 as a
    forged token, and that being barred leaves no `app_user` row behind.
    """

    def test_an_empty_allowlist_permits_everyone(self) -> None:
        settings = api_helpers.api_settings("postgresql+asyncpg://x/y")
        assert settings.allowed_logins == frozenset()
        assert settings.login_permitted("anyone@example.com")

    def test_the_list_is_parsed_case_insensitively_and_trimmed(self) -> None:
        settings = api_helpers.api_settings(
            "postgresql+asyncpg://x/y",
            login_allowlist="  Owner@Example.COM , second@example.com ",
        )
        assert settings.allowed_logins == {"owner@example.com", "second@example.com"}
        assert settings.login_permitted("OWNER@example.com")
        assert settings.login_permitted("second@example.com")
        assert not settings.login_permitted("third@example.com")

    async def test_a_barred_address_is_refused_and_creates_no_account(
        self, seeded_url: str, screener_session: AsyncSession
    ) -> None:
        settings = api_helpers.api_settings(seeded_url, login_allowlist="owner@example.com")
        async with api_helpers.running_app(settings, screener_session) as client:
            response = await sign_in(_with_stub_google(client), email="stranger@example.com")
        assert response.status_code == 401
        # The same wording a forged token gets: the endpoint must not become an oracle for who
        # is on the list. That uniformity is also why this assertion alone is not enough — it
        # passed for a while against a real verifier failing its JWKS fetch, which is a 401 for
        # an entirely different reason. The account check below is what pins the cause.
        assert "could not be verified" in response.text
        rows = (
            (
                await screener_session.execute(
                    select(AppUser).where(AppUser.email == "stranger@example.com")
                )
            )
            .scalars()
            .all()
        )
        assert rows == []

    async def test_an_allowed_address_still_signs_in(
        self, seeded_url: str, screener_session: AsyncSession
    ) -> None:
        settings = api_helpers.api_settings(
            seeded_url, login_allowlist=f"{EMAIL},someone-else@example.com"
        )
        async with api_helpers.running_app(settings, screener_session) as client:
            response = await sign_in(_with_stub_google(client))
        assert response.status_code == 200, response.text
        assert body_of(response)["email"] == EMAIL
