"""LV9 — Qullamaggie's exits on TWT and VBT (``docs/live/DECISIONS-LV.md`` LV9.0; TW20, VB17).

Maulik, 28 Sep 2026: the swing book's exit rule replaces TWT's 20 % high-water trail and VBT's
21-EMA exit. The schema learns:

* ``tw_position`` and ``vb_position``: ``partial_done`` (the third sold into strength, once) and
  ``trail`` (``MA10``/``MA20``, chosen from the ADR the first evening the position is managed);
* ``tw_position`` also gains the queued-sale columns ``vb_position`` already had —
  ``partial_queued_for`` / ``partial_quantity`` for the partial, ``exit_queued_for`` /
  ``exit_reason_queued`` for the remainder — because a TWT plan is built from the book's state
  and the evening's decision has to survive until the morning's confirm;
* ``tw_position.close_reason`` admits ``MA_TRAIL``; ``vb_position.close_reason`` admits
  ``MA_TRAIL`` and ``PARTIAL`` (the latter only ever on an exit order's reason — a partial never
  closes a position — but the enum is one and the check follows it);
* ``vb_plan_line.kind`` admits ``RAISE_GTT_STOP`` — the breakeven move, cancel-then-re-arm.

Downgrade drops the columns and restores the narrower checks; rows carrying the new values are
neutralised first (a ``MA_TRAIL`` close becomes ``MANUAL``; ``RAISE_GTT_STOP`` lines are deleted).

Revision ID: 0058_qulla_exits
Revises: 0057_live_scans
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0058_qulla_exits"
down_revision: str | None = "0057_live_scans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TW_CLOSE_BEFORE: tuple[str, ...] = ("STOP_HIT", "STOP_GAP", "STOP_DAY0", "NO_BAR", "MANUAL")
TW_CLOSE_AFTER: tuple[str, ...] = (*TW_CLOSE_BEFORE, "MA_TRAIL")
VB_CLOSE_BEFORE: tuple[str, ...] = (
    "EMA_EXIT",
    "STOP_HIT",
    "STOP_GAP",
    "NO_BAR",
    "END_OF_RUN",
    "MANUAL",
)
VB_CLOSE_AFTER: tuple[str, ...] = (*VB_CLOSE_BEFORE, "MA_TRAIL", "PARTIAL")
VB_LINE_KINDS_BEFORE: tuple[str, ...] = (
    "PLACE_LIMIT",
    "SELL_AT_OPEN",
    "CANCEL_LIMIT",
    "ARM_GTT",
    "BUY_AT_MARKET",
)
VB_LINE_KINDS_AFTER: tuple[str, ...] = (*VB_LINE_KINDS_BEFORE, "RAISE_GTT_STOP")


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def _swap_check(table: str, name: str, expression: str) -> None:
    # Raw SQL: ``op.drop_constraint`` would apply the naming convention a second time (0057).
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression})")


def upgrade() -> None:
    for table in ("tw_position", "vb_position"):
        op.add_column(
            table,
            sa.Column("partial_done", sa.Boolean(), server_default="false", nullable=False),
        )
        op.add_column(table, sa.Column("trail", sa.String(length=4), nullable=True))
    op.add_column("tw_position", sa.Column("partial_queued_for", sa.Date(), nullable=True))
    op.add_column("tw_position", sa.Column("partial_quantity", sa.Integer(), nullable=True))
    op.add_column("tw_position", sa.Column("exit_queued_for", sa.Date(), nullable=True))
    op.add_column(
        "tw_position", sa.Column("exit_reason_queued", sa.String(length=24), nullable=True)
    )
    _swap_check(
        "tw_position", "ck_tw_position_close_reason_known", _in("close_reason", TW_CLOSE_AFTER)
    )
    _swap_check(
        "vb_position", "ck_vb_position_close_reason_known", _in("close_reason", VB_CLOSE_AFTER)
    )
    _swap_check("vb_plan_line", "ck_vb_plan_line_kind_known", _in("kind", VB_LINE_KINDS_AFTER))


def downgrade() -> None:
    op.execute("DELETE FROM vb_plan_line WHERE kind = 'RAISE_GTT_STOP'")
    op.execute(
        "UPDATE vb_position SET close_reason = 'MANUAL' WHERE close_reason IN ('MA_TRAIL', 'PARTIAL')"
    )
    op.execute("UPDATE tw_position SET close_reason = 'MANUAL' WHERE close_reason = 'MA_TRAIL'")
    _swap_check("vb_plan_line", "ck_vb_plan_line_kind_known", _in("kind", VB_LINE_KINDS_BEFORE))
    _swap_check(
        "vb_position", "ck_vb_position_close_reason_known", _in("close_reason", VB_CLOSE_BEFORE)
    )
    _swap_check(
        "tw_position", "ck_tw_position_close_reason_known", _in("close_reason", TW_CLOSE_BEFORE)
    )
    for column in (
        "exit_reason_queued",
        "exit_queued_for",
        "partial_quantity",
        "partial_queued_for",
    ):
        op.drop_column("tw_position", column)
    for table in ("tw_position", "vb_position"):
        op.drop_column(table, "trail")
        op.drop_column(table, "partial_done")
