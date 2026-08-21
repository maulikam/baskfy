"""Rebalance history (Prompt 14 §4).

One table, not in docs/04's DDL:

    portfolio_rebalance      PROMPTS.md Prompt 14 §4: "persist each computed rebalance so a user
                             can see what they were told and when". docs/04 defines `portfolio`
                             and `portfolio_holding` and stops there.

`payload` is the response body verbatim rather than a set of columns to re-render from — the
screen it was computed against can be edited or deleted afterwards, and the record must still say
what the user was shown. `screen_id` is therefore ON DELETE SET NULL, not CASCADE.

Also an index on `portfolio_holding.instrument_id`: the rebalance join and the "is this instrument
in any portfolio" question both start there, and the composite primary key
(`portfolio_id`, `instrument_id`) cannot serve a predicate on its second column.

Recorded in docs/DECISIONS.md §14.

Revision ID: 0007_portfolio_rebalance
Revises: 0006_billing_tables
Create Date: 2026-08-21 07:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_portfolio_rebalance"
down_revision: str | None = "0006_billing_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_rebalance",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("portfolio_id", sa.BigInteger(), nullable=False),
        sa.Column("screen_id", sa.BigInteger(), nullable=True),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("data_version", sa.BigInteger(), nullable=True),
        sa.Column("top_n", sa.Integer(), nullable=False),
        sa.Column("hold_buffer", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_id"],
            ["portfolio.id"],
            name=op.f("fk_portfolio_rebalance_portfolio_id_portfolio"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["screen_id"],
            ["screen.id"],
            name=op.f("fk_portfolio_rebalance_screen_id_screen"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portfolio_rebalance")),
    )
    op.create_index(
        op.f("ix_portfolio_rebalance_portfolio_id"),
        "portfolio_rebalance",
        ["portfolio_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_portfolio_holding_instrument_id"),
        "portfolio_holding",
        ["instrument_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_portfolio_holding_instrument_id"), table_name="portfolio_holding")
    op.drop_index(op.f("ix_portfolio_rebalance_portfolio_id"), table_name="portfolio_rebalance")
    op.drop_table("portfolio_rebalance")
