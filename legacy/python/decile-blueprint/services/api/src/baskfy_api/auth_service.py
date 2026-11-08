"""The `/auth/*` logic — docs/07 §"Account & billing", docs/11 §Security (Prompt 12).

The router is the HTTP shape; this is what it means. Kept apart because most of what follows is
about *not* leaking things, and that reasoning belongs next to the code that does it rather than
between two decorators.

Three rules run through everything here
---------------------------------------
**Never confirm whether an address is registered.** ``/auth/request-otp``, ``/auth/forgot-password``
and ``/auth/register`` answer the same way for a known and an unknown address. An endpoint that
does otherwise is a free membership oracle, and for a paid product that is a customer list.
:func:`verify_password` hashes even when there is no stored hash, so the *timing* does not answer
the question either.

**Count failures against the identifier presented, not the account found.** Otherwise the lockout
itself becomes the oracle: an attacker learns which addresses exist by which ones can be locked.

**Store nothing replayable.** Codes and refresh tokens are SHA-256 digests
(`baskfy_api.security`); passwords are Argon2id. A database dump is not a set of credentials.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from dataclasses import dataclass
from typing import Final

from sqlalchemy import ColumnElement, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.auth import encode_token
from baskfy_api.auth_google import GoogleIdentity
from baskfy_api.security import (
    digest,
    hash_password,
    new_opaque_token,
)
from baskfy_api.settings import Settings
from baskfy_core.models import (
    AccountDeletion,
    AppUser,
    AuthIdentity,
    ConsentRecord,
    RefreshToken,
)

log = logging.getLogger(__name__)

PUBLIC_ID_BYTES: Final = 6

#: docs/11 §Compliance: the consent record names which document version was agreed to. Bump when
#: the Terms or the Privacy Policy change, so a later revision requires a fresh consent.
CONSENT_DOCUMENT_VERSION: Final = "2026-08-01"


class AuthError(Exception):
    """Base for the failures the router turns into problems."""


class InvalidCredentials(AuthError):
    """Wrong password, wrong code, or no such account. Deliberately one class for all three."""


class AccountLocked(AuthError):
    """Too many failures — docs/11: "lockout after 10 failures"."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("This account is temporarily locked.")
        self.retry_after_seconds = retry_after_seconds


class TokenReused(AuthError):
    """A refresh token was presented twice. The whole family is revoked."""


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """What a successful authentication produces."""

    user: AppUser
    access_token: str
    access_expires_in: int
    refresh_token: str
    refresh_expires_in: int
    csrf_token: str


def now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def new_public_id() -> str:
    """12 hex characters — the shape the web app's adapter also generates."""
    return secrets.token_hex(PUBLIC_ID_BYTES)


def normalise_email(email: str) -> str:
    return email.strip().lower()


# ---------------------------------------------------------------------------
# Lockout — docs/11: "account lockout after 10 failures with email notification"
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


async def find_user(session: AsyncSession, email: str) -> AppUser | None:
    """An *active* account. A soft-deleted one authenticates nothing (Prompt 12 §5)."""
    return (
        await session.execute(
            select(AppUser).where(
                AppUser.email == normalise_email(email), AppUser.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()


async def find_user_including_deleted(session: AsyncSession, email: str) -> AppUser | None:
    return (
        await session.execute(select(AppUser).where(AppUser.email == normalise_email(email)))
    ).scalar_one_or_none()


async def create_user(
    session: AsyncSession,
    email: str,
    *,
    password: str | None,
    name: str | None,
    settings: Settings,
) -> AppUser:
    """docs/11: "OTP login as the default path, **password optional**"."""
    user = AppUser(
        public_id=new_public_id(),
        email=normalise_email(email),
        name=name,
        password_hash=hash_password(password, settings) if password else None,
    )
    session.add(user)
    await session.flush()
    return user


async def record_consent(
    session: AsyncSession,
    user: AppUser,
    kinds: tuple[str, ...],
    *,
    source_ip: str | None,
    user_agent: str | None,
) -> None:
    """docs/11 §Compliance: "DPDP Act: **consent record**". Append-only; see the model."""
    for kind in kinds:
        session.add(
            ConsentRecord(
                user_id=user.id,
                kind=kind,
                document_version=CONSENT_DOCUMENT_VERSION,
                source_ip=source_ip,
                user_agent=user_agent,
            )
        )
    await session.flush()


# ---------------------------------------------------------------------------
# Google sign-in — the only way in (docs/DECISIONS-MERGE.md M46)
# ---------------------------------------------------------------------------

#: The one provider today. A catalog string rather than an enum so a second one is data.
GOOGLE_PROVIDER: Final = "google"


async def ensure_staff_from_allowlist(
    session: AsyncSession, user: AppUser, settings: Settings
) -> None:
    """Promote an allowlisted address to staff. Never demotes.

    ``BASKFY_STAFF_ALLOWLIST`` is how the founder stays staff across a rebuilt database
    without a one-off ``UPDATE``. The list grants; it does not revoke. An address already
    marked staff stays staff even if it is not on the list.
    """
    if user.is_staff:
        return
    if settings.is_staff_email(user.email):
        user.is_staff = True
        await session.flush()


class IdentityConflict(AuthError):
    """The verified address belongs to an account already bound to a *different* Google subject.

    This should not happen — Google does not reissue a ``sub``, and it does not hand the same
    verified address to two accounts. But "should not happen" is not a security control: if it
    ever does, adopting the account would mean the second subject silently inherits the first
    one's data. Refusing is the only safe answer, and it is loud enough to be investigated.
    """


async def link_google_identity(
    session: AsyncSession,
    identity: GoogleIdentity,
    settings: Settings,
    *,
    source_ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[AppUser, bool]:
    """Resolve a verified Google identity to an ``app_user``, creating one if needed.

    Returns the user and whether this call created the account, so the caller can tell a first
    sign-in from a returning one without a second query.

    THREE PATHS, IN THIS ORDER — the order is the security property
    ----------------------------------------------------------------
    1. **The identity row exists.** The normal case. Looked up on ``(provider, subject)``, never
       on the email, so a Workspace rename signs the same person into the same account.
    2. **No identity, but the verified address matches an account.** Adoption — this is how the
       five accounts that predate Google sign-in get in, and how anyone who had a password-era
       account keeps their data. Safe *only* because the address arrived on a signature-checked
       token with ``email_verified`` true; `auth_google` refuses anything less, and this function
       must never be handed an unverified address.
    3. **Neither.** A new account. No password is set — there is no longer any way to use one —
       and ``email_verified_at`` is stamped from Google's assertion rather than from a mail we
       send, which is the entire point of the change.
    """
    existing = await session.scalar(
        select(AuthIdentity).where(
            AuthIdentity.provider == GOOGLE_PROVIDER,
            AuthIdentity.subject == identity.subject,
        )
    )
    if existing is not None:
        user = await session.get(AppUser, existing.user_id)
        if user is None:  # pragma: no cover — the FK makes this unreachable
            raise InvalidCredentials("identity points at no account")
        existing.email_at_provider = identity.email
        existing.last_login_at = now()
        await ensure_staff_from_allowlist(session, user, settings)
        await session.flush()
        return user, False

    email = normalise_email(identity.email)
    user = await find_user_including_deleted(session, email)
    created = False

    if user is None:
        user = await create_user(
            session, email, password=None, name=identity.name, settings=settings
        )
        created = True
        # docs/11 §Compliance, DPDP: a consent record with the document version agreed to. The
        # terms checkbox is gone from the sign-in page (Maulik, 27 Aug 2026) — the page states
        # the agreement instead of gating on it, so consent is recorded at first sign-in rather
        # than collected as a click. `docs/DECISIONS-MERGE.md` M46.2 records the change and why
        # the *record* survived the checkbox.
        await record_consent(
            session,
            user,
            ("terms", "privacy"),
            source_ip=source_ip,
            user_agent=user_agent,
        )
    else:
        conflicting = await session.scalar(
            select(AuthIdentity).where(
                AuthIdentity.user_id == user.id,
                AuthIdentity.provider == GOOGLE_PROVIDER,
            )
        )
        if conflicting is not None:
            log.error(
                "google identity conflict",
                extra={"user_public_id": user.public_id, "reason": "subject mismatch"},
            )
            raise IdentityConflict("this account is bound to a different Google identity")

    # Google asserted the address, which is a stronger proof than the mail we used to send: it
    # proves control of the inbox *now*, not at some point before the link expired.
    if user.email_verified_at is None:
        user.email_verified_at = now()
    if not user.name and identity.name:
        user.name = identity.name

    session.add(
        AuthIdentity(
            user_id=user.id,
            provider=GOOGLE_PROVIDER,
            subject=identity.subject,
            email_at_provider=identity.email,
            last_login_at=now(),
        )
    )
    await ensure_staff_from_allowlist(session, user, settings)
    await session.flush()
    return user, created


# ---------------------------------------------------------------------------
# One-time codes — OTP, email verification, password reset
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Sessions — access tokens and rotating refresh families
# ---------------------------------------------------------------------------


async def issue_session(
    session: AsyncSession,
    user: AppUser,
    settings: Settings,
    *,
    family_id: str | None = None,
) -> IssuedSession:
    """Mint an access token and a refresh token, in a family.

    ``family_id`` is carried across a rotation so reuse detection can revoke every descendant of
    one login at once; a fresh login starts a new family.
    """
    refresh_plaintext = new_opaque_token()
    ttl = dt.timedelta(days=settings.refresh_token_ttl_days)
    session.add(
        RefreshToken(
            user_id=user.id,
            family_id=family_id or secrets.token_hex(16),
            token_hash=digest(refresh_plaintext),
            expires_at=now() + ttl,
        )
    )
    await session.flush()

    access = encode_token(
        user.public_id,
        settings.jwt_secret,
        lifetime_seconds=settings.access_token_ttl_seconds,
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        epoch=user.session_epoch,
    )
    return IssuedSession(
        user=user,
        access_token=access,
        access_expires_in=settings.access_token_ttl_seconds,
        refresh_token=refresh_plaintext,
        refresh_expires_in=int(ttl.total_seconds()),
        csrf_token=new_opaque_token(),
    )


async def revoke_family(session: AsyncSession, family_id: str, reason: str) -> int:
    """Kill every live token descended from one login. Returns how many were live."""
    return await _revoke_where(
        session,
        RefreshToken.family_id == family_id,
        reason,
    )


async def revoke_all_for_user(session: AsyncSession, user_id: int, reason: str) -> int:
    """Every session, everywhere — used by a password change and by account deletion.

    **Two stores, and for a long time this function only emptied one of them.** Revoking the
    ``refresh_token`` rows ends the API's own sessions, and the web app has never held one: its
    session is an Auth.js JWT cookie, it mints its own access tokens from the shared secret, and
    ``current_principal`` checks only that ``sub`` names a row in ``app_user``. So the docstring
    above used to be true of the API and false of the session every real user actually has —
    a password change left whoever prompted it signed in for the remainder of thirty days
    (``NEEDS-MAULIK.md`` §22).

    Bumping ``session_epoch`` is what closes that. The number is stamped into the cookie at
    sign-in and compared on every gated render, so every cookie issued before this line fails
    that comparison on the caller's next navigation.

    The bump happens even when no refresh token was live. The two stores are independent, and
    conditioning one on the other would mean an OTP-only account — which may have no refresh row
    to revoke — kept its web session through a password change.
    """
    await session.execute(
        update(AppUser)
        .where(AppUser.id == user_id)
        .values(session_epoch=AppUser.session_epoch + 1),
        # `fetch`, spelled out rather than left to `synchronize_session="auto"`. The reset-password
        # path calls `set_password` (which lands here) and *then* `issue_session`, and the
        # `SessionOut` it builds reads `user.session_epoch` off the same ORM object this statement
        # just changed underneath. Without synchronisation that attribute is the pre-bump value,
        # so the brand-new session would be stamped with the generation this call invalidated —
        # and the user would be signed out of the session their password reset just created.
        # `auto` happens to choose `fetch` here, because `session_epoch + 1` is a SQL expression
        # it cannot evaluate in Python; depending on that inference is depending on a fallback.
        execution_options={"synchronize_session": "fetch"},
    )
    return await _revoke_where(session, RefreshToken.user_id == user_id, reason)


async def _revoke_where(session: AsyncSession, predicate: ColumnElement[bool], reason: str) -> int:
    """Revoke every *live* token matching ``predicate``, returning the count.

    ``RETURNING id`` rather than ``rowcount``: the async result object does not expose a row count
    for an UPDATE across every driver, and the ids are what a reuse-detection log line wants
    anyway.
    """
    rows = (
        await session.execute(
            update(RefreshToken)
            .where(predicate, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now(), revoked_reason=reason)
            .returning(RefreshToken.id)
        )
    ).scalars()
    return len(list(rows))


async def rotate_refresh(session: AsyncSession, supplied: str, settings: Settings) -> IssuedSession:
    """docs/11: "**rotating** refresh". Every use mints a successor and retires the predecessor.

    Reuse detection is the reason the retired row is kept. If a token that has already been
    rotated away (or revoked) arrives again, either the cookie was stolen and replayed or the
    legitimate client raced itself; the safe response to both is to revoke the entire family and
    make everyone sign in again. RFC 9700 §4.14.2.
    """
    row = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == digest(supplied))
        )
    ).scalar_one_or_none()
    if row is None:
        raise InvalidCredentials("That refresh token is not valid.")

    if row.revoked_at is not None:
        revoked = await revoke_family(session, row.family_id, "reuse")
        log.warning(
            "refresh token reuse detected",
            extra={"family_id": row.family_id, "user_id": row.user_id, "revoked": revoked},
        )
        raise TokenReused("That refresh token has already been used.")

    if row.expires_at <= now():
        row.revoked_at = now()
        row.revoked_reason = "expired"
        await session.flush()
        raise InvalidCredentials("That refresh token has expired.")

    user = (
        await session.execute(
            select(AppUser).where(AppUser.id == row.user_id, AppUser.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if user is None:
        await revoke_family(session, row.family_id, "deleted")
        raise InvalidCredentials("That refresh token refers to an account that no longer exists.")

    issued = await issue_session(session, user, settings, family_id=row.family_id)
    successor = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == digest(issued.refresh_token))
        )
    ).scalar_one()
    row.revoked_at = now()
    row.revoked_reason = "rotated"
    row.replaced_by_id = successor.id
    await session.flush()
    return issued


async def revoke_one(session: AsyncSession, supplied: str, reason: str) -> None:
    """Logout. Idempotent: a token we do not recognise is already not usable."""
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == digest(supplied), RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now(), revoked_reason=reason)
    )


# ---------------------------------------------------------------------------
# Password login
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Erasure — docs/11 §Compliance (DPDP), Prompt 12 §5
# ---------------------------------------------------------------------------


async def request_deletion(
    session: AsyncSession, user: AppUser, settings: Settings
) -> AccountDeletion:
    """Soft-delete now, hard-delete after the window. Signing in again cancels it."""
    moment = now()
    user.deleted_at = moment
    existing = (
        await session.execute(select(AccountDeletion).where(AccountDeletion.user_id == user.id))
    ).scalar_one_or_none()
    purge_after = moment + dt.timedelta(days=settings.account_purge_after_days)
    if existing is None:
        existing = AccountDeletion(user_id=user.id, purge_after=purge_after)
        session.add(existing)
    else:
        existing.purge_after = purge_after
        existing.cancelled_at = None
    await revoke_all_for_user(session, user.id, "deleted")
    await session.flush()
    return existing


async def cancel_deletion(session: AsyncSession, user: AppUser) -> bool:
    """Undo a pending erasure. Returns False when there was nothing pending."""
    row = (
        await session.execute(
            select(AccountDeletion).where(
                AccountDeletion.user_id == user.id,
                AccountDeletion.cancelled_at.is_(None),
                AccountDeletion.purged_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    row.cancelled_at = now()
    user.deleted_at = None
    await session.flush()
    return True
