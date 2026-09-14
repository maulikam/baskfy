"""broker_account.holdings_synced_at: the instant a live holdings sync last persisted.

The Portfolio page's sync status read ``broker_cash.as_of``, and nothing writes ``broker_cash`` —
so an account whose holdings had synced was reported as "never synced" (14 Sep 2026). The sync
now stamps this column and the status reads it.

No backfill. ``portfolio_holding.added_on`` is preserved across syncs, so it is the day a name
was first seen, not the day of the last sync; stamping it here would claim a sync date nothing
recorded. The next sync fills the column.

Revision ID: 0048_broker_holdings_synced_at
Revises: 0047_desk_score_daily
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048_broker_holdings_synced_at"
down_revision: str | None = "0047_desk_score_daily"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "broker_account",
        sa.Column("holdings_synced_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("broker_account", "holdings_synced_at")
