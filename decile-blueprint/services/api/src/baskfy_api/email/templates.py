"""Every transactional email the product sends — Prompt 12 deliverable 3.

    "Transactional email via Resend with local delivery to mailpit: verification, OTP, password
     reset, lockout notice. **Templates in one place, plain-text alternatives included.**"

One module, four messages, each returning a :class:`Message` with both a ``text`` and an ``html``
body. The plain-text half is not a courtesy: a message with no text part is scored as spam by most
filters, and it is the version a screen reader and a terminal client actually render.

The HTML is deliberately plain — a table-free, inline-styled document that renders in Outlook and
in mutt. An email template system is a dependency and a maintenance surface; four messages are not
enough to justify either.

**No secret is ever logged from here.** The code or link is a parameter, it goes into the body,
and the body is handed to a transport. `baskfy_api.logging`'s redaction filter is the backstop.
"""

from __future__ import annotations

import datetime as dt
import html
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from baskfy_core.seed_data import PRODUCT_NAME

#: docs/11 §"Compliance & legal (India)" — the same sentence the UI's `<Disclaimer/>` carries.
#: An email that discusses a screener is an analytics surface too.
DISCLAIMER: Final = (
    f"{PRODUCT_NAME} is not a SEBI-registered investment adviser. Everything here is factual "
    "analysis of published market data, not investment advice."
)

FOOTER_TEXT: Final = (
    f"You are receiving this because someone used this address at {PRODUCT_NAME}. "
    "If it was not you, you can ignore this message."
)

#: Prompt 20 §3's alerts are the one message a recipient *subscribed* to, so the footer says how
#: to stop rather than "if it was not you, ignore this".
ALERT_FOOTER_TEXT: Final = (
    f"You are receiving this because you subscribed a {PRODUCT_NAME} screen to alerts. "
    "Every alert carries its own link to stop it."
)


@dataclass(frozen=True, slots=True)
class Message:
    """One email, addressed and rendered. Both bodies are always present."""

    to: str
    subject: str
    text: str
    html: str
    #: Overrides the deployment-wide ``email_reply_to`` for this message only.
    #:
    #: Exactly one template needs it: the support form (Prompt 18 §2), where replying to the mail
    #: has to reach the visitor who sent it rather than the no-reply address every other message
    #: goes out under. It is a *field on the message* rather than an argument to ``deliver`` so
    #: that the template — which is the only thing that knows whether a reply makes sense —
    #: decides, and so no caller can attach one by accident.
    reply_to: str | None = None


#: Where the mark is fetched from. A *setting* would be better and is deliberately not used: this
#: module is pure and has no `Settings` in scope, every caller is a one-line template function, and
#: the URL is a constant of the brand rather than of a deployment. `BASKFY_BRAND_ORIGIN` overrides
#: it for a host that is not the production one.
BRAND_ORIGIN: Final = os.environ.get("BASKFY_BRAND_ORIGIN", "https://staging.baskfy.com")
LOGO_URL: Final = f"{BRAND_ORIGIN}/brand/logo-mark-192.png"


def _masthead() -> str:
    """The mark and the name, above every message.

    **The wordmark is text, not part of the image, and that is the whole design.** Gmail, Outlook
    and Apple Mail all block remote images until the reader allows them, so an all-image masthead
    renders as an empty box with a red X on first open — on the *verification* email, which is the
    first thing anyone ever receives from us. Text always renders; the mark is decoration that
    improves it when images load.

    For the same reason the `<img>` carries `alt=""` rather than "Baskfy": with the word already
    beside it in real text, a non-empty alt would make a screen reader say the name twice, and
    would put a stray "Baskfy" where the broken-image placeholder sits.

    PNG, not the site's SVG: Gmail strips `<img>` elements pointing at SVG outright. 192px for a
    48px slot, so it stays sharp on a retina screen.
    """
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:0 0 24px"><tr>'
        f'<td style="padding-right:10px;vertical-align:middle">'
        f'<img src="{LOGO_URL}" width="34" height="34" alt="" '
        'style="display:block;border:0;outline:none;text-decoration:none;height:34px;width:auto">'
        "</td>"
        '<td style="vertical-align:middle;font-size:18px;font-weight:600;'
        'letter-spacing:-0.035em;color:#14161a">Baskfy</td>'
        "</tr></table>"
    )


def _document(heading: str, paragraphs: list[str], footer: str = FOOTER_TEXT) -> str:
    """The one HTML shell. Inline styles only — every mail client strips a `<style>` block.

    ``paragraphs`` are **already-escaped HTML fragments**, not text: a couple of the messages need
    a link or a code block, and escaping at the call site keeps the one shared shell from having to
    know which is which. Every caller passes `html.escape(...)` for anything user-supplied.

    The masthead is prepended here rather than by each template, so no message can ship unbranded
    and none can brand itself differently. See :func:`_masthead`.
    """
    body = "".join(
        paragraph
        if paragraph.startswith("<")
        else f'<p style="margin:0 0 16px;line-height:1.5">{paragraph}</p>'
        for paragraph in paragraphs
    )
    return (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'font-size:15px;color:#14161a;max-width:560px;margin:0 auto;padding:24px">'
        f"{_masthead()}"
        f'<h1 style="font-size:18px;margin:0 0 20px">{html.escape(heading)}</h1>'
        f"{body}"
        '<hr style="border:none;border-top:1px solid #e4e6eb;margin:24px 0">'
        f'<p style="font-size:12px;color:#6b7280;margin:0 0 8px">{html.escape(footer)}</p>'
        f'<p style="font-size:12px;color:#6b7280;margin:0">{html.escape(DISCLAIMER)}</p>'
        "</div>"
    )


def _plain(heading: str, paragraphs: list[str], footer: str = FOOTER_TEXT) -> str:
    lines = [heading, "", *paragraphs, "", "--", footer, DISCLAIMER]
    return "\n".join(lines) + "\n"


def _code_block(code: str) -> str:
    return (
        '<p style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:28px;'
        f'letter-spacing:4px;margin:0 0 16px">{html.escape(code)}</p>'
    )


def otp(to: str, code: str, minutes: int) -> Message:
    """docs/11: "OTP login as the default path". The code, its life, and nothing else."""
    heading = f"Your {PRODUCT_NAME} sign-in code"
    body = [
        f"Use this code to sign in. It expires in {minutes} minutes and works once.",
        code,
        "If you did not ask to sign in, no action is needed — the code is useless without "
        "access to this inbox.",
    ]
    html_body = _document(
        heading,
        [
            html.escape(f"Use this code to sign in. It expires in {minutes} minutes."),
            "",
            html.escape(
                "If you did not ask to sign in, no action is needed — the code is useless "
                "without access to this inbox."
            ),
        ],
    ).replace('<p style="margin:0 0 16px;line-height:1.5"></p>', _code_block(code))
    return Message(
        to=to,
        subject=f"{code} is your {PRODUCT_NAME} sign-in code",
        text=_plain(heading, body),
        html=html_body,
    )


def verify_email(to: str, url: str, hours: int) -> Message:
    heading = f"Confirm your email for {PRODUCT_NAME}"
    body = [
        "Confirm this address to finish setting up your account.",
        url,
        f"The link expires in {hours} hours.",
    ]
    html_body = _document(
        heading,
        [
            html.escape("Confirm this address to finish setting up your account."),
            f'<a href="{html.escape(url, quote=True)}" '
            'style="display:inline-block;background:#14161a;color:#fff;padding:10px 18px;'
            'border-radius:6px;text-decoration:none">Confirm email</a>',
            html.escape(f"The link expires in {hours} hours. If the button does not work: {url}"),
        ],
    )
    return Message(
        to=to,
        subject=f"Confirm your email for {PRODUCT_NAME}",
        text=_plain(heading, body),
        html=html_body,
    )


def password_reset(to: str, url: str, minutes: int) -> Message:
    heading = f"Reset your {PRODUCT_NAME} password"
    body = [
        "Someone asked to reset the password for this account.",
        url,
        f"The link expires in {minutes} minutes and works once. "
        "If it was not you, ignore this message — your password has not changed.",
    ]
    html_body = _document(
        heading,
        [
            html.escape("Someone asked to reset the password for this account."),
            f'<a href="{html.escape(url, quote=True)}" '
            'style="display:inline-block;background:#14161a;color:#fff;padding:10px 18px;'
            'border-radius:6px;text-decoration:none">Choose a new password</a>',
            html.escape(
                f"The link expires in {minutes} minutes and works once. If it was not you, "
                "ignore this message — your password has not changed."
            ),
            html.escape(f"If the button does not work: {url}"),
        ],
    )
    return Message(
        to=to,
        subject=f"Reset your {PRODUCT_NAME} password",
        text=_plain(heading, body),
        html=html_body,
    )


def lockout(to: str, minutes: int, failures: int) -> Message:
    """docs/11 §Security: "account lockout after 10 failures **with email notification**"."""
    heading = f"Your {PRODUCT_NAME} account is temporarily locked"
    body = [
        f"There have been {failures} failed sign-in attempts for this address, so sign-in is "
        f"locked for {minutes} minutes.",
        "If this was you, wait and try again, or use a sign-in code instead of a password.",
        "If it was not you, someone knows your address and is guessing. Nothing has been "
        "accessed — the attempts failed. Resetting your password will end the lockout "
        "immediately.",
    ]
    return Message(
        to=to,
        subject=f"Unusual sign-in attempts on your {PRODUCT_NAME} account",
        text=_plain(heading, body),
        html=_document(heading, [html.escape(paragraph) for paragraph in body]),
    )


def account_deletion_scheduled(to: str, days: int) -> Message:
    """Prompt 12 §5's soft-delete window, said out loud — an erasure nobody was told about is
    indistinguishable from an account takeover."""
    heading = f"Your {PRODUCT_NAME} account is scheduled for deletion"
    body = [
        f"Your account has been deactivated and will be permanently erased in {days} days.",
        "Signing in again before then cancels the deletion and restores the account.",
        "After that, the data is gone and cannot be recovered.",
    ]
    return Message(
        to=to,
        subject=f"Your {PRODUCT_NAME} account will be deleted in {days} days",
        text=_plain(heading, body),
        html=_document(heading, [html.escape(paragraph) for paragraph in body]),
    )


@dataclass(frozen=True, slots=True)
class SupportSubmission:
    """One filled-in contact form, as the template needs it.

    A record rather than five keyword arguments: the fields travel together everywhere, and
    ``ruff``'s ``PLR0913`` is right that a six-argument template is a signature nobody will read
    correctly at the call site.
    """

    topic: str
    from_name: str
    from_email: str
    body_text: str
    #: ``None`` when the sender was not signed in, which is most of the time.
    account_public_id: str | None


def support_request(to: str, submission: SupportSubmission) -> Message:
    """A message from the `/support` contact form (Prompt 18 §2), addressed to *us*.

    The only template in this module whose recipient is the operator rather than the customer,
    which changes two things.

    **The footer is wrong for it.** :data:`FOOTER_TEXT` explains why *you* received this, and the
    reason here is "someone filled in the contact form", not "someone used your address".

    **Every field is untrusted.** The name, the address and the body are typed by an anonymous
    visitor. They are escaped for the HTML part exactly like everything else, and the *subject*
    carries only the topic — one of a closed set the endpoint validates — so a newline in a
    submitted name cannot inject a header. ``Reply-To`` is set by the caller, not here, for the
    same reason.
    """
    heading = f"Support: {submission.topic}"
    identity = f"{submission.from_name} <{submission.from_email}>"
    attribution = (
        f"Signed in as account {submission.account_public_id}."
        if submission.account_public_id
        else "Not signed in when this was sent."
    )
    paragraphs = [
        f"From: {identity}",
        attribution,
        submission.body_text,
    ]
    footer = "Sent by the Baskfy support form. Reply to this message to answer the sender."
    return Message(
        to=to,
        subject=f"[{PRODUCT_NAME} support] {submission.topic}",
        text=_plain(heading, paragraphs, footer),
        html=_document(heading, [html.escape(paragraph) for paragraph in paragraphs], footer),
        # Answering the mail answers the person. `from_email` is a validated `EmailStr`, so it
        # cannot carry the CR/LF that would make this a header injection.
        reply_to=submission.from_email,
    )


def support_receipt(to: str, *, topic: str) -> Message:
    """The copy that goes back to the sender, so a form submission is not a black hole."""
    heading = "We have your message"
    body = [
        f"Thanks — your message about “{topic}” reached us and someone will read it.",
        "Replies come from a real address, so you can answer them directly.",
        "This is a copy for your records; you do not need to do anything.",
    ]
    return Message(
        to=to,
        subject=f"We have your {PRODUCT_NAME} support message",
        text=_plain(heading, body),
        html=_document(heading, [html.escape(paragraph) for paragraph in body]),
    )


# ---------------------------------------------------------------------------
# Screen alerts — PROMPTS.md Prompt 20 deliverable 3
# ---------------------------------------------------------------------------
#
#     "... receives an email with entries, exits, and rank changes since the last run — computed
#      by diffing screen_run rows. Include an unsubscribe link and a digest preference."
#
# The diff itself is `baskfy_core.screen_diff`; the loading is `baskfy_api.alerts`. What is here
# is only the wording, and it is deliberately austere: a table of rows, three headings, no
# adjectives. docs/11 §Compliance forbids "advice" language, and "CUPID is breaking out" is
# exactly the sentence that turns a factual diff into a recommendation.
#
# Prompt 20's second acceptance criterion pins the *bytes*: "given two consecutive screen_run
# fixtures, the email content matches an expected snapshot exactly". The snapshot lives in
# `tests/fixtures/alerts/` and `services/api/tests/test_api_alerts.py` compares against it, so
# every change to the wording below is a deliberate change to a committed file.


@dataclass(frozen=True, slots=True)
class AlertRow:
    """One line of an alert email.

    ``previous_rank`` is ``None`` for an entry (it was not there) and for an exit (its rank is
    ``rank``, the one it held before it left). A mover carries both.
    """

    symbol: str
    name: str
    rank: int
    previous_rank: int | None = None

    @property
    def places_moved(self) -> int:
        """Positive when the name climbed — the same convention as `baskfy_core.screen_diff`."""
        return 0 if self.previous_rank is None else self.previous_rank - self.rank


@dataclass(frozen=True, slots=True)
class AlertSection:
    """One screen's changes. A digest email carries several; a single alert carries one."""

    screen_name: str
    screen_url: str
    unsubscribe_url: str
    as_of: dt.date
    previous_as_of: dt.date
    entries: tuple[AlertRow, ...]
    exits: tuple[AlertRow, ...]
    movers: tuple[AlertRow, ...]
    #: The totals *before* the per-email row cap, so "and 12 more" can be honest.
    entry_total: int
    exit_total: int
    mover_total: int
    held_count: int


#: Column widths for the plain-text table. Fixed, because a monospaced alignment that depends on
#: the longest value in *this* email makes two consecutive alerts about the same screen look like
#: different documents.
_RANK_WIDTH: Final = 6
_SYMBOL_WIDTH: Final = 14
_MOVE_WIDTH: Final = 4


def _text_row(row: AlertRow, *, show_move: bool) -> str:
    rank = f"#{row.rank}".ljust(_RANK_WIDTH)
    symbol = row.symbol[:_SYMBOL_WIDTH].ljust(_SYMBOL_WIDTH)
    if not show_move:
        return f"  {rank}{symbol}{row.name}"
    marker = f"{row.places_moved:+d}".rjust(_MOVE_WIDTH)
    return f"  {marker}  {rank}{symbol}{row.name} (was #{row.previous_rank})"


def _held(count: int) -> str:
    """ "1 held their rank" is not a sentence. One name holds its rank; several hold theirs."""
    return "1 name held its rank." if count == 1 else f"{count} names held their rank."


def _more(shown: int, total: int) -> list[str]:
    return [f"  … and {total - shown} more"] if total > shown else []


def _section_text(section: AlertSection, *, with_title: bool) -> list[str]:
    """``with_title`` is False for a single-screen alert, whose heading is already the screen."""
    lines = [section.screen_name] if with_title else []
    lines += [
        f"{section.previous_as_of.isoformat()} → {section.as_of.isoformat()}",
        "",
        f"Entries ({section.entry_total})",
    ]
    lines += [_text_row(row, show_move=False) for row in section.entries]
    lines += _more(len(section.entries), section.entry_total)
    if not section.entry_total:
        lines.append("  none")
    lines += ["", f"Exits ({section.exit_total})"]
    lines += [_text_row(row, show_move=False) for row in section.exits]
    lines += _more(len(section.exits), section.exit_total)
    if not section.exit_total:
        lines.append("  none")
    lines += ["", f"Rank changes ({section.mover_total})"]
    lines += [_text_row(row, show_move=True) for row in section.movers]
    lines += _more(len(section.movers), section.mover_total)
    if not section.mover_total:
        lines.append("  none")
    lines += [
        "",
        _held(section.held_count),
        "",
        f"View the screen: {section.screen_url}",
        f"Stop this alert: {section.unsubscribe_url}",
    ]
    return lines


def _section_html(section: AlertSection, *, with_title: bool) -> str:
    """``with_title`` is False for a single-screen alert, whose ``<h1>`` is already the screen."""

    def table(rows: tuple[AlertRow, ...], total: int, *, show_move: bool) -> str:
        if not total:
            return '<p style="margin:0 0 16px;color:#6b7280">none</p>'
        cells = "".join(
            '<tr><td style="padding:2px 12px 2px 0;font-variant-numeric:tabular-nums">'
            + (
                f"{html.escape(f'{row.places_moved:+d}')} (#{row.previous_rank} → #{row.rank})"
                if show_move
                else f"#{row.rank}"
            )
            + '</td><td style="padding:2px 12px 2px 0"><strong>'
            + html.escape(row.symbol)
            + '</strong></td><td style="padding:2px 0;color:#4b5563">'
            + html.escape(row.name)
            + "</td></tr>"
            for row in rows
        )
        tail = (
            f'<tr><td colspan="3" style="padding:2px 0;color:#6b7280">… and {total - len(rows)}'
            " more</td></tr>"
            if total > len(rows)
            else ""
        )
        return (
            '<table style="border-collapse:collapse;font-size:14px;margin:0 0 16px">'
            f"{cells}{tail}</table>"
        )

    title = (
        f'<h2 style="font-size:16px;margin:24px 0 4px">{html.escape(section.screen_name)}</h2>'
        if with_title
        else ""
    )
    return (
        f"{title}"
        f'<p style="margin:0 0 16px;color:#6b7280;font-size:13px">'
        f"{section.previous_as_of.isoformat()} &rarr; {section.as_of.isoformat()}</p>"
        f'<h3 style="font-size:14px;margin:0 0 6px">Entries ({section.entry_total})</h3>'
        f"{table(section.entries, section.entry_total, show_move=False)}"
        f'<h3 style="font-size:14px;margin:0 0 6px">Exits ({section.exit_total})</h3>'
        f"{table(section.exits, section.exit_total, show_move=False)}"
        f'<h3 style="font-size:14px;margin:0 0 6px">Rank changes ({section.mover_total})</h3>'
        f"{table(section.movers, section.mover_total, show_move=True)}"
        f'<p style="margin:0 0 16px;color:#6b7280;font-size:13px">'
        f"{html.escape(_held(section.held_count))}</p>"
        f'<p style="margin:0 0 16px"><a href="{html.escape(section.screen_url, quote=True)}">'
        "View the screen</a> &middot; "
        f'<a href="{html.escape(section.unsubscribe_url, quote=True)}">Stop this alert</a></p>'
    )


def screen_alert(to: str, sections: Sequence[AlertSection], *, manage_url: str) -> Message:
    """One alert email — a single screen, or a digest covering several.

    The subject names the screen when there is one and the count when there are more, because a
    subject line that always said "Your screen alerts" would make an inbox of them unsortable.
    """
    if not sections:
        raise ValueError("an alert email with no sections has nothing to say")

    if len(sections) == 1:
        only = sections[0]
        heading = only.screen_name
        subject = (
            f"{only.screen_name}: {only.entry_total} in, {only.exit_total} out "
            f"({only.as_of.isoformat()})"
        )
    else:
        heading = f"Your {PRODUCT_NAME} screen alerts"
        subject = f"{PRODUCT_NAME} screen alerts: {len(sections)} screens changed"

    text_lines: list[str] = []
    for index, section in enumerate(sections):
        if index:
            text_lines += ["", "-" * 60, ""]
        text_lines += _section_text(section, with_title=len(sections) > 1)
    text_lines += ["", f"Manage your alerts: {manage_url}"]

    html_body = _document(
        heading,
        [
            "".join(_section_html(section, with_title=len(sections) > 1) for section in sections),
            f'<p style="margin:0 0 16px;font-size:13px">'
            f'<a href="{html.escape(manage_url, quote=True)}">Manage your alerts</a></p>',
        ],
        ALERT_FOOTER_TEXT,
    )
    return Message(
        to=to,
        subject=subject,
        text=_plain(heading, text_lines, ALERT_FOOTER_TEXT),
        html=html_body,
    )


@dataclass(frozen=True, slots=True)
class SwingCandidate:
    """One name on the swing email — a candidate to watch, or a line to act on."""

    symbol: str
    setup: str
    trigger: str
    stop: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class SwingDigest:
    """One evening of the swing book (`docs/swing/05` §4).

    Five things, and the order is the order a person needs them in: **what is unprotected**,
    what tomorrow's plan does, what it refused, what is worth watching, and how far the paper
    period has got.

    `naked` is first and is rendered even when it is empty, which is unusual for an email
    section and deliberate. A position without a resting stop is the one state the method
    forbids outright (`04` §6, `03` §7), and a section that appeared only when something was
    wrong would train the reader to skim past the top of the message.
    """

    as_of: dt.date
    gate: str
    exposure_level: int
    max_open_positions: int
    #: Symbols with an open quantity and no `gtt_id`. Empty is the normal case and is said aloud.
    naked: tuple[str, ...]
    exits: tuple[SwingCandidate, ...]
    entries: tuple[SwingCandidate, ...]
    skips: tuple[tuple[str, str], ...]
    flags: tuple[SwingCandidate, ...]
    eps: tuple[SwingCandidate, ...]
    sessions_logged: int
    sessions_required: int


def _swing_lines(title: str, rows: Sequence[SwingCandidate]) -> list[str]:
    if not rows:
        return [f"{title}: none."]
    out = [f"{title}:"]
    out += [
        f"  {row.symbol:<14} {row.setup:<6} trigger {row.trigger:>10}  stop {row.stop:>10}"
        + (f"  {row.note}" if row.note else "")
        for row in rows
    ]
    return out


def swing_eod(to: str, digest: SwingDigest, *, swing_url: str) -> Message:
    """The evening email of `docs/swing/05` §4.

    The subject carries the two numbers that decide whether the message needs opening tonight:
    how many positions are unprotected, and how many lines tomorrow's plan has. An email whose
    subject is always "Your swing update" is an email that gets read on Saturday.
    """
    naked_line = (
        "Every open position has a resting stop."
        if not digest.naked
        else f"UNPROTECTED: {', '.join(digest.naked)} — no resting stop. Arm one before the open."
    )
    heading = f"Swing · {digest.as_of.isoformat()} · {digest.gate}"
    urgent = f"{len(digest.naked)} unprotected · " if digest.naked else ""
    subject = (
        f"{urgent}{len(digest.exits)} exits, {len(digest.entries)} entries "
        f"({digest.as_of.isoformat()})"
    )

    text_lines: list[str] = [
        naked_line,
        "",
        f"Tape: {digest.gate}. Rung {digest.exposure_level + 1} of 4, up to "
        f"{digest.max_open_positions} positions.",
        "",
    ]
    text_lines += _swing_lines("Tomorrow's exits", digest.exits)
    text_lines += [""]
    text_lines += _swing_lines("Tomorrow's entries", digest.entries)
    text_lines += [""]
    if digest.skips:
        text_lines += ["Refused, and why:"]
        text_lines += [f"  {symbol:<14} {reason}" for symbol, reason in digest.skips]
    else:
        text_lines += ["Refused, and why: nothing was refused."]
    text_lines += [""]
    text_lines += _swing_lines("Flags forming", digest.flags)
    text_lines += [""]
    text_lines += _swing_lines("Episodic pivots", digest.eps)
    text_lines += [
        "",
        f"{digest.sessions_logged} of {digest.sessions_required} paper sessions logged.",
        "",
        f"Open the swing hub: {swing_url}",
    ]

    body_html = [
        f'<p style="margin:0 0 16px;font-size:15px'
        f'{";color:#b45309;font-weight:600" if digest.naked else ""}">'
        f"{html.escape(naked_line)}</p>",
        f'<p style="margin:0 0 16px;font-size:14px">Tape: <strong>{html.escape(digest.gate)}'
        f"</strong>. Rung {digest.exposure_level + 1} of 4, up to "
        f"{digest.max_open_positions} positions.</p>",
        f'<pre style="margin:0 0 16px;font-size:13px;white-space:pre-wrap">'
        f"{html.escape(chr(10).join(text_lines[4:]))}</pre>",
        f'<p style="margin:0 0 16px;font-size:13px">'
        f'<a href="{html.escape(swing_url, quote=True)}">Open the swing hub</a></p>',
    ]
    return Message(
        to=to,
        subject=subject,
        text=_plain(heading, text_lines, ALERT_FOOTER_TEXT),
        html=_document(heading, body_html, ALERT_FOOTER_TEXT),
    )


def rebalance_available(
    to: str,
    *,
    basket_name: str,
    version_no: int | None,
    investments_path: str = "/me/investments",
) -> Message:
    """T8.3 — one mail when a published version raises ``REBALANCE_AVAILABLE``."""
    heading = f"Rebalance update available for {basket_name}"
    version_bit = f"Version {version_no} is live. " if version_no is not None else ""
    body = [
        f"{version_bit}A new target is ready for {basket_name}.",
        f"Open Investments ({investments_path}) to review the update. "
        "This message does not place an order; you decide at the broker.",
    ]
    return Message(
        to=to,
        subject=f"Rebalance update: {basket_name}",
        text=_plain(heading, body),
        html=_document(heading, [html.escape(paragraph) for paragraph in body]),
    )
