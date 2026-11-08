"""``sw_backtest_run`` — one row per run of the swing EOD backtest (``docs/swing/03`` §10, SW9).

    "A number, with its caveats, before any real money."  (docs/swing/06, SW9)

The engine is pure (``baskfy_core.swing.backtest``: bars in, a result out, no clock and no I/O);
this table is where the runner puts what it computed, so `/swing/journal` can show the latest
finished run and `02` §3.3's gate can point at it.

THREE THINGS TO KNOW BEFORE READING THE DDL
-------------------------------------------
**A run is a fact, not a slot.** Re-running the backtest inserts a *new* row; nothing updates a
stored ``stats``. The number on the page before the flag flipped must still be readable after a
detector recalibration produces a different one — the same reason ``sw_setup_daily`` is snapshotted
and ``sw_config_audit`` is append-only.

**``stats`` is ``BacktestResult.to_json()`` stored as-is.** ``params, trades, stats, by_setup,
by_year, equity_curve, funnel, ladder, caveats`` with every price a string of its exact decimal.
JSONB rather than columns because the result's shape belongs to the engine (contract C3 lets it
grow — the ``ladder`` trace is already an addition), and a column per statistic would need a
migration every time it did. ``params`` is stored separately, on the way *in*, so a run that
never finished still says what it was asked to do.

**A failure is a row too.** ``error`` and ``finished_at`` are written when the run raises; the
exception is re-raised after, so Celery sees it. A run with ``finished_at`` set and ``error``
null is finished; that is the one the journal reads.

Revision ID: 0029_swing_backtest
Revises: 0028_swing
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029_swing_backtest"
down_revision: str | None = "0028_swing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sw_backtest_run",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_sw_backtest_run_finished_after_started"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_sw_backtest_run_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sw_backtest_run")),
    )
    # The journal's one query: this user's latest *finished* run. `finished_at` leads so the
    # index answers "newest finished" without a sort over every run ever stored.
    op.create_index(
        "ix_sw_backtest_run_user_id_finished_at",
        "sw_backtest_run",
        ["user_id", "finished_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the one table. The index goes with it; nothing else references it."""
    op.drop_table("sw_backtest_run")
