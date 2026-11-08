"""broker_trade: every execution at a broker, from a Console export or Kite's same-day read.

NEEDS-MAULIK §32: "Postgres has no transaction table." This is the table `gates/kite-sync.md`
step 2 asked for. Numeric quantities and prices (house rule 9); one row per
(broker_account_id, exchange, trade_id), so re-importing writes nothing new (house rule 7).

Revision ID: 0049_broker_trade
Revises: 0048_broker_holdings_synced_at
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049_broker_trade"
down_revision: str | None = "0048_broker_holdings_synced_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "broker_trade",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column(
            "broker_account_id",
            sa.BigInteger(),
            sa.ForeignKey("broker_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("instrument_id", sa.BigInteger(), sa.ForeignKey("instrument.id"), nullable=True),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("isin", sa.String(), nullable=True),
        sa.Column("exchange", sa.String(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 4), nullable=False),
        sa.Column("price", sa.Numeric(18, 4), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trade_id", sa.String(), nullable=False),
        sa.Column("order_id", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "broker_account_id", "exchange", "trade_id", name="uq_broker_trade_account_trade"
        ),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="broker_trade_side"),
        sa.CheckConstraint("source IN ('CONSOLE_CSV', 'KITE_API')", name="broker_trade_source"),
        sa.CheckConstraint("quantity > 0", name="broker_trade_quantity_positive"),
        sa.CheckConstraint("price >= 0", name="broker_trade_price_non_negative"),
    )
    op.create_index(
        "ix_broker_trade_user_instrument_date",
        "broker_trade",
        ["user_id", "instrument_id", "trade_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_broker_trade_user_instrument_date", table_name="broker_trade")
    op.drop_table("broker_trade")
