"""A basket can be cut from a saved screen — the third genesis path.

Until now ``cb_basket.source`` admitted two origins: ``SCAN`` (the engine's ranked output) and
``MANUAL`` (somebody typed a list of symbols). A basket built from a screen is neither, and
recording it as ``MANUAL`` would have thrown away the fact that matters most about it: a screen is
a **live rule**, so the basket can be re-cut from it on a later date. A hand-typed list can never
be. ``source_screen_id`` is what makes the re-cut possible, and ``source = 'SCREEN'`` is what tells
a reader to expect it.

``ON DELETE SET NULL`` rather than CASCADE, deliberately. Deleting a screen must not delete a
basket somebody is holding — the two are different objects with different lifetimes. Losing the
rule means the basket can no longer be re-cut, and a NULL says precisely that, where a cascade
would silently destroy the holding.

The column is nullable with no default, so every existing ``SCAN`` and ``MANUAL`` row is unchanged
and no backfill is needed. The check constraint has to be dropped and recreated because Postgres
cannot widen an ``IN`` list in place; the recreated version must match
``baskfy_core.models.curated_baskets.BASKET_SOURCES`` exactly, or the create route's own writes
would start bouncing.

Revision ID: 0017_cb_basket_from_screen
Revises: 0016_cb_metrics_disclosure
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017_cb_basket_from_screen"
down_revision: str | None = "0016_cb_metrics_disclosure"
branch_labels: str | None = None
depends_on: str | None = None

_CONSTRAINT = "cb_basket_source"
_WIDENED = "source IN ('SCAN', 'MANUAL', 'SCREEN')"
_ORIGINAL = "source IN ('SCAN', 'MANUAL')"


def upgrade() -> None:
    op.add_column(
        "cb_basket",
        sa.Column("source_screen_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_cb_basket_source_screen_id",
        "cb_basket",
        "screen",
        ["source_screen_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_cb_basket_source_screen_id", "cb_basket", ["source_screen_id"])
    op.drop_constraint(_CONSTRAINT, "cb_basket", type_="check")
    op.create_check_constraint(_CONSTRAINT, "cb_basket", _WIDENED)


def downgrade() -> None:
    # A SCREEN-sourced basket cannot survive the narrower check. Reverting means those baskets
    # become MANUAL — the honest description of what is left once the link to the rule is gone.
    op.execute(sa.text("UPDATE cb_basket SET source = 'MANUAL' WHERE source = 'SCREEN'"))
    op.drop_constraint(_CONSTRAINT, "cb_basket", type_="check")
    op.create_check_constraint(_CONSTRAINT, "cb_basket", _ORIGINAL)
    op.drop_index("ix_cb_basket_source_screen_id", table_name="cb_basket")
    op.drop_constraint("fk_cb_basket_source_screen_id", "cb_basket", type_="foreignkey")
    op.drop_column("cb_basket", "source_screen_id")
