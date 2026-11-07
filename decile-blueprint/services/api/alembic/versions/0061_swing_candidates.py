"""Index-led regime: one row per regime run and its scanner candidates (Maulik, 6 Nov 2026).

``swing_regime_run`` holds each run's verdict (leading indices, gate open or shut);
``swing_candidates`` holds the scanner rows over the leading indices' constituents.
Written by the worker only; nothing here builds or confirms a plan.

Revision ID: 0061_swing_candidates
Revises: 0060_index_service
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0061_swing_candidates"
down_revision: str | None = "0060_index_service"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "swing_regime_run",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("gate_open", sa.Boolean(), nullable=False),
        sa.Column("leading_count", sa.Integer(), nullable=False),
        sa.Column("min_leading", sa.Integer(), nullable=False),
        sa.Column("evaluated", sa.Integer(), nullable=False),
        sa.Column("universe_size", sa.Integer(), nullable=False),
        sa.Column("leading", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("kind IN ('nightly', 'intraday')", name=op.f("ck_swing_regime_run_kind")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_swing_regime_run")),
    )
    op.create_index(
        "ix_swing_regime_run_mode_run_at", "swing_regime_run", ["mode", sa.text("run_at DESC")]
    )
    op.create_table(
        "swing_candidates",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("scanner", sa.Text(), nullable=False),
        sa.Column("setup", sa.Text(), nullable=False),
        sa.Column("entry", sa.Numeric(14, 2), nullable=True),
        sa.Column("stop", sa.Numeric(14, 2), nullable=True),
        sa.Column("risk_pct", sa.Numeric(8, 2), nullable=True),
        sa.Column("via_indices", postgresql.JSONB(), nullable=False),
        sa.Column("universe_score", sa.Numeric(10, 4), nullable=False),
        sa.Column("score", sa.Numeric(10, 4), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["swing_regime_run.id"],
            name=op.f("fk_swing_candidates_run_id_swing_regime_run"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_swing_candidates")),
    )
    op.create_index("ix_swing_candidates_run_at", "swing_candidates", [sa.text("run_at DESC")])
    op.create_index("ix_swing_candidates_symbol", "swing_candidates", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_swing_candidates_symbol", table_name="swing_candidates")
    op.drop_index("ix_swing_candidates_run_at", table_name="swing_candidates")
    op.drop_table("swing_candidates")
    op.drop_index("ix_swing_regime_run_mode_run_at", table_name="swing_regime_run")
    op.drop_table("swing_regime_run")
