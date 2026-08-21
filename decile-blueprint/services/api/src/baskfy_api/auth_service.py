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

from sqlalchemy import ColumnElement, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.auth import encode_token
from baskfy_api.email import Mailer
from baskfy_api.email import templates as mail
from baskfy_api.security import (
    digest,
    hash_password,
    needs_rehash,
    new_opaque_token,
    new_otp,
    verify_password,
)
from baskfy_api.settings import Settings
from baskfy_core.models import (
    AccountDeletion,
    AppUser,
    AuthLockout,
    AuthToken,
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


async def _lockout_row(session: AsyncSession, identifier: str) -> AuthLockout | None:
    return (
        await session.execute(select(AuthLockout).where(AuthLockout.identifier == identifier))
    ).scalar_one_or_none()


async def assert_not_locked(session: AsyncSession, identifier: str, settings: Settings) -> None:
    """Raise :class:`AccountLocked` while a lockout is in force."""
    row = await _lockout_row(session, identifier)
    if row is None or row.locked_until is None:
        return
    remaining = (row.locked_until - now()).total_seconds()
    if remaining > 0:
        raise AccountLocked(int(remaining) + 1)
    # The window has passed. Clear it here rather than in a job: the next attempt is exactly when
    # anyone cares, and a sweeper would be a moving part for state nobody reads in between.
    await _reset_failures(session, identifier)


async def _reset_failures(session: AsyncSession, identifier: str) -> None:
    await session.execute(delete(AuthLockout).where(AuthLockout.identifier == identifier))


async def record_failure(
    session: AsyncSession,
    identifier: str,
    settings: Settings,
    mailer: Mailer,
) -> None:
    """Count one failed attempt, and lock plus notify once the count reaches the limit.

    The count is over a rolling window (``auth_failure_window_minutes``), so nine failures last
    month plus one today is not a lockout. Failures against an address with no account are counted
    the same way — see the module docstring.
    """
    moment = now()
    window_start = moment - dt.timedelta(minutes=settings.auth_failure_window_minutes)
    row = await _lockout_row(session, identifier)

    if row is None:
        row = AuthLockout(identifier=identifier, failures=0)
        session.add(row)
    if row.last_failure_at is not None and row.last_failure_at < window_start:
        row.failures = 0
        row.notified_at = None

    row.failures += 1
    row.first_failure_at = row.first_failure_at or moment
    row.last_failure_at = moment

    if row.failures >= settings.auth_max_failures:
        row.locked_until = moment + dt.timedelta(minutes=settings.auth_lockout_minutes)
        if row.notified_at is None:
            # Only to an address that actually has an account: mailing a stranger to say their
            # non-existent account is locked is both confusing and a disclosure of nothing useful.
            user = await find_user(session, identifier)
            if user is not None:
                await mailer.deliver(
                    mail.lockout(user.email, settings.auth_lockout_minutes, row.failures)
                )
            row.notified_at = moment
    await session.flush()


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
# One-time codes — OTP, email verification, password reset
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CodeSpec:
    """What kind of one-time code to mint.

    A record rather than four keyword arguments, because the four always travel together and the
    three call sites each pass a fixed combination — the OTP, the verification link, the reset
    link. The named constructors below are those three.
    """

    purpose: str
    ttl_seconds: int
    #: Digits, for something read out of an email; otherwise a 256-bit URL-safe secret for a link.
    numeric: bool
    length: int = 6

    @staticmethod
    def otp(settings: Settings) -> CodeSpec:
        return CodeSpec("otp", settings.otp_ttl_seconds, numeric=True, length=settings.otp_length)

    @staticmethod
    def email_verify(settings: Settings) -> CodeSpec:
        return CodeSpec("email_verify", settings.email_verification_ttl_seconds, numeric=False)

    @staticmethod
    def password_reset(settings: Settings) -> CodeSpec:
        return CodeSpec("password_reset", settings.password_reset_ttl_seconds, numeric=False)


async def issue_code(
    session: AsyncSession, spec: CodeSpec, *, email: str, user: AppUser | None
) -> str:
    """Mint a code, store its digest, and return the plaintext for delivery.

    Any outstanding code for the same address and purpose is consumed first. Two live sign-in
    codes in one inbox is a support question, and it doubles the guessing surface for no gain.
    """
    await session.execute(
        update(AuthToken)
        .where(
            AuthToken.email == normalise_email(email),
            AuthToken.purpose == spec.purpose,
            AuthToken.consumed_at.is_(None),
        )
        .values(consumed_at=now())
    )
    plaintext = new_otp(spec.length) if spec.numeric else new_opaque_token()
    session.add(
        AuthToken(
            user_id=user.id if user is not None else None,
            email=normalise_email(email),
            purpose=spec.purpose,
            token_hash=digest(plaintext),
            expires_at=now() + dt.timedelta(seconds=spec.ttl_seconds),
        )
    )
    await session.flush()
    return plaintext


async def consume_code(
    session: AsyncSession, *, email: str, purpose: str, supplied: str, max_attempts: int
) -> AuthToken:
    """Verify and burn a code. Raises :class:`InvalidCredentials` on anything but a clean match.

    The lookup is by digest, so a wrong code finds nothing and the stored value is never compared
    as a string. A near-miss still costs the live code an attempt, which is what stops a six-digit
    space being walked inside its ten-minute life.
    """
    address = normalise_email(email)
    row = (
        await session.execute(
            select(AuthToken)
            .where(
                AuthToken.email == address,
                AuthToken.purpose == purpose,
                AuthToken.token_hash == digest(supplied),
                AuthToken.consumed_at.is_(None),
            )
            .order_by(AuthToken.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if row is None:
        await _burn_attempt(session, address, purpose, max_attempts)
        raise InvalidCredentials("That code is not valid.")
    if row.expires_at <= now():
        row.consumed_at = now()
        await session.flush()
        raise InvalidCredentials("That code has expired.")

    row.consumed_at = now()
    await session.flush()
    return row


async def consume_link_token(session: AsyncSession, *, purpose: str, supplied: str) -> AuthToken:
    """Burn a code that arrived in a link, where the address is not supplied separately.

    Verification and reset links carry a 256-bit secret and nothing else — asking the user to
    retype their address alongside it would add a step and no security, since the secret already
    proves inbox control. The lookup is by digest, so a wrong token finds nothing.
    """
    row = (
        await session.execute(
            select(AuthToken).where(
                AuthToken.purpose == purpose,
                AuthToken.token_hash == digest(supplied),
                AuthToken.consumed_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise InvalidCredentials("That link is not valid.")
    if row.expires_at <= now():
        row.consumed_at = now()
        await session.flush()
        raise InvalidCredentials("That link has expired.")
    row.consumed_at = now()
    await session.flush()
    return row


async def _burn_attempt(session: AsyncSession, email: str, purpose: str, max_attempts: int) -> None:
    """Charge a wrong guess to whichever code is currently live for this address."""
    live = (
        await session.execute(
            select(AuthToken)
            .where(
                AuthToken.email == email,
                AuthToken.purpose == purpose,
                AuthToken.consumed_at.is_(None),
            )
            .order_by(AuthToken.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if live is None:
        return
    live.attempts += 1
    if live.attempts >= max_attempts:
        live.consumed_at = now()
    await session.flush()


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
    """Every session, everywhere — used by a password change and by account deletion."""
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


async def authenticate_password(
    session: AsyncSession, email: str, password: str, settings: Settings
) -> AppUser:
    """Verify a password, upgrading the stored hash if the parameters have moved on.

    Raises :class:`InvalidCredentials` for a wrong password *and* for an unknown address, having
    paid the same Argon2id cost in both cases.
    """
    user = await find_user(session, email)
    stored = user.password_hash if user is not None else None
    if not verify_password(password, stored, settings) or user is None:
        raise InvalidCredentials("Those credentials are not valid.")

    if user.password_hash is not None and needs_rehash(user.password_hash, settings):
        # The only moment the plaintext exists to re-derive from.
        user.password_hash = hash_password(password, settings)
        await session.flush()
    return user


async def set_password(
    session: AsyncSession, user: AppUser, password: str, settings: Settings, *, reason: str
) -> None:
    """Change a password and end every other session.

    A password change that leaves old sessions alive does not evict whoever prompted it.
    """
    user.password_hash = hash_password(password, settings)
    await session.flush()
    await revoke_all_for_user(session, user.id, reason)
    await _reset_failures(session, user.email)


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
