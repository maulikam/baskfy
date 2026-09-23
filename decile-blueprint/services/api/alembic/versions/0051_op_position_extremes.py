"""OP11: `op_position.trough_value` — the worst marked P&L, beside `peak_value`, for MAE/MFE.

`docs/options/06` OP11: "Close → `op_journal` with the cost breakdown and MAE/MFE". `op_position`
carried `peak_value` (the best marked P&L) and nothing for the worst, so the journal could not say
how far a trade went against it. Both are P&L **per unit, in points**, signed so that a gain is
positive whatever the structure (a condor's credit decaying is a gain), and the desk's monitor keeps
them as it marks (DECISIONS-OP OP11.2). Nullable: a position never marked has no extremes.

Revision ID: 0051_op_position_extremes
Revises: 0050_options
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0051_op_position_extremes"
down_revision: str | None = "0050_options"
branch_labels = None
depends_on = None

PRICE = sa.Numeric(precision=18, scale=2)


def upgrade() -> None:
    op.add_column("op_position", sa.Column("trough_value", PRICE, nullable=True))


def downgrade() -> None:
    op.drop_column("op_position", "trough_value")
