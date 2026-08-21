"""Transactional email — templates in `templates`, delivery in `sender` (Prompt 12 §3)."""

from decile_api.email.sender import (
    ConsoleTransport,
    EmailNotSent,
    Mailer,
    ResendTransport,
    SmtpTransport,
    Transport,
    build_transport,
)
from decile_api.email.templates import AlertRow, AlertSection, Message, SupportSubmission

__all__ = [
    "AlertRow",
    "AlertSection",
    "ConsoleTransport",
    "EmailNotSent",
    "Mailer",
    "Message",
    "ResendTransport",
    "SmtpTransport",
    "SupportSubmission",
    "Transport",
    "build_transport",
]
