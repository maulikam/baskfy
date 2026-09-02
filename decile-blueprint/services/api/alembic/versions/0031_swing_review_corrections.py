"""Maulik's review corrections (``docs/swing/STANDING-ANSWERS.md`` A7, A9, A14; SW10.5).

Four of the six standing answers that change behaviour need the schema to remember something
it did not:

**A7 — ``PENDING_RANGE`` lines.** A live gap found at 09:09 has a trigger and no stop until the
opening range sets one. The MORNING plan now shows it as a line of kind ``PENDING_RANGE`` — no
quantity, no stop, never executable — that reserves one of the session's new-entry slots, so
``sw_plan_line.kind``'s check constraint is rebuilt with the fourth kind. Both lists are written
out here (a migration is frozen) and ``test_swing_schema_and_settings`` asserts the new one is
the engine's ``SW_LINE_KINDS``.

**A9 — half risk at plan time, countdown persisted.** ``sw_position.half_risk`` tags an entry
sized at the first-live ``risk_multiplier`` (0.5) — the journal's tag. ``sw_session.
first_live_counted`` records that the evening counted this LIVE session against
``sw_config.first_live_sessions_left``, so a re-run of the evening decrements once and a desk
restart mid-countdown changes nothing.

**A14 — the watch funnel.** ``sw_watch.score`` is what a row is ranked by (the top 20 flags by
score are auto-watched; the top 5 plus every EP are the daily focus, ``sw_watch.focus``, set by
the evening and the premarket and read by the desk page and the notifier). ``sw_watch.adr_pct``
is the name's ADR when the row was written — a live gap has no detection row, and a stop that
cannot be measured against one ADR is a stop the plan refuses (SW9.5.2). ``sw_watch.
reconfirmed_on`` is the last time a person re-confirmed a MANUAL row on the watchlist page,
which resets its expiry: MANUAL rows now expire after ten sessions like a detector's flag.

Revision ID: 0031_swing_review_corrections
Revises: 0030_swing_primary_sources
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_swing_review_corrections"
down_revision: str | None = "0030_swing_primary_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `0028_swing.py`'s list, verbatim, so the downgrade restores exactly the constraint it made.
OLD_LINE_KINDS = ("BUY_ON_TRIGGER", "SELL_AT_OPEN", "RAISE_GTT_STOP")
#: `baskfy_core.swing.plan.LineKind` at SW10.5, in the enum's order.
NEW_LINE_KINDS = ("BUY_ON_TRIGGER", "SELL_AT_OPEN", "RAISE_GTT_STOP", "PENDING_RANGE")


def _kind_check(values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"kind IN ({quoted})"


def upgrade() -> None:
    op.add_column("sw_watch", sa.Column("score", sa.Numeric(precision=5, scale=2), nullable=True))
    op.add_column(
        "sw_watch", sa.Column("adr_pct", sa.Numeric(precision=10, scale=2), nullable=True)
    )
    op.add_column(
        "sw_watch", sa.Column("focus", sa.Boolean(), server_default="false", nullable=False)
    )
    op.add_column("sw_watch", sa.Column("reconfirmed_on", sa.Date(), nullable=True))
    op.add_column(
        "sw_position",
        sa.Column("half_risk", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "sw_session",
        sa.Column("first_live_counted", sa.Boolean(), server_default="false", nullable=False),
    )
    op.drop_constraint(op.f("ck_sw_plan_line_kind_known"), "sw_plan_line", type_="check")
    op.create_check_constraint(
        op.f("ck_sw_plan_line_kind_known"), "sw_plan_line", _kind_check(NEW_LINE_KINDS)
    )


def downgrade() -> None:
    """Drop the six columns and restore the three-kind constraint. A `PENDING_RANGE` line
    cannot exist under the old constraint and is deleted first: a downgrade to a schema that
    cannot name a kind cannot keep it. Nothing else a row carries is changed."""
    op.execute("DELETE FROM sw_plan_line WHERE kind = 'PENDING_RANGE'")
    op.drop_constraint(op.f("ck_sw_plan_line_kind_known"), "sw_plan_line", type_="check")
    op.create_check_constraint(
        op.f("ck_sw_plan_line_kind_known"), "sw_plan_line", _kind_check(OLD_LINE_KINDS)
    )
    op.drop_column("sw_session", "first_live_counted")
    op.drop_column("sw_position", "half_risk")
    op.drop_column("sw_watch", "reconfirmed_on")
    op.drop_column("sw_watch", "focus")
    op.drop_column("sw_watch", "adr_pct")
    op.drop_column("sw_watch", "score")
