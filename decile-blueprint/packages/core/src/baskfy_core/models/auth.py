"""Authentication tables — docs/11 §Security, docs/07 §"Account & billing" (Prompt 12).

**None of these are in docs/04.** docs/04's account section defines `app_user` with a
``password_hash`` column and nothing else about authentication: no refresh tokens, no OTP codes,
no lockout state, no deletion window. Every table here is an addition, and each one exists because
a numbered requirement cannot be met without it:

============================  ==========================================================
``auth_verification_token``   Auth.js's own adapter contract. `apps/web/src/lib/auth/adapter.ts`
                              is written against it (`createVerificationToken` /
                              `useVerificationToken`), and docs/02 locks Auth.js v5.
``auth_token``                docs/11: "OTP login as the default path". A one-time code has to
                              be stored hashed, with an expiry and an attempt count, or it is a
                              password that never changes.
``refresh_token``             docs/11: "rotating refresh in an httpOnly ... cookie". Rotation is
                              only meaningful if the old token is remembered long enough to
                              notice it being replayed.
``auth_lockout``              docs/11: "account lockout after 10 failures with email
                              notification". Durable rather than in Redis: a lockout that a cache
                              flush clears is not a lockout, and the notification must not be
                              sent twice.
``account_deletion``          docs/11 §Compliance: "DPDP Act: ... deletion endpoints", with
                              PROMPTS.md Prompt 12 §5's "7-day soft-delete window".
``consent_record``            docs/11 §Compliance: "DPDP Act: **consent record**, ...".
============================  ==========================================================

Written up in `docs/04c-auth-tables-addendum.md`.

Nothing here stores a secret in the clear. Codes and refresh tokens are stored as SHA-256 digests
of the value that was sent, so a database dump cannot be replayed against the service — the only
copy of the plaintext is the one in the user's email or cookie.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.models.base import Base, BigIntPk, CreatedAt

#: What a one-time code is for. One table rather than three, because the lifecycle is identical —
#: issue, deliver, verify once, expire — and three tables would be three places to forget to
#: expire something.
TOKEN_PURPOSES: tuple[str, ...] = ("otp", "email_verify", "password_reset")

#: docs/11 §Compliance: "DPDP Act: consent record". One row per thing consented to.
CONSENT_KINDS: tuple[str, ...] = ("terms", "privacy", "marketing")


class AuthVerificationToken(Base):
    """Auth.js's `VerificationToken` contract, verbatim.

    The column names are Auth.js's, not ours (`expires`, not `expires_at`), because the adapter
    is written against the library's shape and renaming them here would mean renaming them there.

    Since Prompt 12 the OTP flow runs through `POST /auth/request-otp` and `/auth/verify-otp` on
    the API, so nothing writes this table in the normal path. It exists because the adapter
    satisfies an interface that includes these two methods, and an adapter whose methods raise is
    a worse answer than one whose table is empty.
    """

    __tablename__ = "auth_verification_token"
    __table_args__ = (PrimaryKeyConstraint("identifier", "token"),)

    identifier: Mapped[str] = mapped_column(String, nullable=False)
    token: Mapped[str] = mapped_column(String, nullable=False)
    expires: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuthToken(Base):
    """A one-time code: an OTP, an email verification, or a password reset.

    ``user_id`` is nullable on purpose. An OTP can be requested for an address that has no account
    yet — docs/11 makes OTP "the default path", and requiring registration first would make the
    default path the second step. ``email`` is always set; the user is resolved at verification.

    ``token_hash`` is SHA-256 of the code as delivered. Not Argon2id: these live for minutes, are
    single-use, are rate-limited *and* attempt-limited, and the work factor would be paid on every
    verification of a value that has none of a password's reuse risk. `docs/12a` §3.
    """

    __tablename__ = "auth_token"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('otp', 'email_verify', 'password_reset')", name="auth_token_purpose"
        ),
        UniqueConstraint("token_hash", name="auth_token_hash_key"),
        Index("ix_auth_token_email_purpose", "email", "purpose"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE")
    )
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    token_hash: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Wrong guesses against *this* code. A code is burned after a few, so a six-digit space
    #: cannot be walked before it expires.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[CreatedAt]


class RefreshToken(Base):
    """One issued refresh token, and the family it belongs to.

    docs/11 §Security: "rotating refresh in an httpOnly, `SameSite=Lax`, Secure cookie."

    Rotation means every use mints a successor and revokes the predecessor. The predecessor's row
    stays, so presenting it a second time is *detectable*: a used-or-revoked token arriving again
    means either a stolen cookie or a race, and the safe response to both is to revoke the whole
    ``family_id`` — every descendant of the original login. That is the standard reuse-detection
    construction (RFC 9700 §4.14.2), and it is why this is a table and not a Redis key.
    """

    __tablename__ = "refresh_token"
    __table_args__ = (
        UniqueConstraint("token_hash", name="refresh_token_hash_key"),
        Index("ix_refresh_token_family", "family_id"),
        Index("ix_refresh_token_user", "user_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    #: Shared by every token descended from one login. Revoked as a unit on reuse.
    family_id: Mapped[str] = mapped_column(String, nullable=False)
    token_hash: Mapped[str] = mapped_column(String, nullable=False)
    issued_at: Mapped[CreatedAt]
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Set when the token is rotated away, revoked by logout, or killed by a family revocation.
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Why it was revoked: 'rotated' | 'logout' | 'reuse' | 'password_change' | 'deleted'.
    revoked_reason: Mapped[str | None] = mapped_column(String)
    #: The successor minted when this one was used, for audit.
    replaced_by_id: Mapped[int | None] = mapped_column(BigInteger)


class AuthLockout(Base):
    """Failed-attempt state for one identifier — docs/11: "lockout after 10 failures".

    Keyed by the *identifier presented*, not by user id, so failures against an address with no
    account are counted too. Otherwise the lockout is an oracle: an attacker learns which
    addresses exist by which ones can be locked.

    Durable, not Redis. A cache flush must not silently unlock every account, and
    ``notified_at`` has to be remembered or the lockout email is sent on every subsequent attempt.
    """

    __tablename__ = "auth_lockout"

    identifier: Mapped[str] = mapped_column(CITEXT, primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_failure_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: When the "your account is locked" email was last sent, so it is sent once per lockout.
    notified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class AccountDeletion(Base):
    """A pending erasure — docs/11 §Compliance (DPDP) and Prompt 12 §5's seven-day window.

    Soft, then hard. The row is what makes the account unusable immediately while leaving seven
    days to change one's mind; a nightly job purges rows past ``purge_after``. Deleting on the
    spot would be irreversible on a misclick, and DPDP asks for erasure, not for haste.
    """

    __tablename__ = "account_deletion"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True
    )
    requested_at: Mapped[CreatedAt]
    purge_after: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Set by the purge job, so a completed erasure is auditable without keeping the data.
    purged_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ConsentRecord(Base):
    """docs/11 §Compliance: "DPDP Act: **consent record**".

    Append-only. A consent record whose row is updated in place cannot answer "what did they agree
    to, and when" — which is the only question it exists to answer. Withdrawal is a new row with
    ``withdrawn_at`` set, not an UPDATE of the grant.
    """

    __tablename__ = "consent_record"
    __table_args__ = (
        CheckConstraint("kind IN ('terms', 'privacy', 'marketing')", name="consent_record_kind"),
        Index("ix_consent_record_user", "user_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    #: Which version of the document was agreed to, so a later revision is a new consent.
    document_version: Mapped[str] = mapped_column(String, nullable=False)
    granted_at: Mapped[CreatedAt]
    withdrawn_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Evidence, as DPDP expects. Never a password, never a token.
    source_ip: Mapped[str | None] = mapped_column(String)
    user_agent: Mapped[str | None] = mapped_column(String)
