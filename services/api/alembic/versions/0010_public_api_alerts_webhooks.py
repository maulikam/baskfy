"""API keys, screen alerts and outbound webhooks (Prompt 20).

Six tables, none of them in docs/04. The reasoning per table is in
``decile_core.models.integrations`` and the decision is recorded in ``docs/DECISIONS.md`` §20.

* ``api_key`` / ``api_key_usage_daily`` — Prompt 20 §1. Keys are hashed at rest; the clear-text
  ``prefix`` is what the lookup indexes on and what the UI shows.
* ``screen_alert`` / ``screen_alert_delivery`` — Prompt 20 §3. The delivery row's
  ``(alert_id, as_of)`` unique constraint is what makes a re-run of a night's dispatch send
  nothing (CLAUDE.md house rule 7).
* ``webhook_endpoint`` / ``webhook_delivery`` — Prompt 20 §4. No signing secret is stored; it is
  derived from ``(public_id, secret_version)`` — see ``decile_api.webhooks``.

Nothing here is destructive. The downgrade drops exactly the six tables it created, returning the
schema to 0009.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_public_api_alerts_webhooks"
down_revision: str | None = "0009_admin_and_overrides"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "api_key",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("public_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("prefix", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column(
            "scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(), nullable=True),
        sa.Column("rotated_from_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_api_key"),
        sa.UniqueConstraint("public_id", name="uq_api_key_public_id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name="fk_api_key_user_id_app_user", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["rotated_from_id"], ["api_key.id"], name="fk_api_key_rotated_from_id_api_key"
        ),
    )
    op.create_index("ix_api_key_user_id", "api_key", ["user_id"])
    # Unique, because verification looks a key up by its clear-text prefix and must find at most
    # one row: a second row with the same prefix would make "which key is this" ambiguous.
    op.create_index("uq_api_key_prefix", "api_key", ["prefix"], unique=True)

    op.create_table(
        "api_key_usage_daily",
        sa.Column("api_key_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("requests", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("throttled", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("api_key_id", "date", name="pk_api_key_usage_daily"),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_key.id"],
            name="fk_api_key_usage_daily_api_key_id_api_key",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "screen_alert",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("public_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("screen_id", sa.BigInteger(), nullable=False),
        sa.Column("frequency", sa.String(), nullable=False),
        sa.Column("weekday", sa.SmallInteger(), nullable=True),
        sa.Column("top_n", sa.Integer(), nullable=True),
        sa.Column("min_move", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("digest", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("unsubscribe_token_hash", sa.String(), nullable=False),
        sa.Column("last_run_id", sa.BigInteger(), nullable=True),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_screen_alert"),
        sa.UniqueConstraint("public_id", name="uq_screen_alert_public_id"),
        sa.UniqueConstraint("user_id", "screen_id", name="uq_screen_alert_user_id_screen_id"),
        sa.CheckConstraint(
            "frequency IN ('daily', 'weekly')", name="ck_screen_alert_screen_alert_frequency"
        ),
        sa.CheckConstraint("min_move >= 1", name="ck_screen_alert_screen_alert_min_move"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_screen_alert_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["screen_id"],
            ["screen.id"],
            name="fk_screen_alert_screen_id_screen",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["last_run_id"],
            ["screen_run.id"],
            name="fk_screen_alert_last_run_id_screen_run",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "uq_screen_alert_unsubscribe_token_hash",
        "screen_alert",
        ["unsubscribe_token_hash"],
        unique=True,
    )

    op.create_table(
        "screen_alert_delivery",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("alert_id", sa.BigInteger(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("previous_as_of", sa.Date(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("entry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("exit_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("change_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_screen_alert_delivery"),
        sa.UniqueConstraint("alert_id", "as_of", name="uq_screen_alert_delivery_alert_id_as_of"),
        sa.CheckConstraint(
            "status IN ('sent', 'skipped', 'failed')",
            name="ck_screen_alert_delivery_screen_alert_delivery_status",
        ),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["screen_alert.id"],
            name="fk_screen_alert_delivery_alert_id_screen_alert",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "webhook_endpoint",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("public_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("screen_id", sa.BigInteger(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("secret_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "events",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_reason", sa.String(), nullable=True),
        sa.Column("last_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_endpoint"),
        sa.UniqueConstraint("public_id", name="uq_webhook_endpoint_public_id"),
        sa.UniqueConstraint("user_id", "screen_id", "url", name="uq_webhook_endpoint_target"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_webhook_endpoint_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["screen_id"],
            ["screen.id"],
            name="fk_webhook_endpoint_screen_id_screen",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_webhook_endpoint_user_id", "webhook_endpoint", ["user_id"])

    op.create_table(
        "webhook_delivery",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("endpoint_id", sa.BigInteger(), nullable=False),
        sa.Column("event", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_delivery"),
        sa.UniqueConstraint(
            "endpoint_id", "idempotency_key", name="uq_webhook_delivery_endpoint_id_key"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed')",
            name="ck_webhook_delivery_webhook_delivery_status",
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["webhook_endpoint.id"],
            name="fk_webhook_delivery_endpoint_id_webhook_endpoint",
            ondelete="CASCADE",
        ),
    )
    # The sweeper's query is "pending rows whose next attempt is due", in that order.
    op.create_index(
        "ix_webhook_delivery_next_attempt_at",
        "webhook_delivery",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_delivery_next_attempt_at", table_name="webhook_delivery")
    op.drop_table("webhook_delivery")
    op.drop_index("ix_webhook_endpoint_user_id", table_name="webhook_endpoint")
    op.drop_table("webhook_endpoint")
    op.drop_table("screen_alert_delivery")
    op.drop_index("uq_screen_alert_unsubscribe_token_hash", table_name="screen_alert")
    op.drop_table("screen_alert")
    op.drop_table("api_key_usage_daily")
    op.drop_index("uq_api_key_prefix", table_name="api_key")
    op.drop_index("ix_api_key_user_id", table_name="api_key")
    op.drop_table("api_key")
