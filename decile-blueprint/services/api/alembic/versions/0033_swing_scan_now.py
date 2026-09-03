"""Scan now (SW15; ``docs/swing/DECISIONS-SW.md`` SW15.1).

Maulik, 3 Sep 2026: "I wanted to have the scan anytime, and since we have the Kite API, we
should have all the data." Three changes, all additive:

**``sw_setup_daily.provisional``, ``sw_market_daily.provisional``** — ``false`` for every row the
nightly wrote (the default backfills the history), ``true`` for a row a daytime scan wrote from a
bar it built out of live quotes. The nightly's upsert for the same key sets it back to ``false``
and deletes the provisional rows it did not re-detect, so a flag that only existed at 13:42
never lingers into the evening.

**``sw_scan_run``** — one row per press of "Scan now": who asked, when it was requested,
started and finished, which session the task decided to scan and whether the bar was
provisional, the status (``QUEUED`` / ``RUNNING`` / ``DONE`` / ``FAILED``), the funnel in
``detail`` and the reason in ``error``. ``task_id`` is the broker's id once the Celery message
is published; a row with no ``task_id`` is one the API could not publish (no broker) or one the
desk wrote directly, and the worker's sweep picks it up. The API's "one in flight" (409) and
"one a minute" (429) rules are answered from this table, not from Redis, so they hold with no
cache configured and are testable against the database alone.

Revision ID: 0033_swing_scan_now
Revises: 0032_swing_catalyst
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033_swing_scan_now"
down_revision: str | None = "0032_swing_catalyst"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `baskfy_core.models.swing.SW_SCAN_STATUSES`, written out because a migration is frozen.
STATUSES = ("QUEUED", "RUNNING", "DONE", "FAILED")


def upgrade() -> None:
    op.add_column(
        "sw_setup_daily",
        sa.Column("provisional", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "sw_market_daily",
        sa.Column("provisional", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "sw_scan_run",
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
        sa.Column("provisional", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(), nullable=False, server_default="QUEUED"),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in STATUSES) + ")",
            name=op.f("ck_sw_scan_run_status_known"),
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_sw_scan_run_finished_after_started"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_sw_scan_run_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sw_scan_run")),
    )
    # The API's two questions — "is one in flight" and "when was the last one asked for" — are
    # both "this user's newest row".
    op.create_index(
        "ix_sw_scan_run_user_id_requested_at",
        "sw_scan_run",
        ["user_id", "requested_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the run log and the two flags. A provisional row that is still in the table at
    downgrade time becomes indistinguishable from a nightly one — so the flagged rows go with
    the flag, and the nightly rewrites the day from published bars."""
    op.execute("DELETE FROM sw_setup_daily WHERE provisional")
    op.execute("DELETE FROM sw_market_daily WHERE provisional")
    op.drop_index("ix_sw_scan_run_user_id_requested_at", table_name="sw_scan_run")
    op.drop_table("sw_scan_run")
    op.drop_column("sw_market_daily", "provisional")
    op.drop_column("sw_setup_daily", "provisional")
