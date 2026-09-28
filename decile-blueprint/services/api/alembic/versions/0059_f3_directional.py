"""F3 — the directional index credit spread (``docs/fno/DECISIONS-FO.md`` M.5; ``03`` §9).

Maulik, 28 Sep 2026: NIFTY on the weekly expiry, BANKNIFTY on the monthly, a credit spread with
a far wing, auto-exit under his flag. The schema learns:

* ``fo_sleeve`` (a Postgres enum) gains ``F3N`` and ``F3B``. ``ALTER TYPE … ADD VALUE`` cannot run
  inside a transaction, so it runs in an autocommit block; **a value added to an enum cannot be
  dropped**, so the downgrade leaves the two labels in place and says so;
* ``fo_sleeve_config.sleeve`` and ``fo_config_audit.scope`` admit ``F3``;
* ``fo_plan``, ``fo_position`` and ``fo_journal`` admit the ``CREDIT_SPREAD`` structure;
* ``fo_plan.kind`` admits ``ADD`` (the pyramid — more lots onto the open spread);
* ``fo_index_daily``: the index's own daily OHLC from Kite's history, which the bhavcopy does not
  carry and ``index_snapshot_daily`` keeps only as a level.

Downgrade drops the table and restores the narrower checks after neutralising rows that carry
the new values (F3 rows are deleted — the sleeve is paper — and an ``ADD`` plan becomes
``ENTRY``); the enum labels stay.

Revision ID: 0059_f3_directional
Revises: 0058_qulla_exits
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059_f3_directional"
down_revision: str | None = "0058_qulla_exits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_SLEEVES: tuple[str, ...] = ("F3N", "F3B")
GROUPS_BEFORE: tuple[str, ...] = ("F1", "F2")
GROUPS_AFTER: tuple[str, ...] = (*GROUPS_BEFORE, "F3")
STRUCTURES_BEFORE: tuple[str, ...] = ("IRON_CONDOR", "FUTURE")
STRUCTURES_AFTER: tuple[str, ...] = (*STRUCTURES_BEFORE, "CREDIT_SPREAD")
KINDS_BEFORE: tuple[str, ...] = ("ENTRY", "EXIT", "ROLL")
KINDS_AFTER: tuple[str, ...] = (*KINDS_BEFORE, "ADD")
INDEX_UNDERLYINGS: tuple[str, ...] = ("NIFTY", "BANKNIFTY")
INDEX_SOURCES: tuple[str, ...] = ("KITE_HIST",)
#: The tables whose ``structure`` check names the structures (``03`` §4-§6).
STRUCTURE_TABLES: tuple[str, ...] = ("fo_plan", "fo_position", "fo_journal")
#: The per-user tables that carry a sleeve, for the downgrade's F3 clean-out. Legs, fills and
#: marks hang off plans and positions with ``ON DELETE CASCADE`` (0052) and go with them.
SLEEVE_TABLES: tuple[str, ...] = ("fo_journal", "fo_position", "fo_plan", "fo_scan")

PRICE = sa.Numeric(precision=18, scale=2)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _swap_check(table: str, name: str, expression: str) -> None:
    # Raw SQL on purpose (0057's lesson): ``op.drop_constraint`` applies the naming convention
    # twice. The constraint's real name is the whole ``name`` as the creating migration left it.
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression})")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for label in NEW_SLEEVES:
            op.execute(f"ALTER TYPE fo_sleeve ADD VALUE IF NOT EXISTS '{label}'")
    _swap_check(
        "fo_sleeve_config", "ck_fo_sleeve_config_sleeve_group_known", _in("sleeve", GROUPS_AFTER)
    )
    _swap_check(
        "fo_config_audit", "ck_fo_config_audit_scope_known", _in("scope", ("BOOK", *GROUPS_AFTER))
    )
    for table in STRUCTURE_TABLES:
        _swap_check(table, f"ck_{table}_structure_known", _in("structure", STRUCTURES_AFTER))
    _swap_check("fo_plan", "ck_fo_plan_kind_known", _in("kind", KINDS_AFTER))
    op.create_table(
        "fo_index_daily",
        sa.Column("underlying", sa.String(length=16), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("open", PRICE, nullable=False),
        sa.Column("high", PRICE, nullable=False),
        sa.Column("low", PRICE, nullable=False),
        sa.Column("close", PRICE, nullable=False),
        sa.Column("source", sa.String(length=16), server_default="KITE_HIST", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            _in("underlying", INDEX_UNDERLYINGS), name=op.f("ck_fo_index_daily_underlying_known")
        ),
        sa.CheckConstraint(
            _in("source", INDEX_SOURCES), name=op.f("ck_fo_index_daily_source_known")
        ),
        sa.CheckConstraint("high >= low", name=op.f("ck_fo_index_daily_high_not_below_low")),
        sa.PrimaryKeyConstraint("underlying", "trade_date", name=op.f("pk_fo_index_daily")),
    )


def downgrade() -> None:
    op.drop_table("fo_index_daily")
    # Neutralise before narrowing: F3's paper rows go (children before parents), an ADD is an
    # ENTRY, and the F3 config and audit rows go with them.
    for table in SLEEVE_TABLES:
        op.execute(f"DELETE FROM {table} WHERE sleeve IN ('F3N', 'F3B')")
    op.execute("UPDATE fo_plan SET kind = 'ENTRY' WHERE kind = 'ADD'")
    op.execute("DELETE FROM fo_config_audit WHERE scope = 'F3'")
    op.execute("DELETE FROM fo_sleeve_config WHERE sleeve = 'F3'")
    _swap_check("fo_plan", "ck_fo_plan_kind_known", _in("kind", KINDS_BEFORE))
    for table in STRUCTURE_TABLES:
        _swap_check(table, f"ck_{table}_structure_known", _in("structure", STRUCTURES_BEFORE))
    _swap_check(
        "fo_config_audit", "ck_fo_config_audit_scope_known", _in("scope", ("BOOK", *GROUPS_BEFORE))
    )
    _swap_check(
        "fo_sleeve_config", "ck_fo_sleeve_config_sleeve_group_known", _in("sleeve", GROUPS_BEFORE)
    )
    # Postgres cannot drop a value from an enum: ``F3N`` and ``F3B`` remain labels of
    # ``fo_sleeve``, unused. Recreating the type would rewrite every fo_ table for nothing.
