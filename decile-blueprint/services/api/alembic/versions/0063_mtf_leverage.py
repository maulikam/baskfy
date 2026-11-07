"""Daily Zerodha MTF leverage per NSE symbol (Maulik, 7 Nov 2026): the desk quotes it once each
morning so multi-buy reads it instead of asking the broker per stock.

Revision ID: 0063_mtf_leverage
Revises: 0062_stock_regime_signal
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063_mtf_leverage"
down_revision: str | None = "0062_stock_regime_signal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mtf_leverage",
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("leverage", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("as_of", "symbol"),
    )


def downgrade() -> None:
    op.drop_table("mtf_leverage")
