"""``backtest.started_at`` — so a run that died is not identical to one that never began (M45.2).

An audit measured the two side by side and they matched in every column::

    started-then-SIGKILLed : status='queued' error=None finished_at=None metrics=None
    never-started          : status='queued' error=None finished_at=None metrics=None

There was no query an operator could write to separate a run posted 200 ms ago from one lost
weeks earlier, so no safe sweeper could exist: any rule that reclaimed the second would sometimes
kill the first. ``created_at`` is not a substitute — it records the POST, so age conflates
"abandoned" with "waiting behind the concurrency cap".

``PipelineRun`` has carried ``started_at`` since the beginning (``accounts.py``); this brings the
backtest row to the same standard.

Nullable, because every existing row genuinely has no known start.

Revision ID: 0015_backtest_started_at
Revises: 0014_curated_baskets
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0015_backtest_started_at"
down_revision: str | None = "0014_curated_baskets"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "backtest",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The reaper's query is `status = 'running' AND started_at < now() - interval`, so it reads
    # this column on every sweep and nothing else does.
    op.create_index(
        "ix_backtest_status_started_at",
        "backtest",
        ["status", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_backtest_status_started_at", table_name="backtest")
    op.drop_column("backtest", "started_at")
