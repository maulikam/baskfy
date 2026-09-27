"""``eq_minute_bar`` — one-minute equity bars for the liquid universe (``docs/live/PLAN.md``, LV5).

The review's gap 1: no intraday bar store for equities, so no live TWT/VBT variant can be
defined or backtested. One row per instrument per minute, prices ``numeric(18,2)`` rounded at
write, raw (unadjusted) prints, ``volume`` from Kite's candle. Primary key ``(instrument_id, ts)``.

A TimescaleDB hypertable on ``ts`` with monthly chunks, the way ``ohlcv_daily`` is one on
``date``: ~210,000 rows a session. ``create_hypertable`` has no inverse and the downgrade drops the
table outright — the same path ``make downgrade`` takes for the other hypertables (0008).

Revision ID: 0056_eq_minute_bar
Revises: 0055_live_desk_state
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056_eq_minute_bar"
down_revision: str | None = "0055_live_desk_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "eq_minute_bar"
PRICE = sa.Numeric(precision=18, scale=2)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", PRICE, nullable=False),
        sa.Column("high", PRICE, nullable=False),
        sa.Column("low", PRICE, nullable=False),
        sa.Column("close", PRICE, nullable=False),
        sa.Column("volume", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "source IN ('KITE_HIST', 'TICKS')", name=op.f("ck_eq_minute_bar_source_known")
        ),
        sa.CheckConstraint("high >= low", name=op.f("ck_eq_minute_bar_high_not_below_low")),
        sa.CheckConstraint("volume >= 0", name=op.f("ck_eq_minute_bar_volume_non_negative")),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_eq_minute_bar_instrument_id_instrument"),
        ),
        sa.PrimaryKeyConstraint("instrument_id", "ts", name=op.f("pk_eq_minute_bar")),
    )
    op.execute(
        f"SELECT create_hypertable('{TABLE}', 'ts', chunk_time_interval => INTERVAL '1 month', "
        "if_not_exists => true)"
    )
    op.create_index(op.f("ix_eq_minute_bar_ts"), TABLE, ["ts"], unique=False)


def downgrade() -> None:
    op.drop_table(TABLE)
