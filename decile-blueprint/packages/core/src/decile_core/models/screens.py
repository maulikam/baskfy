"""Screens, screen runs and market health — docs/04-data-model.md."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from decile_core.models.base import (
    BREADTH,
    Base,
    BigIntPk,
    CreatedAt,
    JsonObject,
    UpdatedAt,
)


class Screen(Base):
    """A saved screen. ``user_id IS NULL`` marks a system/example screen (docs/04)."""

    __tablename__ = "screen"
    __table_args__ = (Index("ix_screen_user_id", "user_id"),)

    id: Mapped[BigIntPk]
    public_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("app_user.id"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    #: Validated by decile_core.screen_definition.ScreenDefinition (mirrored in Zod).
    definition: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    columns: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    is_example: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class ScreenRun(Base):
    """Audit trail and "historical ranks" cache.

    ``definition_hash`` is the sha256 of ScreenDefinition.canonical_json(), so a user can prove
    exactly what they saw (docs/06 "Determinism guarantee").
    """

    __tablename__ = "screen_run"
    __table_args__ = (UniqueConstraint("screen_id", "as_of", "definition_hash"),)

    id: Mapped[BigIntPk]
    screen_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("screen.id", ondelete="CASCADE"), nullable=False
    )
    as_of: Mapped[dt.date] = mapped_column(Date, nullable=False)
    definition_hash: Mapped[str] = mapped_column(String, nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: [{rank, instrument_id, factor_value}]
    results: Mapped[list[JsonObject]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[CreatedAt]


class MarketHealthDaily(Base):
    """Breadth per universe (docs/01 §6). Snapshotted daily, never recomputed."""

    __tablename__ = "market_health_daily"
    __table_args__ = (PrimaryKeyConstraint("index_id", "date"),)

    index_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("index_def.id"))
    date: Mapped[dt.date] = mapped_column(Date)
    pct_above_200dma: Mapped[Decimal | None] = mapped_column(BREADTH)
    pct_above_50dma: Mapped[Decimal | None] = mapped_column(BREADTH)
    pct_within_10pct_ath: Mapped[Decimal | None] = mapped_column(BREADTH)
    pct_ret_1y_positive: Mapped[Decimal | None] = mapped_column(BREADTH)
    constituent_count: Mapped[int | None] = mapped_column(Integer)
