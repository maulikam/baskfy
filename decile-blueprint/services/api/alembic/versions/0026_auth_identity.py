"""``auth_identity`` — Google sign-in becomes the only way in.

Registration, the email OTP and the password all go away in this change
(``docs/DECISIONS-MERGE.md`` M46). The immediate cause was operational: SES is in its sandbox, so
every verification mail to an unverified recipient was refused with a 554 and five consecutive
sign-ups reached a dead end while the form answered 202. The deeper reason is that a product with
five users and zero passwords set has nothing to migrate, and delegating identity to Google
removes an entire class of things that can be got wrong — password storage, reset links, OTP
replay, lockout thresholds — in exchange for one federated dependency.

WHY A TABLE AND NOT A COLUMN
-----------------------------
``app_user.google_subject`` would be smaller and would be wrong at the second provider. An
identity is a fact with a provider, a subject and a login history, not an attribute of an account.

WHY THE BACKFILL IS EMPTY
--------------------------
There is deliberately no data migration binding existing users to Google subjects, because there
is no way to know one without the user signing in: a subject is Google's to state, not ours to
guess. Existing accounts are adopted on first sign-in — ``auth_service.link_google_identity``
matches the verified Google address against ``app_user.email`` once, writes the identity row, and
from then on the row is what is looked up. Matching on a *Google-verified* address is safe in a
way that matching on a self-asserted one would not be.

Revision ID: 0026_auth_identity
Revises: 0025_session_epoch
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_auth_identity"
down_revision: str | None = "0025_session_epoch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_identity",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("app_user.id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("email_at_provider", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The lookup a sign-in performs. Unique because one Google account bound to two app users
    # would make "who is this" a coin toss.
    op.create_unique_constraint(
        "uq_auth_identity_provider_subject", "auth_identity", ["provider", "subject"]
    )
    # And one identity per provider per user, which is what makes the sign-in upsert idempotent
    # (house rule 7: re-running produces identical rows).
    op.create_unique_constraint(
        "uq_auth_identity_user_provider", "auth_identity", ["user_id", "provider"]
    )
    op.create_index("ix_auth_identity_user_id", "auth_identity", ["user_id"])


def downgrade() -> None:
    # Dropping the table loses the provider bindings, and the accounts survive. A re-upgrade
    # re-adopts each user on their next sign-in through the same verified-email match the
    # docstring describes, so this is reversible in effect and not only in schema.
    op.drop_index("ix_auth_identity_user_id", table_name="auth_identity")
    op.drop_table("auth_identity")
