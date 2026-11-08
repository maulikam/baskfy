"""desk_score_daily: the weekly book's Momentum Quality Score, stored per day (ranking 2.B).

docs/ranking/PLAN.md C2. The screener reads this table and never re-scores; the nightly worker
fills it from `baskfy_core.desk_score_service.score_day`, which runs the desk's own scan and score
over the whole `nse_cash` universe. Rejected rows are stored with NULL `score` and `score_rank`.

Revision ID: 0047_desk_score_daily
Revises: 0046_ranking_factors
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047_desk_score_daily"
down_revision: str | None = "0046_ranking_factors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "desk_score_daily",
        sa.Column("instrument_id", sa.BigInteger(), sa.ForeignKey("instrument.id"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("score", sa.Numeric(6, 1), nullable=True),
        sa.Column("score_rank", sa.Integer(), nullable=True),
        sa.Column("a_trend", sa.Numeric(8, 4), nullable=True),
        sa.Column("b_momentum", sa.Numeric(8, 4), nullable=True),
        sa.Column("c_sharpe", sa.Numeric(8, 4), nullable=True),
        sa.Column("d_consistency", sa.Numeric(8, 4), nullable=True),
        sa.Column("e_liquidity", sa.Numeric(8, 4), nullable=True),
        sa.Column("f_penalty", sa.Numeric(8, 4), nullable=True),
        sa.Column("ext_over_20dma", sa.Numeric(10, 4), nullable=True),
        sa.Column("reject", sa.String(200), server_default="", nullable=False),
        sa.Column("score_version", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint("instrument_id", "date", name="pk_desk_score_daily"),
    )
    op.create_index(
        "ix_desk_score_daily_date_score_rank", "desk_score_daily", ["date", "score_rank"]
    )


def downgrade() -> None:
    op.drop_index("ix_desk_score_daily_date_score_rank", table_name="desk_score_daily")
    op.drop_table("desk_score_daily")
