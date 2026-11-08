"""cb_metrics gains the four facts the compute already produced — SC-hardening.

``compute_all_metrics`` has been computing ``volatility_basis``, ``months_available`` and the
A6 return-convention pair since SC2, and dropping all four on the floor: they existed only in
the job's return payload, so the API could never serve them and a reader of ``cb_metrics``
could not tell whether a published number was measured on 252 bars or on a 40-day stub, nor
whether it included dividends.

The two disclosure columns are ``NOT NULL`` with server defaults because the convention is a
property of the whole table, not of a row: every number in ``cb_metrics`` is a price return
computed from dividend-free adjusted closes (docs/DECISIONS-MERGE.md M39.3), including the
rows written before this migration. ``TOTAL_RETURN`` is in the check so the column can carry a
future series without another migration; nothing writes it today.

The two measurement columns are nullable because a basket with no version, or with fewer than
two priced days, genuinely has no basis and no measured span — and ``NULL`` says that, where
a zero would claim a measurement of zero months.

Revision ID: 0016_cb_metrics_disclosure
Revises: 0015_backtest_started_at
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016_cb_metrics_disclosure"
down_revision: str | None = "0015_backtest_started_at"
branch_labels: str | None = None
depends_on: str | None = None

# Must match baskfy_core.curated_metrics.VolatilityBasis and RETURN_CONVENTION exactly — the
# job writes those literals, so a check written from memory would reject every row it produces.
_VOL_BASIS = (
    "volatility_basis IS NULL OR volatility_basis IN "
    "('BASKET_252D', 'BASKET_FULL_HISTORY', 'CONSTITUENT_WEIGHTED')"
)
_MONTHS_AVAILABLE = "months_available IS NULL OR months_available >= 0"
_RETURN_CONVENTION = "return_convention IN ('PRICE_RETURN', 'TOTAL_RETURN')"


def upgrade() -> None:
    op.add_column("cb_metrics", sa.Column("volatility_basis", sa.Text(), nullable=True))
    op.add_column("cb_metrics", sa.Column("months_available", sa.Integer(), nullable=True))
    op.add_column(
        "cb_metrics",
        sa.Column(
            "return_convention",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'PRICE_RETURN'"),
        ),
    )
    op.add_column(
        "cb_metrics",
        sa.Column(
            "dividends_included",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_check_constraint("cb_metrics_volatility_basis", "cb_metrics", _VOL_BASIS)
    op.create_check_constraint("cb_metrics_months_available", "cb_metrics", _MONTHS_AVAILABLE)
    op.create_check_constraint("cb_metrics_return_convention", "cb_metrics", _RETURN_CONVENTION)


def downgrade() -> None:
    op.drop_constraint("cb_metrics_return_convention", "cb_metrics", type_="check")
    op.drop_constraint("cb_metrics_months_available", "cb_metrics", type_="check")
    op.drop_constraint("cb_metrics_volatility_basis", "cb_metrics", type_="check")
    op.drop_column("cb_metrics", "dividends_included")
    op.drop_column("cb_metrics", "return_convention")
    op.drop_column("cb_metrics", "months_available")
    op.drop_column("cb_metrics", "volatility_basis")
