"""Stored ranking factors on ``factor_daily`` (docs/ranking/PLAN.md Phase 2, contract C1).

Twenty-five nullable columns, each computed nightly by ``baskfy_core.factors_ranking`` and rounded
at write time through ``precision.COLUMN_PRECISION``. Every existing row gets NULL, which is the
honest value until the worker backfills: C1 is "NULL when the window is not full", and a row
computed before these columns existed had no window at all.

``factor_daily`` is a hypertable that 0001 deliberately left **uncompressed** (docs/04 "keep
uncompressed for 2 years"), so a plain ``ADD COLUMN`` works — unlike ``ohlcv_daily`` in 0044,
which had to switch compression off first. Adding a nullable column with no default is a
catalogue-only change in PostgreSQL: no table rewrite, no long lock on the hot table.

Revision ID: 0046_ranking_factors
Revises: 0045_instrument_watch_and_prefs
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "0046_ranking_factors"
down_revision: str | None = "0045_instrument_watch_and_prefs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "factor_daily"

#: C1's column list, in the contract's order, with the type each is declared at. Written out
#: literally: a migration is a record of what was done on a date, so it must not follow a later
#: edit to the core module's constants.
_COLUMNS: tuple[tuple[str, sa.Numeric[Decimal] | sa.SmallInteger], ...] = (
    ("atr_14", sa.Numeric(18, 4)),
    ("atr_ext_20", sa.Numeric(10, 4)),
    ("ma50_slope_20", sa.Numeric(14, 2)),
    ("eff_ratio_63", sa.Numeric(10, 4)),
    ("max_dd_6m", sa.Numeric(14, 2)),
    ("max_dd_12m", sa.Numeric(14, 2)),
    ("downside_vol_6m", sa.Numeric(18, 10)),
    ("downside_vol_12m", sa.Numeric(18, 10)),
    ("sortino_6m", sa.Numeric(14, 2)),
    ("sortino_12m", sa.Numeric(14, 2)),
    ("underwater_12m", sa.Numeric(7, 2)),
    ("ret_ex_top3_12m", sa.Numeric(14, 2)),
    ("accel_21_105", sa.Numeric(18, 10)),
    ("accel_21_105_vs", sa.Numeric(10, 4)),
    ("vol_exp_21_126", sa.Numeric(10, 4)),
    ("vol_persist_20", sa.SmallInteger()),
    ("excess_ret_3m", sa.Numeric(14, 2)),
    ("excess_ret_6m", sa.Numeric(14, 2)),
    ("excess_ret_12m", sa.Numeric(14, 2)),
    ("resid_ret_12m", sa.Numeric(14, 2)),
    ("rs_persist_126", sa.Numeric(7, 2)),
    ("mom_pctile", sa.Numeric(7, 2)),
    ("rank_persist_20", sa.Numeric(7, 2)),
    ("nse_mr6", sa.Numeric(18, 10)),
    ("nse_mr12", sa.Numeric(18, 10)),
)


def upgrade() -> None:
    for name, column_type in _COLUMNS:
        op.add_column(_TABLE, sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    for name, _column_type in reversed(_COLUMNS):
        op.drop_column(_TABLE, name)
