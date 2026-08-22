"""Screens, screen runs and market health — docs/04-data-model.md."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
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

from baskfy_core.models.base import (
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
    #: Validated by baskfy_core.screen_definition.ScreenDefinition (mirrored in Zod).
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
    #: M14 §1 — the desk's cash bands are calibrated on this one. Nullable: rows written before
    #: migration 0011 have no 20-day figure and must say so rather than claim zero.
    pct_above_20dma: Mapped[Decimal | None] = mapped_column(BREADTH)
    pct_within_10pct_ath: Mapped[Decimal | None] = mapped_column(BREADTH)
    pct_ret_1y_positive: Mapped[Decimal | None] = mapped_column(BREADTH)
    constituent_count: Mapped[int | None] = mapped_column(Integer)


class BasketSnapshot(Base):
    """What the strategy wanted on one date, computed once by the nightly chain (M30).

    `/baskets` used to build this per request: every bar the scanned symbols have ever had, into
    Polars, scored, turned into a plan. Fine at two years of history; **67 seconds** at nine
    (`DECISIONS-MERGE.md` M29.6). The inputs change once a night, so it is computed once a night.

    ``payload`` is the endpoint's response body, stored whole. It is a record of a decision on a
    date, not something anything queries across, and shredding it into columns would mean a
    migration every time the page gains a field.
    """

    __tablename__ = "basket_snapshot"
    __table_args__ = (
        UniqueConstraint("as_of", "data_version", name="uq_basket_snapshot_as_of_version"),
    )

    id: Mapped[BigIntPk]
    as_of: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    #: docs/06's cache key — definition + as-of + data version.
    screen_run_id: Mapped[str] = mapped_column(String, nullable=False)
    data_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    #: So a regression like M29's shows up in the table rather than only in somebody's patience.
    computed_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    computed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
