"""The staff surface (Prompt 17 deliverable 4).

Three additions to docs/04, each because a numbered Prompt 17 deliverable needs storage the
bundle does not define. The reasoning is in ``decile_core.models.admin`` and the decision is
recorded in ``docs/DECISIONS.md`` §17.

1. ``app_user.is_staff`` — docs/09 §Observability puts ``/admin/pipeline`` "behind staff auth"
   and never says what staff is. Defaults to ``false``, so the migration grants nobody anything;
   the first staff account is made by hand (``docs/runbooks/pipeline-failed.md`` says how).
2. ``entitlement_override`` — "entitlement override" cannot be a write to ``plan.features``
   (which would change every account on the plan) or a synthetic ``subscription`` row (which
   would make the billing history lie).
3. ``admin_action`` — nothing in the bundle asks for it. A privileged, un-undoable action with no
   record of who took it is a hole that is only noticed after it matters.

Nothing here is destructive and the downgrade is exact: the two tables are dropped and the column
is removed, which returns the schema to 0008 byte for byte.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009_admin_and_overrides"
down_revision: str | None = "0008_performance_indexes"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column("is_staff", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    op.create_table(
        "entitlement_override",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("feature", sa.String(), nullable=False),
        sa.Column("effect", sa.String(), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=True),
        sa.Column("granted_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_entitlement_override"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_entitlement_override_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"],
            ["app_user.id"],
            name="fk_entitlement_override_granted_by_user_id_app_user",
        ),
        sa.CheckConstraint(
            "effect IN ('grant', 'revoke')",
            name="ck_entitlement_override_entitlement_override_effect",
        ),
    )
    # One override per (account, feature). A second row for the same pair would make "what does
    # this account get" depend on which row the resolver happened to read first.
    op.create_index(
        "uq_entitlement_override_user_feature",
        "entitlement_override",
        ["user_id", "feature"],
        unique=True,
    )

    op.create_table(
        "admin_action",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("detail", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_admin_action"),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["app_user.id"], name="fk_admin_action_actor_user_id_app_user"
        ),
    )
    op.create_index("ix_admin_action_created_at", "admin_action", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_admin_action_created_at", table_name="admin_action")
    op.drop_table("admin_action")
    op.drop_index("uq_entitlement_override_user_feature", table_name="entitlement_override")
    op.drop_table("entitlement_override")
    op.drop_column("app_user", "is_staff")
