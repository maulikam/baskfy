"""``tw_scan_run`` — the TWT page's **Scan now** button (TW12).

One table, and the third of its shape: the swing book has ``sw_scan_run`` (``0033_swing_scan_now``)
and VBT-1 has ``vb_scan_run`` (``0040_vbt_scan_run``). The desk has no Celery client, so its button
writes a ``QUEUED`` row here and the worker's minute sweep publishes it — the same path, for the
same reason, as both older sleeves.

**It is the narrower of the two shapes, like VBT-1's and for a stricter reason.** ``sw_scan_run``
carries a ``provisional`` flag because the swing book's setups can be read off a bar still being
formed. This strategy's signal cannot be: ``docs/twt/04`` §2 measures three *weekly* ranges that
have closed, against a monthly low, with a sessions-out count over closed sessions. So there is no
``provisional`` column, because there is nothing provisional to record — this row asks only for a
**published** session to be detected again. (DECISIONS-TW **TW12.2**.)

**``source`` admits ``web`` where VBT-1's admits only ``desk`` and ``cli``.** The ``/twt`` hub is
getting this button too (DECISIONS-TW **TW12.3**), and a row that cannot say where a request came
from is a row that cannot be audited afterwards.

Nothing here alters an existing table. The downgrade drops exactly what the upgrade created.

Revision ID: 0042_twt_scan_run
Revises: 0041_twt
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0042_twt_scan_run"
down_revision = "0041_twt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tw_scan_run",
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
            name=op.f("ck_tw_scan_run_status_known"),
        ),
        sa.CheckConstraint(
            "source IN ('desk', 'web', 'cli')", name=op.f("ck_tw_scan_run_source_known")
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_tw_scan_run_finished_after_started"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_tw_scan_run_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tw_scan_run")),
    )
    op.create_index(
        "ix_tw_scan_run_user_id_requested_at", "tw_scan_run", ["user_id", "requested_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_tw_scan_run_user_id_requested_at", table_name="tw_scan_run")
    op.drop_table("tw_scan_run")
