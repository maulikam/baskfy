"""Instrument watchlist + discover preferences tables (AF lane I).

Revision ID: 0045_instrument_watch_and_prefs
Revises: 0044_kite_adjusted_and_ohl_raw
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_instrument_watch_and_prefs"
down_revision: str | None = "0044_kite_adjusted_and_ohl_raw"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instrument_watch_item",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), sa.ForeignKey("instrument.id"), nullable=False),
        sa.Column("watched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("close_at_watch", sa.Numeric(18, 2), nullable=True),
        sa.UniqueConstraint(
            "user_id", "instrument_id", name="uq_instrument_watch_item_user_instrument"
        ),
    )
    op.create_index("ix_instrument_watch_item_user_id", "instrument_watch_item", ["user_id"])

    op.create_table(
        "user_discover_preferences",
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("goal", sa.String(), nullable=False),
        sa.Column("horizon", sa.String(), nullable=False),
        sa.Column("risk", sa.String(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("rebalance", sa.String(), nullable=False),
        sa.Column("onboarding_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("user_discover_preferences")
    op.drop_index("ix_instrument_watch_item_user_id", table_name="instrument_watch_item")
    op.drop_table("instrument_watch_item")
