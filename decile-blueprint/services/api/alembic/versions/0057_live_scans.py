"""Live scans for TWT and VBT — ``docs/live/DECISIONS-LV.md`` LV8.0 (Maulik, 28 Sep 2026).

Three things the schema had refused on purpose and now admits by his decision:

* ``tw_plan.source`` / ``vb_plan.source`` gain ``LIVE`` — a plan built from a scan of today so
  far, during the session, with a thirty-minute expiry like the MORNING one;
* ``vb_plan_line.kind`` gains ``BUY_AT_MARKET`` — VBT's live entry: a MARKET buy at the live price
  with Kite market protection (the tested ``PLACE_LIMIT`` at the signal close stays the evening's);
* ``provisional`` (boolean, default false) on ``tw_scan_run``, ``vb_scan_run`` and the five
  detection tables (``tw_signal_daily``, ``tw_state_daily``, ``tw_breadth_daily``,
  ``vb_signal_daily``, ``vb_breadth_daily``), so a row built from a bar still in progress says
  so, the way ``sw_setup_daily.provisional`` does — and the nightly's real rows replace them.

Downgrade removes the columns and restores the narrower checks; a LIVE plan or a BUY_AT_MARKET
line would have to be deleted first, which the downgrade does (they are display-only rows once
expired — nothing downstream reads a plan by source).

Revision ID: 0057_live_scans
Revises: 0056_eq_minute_bar
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057_live_scans"
down_revision: str | None = "0056_eq_minute_bar"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLAN_SOURCES_BEFORE: tuple[str, ...] = ("EVENING", "MORNING", "MANUAL")
PLAN_SOURCES_AFTER: tuple[str, ...] = (*PLAN_SOURCES_BEFORE, "LIVE")
VB_LINE_KINDS_BEFORE: tuple[str, ...] = ("PLACE_LIMIT", "SELL_AT_OPEN", "CANCEL_LIMIT", "ARM_GTT")
VB_LINE_KINDS_AFTER: tuple[str, ...] = (*VB_LINE_KINDS_BEFORE, "BUY_AT_MARKET")
PROVISIONAL_TABLES: tuple[str, ...] = (
    "tw_scan_run",
    "vb_scan_run",
    "tw_signal_daily",
    "tw_state_daily",
    "tw_breadth_daily",
    "vb_signal_daily",
    "vb_breadth_daily",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def _swap_check(table: str, name: str, expression: str) -> None:
    # Raw SQL on purpose: ``op.drop_constraint`` applies the metadata's naming convention and
    # asks for ``ck_tw_plan_ck_tw_plan_source_known``. The constraint's real name is the whole
    # ``name`` as the creating migration left it.
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression})")


def upgrade() -> None:
    _swap_check("tw_plan", "ck_tw_plan_source_known", _in("source", PLAN_SOURCES_AFTER))
    _swap_check("vb_plan", "ck_vb_plan_source_known", _in("source", PLAN_SOURCES_AFTER))
    _swap_check("vb_plan_line", "ck_vb_plan_line_kind_known", _in("kind", VB_LINE_KINDS_AFTER))
    for table in PROVISIONAL_TABLES:
        op.add_column(
            table,
            sa.Column("provisional", sa.Boolean(), server_default="false", nullable=False),
        )


def downgrade() -> None:
    for table in PROVISIONAL_TABLES:
        op.drop_column(table, "provisional")
    op.execute("DELETE FROM vb_plan_line WHERE kind = 'BUY_AT_MARKET'")
    op.execute(
        "DELETE FROM vb_plan_skip WHERE plan_id IN (SELECT id FROM vb_plan WHERE source = 'LIVE')"
    )
    op.execute(
        "DELETE FROM vb_plan_line WHERE plan_id IN (SELECT id FROM vb_plan WHERE source = 'LIVE')"
    )
    op.execute("DELETE FROM vb_plan WHERE source = 'LIVE'")
    op.execute(
        "DELETE FROM tw_plan_skip WHERE plan_id IN (SELECT id FROM tw_plan WHERE source = 'LIVE')"
    )
    op.execute(
        "DELETE FROM tw_plan_line WHERE plan_id IN (SELECT id FROM tw_plan WHERE source = 'LIVE')"
    )
    op.execute("DELETE FROM tw_plan WHERE source = 'LIVE'")
    _swap_check("vb_plan_line", "ck_vb_plan_line_kind_known", _in("kind", VB_LINE_KINDS_BEFORE))
    _swap_check("vb_plan", "ck_vb_plan_source_known", _in("source", PLAN_SOURCES_BEFORE))
    _swap_check("tw_plan", "ck_tw_plan_source_known", _in("source", PLAN_SOURCES_BEFORE))
