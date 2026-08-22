"""market_health_daily gains pct_above_20dma — M14 §1.

The desk's cash bands are calibrated on "percentage of the universe above its 20-day moving
average" and the pipeline computed 50- and 200-day breadth but not 20. `DESK-PARITY.md` §P1.9
named the two ways out: add the column, or re-express the desk's bands against the 50-day figure.

The second is a strategy change wearing a wiring change's clothes — the bands were fitted to the
20-day number, and swapping the input silently re-tunes when the desk holds cash. So: add the
column. `factor_daily.ma_20` already exists, so this is arithmetic the pipeline can already do.

Nullable, like its three siblings: rows written before this migration have no 20-day figure and
should say so rather than claim zero. `run_compute_market_health` backfills on its next pass.

Revision ID: 0011_breadth_above_20dma
Revises: 0010_public_api_alerts_webhooks
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011_breadth_above_20dma"
down_revision: str | None = "0010_public_api_alerts_webhooks"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "market_health_daily",
        sa.Column("pct_above_20dma", sa.Numeric(precision=7, scale=4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("market_health_daily", "pct_above_20dma")
