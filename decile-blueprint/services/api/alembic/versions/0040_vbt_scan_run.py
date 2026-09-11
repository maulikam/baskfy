"""``vb_scan_run`` — the desk's **Re-detect** button (VB12, ``docs/vbt/03`` §13).

One table. The desk has no Celery client, so its button writes a ``QUEUED`` row here and the
worker's minute sweep publishes it — the same path the swing book's "Scan now" takes
(``0033_swing_scan_now``), for the same reason.

**It is deliberately narrower than the swing book's.** ``sw_scan_run`` carries a ``provisional``
flag because that scan can run inside a session against live quotes. This one cannot and should
not: three of VBT-1's five Chartink lines read the day's volume against its 50-day average, the
close's position inside the day's range and the day's change, none of which means anything before
15:30, and the entry limit *is* the signal bar's close. So there is no ``provisional`` column,
because there is nothing provisional to record — this row asks only for a **published** session to
be re-detected.

Nothing here alters an existing table.

Revision ID: 0040_vbt_scan_run
Revises: 0039_merge_duplicate_instruments
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0040_vbt_scan_run"
down_revision = "0039_merge_duplicate_instruments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vb_scan_run",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("session_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="QUEUED", nullable=False),
        sa.Column("source", sa.String(length=8), server_default="desk", nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'DONE', 'FAILED')",
            name=op.f("ck_vb_scan_run_status_known"),
        ),
        sa.CheckConstraint("source IN ('desk', 'cli')", name=op.f("ck_vb_scan_run_source_known")),
        sa.CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_vb_scan_run_finished_after_started"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_vb_scan_run_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vb_scan_run")),
    )
    op.create_index(
        "ix_vb_scan_run_user_id_requested_at", "vb_scan_run", ["user_id", "requested_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_vb_scan_run_user_id_requested_at", table_name="vb_scan_run")
    op.drop_table("vb_scan_run")
