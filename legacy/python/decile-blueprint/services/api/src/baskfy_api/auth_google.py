"""Google ID token verification — the whole of what this service trusts about a sign-in.

Google sign-in replaced registration, the email OTP and the password entirely
(`docs/DECISIONS-MERGE.md` M46). That makes this module the front door: if it accepts a token it
should not have, an attacker is signed in as anyone.

WHY THE API VERIFIES, AND NOT THE WEB APP
-----------------------------------------
`apps/web` runs the OAuth dance and ends up holding an ID token. It would be easier for it to
simply tell this service "user X signed in" over the shared secret. It does not, because that
endpoint would mint a session for any email the web app named, and a single SSRF or template
injection in the front end would become full account takeover. Instead the web app forwards the
*token*, and this module checks Google's signature over it. The web app is then untrusted
plumbing, which is the only role a front end can safely have in an authentication flow.

WHAT IS CHECKED, AND WHY EACH ONE MATTERS
-----------------------------------------
``signature``      against Google's published JWKS. Without it every other claim is attacker-set.
``iss``            one of Google's two spellings. Both appear in real tokens; Google's own docs
                   list them as equally valid, so accepting only one rejects live users.
``aud``            **our** client id. This is the check people skip. A Google ID token is a
                   perfectly valid, correctly signed token — for the application it was minted
                   for. Without an `aud` check, anyone who can get a user to sign into *their*
                   Google app can replay that token here and be that user.
``exp``/``iat``    required, not optional. A token with no expiry never stops working.
``email_verified`` Google will assert an address the account has not proven control of (it
                   happens with some Workspace configurations). We key accounts on email, so an
                   unverified one is an invitation to claim somebody else's.

The JWKS fetch is HTTP and ``PyJWKClient`` is synchronous, so it runs in a worker thread — the
same shape ``email.sender.SmtpTransport`` uses for ``smtplib``, and for the same reason. The
client caches keys, so the fetch is not per-request; Google rotates them roughly daily.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

import jwt
from jwt import PyJWKClient

from baskfy_api.settings import Settings

log = logging.getLogger(__name__)

#: Both spellings appear in tokens Google actually issues.
GOOGLE_ISSUERS: Final[frozenset[str]] = frozenset(
    {"accounts.google.com", "https://accounts.google.com"}
)
GOOGLE_JWKS_URI: Final = "https://www.googleapis.com/oauth2/v3/certs"
#: Google signs ID tokens with RS256. Pinning it here is what stops an `alg: none` or an HS256
#: token signed with the (public) JWKS modulus from being accepted as valid.
GOOGLE_ALGORITHMS: Final[list[str]] = ["RS256"]
#: How long a cached JWKS entry is reused before it is re-fetched.
JWKS_CACHE_SECONDS: Final = 600


class GoogleVerificationError(Exception):
    """The token was absent, malformed, expired, or not minted for us.

    Deliberately carries no detail about *which*: the caller turns it into one flat 401, because
    telling a caller that the signature was fine but the audience was wrong tells them how to
    get closer on the next attempt.
    """


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    """The claims we act on. Everything else in the token is ignored on purpose."""

    #: Google's stable, never-reused identifier for the account. This — not the email — is what
    #: an `auth_identity` row is keyed on: a Workspace user can change their address, and a
    #: released consumer address can be re-registered by a different person.
    subject: str
    email: str
    name: str | None


@runtime_checkable
class TokenVerifier(Protocol):
    """What the router depends on — the same shape ``email.sender.Transport`` uses, for the same
    reason: one method, so a test double is three lines and the real thing never has to be
    reached around.

    ``runtime_checkable`` because the router resolves the verifier off ``app.state``, which is
    typed as ``Any``, and a narrowing check there is the difference between "the app was wired"
    and "something is on the attribute".
    """

    async def verify(self, id_token: str) -> GoogleIdentity: ...


class GoogleVerifier:
    """Verifies ID tokens for one OAuth client. Built once, held on ``app.state``."""

    def __init__(self, settings: Settings) -> None:
        self._client_id = settings.google_client_id
        self._leeway = settings.jwt_leeway_seconds
        # Constructed eagerly, but it performs no I/O until the first `get_signing_key_from_jwt`,
        # so importing this module never reaches the network.
        self._jwks = PyJWKClient(GOOGLE_JWKS_URI, cache_keys=True, lifespan=JWKS_CACHE_SECONDS)

    @property
    def configured(self) -> bool:
        """False when no client id is set — Google sign-in is then off, not permissive."""
        return bool(self._client_id)

    async def verify(self, id_token: str) -> GoogleIdentity:
        if not self.configured:
            raise GoogleVerificationError("Google sign-in is not configured")
        if not id_token:
            raise GoogleVerificationError("no token supplied")
        return await asyncio.to_thread(self._verify_blocking, id_token)

    def _verify_blocking(self, id_token: str) -> GoogleIdentity:
        try:
            key = self._jwks.get_signing_key_from_jwt(id_token)
            # `object`, not `Any`: every claim below is narrowed with `isinstance` before it
            # is used, and `Any` would let a typo past the type checker on the one dict in
            # this service whose contents an attacker chooses. House rule 3.
            claims: dict[str, object] = jwt.decode(
                id_token,
                key.key,
                algorithms=GOOGLE_ALGORITHMS,
                audience=self._client_id,
                leeway=self._leeway,
                options={
                    "require": ["exp", "iat", "aud", "iss", "sub"],
                    "verify_exp": True,
                    "verify_aud": True,
                    "verify_iss": False,  # checked below against the two accepted spellings
                    "verify_signature": True,
                },
            )
        except jwt.PyJWTError as exc:
            # The class, never the message: PyJWT's text can quote claim values back.
            raise GoogleVerificationError(f"token rejected: {type(exc).__name__}") from exc
        # Broad by intent: a JWKS fetch failure is a network error, and every variant of it
        # means the same thing to the caller. Re-raised, never swallowed (house rule 3).
        except Exception as exc:
            raise GoogleVerificationError(f"JWKS unavailable: {type(exc).__name__}") from exc

        issuer = claims.get("iss")
        if issuer not in GOOGLE_ISSUERS:
            raise GoogleVerificationError("issuer is not Google")

        subject = claims.get("sub")
        email = claims.get("email")
        if not isinstance(subject, str) or not subject:
            raise GoogleVerificationError("token carries no subject")
        if not isinstance(email, str) or not email:
            raise GoogleVerificationError("token carries no email")
        # Google sends this as a real bool, but some Workspace tokens have carried the string
        # "true". Accept both spellings and nothing else — a missing claim is not consent.
        verified = claims.get("email_verified")
        if verified is not True and verified != "true":
            raise GoogleVerificationError("Google has not verified this address")

        name = claims.get("name")
        return GoogleIdentity(
            subject=subject,
            email=email,
            name=name if isinstance(name, str) and name else None,
        )
