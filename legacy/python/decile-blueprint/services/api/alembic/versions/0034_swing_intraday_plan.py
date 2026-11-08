"""The plan can be rebuilt intraday, so a live setup is buyable the same afternoon.

Maulik, 4 Sep 2026: "the scans we are doing are not the live data, they would be the past data."

The scan itself was already live — M85's `baskfy.swing.scan_after_login` builds a provisional bar
per liquid name from Kite quotes and runs the real detectors over today-so-far. What never ran was
anything *downstream* of it. On the box this afternoon that showed exactly as reported: scan #7 at
14:08 wrote thirteen provisional setups for 2026-09-04, while the only executable line on the desk
was one `BUY_ON_TRIGGER` in a MORNING plan built at 09:16 from the previous close. Visible, and
not buyable.

An `INTRADAY` plan is the evening's own build — `auto_watch` → `watch_items` → `build_entries` →
`store_plan` — re-run against the provisional session. Nothing about how a line is sized, gated or
confirmed changes; only when the build may happen.

Widening a CHECK is backwards-compatible: every existing row still satisfies it. The downgrade
deletes `INTRADAY` plans before narrowing the constraint again, because a row the constraint
forbids would make the rollback fail halfway.

Revision ID: 0034_swing_intraday_plan
Revises: 0033_swing_scan_now
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0034_swing_intraday_plan"
down_revision: str | None = "0033_swing_scan_now"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_sw_plan_source_known"
_OLD = "source IN ('EOD_PREVIEW', 'MORNING', 'SIGNAL')"
_NEW = "source IN ('EOD_PREVIEW', 'MORNING', 'SIGNAL', 'INTRADAY')"


# `op.f` marks the name as ALREADY FINAL. Without it the metadata naming convention
# (`ck_%(table_name)s_%(constraint_name)s`) is applied a second time and both statements
# address `ck_sw_plan_ck_sw_plan_source_known`, which does not exist — the upgrade then fails
# on the DROP. 0028 created it through `op.f` for the same reason.


def upgrade() -> None:
    op.drop_constraint(op.f(_CONSTRAINT), "sw_plan", type_="check")
    op.create_check_constraint(op.f(_CONSTRAINT), "sw_plan", _NEW)


def downgrade() -> None:
    # The lines and skips go with them: `sw_plan_line` / `sw_plan_skip` are ON DELETE CASCADE
    # from `sw_plan`, so deleting the parent is enough and leaves no orphans.
    op.execute("DELETE FROM sw_plan WHERE source = 'INTRADAY'")
    op.drop_constraint(op.f(_CONSTRAINT), "sw_plan", type_="check")
    op.create_check_constraint(op.f(_CONSTRAINT), "sw_plan", _OLD)
