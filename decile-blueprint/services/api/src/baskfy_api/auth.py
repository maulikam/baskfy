"""Bearer-token verification (Prompt 7 deliverable 3).

docs/07 §header: "Auth: `Authorization: Bearer <JWT>` issued by the Next.js app (HS256, shared
secret, 15-min access + refresh)". The API **verifies**; it never issues. Minting, refresh
rotation and the OTP flow are Prompt 12's ``/auth/*`` endpoints.

What this refuses, and why
--------------------------
* **No algorithm negotiation.** ``algorithms=[JWT_ALGORITHM]`` is a fixed list of one. Letting the
  token's own header choose is how ``alg: none`` and RS256-verified-as-HS256 attacks work.
* **No unbounded lifetime.** docs/11 §Security says "15-min access". A token whose ``exp - iat``
  exceeds :attr:`Settings.jwt_max_lifetime_seconds` is refused even if it is otherwise valid, so a
  misconfigured web app cannot quietly issue year-long credentials against the same secret.
* **No unknown subjects.** ``sub`` is an ``app_user.public_id``; a token for a user who is not in
  the database is refused rather than treated as a nameless authenticated caller.

Anonymous requests are allowed through as :data:`ANONYMOUS`. docs/07 rate-limits anonymous traffic
separately, which only makes sense if there is some, and the example screens are readable without
an account.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Final

import jwt
from fastapi import Depends, Request
from jwt.types import Options
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.db import SessionDep
from baskfy_api.problems import Problem, ProblemType, unauthenticated
from baskfy_api.settings import JWT_ALGORITHM, Settings, get_settings
from baskfy_core.models import AppUser

log = logging.getLogger(__name__)

BEARER_PREFIX: Final = "Bearer "

#: docs/07 names ``X-API-Key`` for the public read API. Prompt 20 delivers it; until there is a
#: key store, presenting the header authenticates nothing and buys no rate-limit tier. Recorded
#: here so the header is knowingly ignored rather than accidentally trusted.
API_KEY_HEADER: Final = "X-API-Key"


class PrincipalKind(StrEnum):
    ANONYMOUS = "anonymous"
    USER = "user"
    #: Reachable only once Prompt 20 issues keys.
    API_KEY = "api_key"


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making this request."""

    kind: PrincipalKind
    user_id: int | None = None
    public_id: str | None = None
    email: str | None = None
    #: ``app_user.is_staff``. docs/09 §Observability puts ``/admin/pipeline`` "behind staff auth";
    #: this is what that check reads. Loaded with the account rather than queried again per route,
    #: so a staff route costs the same one lookup every other authenticated route does.
    is_staff: bool = False

    @property
    def is_authenticated(self) -> bool:
        return self.kind is not PrincipalKind.ANONYMOUS

    def require_user(self) -> int:
        """The caller's ``app_user.id``, or a 401."""
        if self.user_id is None:
            raise unauthenticated()
        return self.user_id


ANONYMOUS: Final = Principal(kind=PrincipalKind.ANONYMOUS)


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization")
    if header is None or not header.startswith(BEARER_PREFIX):
        return None
    token = header[len(BEARER_PREFIX) :].strip()
    return token or None


def decode_token(token: str, settings: Settings) -> dict[str, object]:
    """Verify signature, expiry and lifetime. Raises :class:`Problem` (401) on any failure.

    Deliberately uniform: every failure mode returns the same 401 shape with a short reason. A
    caller that can distinguish "bad signature" from "expired" from "wrong audience" has an
    oracle it does not need.
    """
    if not settings.jwt_secret:
        raise unauthenticated("This deployment cannot verify bearer tokens.")
    # docs/11 pins a short-lived access token; a token without `exp` is not one, and a token
    # without `sub` names nobody. PyJWT only enforces claims it is told to require.
    options: Options = {"require": ["exp", "iat", "sub"]}
    try:
        claims: dict[str, object] = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[JWT_ALGORITHM],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=settings.jwt_leeway_seconds,
            options=options,
        )
    except jwt.PyJWTError as exc:
        log.info("token rejected", extra={"reason": type(exc).__name__})
        raise unauthenticated("The bearer token is not valid.") from exc

    issued = claims.get("iat")
    expires = claims.get("exp")
    if isinstance(issued, int) and isinstance(expires, int):
        lifetime = expires - issued
        if lifetime > settings.jwt_max_lifetime_seconds:
            log.warning("token lifetime too long", extra={"lifetime_seconds": lifetime})
            raise unauthenticated("The bearer token's lifetime exceeds the accepted maximum.")
    return claims


async def _load_user(session: AsyncSession, public_id: str) -> AppUser | None:
    return (
        await session.execute(select(AppUser).where(AppUser.public_id == public_id))
    ).scalar_one_or_none()


def settings_for(request: Request) -> Settings:
    """This application's settings, not the process-wide cache.

    ``create_app`` stores the settings it was built with on ``app.state``; reading the module-level
    ``get_settings()`` here instead would make the verifier consult a different configuration than
    the app was constructed with — which is exactly what happens under test, and is the kind of
    difference that makes a suite pass against a config nothing runs in production.
    """
    settings = getattr(request.app.state, "settings", None)
    return settings if isinstance(settings, Settings) else get_settings()


async def current_principal(request: Request, session: SessionDep) -> Principal:
    """The FastAPI dependency every route depends on, directly or through another."""
    token = _bearer_token(request)
    if token is None:
        return ANONYMOUS

    settings = settings_for(request)
    claims = decode_token(token, settings)
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise unauthenticated("The bearer token has no subject.")

    user = await _load_user(session, subject)
    if user is None:
        log.info("token for unknown subject", extra={"subject": subject})
        raise unauthenticated("The bearer token refers to an unknown account.")
    return Principal(
        kind=PrincipalKind.USER,
        user_id=user.id,
        public_id=user.public_id,
        email=user.email,
        is_staff=user.is_staff,
    )


PrincipalDep = Annotated[Principal, Depends(current_principal)]


async def require_authenticated(principal: PrincipalDep) -> Principal:
    """For routes that have no anonymous behaviour at all."""
    if not principal.is_authenticated:
        raise unauthenticated()
    return principal


AuthenticatedDep = Annotated[Principal, Depends(require_authenticated)]


async def require_staff(principal: PrincipalDep) -> Principal:
    """The gate on every ``/admin/*`` route (Prompt 17 deliverable 4).

    A non-staff caller gets **404**, not 403. docs/07's catalogue has no ``forbidden`` type, and
    more to the point: a 403 confirms that the path exists and that the account simply lacks the
    bit, which tells an attacker exactly which endpoint to go and get a session for. An anonymous
    caller still gets 401, because "you are not signed in" is not a secret.
    """
    if not principal.is_authenticated:
        raise unauthenticated()
    if not principal.is_staff:
        log.warning(
            "non-staff principal refused an admin route",
            extra={"public_id": principal.public_id},
        )
        raise Problem(ProblemType.NOT_FOUND, "Not found.")
    return principal


StaffDep = Annotated[Principal, Depends(require_staff)]


def encode_token(  # noqa: PLR0913 - one parameter per JWT claim the web app may set
    subject: str,
    secret: str,
    *,
    lifetime_seconds: int = 15 * 60,
    issued_at: dt.datetime | None = None,
    audience: str | None = None,
    issuer: str | None = None,
) -> str:
    """Mint a token the way the web app will.

    Lives here, beside the verifier, because the two have to agree about claim names and the two
    disagreeing is the failure this is most likely to have. Used by the contract tests; Prompt 12
    will use it for the real ``/auth/login``.
    """
    moment = issued_at or dt.datetime.now(tz=dt.UTC)
    claims: dict[str, object] = {
        "sub": subject,
        "iat": int(moment.timestamp()),
        "exp": int((moment + dt.timedelta(seconds=lifetime_seconds)).timestamp()),
    }
    if audience is not None:
        claims["aud"] = audience
    if issuer is not None:
        claims["iss"] = issuer
    return jwt.encode(claims, secret, algorithm=JWT_ALGORITHM)
