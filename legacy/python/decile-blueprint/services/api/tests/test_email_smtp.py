"""The SMTP transport, against mailpit's shape and against SES's.

One class serves two relays that agree on almost nothing. Mailpit accepts a plaintext,
unauthenticated session; Amazon SES — `docs/08` §2's sender — refuses both. The settings that
bridge them default to mailpit's shape, so the risk is not that SES breaks: it is that a later
change quietly makes the *local* path require TLS, or that credentials go out over a plaintext
session because a flag was missed.

These assert the spec on both sides. No socket is opened: `smtplib.SMTP` is replaced with a
recorder, because what matters is which protocol steps are issued and in what order.
"""

from __future__ import annotations

import smtplib
from types import TracebackType

import pytest

from baskfy_api.email.sender import SmtpTransport
from baskfy_api.email.templates import Message
from baskfy_api.settings import Settings

#: Every client the transport opened during one test. A module-level list rather than a mutable
#: class attribute, which ruff rightly refuses (RUF012) — a shared default on a class is a
#: cross-test leak waiting to happen, and the fixture below resets this one explicitly.
SENT_VIA: list[RecordingSMTP] = []


class RecordingSMTP:
    """Stands in for `smtplib.SMTP`, recording the sequence of protocol steps."""

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[str] = []
        self.login_args: tuple[str, str] | None = None
        self.sent: list[object] = []
        SENT_VIA.append(self)

    def __enter__(self) -> RecordingSMTP:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append("login")
        self.login_args = (user, password)

    def send_message(self, mail: object) -> None:
        self.calls.append("send_message")
        self.sent.append(mail)


@pytest.fixture(autouse=True)
def _recorder(monkeypatch: pytest.MonkeyPatch) -> None:
    SENT_VIA.clear()
    monkeypatch.setattr(smtplib, "SMTP", RecordingSMTP)


def message() -> Message:
    # Both bodies are always present — `Message`'s own docstring says so, and `_mime` calls
    # `add_alternative` unconditionally. A None here raises inside the stdlib, several frames from
    # anything that names the cause.
    return Message(
        to="reader@example.com",
        subject="Confirm your address",
        text="Confirm: https://staging.baskfy.com/verify?token=x",
        html="<p>Confirm</p>",
    )


async def send_via(settings: Settings) -> RecordingSMTP:
    """Send one message and hand back the client the transport opened.

    Takes a built `Settings` rather than `**kwargs`: a kwargs signature here could only be typed
    as `object`, and the construction then needed a type-checker suppression comment, which house
    rule 3 forbids and `test_no_escape_hatches.py` enforces by scanning the source. Passing the
    real type is not a way around the rule — it is what the rule was pointing at.
    """
    await SmtpTransport(settings).send(message())
    return SENT_VIA[-1]


class TestMailpit:
    """The local path. Every one of these passed before STARTTLS and AUTH existed, and must
    keep passing: adding a deployment's needs must not require a developer to run a TLS relay."""

    @staticmethod
    def settings() -> Settings:
        return Settings(email_transport="smtp", smtp_host="mailpit", smtp_port=1025)

    @pytest.mark.asyncio
    async def test_no_tls_and_no_login_by_default(self) -> None:
        client = await send_via(self.settings())
        assert "starttls" not in client.calls
        assert "login" not in client.calls
        assert client.calls[-1] == "send_message"

    @pytest.mark.asyncio
    async def test_reaches_the_configured_relay(self) -> None:
        client = await send_via(self.settings())
        assert (client.host, client.port) == ("mailpit", 1025)


class TestSes:
    """The deployment path."""

    @staticmethod
    def settings() -> Settings:
        return Settings(
            email_transport="smtp",
            smtp_host="email-smtp.ap-south-1.amazonaws.com",
            smtp_port=587,
            smtp_username="AKIAEXAMPLE",
            smtp_password="smtp-derived-secret",
            smtp_starttls=True,
        )

    @pytest.mark.asyncio
    async def test_encrypts_before_authenticating(self) -> None:
        client = await send_via(self.settings())
        # The order is the protocol's, not a preference: LOGIN before STARTTLS would put the
        # relay credential on the wire in base64.
        assert client.calls.index("starttls") < client.calls.index("login")

    @pytest.mark.asyncio
    async def test_greets_again_after_starttls(self) -> None:
        client = await send_via(self.settings())
        # A server's advertised capabilities change once the session is encrypted, and AUTH
        # commonly appears only in the second EHLO. Skipping it works against some relays and
        # fails against others, which is the worst kind of bug to carry.
        assert client.calls[:3] == ["ehlo", "starttls", "ehlo"]

    @pytest.mark.asyncio
    async def test_sends_the_credentials_it_was_given(self) -> None:
        client = await send_via(self.settings())
        assert client.login_args == ("AKIAEXAMPLE", "smtp-derived-secret")

    @pytest.mark.asyncio
    async def test_still_delivers_the_message(self) -> None:
        client = await send_via(self.settings())
        assert client.calls[-1] == "send_message"
        assert len(client.sent) == 1


class TestConfigurationIsRefusedEarly:
    """A misconfigured relay must fail at boot, not as silence six hours later."""

    def test_username_without_password_is_refused(self) -> None:
        # SES answers 530 and the mail simply never arrives — the least diagnosable symptom
        # available, and indistinguishable from "the user mistyped their address".
        with pytest.raises(RuntimeError, match="must be set together"):
            Settings(email_transport="smtp", smtp_username="AKIAEXAMPLE")

    def test_password_without_username_is_refused(self) -> None:
        with pytest.raises(RuntimeError, match="must be set together"):
            Settings(email_transport="smtp", smtp_password="secret")

    def test_credentials_without_tls_are_refused(self) -> None:
        with pytest.raises(RuntimeError, match="clear"):
            Settings(
                email_transport="smtp",
                smtp_username="AKIAEXAMPLE",
                smtp_password="secret",
                smtp_starttls=False,
            )

    def test_mailpit_without_credentials_is_still_fine(self) -> None:
        settings = Settings(email_transport="smtp", smtp_host="mailpit", smtp_port=1025)
        assert settings.smtp_starttls is False
        assert settings.smtp_username == ""
