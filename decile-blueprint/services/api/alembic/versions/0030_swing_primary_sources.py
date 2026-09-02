"""The primary-source corrections (``docs/swing/07``, SW9.5): the drawdown columns and two defaults.

    "I try to contain them at 15-20%, which happen a few times per year."  (his FAQ)

Five of the pack's rules were re-read from his own words rather than a summary's (`07`), and
two of them need the schema to know something it did not:

**The drawdown containment (`04` §8.5).** A sleeve 15% below its peak stops opening new positions
until it is back within 10% — a lock-out with hysteresis, so a book oscillating around 15% does
not flap. The state that needs is three numbers on ``sw_config`` — the peak EOD NAV the sleeve
has reached (``sleeve_peak_inr``, null until the first evening has run: a sleeve with no session
behind it is not in drawdown), tonight's distance below it (``drawdown_pct``) and whether the
lock-out is in force (``drawdown_locked``) — and the same two measurements on ``sw_market_daily``
beside the rung they produced, so a rung of 0 on a GREEN day explains itself. All of them are
written by ``swing-eod`` from the sleeve's NAV (``DECISIONS-SW`` SW9.5.1) and never by a form:
``SwingConfigPatch`` forbids them, because a person who could reset the peak could trade
through a drawdown.

**Two skip reasons.** The plan now answers `SESSION_CAP` ("1, 2, 3 stocks per day") and
`DRAWDOWN_LOCKOUT`, and ``sw_plan_skip.reason`` is check-constrained to a list, so the constraint
is rebuilt with the two added. The list is written out here — a migration is frozen, and one that
imported the engine's enum would produce a different database next year than it did today — and
``test_swing_schema_and_settings`` asserts it equals the model's ``SW_SKIP_REASONS``.

**Two defaults.** ``max_open_positions`` was 8, the old ladder's top rung; his "typically 5-10
positions" makes the top rung ten, and the plan now takes the smaller of the rung and this
setting, so the default is ten (the env ceiling, twenty, is his 15-20 of a great market).
``adr_min_pct`` was 3.5; his screens use 5%+ and 3.5-4 is the floor he names on stream, so the
engine's default is 4.0 and the row's default follows it. Rows still sitting at the *old*
default are moved to the new one: ``apply_patch`` writes no audit row for an unchanged value,
so a row at exactly 8 or 3.50 is a row nobody set — the seed's number, not a person's — and a
person who wants 3.5 back can set it, audited, in the form.

Revision ID: 0030_swing_primary_sources
Revises: 0029_swing_backtest
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "0030_swing_primary_sources"
down_revision: str | None = "0029_swing_backtest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The defaults this migration moves, as ``(column, old, new)``. Named once so the upgrade and
#: the downgrade cannot disagree about what "the old default" was.
OLD_MAX_OPEN_POSITIONS = "8"
NEW_MAX_OPEN_POSITIONS = "10"
OLD_ADR_MIN_PCT = "3.50"
NEW_ADR_MIN_PCT = "4.00"

#: `0028_swing.py`'s list, verbatim, so the downgrade restores exactly the constraint it made.
OLD_SKIP_REASONS = (
    "NOT_TRADEABLE_SETUP",
    "GATE_RED",
    "TIER_FULL",
    "EXPOSURE_FULL",
    "ALREADY_HELD",
    "LOCKED_UPPER_CIRCUIT",
    "SIZE_REFUSED",
)
#: `baskfy_core.swing.plan.SkipReason` at SW9.5, in the enum's order.
NEW_SKIP_REASONS = (
    "NOT_TRADEABLE_SETUP",
    "GATE_RED",
    "DRAWDOWN_LOCKOUT",
    "TIER_FULL",
    "SESSION_CAP",
    "EXPOSURE_FULL",
    "ALREADY_HELD",
    "LOCKED_UPPER_CIRCUIT",
    "SIZE_REFUSED",
)


def _reason_check(values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"reason IN ({quoted})"


def upgrade() -> None:
    op.add_column(
        "sw_config",
        sa.Column("sleeve_peak_inr", sa.Numeric(precision=20, scale=2), nullable=True),
    )
    op.add_column(
        "sw_config",
        sa.Column(
            "drawdown_pct", sa.Numeric(precision=10, scale=2), server_default="0", nullable=False
        ),
    )
    op.add_column(
        "sw_config",
        sa.Column("drawdown_locked", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "sw_market_daily",
        sa.Column(
            "drawdown_pct", sa.Numeric(precision=10, scale=2), server_default="0", nullable=False
        ),
    )
    op.add_column(
        "sw_market_daily",
        sa.Column("drawdown_locked", sa.Boolean(), server_default="false", nullable=False),
    )

    op.drop_constraint(op.f("ck_sw_plan_skip_reason_known"), "sw_plan_skip", type_="check")
    op.create_check_constraint(
        op.f("ck_sw_plan_skip_reason_known"), "sw_plan_skip", _reason_check(NEW_SKIP_REASONS)
    )

    op.alter_column("sw_config", "max_open_positions", server_default=NEW_MAX_OPEN_POSITIONS)
    op.alter_column("sw_config", "adr_min_pct", server_default=NEW_ADR_MIN_PCT)
    op.execute(
        f"UPDATE sw_config SET max_open_positions = {int(NEW_MAX_OPEN_POSITIONS)} "
        f"WHERE max_open_positions = {int(OLD_MAX_OPEN_POSITIONS)}"
    )
    op.execute(
        f"UPDATE sw_config SET adr_min_pct = {Decimal(NEW_ADR_MIN_PCT)} "
        f"WHERE adr_min_pct = {Decimal(OLD_ADR_MIN_PCT)}"
    )


def downgrade() -> None:
    """Restore the old defaults and the old constraint, and drop the five columns. The values a
    row carries are left as they are: a downgrade is not a reason to change what a person set.
    A skip row carrying one of the two new reasons would refuse the old constraint; they are
    deleted first, because a downgrade to a schema that cannot name a reason cannot keep it."""
    op.execute("DELETE FROM sw_plan_skip WHERE reason IN ('SESSION_CAP', 'DRAWDOWN_LOCKOUT')")
    op.drop_constraint(op.f("ck_sw_plan_skip_reason_known"), "sw_plan_skip", type_="check")
    op.create_check_constraint(
        op.f("ck_sw_plan_skip_reason_known"), "sw_plan_skip", _reason_check(OLD_SKIP_REASONS)
    )
    op.alter_column("sw_config", "adr_min_pct", server_default=OLD_ADR_MIN_PCT)
    op.alter_column("sw_config", "max_open_positions", server_default=OLD_MAX_OPEN_POSITIONS)
    op.drop_column("sw_market_daily", "drawdown_locked")
    op.drop_column("sw_market_daily", "drawdown_pct")
    op.drop_column("sw_config", "drawdown_locked")
    op.drop_column("sw_config", "drawdown_pct")
    op.drop_column("sw_config", "sleeve_peak_inr")
