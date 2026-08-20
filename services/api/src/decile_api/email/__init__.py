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
from decile_api.email.templates import Message

__all__ = [
    "ConsoleTransport",
    "EmailNotSent",
    "Mailer",
    "Message",
    "ResendTransport",
    "SmtpTransport",
    "Transport",
    "build_transport",
]
