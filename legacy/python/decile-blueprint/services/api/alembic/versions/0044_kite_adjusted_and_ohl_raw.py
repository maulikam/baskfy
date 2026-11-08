"""Distinguish vendor-adjusted deep history from nightly Kite bars; store OHL raw prints.

AF 0.5 / 0.6 (12 Sep 2026). Deep-history rows written by ``deep_backfill`` were stamped
``source='kite'`` — the same label the nightly fetch uses for raw candles. ``adjust_bars``
skipped every ``kite`` row, so post-seam Kite bars never received corporate-action factors.
Retag date < 2024-01-01 (the M29 bhavcopy seam) as ``kite_adjusted``; nightly ``kite`` stays
adjustable.

``open``/``high``/``low`` have no raw counterparts today, so a raw refetch overwrites an already
adjusted OHL and the next reprocess divides again. Nullable ``open_raw``/``high_raw``/``low_raw``
hold the exchange print when known; NULL means recover via ``price / adj_factor`` only when
``adj_factor = 1``, otherwise leave OHL alone on conflict.

``ohlcv_daily`` is a compressed hypertable (0001). ADD CONSTRAINT refuses while
``compression_enabled`` is true — even with zero chunks — so this migration turns compression
off, alters, then restores 0001's settings.

Revision ID: 0044_kite_adjusted_and_ohl_raw
Revises: 0043_split_holding_reason
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044_kite_adjusted_and_ohl_raw"
down_revision: str | None = "0043_split_holding_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: M29 bhavcopy seam — deep Kite history sits strictly before this calendar day.
_SEAM = "2024-01-01"

#: 0001 created this via ``op.f("ck_ohlcv_daily_ohlcv_daily_source")``.
_SOURCE_CHECK = "ck_ohlcv_daily_ohlcv_daily_source"


def _disable_ohlcv_compression() -> None:
    """Compression blocks ADD CONSTRAINT even when the hypertable has no chunks yet."""
    op.execute("SELECT remove_compression_policy('ohlcv_daily', if_exists => true)")
    op.execute(
        """
        DO $body$
        DECLARE chunk regclass;
        BEGIN
            FOR chunk IN SELECT show_chunks('ohlcv_daily')
            LOOP
                PERFORM decompress_chunk(chunk, true);
            END LOOP;
        END
        $body$
        """
    )
    # The hypertable flag itself, not only the chunks — empty DBs have compression_enabled
    # with zero chunks and still refuse ALTER … ADD CONSTRAINT.
    op.execute("ALTER TABLE ohlcv_daily SET (timescaledb.compress = false)")


def _reenable_ohlcv_compression() -> None:
    """Restore 0001's compress settings and 90-day policy."""
    op.execute(
        "ALTER TABLE ohlcv_daily SET ("
        "timescaledb.compress, timescaledb.compress_segmentby = 'instrument_id')"
    )
    op.execute("SELECT add_compression_policy('ohlcv_daily', INTERVAL '90 days')")


def upgrade() -> None:
    _disable_ohlcv_compression()
    op.execute("SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0")

    op.execute(f'ALTER TABLE ohlcv_daily DROP CONSTRAINT IF EXISTS "{_SOURCE_CHECK}"')
    op.execute('ALTER TABLE ohlcv_daily DROP CONSTRAINT IF EXISTS "ohlcv_daily_source"')
    op.execute(
        f'ALTER TABLE ohlcv_daily ADD CONSTRAINT "{_SOURCE_CHECK}" '
        "CHECK (source IN ('kite', 'nse', 'kite_adjusted'))"
    )
    op.execute(
        f"""
        UPDATE ohlcv_daily
           SET source = 'kite_adjusted'
         WHERE source = 'kite'
           AND date < DATE '{_SEAM}'
        """
    )
    op.add_column(
        "ohlcv_daily",
        sa.Column("open_raw", sa.Numeric(18, 4), nullable=True),
    )
    op.add_column(
        "ohlcv_daily",
        sa.Column("high_raw", sa.Numeric(18, 4), nullable=True),
    )
    op.add_column(
        "ohlcv_daily",
        sa.Column("low_raw", sa.Numeric(18, 4), nullable=True),
    )
    op.execute(
        """
        UPDATE ohlcv_daily
           SET open_raw = open,
               high_raw = high,
               low_raw = low
         WHERE adj_factor = 1
        """
    )
    _reenable_ohlcv_compression()


def downgrade() -> None:
    _disable_ohlcv_compression()
    op.execute("SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0")
    op.execute(
        """
        UPDATE ohlcv_daily
           SET source = 'kite'
         WHERE source = 'kite_adjusted'
        """
    )
    op.drop_column("ohlcv_daily", "low_raw")
    op.drop_column("ohlcv_daily", "high_raw")
    op.drop_column("ohlcv_daily", "open_raw")
    op.execute(f'ALTER TABLE ohlcv_daily DROP CONSTRAINT IF EXISTS "{_SOURCE_CHECK}"')
    op.execute(
        f'ALTER TABLE ohlcv_daily ADD CONSTRAINT "{_SOURCE_CHECK}" '
        "CHECK (source IN ('kite', 'nse'))"
    )
    _reenable_ohlcv_compression()
