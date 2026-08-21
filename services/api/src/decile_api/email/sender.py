"""Delivery — docs/02 §Email ("Resend"), Prompt 12 §3 ("local delivery to mailpit").

Three transports behind one Protocol:

``resend``   the production path. Resend is an HTTPS API; the client is one POST.
``smtp``     local development and the browser suite, pointed at **mailpit** — which accepts
             SMTP on 1025 and serves what it caught on 8025, so a developer can read the OTP
             they were just sent instead of grepping a log.
``console``  the default when neither is configured. Prints the *subject and recipient* and the
             plain-text body to the logger. Refused in production by ``require_configured``.

**Failures are surfaced, never swallowed.** A `/auth/request-otp` that answers 202 while the mail
bounced is a support ticket that looks like a bug in the login page. :class:`EmailNotSent` is
raised, and the caller decides — which for the auth endpoints means logging the failure and still
returning the same neutral response, because *whether an address exists* must not be inferable
from whether the send succeeded.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Final, Protocol

import httpx

from decile_api.email.templates import Message
from decile_api.settings import Settings

log = logging.getLogger(__name__)

RESEND_ENDPOINT: Final = "https://api.resend.com/emails"
SEND_TIMEOUT_SECONDS: Final = 10.0


class EmailNotSent(RuntimeError):
    """The transport refused or failed. Never carries the message body."""


class Transport(Protocol):
    """What a delivery mechanism has to do. One method, so a test double is three lines."""

    async def send(self, message: Message) -> None: ...


def _mime(message: Message, sender: str, reply_to: str | None) -> EmailMessage:
    """A multipart/alternative with the text part first — the order the RFC gives precedence in.

    Prompt 12 §3: "plain-text alternatives included". A client that cannot render HTML shows the
    text part; a spam filter that sees only HTML scores the message worse.
    """
    mail = EmailMessage()
    mail["From"] = sender
    mail["To"] = message.to
    mail["Subject"] = message.subject
    if reply_to:
        mail["Reply-To"] = reply_to
    mail.set_content(message.text)
    mail.add_alternative(message.html, subtype="html")
    return mail


class ResendTransport:
    """docs/02: "Email — **Resend** (transactional)"."""

    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.resend_api_key
        self._from = settings.email_from
        self._reply_to = settings.email_reply_to

    async def send(self, message: Message) -> None:
        payload: dict[str, object] = {
            "from": self._from,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
            "html": message.html,
        }
        # The message's own `Reply-To` wins over the deployment-wide one: only the support form
        # sets it, and there it is the whole point (`templates.support_request`).
        reply_to = message.reply_to or self._reply_to
        if reply_to:
            payload["reply_to"] = reply_to
        try:
            async with httpx.AsyncClient(timeout=SEND_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    RESEND_ENDPOINT,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except httpx.HTTPError as exc:
            raise EmailNotSent(f"Resend request failed: {type(exc).__name__}") from exc
        if response.status_code >= httpx.codes.BAD_REQUEST:
            # The status, not the body: a provider error body can echo the recipient.
            raise EmailNotSent(f"Resend answered {response.status_code}")


class SmtpTransport:
    """Local delivery to mailpit (Prompt 12 §3), and any other plain SMTP relay.

    ``smtplib`` is blocking, so the send runs in a worker thread. The alternative is another
    dependency for an operation that happens a handful of times per user, ever.
    """

    def __init__(self, settings: Settings) -> None:
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._from = settings.email_from
        self._reply_to = settings.email_reply_to

    def _send_blocking(self, mail: EmailMessage) -> None:
        with smtplib.SMTP(self._host, self._port, timeout=SEND_TIMEOUT_SECONDS) as client:
            client.send_message(mail)

    async def send(self, message: Message) -> None:
        mail = _mime(message, self._from, message.reply_to or self._reply_to)
        try:
            await asyncio.to_thread(self._send_blocking, mail)
        except (OSError, smtplib.SMTPException) as exc:
            raise EmailNotSent(f"SMTP delivery failed: {type(exc).__name__}") from exc


class ConsoleTransport:
    """The no-configuration default. Logs the message so a developer can act on it.

    This is the one place a code or a link is deliberately written to a log, and it is only
    reachable when ``email_transport`` is ``console`` — which ``Settings.require_configured``
    refuses in production. The redaction filter in `decile_api.logging` leaves it alone precisely
    because a development inbox has to be readable; `docs/12a` §7 records the exemption.
    """

    async def send(self, message: Message) -> None:
        log.warning(
            "email not delivered (transport=console)",
            extra={"email_to": message.to, "email_subject": message.subject},
        )
        log.info("email body\n%s", message.text)


def build_transport(settings: Settings) -> Transport:
    if settings.email_transport == "resend":
        return ResendTransport(settings)
    if settings.email_transport == "smtp":
        return SmtpTransport(settings)
    return ConsoleTransport()


class Mailer:
    """What the routers depend on. Holds the transport and never raises at the call site.

    :meth:`deliver` reports failure by returning ``False``, because every caller's correct
    response to a failed send is the same: log it, and answer the request as if nothing had gone
    wrong. An auth endpoint whose response changes when the mail bounces tells a stranger whether
    the address is registered.
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    async def deliver(self, message: Message) -> bool:
        try:
            await self._transport.send(message)
        except EmailNotSent as exc:
            log.error(
                "email delivery failed",
                extra={"email_to": message.to, "reason": str(exc)},
            )
            return False
        return True
