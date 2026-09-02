"""The swing book's notifier: a TRIGGERED signal for a daily-focus name, told once, one way.

SW11, docs/swing/STANDING-ANSWERS A2: "Email via the existing mailer now, one-way, carrying the
whole line (symbol, entry, stop, qty, rupee risk, plan expiry). Scaffold a Telegram sender dark
behind BASKFY_SWING_TELEGRAM_BOT_TOKEN / _CHAT_ID. Never a confirm path — a reply or tap must
not place an order."

WHAT THIS MODULE CAN AND CANNOT DO
----------------------------------
It can compose a message and hand it to an SMTP relay or to Telegram's ``sendMessage``. That is
the whole surface. It imports nothing from the execution side of the desk, names no route that
acts, holds no reference to a broker client, and reads nothing back from either channel: the
Telegram sender has no update polling, no command handler and no webhook, so a message typed at
the bot goes nowhere. ``tests/test_swing_notify.py`` scans this file's code for the words that
would let it act and fails the build if one appears.

The desk does not import the data plant's mailer (``baskfy_api.email``): the desk process
cannot take a dependency on the API package, so the email is a stdlib ``smtplib`` sender with
its own ``DESK_SMTP_*`` knobs in ``app/config.py``. Same relay, same inbox; a different process.

FAIL SOFT, ALWAYS
-----------------
A notifier that raises into the monitor is a notifier that can stop the morning. Every sender
returns ``True``/``False`` and logs; :meth:`Notifier.notify` never raises. A channel that is
not configured is not an error — it is a laptop — and the monitor says so once at start-up.

WHO IS TOLD
-----------
Only the daily focus (A14): the top ``focus_top_n`` flags by score plus every EP carry
``sw_watch.focus = true``, and only their signals are pushed. The other watched names still
raise signal rows and plan lines and are shown below the fold; they are never pushed.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from email.message import EmailMessage
from typing import Protocol

from . import config as C

log = logging.getLogger("swing_notify")

#: How long a channel is given. The monitor's tick loop waits on this; a relay that hangs for
#: thirty seconds would hold the next verdict behind it.
SEND_TIMEOUT_SECONDS = 5.0

#: Telegram's Bot API: the one method this module knows. There is no `getUpdates` here and
#: there must never be — reading messages back is the beginning of a reply path.
TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


@dataclass(frozen=True)
class SignalNotice:
    """What one TRIGGERED signal is told as — the whole line, or the skip and its reason."""

    symbol: str
    setup: str
    at: dt.datetime
    entry: Decimal | None
    stop: Decimal | None
    quantity: int | None
    risk_inr: Decimal | None
    plan_expires_at: dt.datetime | None
    #: When the rules lined nothing: the skip reason and its detail (`04` §9.1).
    skipped: str | None = None
    mode: str = "DRY_RUN"

    def subject(self) -> str:
        if self.skipped:
            return f"[swing] {self.symbol} triggered at {self.at:%H:%M} — skipped ({self.skipped})"
        return f"[swing] {self.symbol} triggered at {self.at:%H:%M} — {self.mode}"

    def text(self) -> str:
        lines = [
            f"{self.symbol} ({self.setup}) broke its opening range at {self.at:%H:%M} IST.",
        ]
        if self.skipped:
            lines.append(f"No line: {self.skipped}.")
        else:
            lines.extend(
                [
                    f"entry   {self.entry}",
                    f"stop    {self.stop}",
                    f"qty     {self.quantity}",
                    f"risk    ₹{self.risk_inr:,.2f}" if self.risk_inr is not None else "risk    -",
                    (
                        f"expires {self.plan_expires_at:%H:%M} IST"
                        if self.plan_expires_at is not None
                        else "expires -"
                    ),
                    f"mode    {self.mode}",
                ]
            )
        lines.append("This message tells; it cannot act. Open the desk page.")
        return "\n".join(lines)


class Sender(Protocol):
    name: str

    def send(self, subject: str, text: str) -> bool: ...


class EmailSender:
    """One message through ``DESK_SMTP_HOST``. Returns whether the relay accepted it."""

    name = "email"

    def __init__(  # noqa: PLR0913 - the relay's knobs, named
        self,
        *,
        host: str,
        port: int,
        user: str = "",
        password: str = "",
        starttls: bool = True,
        sender: str,
        to: str,
        timeout: float = SEND_TIMEOUT_SECONDS,
        smtp: Callable[..., smtplib.SMTP] = smtplib.SMTP,
    ) -> None:
        self.host, self.port, self.user, self.password = host, port, user, password
        self.starttls, self.sender, self.to, self.timeout = starttls, sender, to, timeout
        self._smtp = smtp

    def send(self, subject: str, text: str) -> bool:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.sender, self.to, subject
        message.set_content(text)
        try:
            with self._smtp(self.host, self.port, timeout=self.timeout) as relay:
                if self.starttls:
                    relay.starttls()
                if self.user:
                    relay.login(self.user, self.password)
                relay.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            log.warning("swing email not sent (%s): %s", subject, exc)
            return False
        return True


class TelegramSender:
    """One ``sendMessage`` to one chat. One-way: this class has no method that reads."""

    name = "telegram"

    def __init__(
        self,
        *,
        token: str,
        chat_id: str,
        timeout: float = SEND_TIMEOUT_SECONDS,
        opener: Callable[..., object] = urllib.request.urlopen,
    ) -> None:
        if not token or not chat_id:
            raise ValueError("a Telegram sender needs both a bot token and a chat id")
        self._url = TELEGRAM_SEND_URL.format(token=token)
        self.chat_id = chat_id
        self.timeout = timeout
        self._open = opener

    def send(self, subject: str, text: str) -> bool:
        body = urllib.parse.urlencode({"chat_id": self.chat_id, "text": f"{subject}\n{text}"})
        request = urllib.request.Request(  # noqa: S310 - the host is Telegram's, fixed above
            self._url, data=body.encode("utf-8"), method="POST"
        )
        try:
            with self._open(request, timeout=self.timeout):
                pass
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("swing telegram not sent (%s): %s", subject, exc)
            return False
        return True


def build_senders() -> list[Sender]:
    """The channels ``app/config.py`` configures — possibly none.

    Email needs a host, a from and a to. Telegram is constructed only when BOTH the token and
    the chat id are set; one without the other constructs nothing, deliberately, so a token
    pasted into the wrong environment cannot start sending on its own.
    """
    senders: list[Sender] = []
    if C.DESK_SMTP_HOST and C.DESK_NOTIFY_FROM and C.DESK_NOTIFY_TO:
        senders.append(
            EmailSender(
                host=C.DESK_SMTP_HOST, port=int(C.DESK_SMTP_PORT), user=C.DESK_SMTP_USER,
                password=C.DESK_SMTP_PASSWORD, starttls=bool(C.DESK_SMTP_STARTTLS),
                sender=C.DESK_NOTIFY_FROM, to=C.DESK_NOTIFY_TO,
            )
        )
    if C.SWING_TELEGRAM_BOT_TOKEN and C.SWING_TELEGRAM_CHAT_ID:
        senders.append(
            TelegramSender(token=C.SWING_TELEGRAM_BOT_TOKEN, chat_id=C.SWING_TELEGRAM_CHAT_ID)
        )
    return senders


class Notifier:
    """Fan one notice out to every configured channel. Never raises."""

    def __init__(
        self,
        senders: list[Sender] | None = None,
        *,
        observe: Callable[[str, str], None] | None = None,
    ) -> None:
        self.senders = list(senders) if senders is not None else build_senders()
        self.sent: list[tuple[str, str, bool]] = []
        self._observe = observe
        if not self.senders:
            log.info("swing notifier: no channel configured (DESK_SMTP_HOST / Telegram); "
                     "signals are the row, the page and this log")

    @property
    def channels(self) -> list[str]:
        return [s.name for s in self.senders]

    def notify(self, notice: SignalNotice) -> int:
        """Send to every channel; returns how many accepted it. A channel that raises is
        counted as not sent and logged — the monitor's next verdict is not its concern."""
        delivered = 0
        subject, text = notice.subject(), notice.text()
        for sender in self.senders:
            try:
                ok = bool(sender.send(subject, text))
            except Exception as exc:  # noqa: BLE001 - fail soft: a channel never stops the morning
                log.warning("swing notifier %s raised: %s", sender.name, exc)
                ok = False
            self.sent.append((sender.name, subject, ok))
            delivered += int(ok)
            if self._observe is not None:
                try:
                    self._observe(sender.name, "sent" if ok else "failed")
                except Exception as exc:  # noqa: BLE001 - telemetry never stops a notice
                    log.debug("notifier sink failed: %s", exc)
        log.info("swing notice %s: %d of %d channels", subject, delivered, len(self.senders))
        return delivered


def notice_json(notice: SignalNotice) -> str:
    """The notice as one JSON line — what the log carries when no channel is configured."""
    return json.dumps(
        {
            "symbol": notice.symbol, "setup": notice.setup, "at": notice.at.isoformat(),
            "entry": None if notice.entry is None else str(notice.entry),
            "stop": None if notice.stop is None else str(notice.stop),
            "quantity": notice.quantity,
            "risk_inr": None if notice.risk_inr is None else str(notice.risk_inr),
            "plan_expires_at": (
                None if notice.plan_expires_at is None else notice.plan_expires_at.isoformat()
            ),
            "skipped": notice.skipped, "mode": notice.mode,
        }
    )
