"""Per-stock regime signal (Maulik, 7 Nov 2026; docs/swing/REGIME-SIGNAL.md).

``index_constituents.industry`` (the CSV's Industry), ``index_stats_daily`` EMA(10)/EMA(20) and
their cross, ``sector_index_map`` (industry -> related indices, seeded), and
``stock_regime_signal`` (one GREEN/RED row per symbol per scan date, written by the worker).

Revision ID: 0062_stock_regime_signal
Revises: 0061_swing_candidates
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0062_stock_regime_signal"
down_revision: str | None = "0061_swing_candidates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRICE = sa.Numeric(precision=14, scale=2)

#: NSE industry -> index_master names. Textiles, Forest Materials, Diversified: no mapping.
SECTOR_INDEX_MAP: dict[str, list[str]] = {
    "Financial Services": ["Nifty Financial Services", "Nifty MidSmall Financial Services"],
    "Capital Goods": ["Nifty Capital Goods", "Nifty India Manufacturing", "Nifty Infrastructure"],
    "Healthcare": [
        "Nifty Healthcare", "Nifty Pharma", "Nifty500 Healthcare", "Nifty MidSmall Healthcare",
    ],
    "Automobile and Auto Components": [
        "Nifty Auto", "Nifty Mobility", "Nifty EV & New Age Automotive",
    ],
    "Fast Moving Consumer Goods": [
        "Nifty FMCG", "Nifty India Consumption", "Nifty Non-Cyclical Consumer",
    ],
    "Consumer Durables": ["Nifty Consumer Durables", "Nifty India Consumption"],
    "Consumer Services": [
        "Nifty Consumer Services", "Nifty India Consumption", "Nifty India New Age Consumption",
    ],
    "Chemicals": ["Nifty Chemicals"],
    "Information Technology": ["Nifty IT", "Nifty MidSmall IT & Telecom", "Nifty India Digital"],
    "Telecommunication": ["Nifty Telecommunications", "Nifty MidSmall IT & Telecom"],
    "Metals & Mining": ["Nifty Metal", "Nifty Commodities"],
    "Oil Gas & Consumable Fuels": ["Nifty Oil & Gas", "Nifty Energy", "Nifty Commodities"],
    "Power": ["Nifty Power", "Nifty Energy"],
    "Utilities": ["Nifty Energy", "Nifty Infrastructure"],
    "Construction": ["Nifty Construction", "Nifty Infrastructure", "Nifty Core Housing"],
    "Construction Materials": ["Nifty Cement", "Nifty Commodities", "Nifty Core Housing"],
    "Realty": ["Nifty Realty", "Nifty REITs & Realty", "Nifty Housing"],
    "Services": ["Nifty Services Sector", "Nifty Commercial & Transport Services"],
    "Media Entertainment & Publication": ["Nifty Media"],
}


def upgrade() -> None:
    op.add_column("index_constituents", sa.Column("industry", sa.Text(), nullable=True))
    op.add_column("index_stats_daily", sa.Column("ema10", PRICE, nullable=True))
    op.add_column("index_stats_daily", sa.Column("ema20", PRICE, nullable=True))
    op.add_column("index_stats_daily", sa.Column("ema_cross", sa.Boolean(), nullable=True))
    op.create_table(
        "sector_index_map",
        sa.Column("industry", sa.Text(), nullable=False),
        sa.Column("index_name", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("industry", "index_name", name=op.f("pk_sector_index_map")),
    )
    op.bulk_insert(
        sa.table("sector_index_map", sa.column("industry"), sa.column("index_name")),
        [
            {"industry": industry, "index_name": name}
            for industry, names in SECTOR_INDEX_MAP.items()
            for name in names
        ],
    )
    op.create_table(
        "stock_regime_signal",
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("signal", sa.Text(), nullable=False),
        sa.Column("fallback", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("signal IN ('GREEN', 'RED')", name=op.f("ck_stock_regime_signal_signal")),
        sa.PrimaryKeyConstraint("as_of", "symbol", name=op.f("pk_stock_regime_signal")),
    )
    op.create_index("ix_stock_regime_signal_symbol", "stock_regime_signal", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_stock_regime_signal_symbol", table_name="stock_regime_signal")
    op.drop_table("stock_regime_signal")
    op.drop_table("sector_index_map")
    op.drop_column("index_stats_daily", "ema_cross")
    op.drop_column("index_stats_daily", "ema20")
    op.drop_column("index_stats_daily", "ema10")
    op.drop_column("index_constituents", "industry")
