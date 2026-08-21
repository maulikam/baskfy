"""Password hashing, one-time codes and opaque tokens — docs/11 §Security (Prompt 12).

Four primitives, kept in one place so there is exactly one answer to "how is this stored":

* :func:`hash_password` / :func:`verify_password` — **Argon2id**, named by docs/11.
* :func:`new_otp` — a numeric code short enough to read out of an email.
* :func:`new_opaque_token` — a 256-bit URL-safe secret for refresh cookies and reset links.
* :func:`digest` — SHA-256, how every one of those is *stored*.

Why codes and refresh tokens are SHA-256 and passwords are Argon2id
-------------------------------------------------------------------
Argon2id is deliberately slow because a password is low-entropy, long-lived and reused across
sites: an attacker with the hash gets an offline guessing game worth playing. A refresh token is
256 random bits with a 30-day life and a single use; a six-digit OTP lives ten minutes, allows
five guesses, and is rate-limited per address and per IP besides. Neither has a guessing game to
slow down, and paying 19 MiB of memory on every token refresh would be a denial-of-service
surface rather than a defence.

What SHA-256 *does* buy is that a database dump cannot be replayed: the stored value is not a
credential. That is the property that matters for a high-entropy secret, and it is preserved.

Nothing here logs. `decile_api.logging` has a redaction filter (Prompt 12 deliverable 2), but the
first line of defence is that the values never reach a logger in the first place.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from decile_api.settings import Settings

#: docs/11 §Security: "Argon2id password hashing". Not Argon2i, not Argon2d — the hybrid is what
#: the RFC 9106 §4 recommends for password storage, and it is what the document names.
ARGON2_TYPE: Final = Type.ID

#: 256 bits, URL-safe. `token_urlsafe(32)` yields 43 characters with no padding.
OPAQUE_TOKEN_BYTES: Final = 32

#: The shortest password we will store. NIST SP 800-63B §5.1.1.2: minimum eight, no composition
#: rules, and check against known-breached lists rather than demanding a symbol.
MIN_PASSWORD_LENGTH: Final = 8

#: Argon2 rejects anything longer than this outright; refusing early gives a clear message and
#: stops a multi-megabyte body being hashed.
MAX_PASSWORD_LENGTH: Final = 128


class WeakPassword(ValueError):
    """The supplied password is shorter than :data:`MIN_PASSWORD_LENGTH`, or absurdly long."""


def _hasher(settings: Settings) -> PasswordHasher:
    """A hasher built from settings rather than a module constant.

    The right memory cost depends on the box the API runs on, and a test suite that paid the
    production cost on every fixture would spend most of its time in a KDF.
    """
    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_kib,
        parallelism=settings.argon2_parallelism,
        type=ARGON2_TYPE,
    )


def check_password_policy(password: str) -> None:
    """Raise :class:`WeakPassword` if the password cannot be accepted."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(f"Passwords must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPassword(f"Passwords must be at most {MAX_PASSWORD_LENGTH} characters.")


def hash_password(password: str, settings: Settings) -> str:
    """An Argon2id PHC string. The parameters travel inside it, so they can change later."""
    check_password_policy(password)
    return _hasher(settings).hash(password)


def verify_password(password: str, stored: str | None, settings: Settings) -> bool:
    """Constant-time as far as Argon2 is: a mismatch and a match cost the same derivation.

    ``stored is None`` — an OTP-only account, which docs/11 explicitly allows ("password
    optional") — still pays a hash, so "this address has no password" is not observable from the
    response time. Without that, the password endpoint is an account-enumeration oracle even
    though it returns the same body either way.
    """
    hasher = _hasher(settings)
    if stored is None:
        hasher.hash(password)
        return False
    try:
        return hasher.verify(stored, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str, settings: Settings) -> bool:
    """True when the stored hash used weaker parameters than the ones configured now.

    Called on a successful login, which is the only moment the plaintext is available to re-hash
    with. A parameter bump that never re-hashes anything is a parameter bump in name only.
    """
    try:
        return _hasher(settings).check_needs_rehash(stored)
    except InvalidHashError:
        return True


def new_otp(length: int) -> str:
    """A numeric code of exactly ``length`` digits, uniformly distributed.

    Digits, because it is read out of an email and typed on a phone. Leading zeros are kept —
    ``randbelow`` over the full range and zero-padding, not a random int in [10^(n-1), 10^n),
    which would silently shrink the space by 10%.
    """
    upper = 10**length
    return str(secrets.randbelow(upper)).zfill(length)


def new_opaque_token() -> str:
    """A refresh token or a reset link's secret: 256 bits, URL-safe, never stored in the clear."""
    return secrets.token_urlsafe(OPAQUE_TOKEN_BYTES)


def digest(value: str) -> str:
    """How a code or an opaque token is stored. See the module docstring for why not Argon2id."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digests_match(supplied: str, stored: str) -> bool:
    """Compare a supplied secret against a stored digest without leaking its length by timing."""
    return hmac.compare_digest(digest(supplied), stored)
