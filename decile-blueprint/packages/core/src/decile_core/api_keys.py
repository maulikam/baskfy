"""API-key shape and scopes — Prompt 20 deliverable 1, the part with no I/O in it.

    "API keys: creation, scoping (**read-only**), rotation, revocation, per-key rate limits, and
     a usage dashboard. Keys are hashed at rest and shown once."

docs/07 §header names the transport: ``X-API-Key``. This module fixes what goes in that header and
what a key is allowed to mean; ``decile_api.api_keys`` mints, stores and verifies them, and
``decile_api.routers.api_keys`` exposes the lifecycle.

The wire format
---------------
``dk_<prefix>_<secret>`` — for example ``dk_7f3a9c1b4d2e_kQ8...`` (43 more characters).

* ``dk`` is a fixed marker. GitHub's secret scanners, and ours, match on a prefix; a key that
  looked like an opaque blob would be indistinguishable from a session id in a log.
* ``prefix`` is 12 hex characters, **stored in the clear** and unique. It is what the lookup
  indexes on, so verification is one indexed row fetch and a digest comparison rather than a scan
  over every key in the table hashing each one. It is also what the usage dashboard and the audit
  log display, because showing a user ``dk_7f3a9c1b4d2e…`` lets them tell two keys apart without
  us ever storing the secret.
* ``secret`` is 256 bits, URL-safe, and is **never stored**. ``decile_api.security.digest``
  (SHA-256) is what the row holds — the same treatment refresh tokens and OTPs get, and for the
  same reason: a 256-bit random secret has no offline guessing game worth slowing down, and what
  matters is that a database dump cannot be replayed. See ``decile_api.security``'s module
  docstring.

Read-only, structurally
-----------------------
:class:`Scope` has no member that is not a read. That is the enforcement — not a check somewhere
that a write scope was not requested, but the absence of anything to request.
``packages/core/tests/test_public_api_policy.py`` asserts the property holds for every member, so
adding a ``screens:write`` later is a test failure that has to be argued for, not a quiet widening.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "DEFAULT_SCOPES",
    "KEY_PREFIX_LENGTH",
    "KEY_TOKEN_MARKER",
    "MAX_KEYS_PER_ACCOUNT",
    "ParsedKey",
    "Scope",
    "format_key",
    "normalise_scopes",
    "parse_key",
    "redact",
]

#: The fixed marker every key starts with. Two characters, lower case, no ambiguity with a JWT
#: (which starts ``ey``) or with a Razorpay key (``rzp_``).
KEY_TOKEN_MARKER: Final = "dk"

#: Hex characters in the public prefix. Twelve gives 48 bits — enough that a collision across a
#: realistic number of keys is not something the unique index will ever have to reject twice.
KEY_PREFIX_LENGTH: Final = 12

#: Bytes of randomness behind the secret half. 32 bytes -> 43 URL-safe characters, the same size
#: as a refresh token (``decile_api.security.OPAQUE_TOKEN_BYTES``).
KEY_SECRET_BYTES: Final = 32

#: NOT IN THE BUNDLE. docs/07 says nothing about how many keys an account may hold. Ten is enough
#: for one key per environment per integration and small enough that a compromised session cannot
#: quietly mint a thousand. ``docs/DECISIONS.md`` §20.2.
MAX_KEYS_PER_ACCOUNT: Final = 10

_PREFIX_RE: Final = re.compile(rf"^[0-9a-f]{{{KEY_PREFIX_LENGTH}}}$")
_SECRET_RE: Final = re.compile(r"^[A-Za-z0-9_-]{40,64}$")


class Scope(StrEnum):
    """What a key may read. **Every member is a read.** See the module docstring.

    The four map onto the three surfaces Prompt 20 deliverable 2 lists — "screen results, factor
    values, breadth" — plus the metadata a client needs to interpret them (the factor registry,
    the universe list, the current ``as_of``/``data_version``).
    """

    SCREENS_READ = "screens:read"
    FACTORS_READ = "factors:read"
    BREADTH_READ = "breadth:read"
    META_READ = "meta:read"

    @property
    def is_read_only(self) -> bool:
        return self.value.endswith(":read")


#: What a key gets when the caller does not narrow it. Everything, because every scope is a read
#: and the alternative — a default of nothing — makes the first request of every new integration
#: a 403 for no security gain.
DEFAULT_SCOPES: Final[tuple[Scope, ...]] = tuple(Scope)


class InvalidKeyFormat(ValueError):
    """The presented ``X-API-Key`` is not shaped like one of ours."""


@dataclass(frozen=True, slots=True)
class ParsedKey:
    """A presented key, split into the half we look up by and the half we compare."""

    prefix: str
    secret: str

    @property
    def display(self) -> str:
        """What a UI or a log line may show: the marker and the prefix, never the secret."""
        return f"{KEY_TOKEN_MARKER}_{self.prefix}"


def format_key(prefix: str, secret: str) -> str:
    """Assemble the one string the user is shown, once, at creation."""
    return f"{KEY_TOKEN_MARKER}_{prefix}_{secret}"


def parse_key(presented: str) -> ParsedKey:
    """Split a presented key, or raise :class:`InvalidKeyFormat`.

    Validating the shape *before* touching the database is not cosmetic: it means a malformed
    header costs no query, which is what stops an unauthenticated caller using the key endpoint
    as a way to make us do work.
    """
    # ``maxsplit=2``: the secret half is ``token_urlsafe``, whose alphabet **includes** ``_``.
    # An unbounded split turns roughly half of all real keys into four or more parts and rejects
    # them. Found by a contract test, not by review.
    parts = presented.strip().split("_", 2)
    expected_parts = 3
    if len(parts) != expected_parts:
        raise InvalidKeyFormat("An API key has three underscore-separated parts.")
    marker, prefix, secret = parts
    if marker != KEY_TOKEN_MARKER:
        raise InvalidKeyFormat(f"An API key starts with {KEY_TOKEN_MARKER!r}.")
    if not _PREFIX_RE.match(prefix):
        raise InvalidKeyFormat("The key prefix is not the expected shape.")
    if not _SECRET_RE.match(secret):
        raise InvalidKeyFormat("The key secret is not the expected shape.")
    return ParsedKey(prefix=prefix, secret=secret)


def redact(presented: str) -> str:
    """A key as it may appear in a log or an error. The secret half never survives this.

    ``decile_api.logging``'s redaction filter is the backstop; this is the thing that means the
    value never reaches it in the first place.
    """
    parts = presented.strip().split("_", 2)
    expected_parts = 3
    if len(parts) != expected_parts or parts[0] != KEY_TOKEN_MARKER:
        return "<redacted>"
    return f"{parts[0]}_{parts[1]}_<redacted>"


def normalise_scopes(requested: Iterable[str] | None) -> tuple[Scope, ...]:
    """Turn what a caller asked for into scopes, in a fixed order, or raise.

    An unknown scope is an **error**, not something to ignore. The alternative — dropping it —
    hands back a key that silently does less than the caller asked for, and the first they hear of
    it is a 403 in production.
    """
    if requested is None:
        return DEFAULT_SCOPES
    resolved: set[Scope] = set()
    for name in requested:
        try:
            resolved.add(Scope(name))
        except ValueError as exc:
            known = ", ".join(scope.value for scope in Scope)
            raise ValueError(f"unknown scope {name!r}; known scopes are {known}") from exc
    if not resolved:
        raise ValueError("a key with no scopes could read nothing; omit the field for all scopes")
    return tuple(scope for scope in Scope if scope in resolved)
