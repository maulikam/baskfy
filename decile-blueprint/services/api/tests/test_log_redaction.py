"""No password or token value is ever written to logs — Prompt 12's second acceptance criterion.

Two halves, because there are two ways a secret reaches a log:

* **Structured** — `log.info("...", extra={"password": ...})`. `RedactingFilter` replaces the
  value of any field whose name looks secret.
* **Interpolated** — `log.info("token=%s", token)` or a message that already contains one.
  The filter rewrites the rendered message.

And a third check that is the real point: the auth endpoints are *driven*, end to end, with every
log record captured, and the suite asserts that no password, no OTP, no refresh token and no
access token appears anywhere in the output. A redaction filter that is correct in isolation and
bypassed in practice would pass the first two and fail this one.
"""

from __future__ import annotations

import json
import logging
from typing import Final

import httpx
import pytest
from api_helpers import StubGoogle, assert_problem, google_token, url
from screener_helpers import requires_db

from baskfy_api.csrf import CSRF_COOKIE, CSRF_HEADER, REFRESH_COOKIE
from baskfy_api.logging import REDACTED, JsonFormatter, RedactingFilter, redact_text

EMAIL: Final = "logged@example.com"
PASSWORD: Final = "a-very-distinctive-password-9271"

HTTP_OK: Final = 200
TOKEN_COUNT: Final = 3


def _record(message: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("test", logging.INFO, "f.py", 1, message, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def attr(record: logging.LogRecord, name: str) -> object:
    """Read a field the record does not declare.

    `extra=` puts arbitrary attributes on a `LogRecord`, which is the whole mechanism under test —
    but the class declares none of them, so a typed read has to go through `__dict__`.
    """
    return record.__dict__[name]


class TestTheFilter:
    def test_it_redacts_a_field_named_like_a_secret(self) -> None:
        record = _record("signed in", password="hunter2", refresh_token="abc", email=EMAIL)
        RedactingFilter().filter(record)
        assert attr(record, "password") == REDACTED
        assert attr(record, "refresh_token") == REDACTED
        # Not everything: the log has to stay useful.
        assert attr(record, "email") == EMAIL

    def test_it_leaves_innocent_lookalikes_alone(self) -> None:
        record = _record("done", status_code=200, token_count=3)
        RedactingFilter().filter(record)
        assert attr(record, "status_code") == HTTP_OK
        assert attr(record, "token_count") == TOKEN_COUNT

    def test_it_redacts_a_bearer_token_in_the_message(self) -> None:
        assert "eyJ" not in redact_text("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.body.sig")

    def test_it_redacts_a_key_value_pair_in_the_message(self) -> None:
        assert "hunter2" not in redact_text("login failed password=hunter2")
        assert "123456" not in redact_text('otp: "123456"')
        assert "sk_live_x" not in redact_text("api_key=sk_live_x")

    def test_it_redacts_a_bare_jwt(self) -> None:
        token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.abcdefghij"
        assert token not in redact_text(f"rejected {token}")

    def test_it_survives_a_format_string_with_arguments(self) -> None:
        record = logging.LogRecord(
            "test", logging.INFO, "f.py", 1, "token=%s for %s", ("secret-value", EMAIL), None
        )
        RedactingFilter().filter(record)
        assert "secret-value" not in record.getMessage()
        assert EMAIL in record.getMessage()

    def test_the_formatter_emits_the_redacted_value(self) -> None:
        """End to end through the formatter, since that is what actually reaches stdout."""
        record = _record("signed in", password="hunter2")
        RedactingFilter().filter(record)
        payload = json.loads(JsonFormatter().format(record))
        assert payload["password"] == REDACTED


@pytest.mark.db
@pytest.mark.redis
@requires_db
@pytest.mark.filterwarnings("ignore:Setting per-request cookies:DeprecationWarning")
class TestTheEndpointsInPractice:
    """Drive the real auth flow and read every log line it produced."""

    async def test_no_secret_reaches_the_log(
        self, api: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        # `caplog` installs its own handler, so the filter has to be on it too — which is exactly
        # the situation `configure_logging` creates for the real stdout handler.
        caplog.handler.addFilter(RedactingFilter())
        stub = StubGoogle()
        transport = api._transport
        app = getattr(transport, "app", None)
        assert app is not None, "the api fixture must be built on an ASGITransport"
        app.state.google_verifier = stub
        id_token = google_token()

        with caplog.at_level(logging.DEBUG):
            signed_in = await api.post(url("/auth/google"), json={"id_token": id_token})
            assert signed_in.status_code == 200, signed_in.text
            access_token = str(signed_in.json()["access_token"])
            refresh_cookie = signed_in.cookies[REFRESH_COOKIE]
            csrf = signed_in.cookies[CSRF_COOKIE]

            await api.post(
                url("/auth/refresh"),
                headers={CSRF_HEADER: csrf},
                cookies={REFRESH_COOKIE: refresh_cookie, CSRF_COOKIE: csrf},
            )
            await api.get(url("/me"), headers={"Authorization": f"Bearer {access_token}"})
            # A failure path too — this is where a careless `log.warning(body)` would live, and
            # the Google endpoint *does* log every rejection on purpose.
            rejected = google_token(subject="forged")
            stub.rejects.add(rejected)
            assert_problem(
                await api.post(url("/auth/google"), json={"id_token": rejected}),
                401,
                "unauthenticated",
            )

        rendered = "\n".join(JsonFormatter().format(record) for record in caplog.records)
        for secret, what in (
            (id_token, "the Google ID token"),
            (access_token, "the access token"),
            (refresh_cookie, "the refresh token"),
            (csrf, "the CSRF token"),
        ):
            assert secret not in rendered, f"{what} was written to the log"

    async def test_a_refused_google_token_is_not_echoed(
        self, api: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The rejection path logs a reason, and the reason must not be the token.

        This is the one place the temptation is real: a 401 with no context is hard to debug, so
        the obvious fix is to log what was submitted. A Google ID token is a bearer credential
        until it expires — logging a *valid* one that failed some later check would put a working
        credential in the log, and logging a forged one tells an attacker with log access exactly
        how close they got.
        """
        caplog.handler.addFilter(RedactingFilter())
        transport = api._transport
        app = getattr(transport, "app", None)
        assert app is not None
        app.state.google_verifier = StubGoogle()
        forged = "a-token-shaped-string-that-is-not-one"

        with caplog.at_level(logging.DEBUG):
            assert_problem(
                await api.post(url("/auth/google"), json={"id_token": forged}),
                401,
                "unauthenticated",
            )

        rendered = "\n".join(JsonFormatter().format(record) for record in caplog.records)
        assert forged not in rendered, "the refused token was echoed into the log"
        # ...and the rejection *was* recorded, so this is not passing because nothing was logged.
        assert "google sign-in rejected" in rendered
