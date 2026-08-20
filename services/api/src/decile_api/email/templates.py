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
and the body is handed to a transport. `decile_api.logging`'s redaction filter is the backstop.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Final

from decile_core.seed_data import PRODUCT_NAME

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


@dataclass(frozen=True, slots=True)
class Message:
    """One email, addressed and rendered. Both bodies are always present."""

    to: str
    subject: str
    text: str
    html: str


def _document(heading: str, paragraphs: list[str], footer: str = FOOTER_TEXT) -> str:
    """The one HTML shell. Inline styles only — every mail client strips a `<style>` block.

    ``paragraphs`` are **already-escaped HTML fragments**, not text: a couple of the messages need
    a link or a code block, and escaping at the call site keeps the one shared shell from having to
    know which is which. Every caller passes `html.escape(...)` for anything user-supplied.
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
