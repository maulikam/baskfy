"""Authentication, consent and erasure tables (Prompt 12).

Six tables and one column, none of which are in docs/04. Each is required by a numbered line of
docs/11 §Security or §"Compliance & legal (India)" that cannot be met without it — the reasoning
is on each model in `decile_core.models.auth` and collected in `docs/04c-auth-tables-addendum.md`.

    auth_verification_token   Auth.js v5's adapter contract (docs/02 locks Auth.js).
    auth_token                OTP / email verification / password reset codes, stored hashed.
    refresh_token             rotating refresh with family-level reuse detection.
    auth_lockout              "account lockout after 10 failures with email notification".
    account_deletion          DPDP erasure with Prompt 12 §5's seven-day window.
    consent_record            DPDP consent record, append-only.
    app_user.deleted_at       the soft-delete marker the window needs.

Nothing here stores a secret in the clear: codes and refresh tokens are SHA-256 digests of the
value delivered to the user.

Revision ID: 0005_auth_tables
Revises: 0004_index_member_source
Create Date: 2026-08-21 03:02:32.166093
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_auth_tables"
down_revision: str | None = "0004_index_member_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_lockout",
        sa.Column("identifier", postgresql.CITEXT(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("first_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("identifier", name=op.f("pk_auth_lockout")),
    )
    op.create_table(
        "auth_verification_token",
        sa.Column("identifier", sa.String(), nullable=False),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("expires", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("identifier", "token", name=op.f("pk_auth_verification_token")),
    )
    op.create_table(
        "account_deletion",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_account_deletion_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_account_deletion")),
    )
    op.create_table(
        "auth_token",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "purpose IN ('otp', 'email_verify', 'password_reset')",
            name=op.f("ck_auth_token_auth_token_purpose"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_auth_token_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_token")),
        sa.UniqueConstraint("token_hash", name="auth_token_hash_key"),
    )
    op.create_index("ix_auth_token_email_purpose", "auth_token", ["email", "purpose"], unique=False)
    op.create_table(
        "consent_record",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("document_version", sa.String(), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_ip", sa.String(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('terms', 'privacy', 'marketing')",
            name=op.f("ck_consent_record_consent_record_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_consent_record_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consent_record")),
    )
    op.create_index("ix_consent_record_user", "consent_record", ["user_id"], unique=False)
    op.create_table(
        "refresh_token",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("family_id", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(), nullable=True),
        sa.Column("replaced_by_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_refresh_token_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_token")),
        sa.UniqueConstraint("token_hash", name="refresh_token_hash_key"),
    )
    op.create_index("ix_refresh_token_family", "refresh_token", ["family_id"], unique=False)
    op.create_index("ix_refresh_token_user", "refresh_token", ["user_id"], unique=False)
    op.add_column("app_user", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("app_user", "deleted_at")
    op.drop_index("ix_refresh_token_user", table_name="refresh_token")
    op.drop_index("ix_refresh_token_family", table_name="refresh_token")
    op.drop_table("refresh_token")
    op.drop_index("ix_consent_record_user", table_name="consent_record")
    op.drop_table("consent_record")
    op.drop_index("ix_auth_token_email_purpose", table_name="auth_token")
    op.drop_table("auth_token")
    op.drop_table("account_deletion")
    op.drop_table("auth_verification_token")
    op.drop_table("auth_lockout")
