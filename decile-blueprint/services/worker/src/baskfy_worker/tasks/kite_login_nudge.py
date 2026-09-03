"""SW18 — the one-tap morning Kite login (Maulik, 3 Sep 2026).

Kite invalidates an access token at the start of every trading day, so somebody has to log in
before 09:15 or the swing monitor starts at 09:14 with no session and idles. Nothing on this
box can do that login by itself, and nothing on this box is going to try: the alternative —
storing the Zerodha password and a TOTP seed on a server so a job can type them — puts the
whole account one file read away from anyone who reaches the volume, and is against Zerodha's
terms besides. ``docs/swing/DECISIONS-SW.md`` SW18.1.

So the job does the only part a machine can do. At 08:45 IST it asks whether a usable token
exists for today; if not, it sends **one** message carrying a Kite login link. Maulik taps it
and logs in on Zerodha's own page; the callback that already exists
(``GET /api/v1/brokers/callback``) validates the ``state``, exchanges the ``request_token`` and
writes the encrypted blob. A second check at 09:05 sends the second and last message if the
first went unanswered.

The link is built by :func:`baskfy_api.broker_oauth.kite_login_url` — the *same* builder
``POST /brokers/{id}/connect`` uses, so the ``state`` this task mints is one the callback's
own check accepts. It is bound to a user id, not to a web session, which is what makes an
offline minter possible at all; the caller passes ``BASKFY_SOLE_USER_ID``'s tenant, and
``BASKFY_BROKER_OAUTH_STATE_PATH`` (a file on the shared state volume) is what carries the
pending state from this process to the API's.

What this module is careful about
---------------------------------
* **One message per session date per window.** The marker is an ``O_EXCL`` file beside the
  token blob, so two workers racing the same Beat tick produce one email, not two. It is
  claimed *before* the send: a mail that fails is a note and the 09:05 window is the retry,
  which beats a task that re-mails on every redelivery.
* **A holiday is not a morning.** Beat already restricts the entries to ``mon-fri``; the NSE
  calendar is what rules out Diwali. No session, no nudge.
* **Fail soft, always.** A missing recipient, an unbuildable URL, a mailer that raises — each
  is a note. This function never raises: an alarm clock that can crash the worker is worse
  than no alarm clock.
* **Nothing secret is rendered or logged.** The message carries a login URL — whose ``api_key``
  is a public app identifier — and never the API secret, the access token or the Fernet key.
  ``tests/test_kite_login_nudge.py`` asserts that over the rendered text. The recipient address
  is not logged either, not even its domain.

This module places nothing and reads no market data. Law 2 is untouched, and there is no reply
path: STANDING-ANSWERS A2's notifier rule holds — a tap starts a *login*, and the only thing a
completed login does is store a token.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from html import escape
from pathlib import Path
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.broker_oauth import KiteLoginUrl, is_simulated_token
from baskfy_api.email import Mailer, Message, build_transport
from baskfy_api.settings import Settings, get_settings
from baskfy_api.swing_health import IST, is_session_day
from baskfy_core.models.base import JsonObject
from baskfy_providers.errors import ProviderError
from baskfy_providers.tokens import AccessTokenStore
from baskfy_worker.steps import StepOutcome

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_ACCOUNT",
    "MARKER_PREFIX",
    "SUBJECT",
    "NudgeReport",
    "NudgeWindow",
    "marker_path",
    "render_nudge",
    "run_login_nudge",
]

#: The subject line, identical for both windows so the two land in one thread on a phone.
SUBJECT: Final = "Kite login needed before 09:15"

#: Marker files: ``kite-login-nudge-2026-09-03.first``, beside the token blob.
MARKER_PREFIX: Final = "kite-login-nudge"

#: What the login is for, in the message. A label — never a client id or an account number.
DEFAULT_ACCOUNT: Final = "Zerodha — Baskfy's Kite Connect app"

#: Monday..Friday in ``datetime.weekday()`` terms.
_SATURDAY: Final = 5


class NudgeWindow(StrEnum):
    """Which of the morning's two checks this run is.

    ``FIRST`` at 08:45 — half an hour of slack before the pre-open matters. ``SECOND`` at
    09:05, ten minutes before the open, and it says so: there is no third.
    """

    FIRST = "first"
    SECOND = "second"

    @property
    def is_last(self) -> bool:
        return self is NudgeWindow.SECOND

    @property
    def clock(self) -> str:
        return "09:05" if self.is_last else "08:45"


@dataclass(slots=True)
class NudgeReport:
    """What one run of the check found and did.

    ``notes`` is the whole failure vocabulary — this function returns rather than raises, so a
    caller that wants to know *why* nothing was sent reads them. They are written for a human,
    and never carry an address, a token, or a path to a secret.
    """

    window: NudgeWindow
    date: dt.date
    sent: bool = False
    notes: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def as_detail(self) -> JsonObject:
        return {
            "window": self.window.value,
            "date": self.date.isoformat(),
            "sent": self.sent,
            "notes": list(self.notes),
        }


def marker_path(state_dir: Path, day: dt.date, window: NudgeWindow) -> Path:
    """The file whose existence means "this window has already been used today"."""
    return state_dir / f"{MARKER_PREFIX}-{day.isoformat()}.{window.value}"


def _claim(state_dir: Path, day: dt.date, window: NudgeWindow) -> bool:
    """Take the window for ``day``, atomically. ``False`` if somebody already holds it.

    ``O_EXCL`` rather than "exists? then write": two compute workers pick up the same Beat tick
    often enough (a redelivery, a second replica) that check-then-act would send two identical
    emails on the morning the reader is least inclined to read either carefully.
    """
    path = marker_path(state_dir, day, window)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            handle.write(f"{day.isoformat()} {window.value}\n")
    except FileExistsError:
        return False
    except OSError as exc:
        # An unwritable state directory must not silence the morning. Two mails beat none, and
        # this is the only branch that can produce two.
        log.warning(
            "could not write the login-nudge marker (%s); sending anyway", type(exc).__name__
        )
    return True


def _token_state(store: AccessTokenStore, *, now: dt.datetime) -> str | None:
    """``None`` when a usable session for today is already stored; else why it is not.

    :meth:`AccessTokenStore.require_fresh` is the semantics, verbatim: Kite kills a token at
    the start of the next trading day, so "fresh" means *issued on today's IST date*. One
    written at 08:00 this morning is fresh; last night's is not, however few hours old it is.
    A ``sim_`` token is not a session at all — it is what a deployment that cannot complete a
    login writes to prove the flow ran — so it counts as absent here.

    Reasons are written by hand rather than lifted from the exception text: the store's own
    messages name the blob's path and the encryption-key variable, and neither belongs in an
    email.
    """
    try:
        token = store.require_fresh(now=now)
    except ProviderError:
        # `CredentialsMissing` (no file / no key / key rotated), `AccessTokenExpired` and
        # `UnexpectedPayload` share this base and mean one thing to this job: there is no
        # session Kite would accept this morning. Caught by the base rather than bare, so a
        # genuine bug in the store still reaches the caller (house rule 3).
        return "no usable Kite session is stored"
    if is_simulated_token(token.value):
        return "the only stored session is a simulated one, which Kite will not accept"
    return None


def render_nudge(  # noqa: PLR0913 - every fact the message states, named
    login: KiteLoginUrl,
    *,
    to: str,
    day: dt.date,
    window: NudgeWindow,
    account: str,
    reason: str,
) -> Message:
    """The message itself. One link, one sentence of consequence, no doubt about the sender.

    Written to survive being read on a phone at 08:45 by someone half awake and — just as
    important — to not read like the phishing mail it structurally resembles. An unexpected
    email carrying a broker login link is exactly the shape of an attack, so the first lines
    say who sent it and what it does not hold: no password, no 2FA seed, and the link opens
    Zerodha's own page rather than one of ours.
    """
    opening = (
        "Second and last reminder — Baskfy's own box is asking, not Zerodha."
        if window.is_last
        else "Baskfy's own box is asking, not Zerodha."
    )
    closing = (
        "There is no further reminder today."
        if window.is_last
        else "If it has expired by the time you tap it, the 09:05 check sends a fresh one."
    )
    lines = [
        opening,
        "",
        f"Sent by the Baskfy worker on your own box at the {window.clock} check. It holds no "
        "Zerodha password and no 2FA seed — it cannot log in for you, which is the point. The "
        "link below opens Zerodha's own login page.",
        "",
        f"For {day:%A %d %B %Y}: {reason}.",
        "",
        "Open this and log in:",
        "",
        f"  {login.url}",
        "",
        f"Account: {account}.",
        "",
        "If Baskfy asks you to sign in on the way back from Zerodha, that is the last "
        "step and not a failure — the session is stored once it finishes.",
        "",
        "If you ignore this: the swing monitor starts at 09:14 with no session and idles, no "
        "plan line can be sent, and no order is possible today. Nothing else on the box "
        "breaks, and nothing is placed on your behalf either way.",
        "",
        f"The link can be used once and is good for {login.valid_for_minutes} minutes. {closing}",
        "",
    ]
    prose = [line for line in lines if line and not line.startswith("  ")]
    html = (
        "".join(f"<p>{escape(line)}</p>" for line in prose[:5])
        + f'<p><a href="{escape(login.url, quote=True)}">Log in to Kite on Zerodha\'s site</a></p>'
        + "".join(f"<p>{escape(line)}</p>" for line in prose[5:])
    )
    return Message(to=to, subject=SUBJECT, text="\n".join(lines), html=html)


async def run_login_nudge(  # noqa: PLR0913, PLR0911 - the seams and the settings, named;
    # and one early return per reason to stay quiet, which is the shape a reader wants here
    session: AsyncSession,
    outcome: StepOutcome,
    *,
    now: dt.datetime,
    token_store: AccessTokenStore,
    mailer: Mailer | None = None,
    login_url_for: Callable[[NudgeWindow], KiteLoginUrl],
    to: str,
    state_dir: Path,
    window: NudgeWindow = NudgeWindow.FIRST,
    account: str = DEFAULT_ACCOUNT,
    settings: Settings | None = None,
) -> NudgeReport:
    """Check for today's Kite session and, finding none, send one login link.

    Returns a :class:`NudgeReport` in every branch and raises in none of them. The checks are
    in order of cost: configuration, then the calendar (one indexed row), then the token blob
    (a file read and a Fernet decrypt), then the marker, and only then a URL and a send.

    ``login_url_for`` takes the window so the caller mints a **fresh** one-time state per
    message rather than reusing the 08:45 one at 09:05 — a state expires 30 minutes after it
    is minted, and the 09:05 link has to still work at 09:20.
    """
    day = now.astimezone(IST).date()
    report = NudgeReport(window=window, date=day)

    def done() -> NudgeReport:
        outcome.note(login_nudge=report.as_detail())
        return report

    if not to:
        # Never the address, never its domain — the variable's name is enough to act on.
        report.note("BASKFY_KITE_LOGIN_NUDGE_TO is not set; nothing was sent")
        log.info("login nudge: no recipient configured")
        return done()

    if now.astimezone(IST).weekday() >= _SATURDAY:
        report.note("not a weekday")
        return done()
    if not await is_session_day(session, day):
        report.note("an NSE holiday; there is no session to log in for")
        return done()

    reason = _token_state(token_store, now=now)
    if reason is None:
        report.note("a Kite session issued today is already stored; no message sent")
        return done()

    if not _claim(state_dir, day, window):
        report.note(f"the {window.value} window has already been used today")
        return done()

    try:
        login = login_url_for(window)
    except Exception as exc:
        # Building the URL needs an api key and somewhere to record the state; a box missing
        # either says so in the task result rather than crashing the beat worker at 08:45.
        report.note(f"the login URL could not be built ({type(exc).__name__})")
        log.error("login nudge: could not build the login URL", extra={"error": type(exc).__name__})
        return done()

    message = render_nudge(login, to=to, day=day, window=window, account=account, reason=reason)
    try:
        transport = mailer or Mailer(build_transport(settings or get_settings()))
        report.sent = await transport.deliver(message)
    except Exception as exc:
        # `Mailer.deliver` reports failure by returning False, but `build_transport` and a
        # transport constructed elsewhere can both raise, and a sink never propagates
        # (`baskfy_worker.alerts`, same rule).
        report.note(f"the message could not be sent ({type(exc).__name__})")
        log.error("login nudge: send failed", extra={"error": type(exc).__name__})
        return done()

    report.note(
        f"{window.value} login link sent" if report.sent else "the mailer refused the message"
    )
    return done()
