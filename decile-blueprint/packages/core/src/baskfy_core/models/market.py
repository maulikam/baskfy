"""Price history, corporate actions, index universes, fundamentals — docs/04-data-model.md."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.models.base import (
    ADJ_FACTOR,
    FUNDAMENTAL,
    MONEY,
    PRICE_RAW,
    RATIO_6DP,
    WEIGHT,
    YIELD,
    Base,
    BigIntPk,
    JsonObject,
    SmallIntPk,
)

#: docs/04: corporate_action.action_type
CORPORATE_ACTION_TYPES: tuple[str, ...] = (
    "dividend",
    "split",
    "bonus",
    "rights",
    "demerger",
)

#: docs/04: ohlcv_daily.source — ``kite_adjusted`` is M29 deep history (vendor-adjusted, no
#: exchange print); ``kite`` is a nightly raw candle that ``apply_adjustments`` must cover.
BAR_SOURCES: tuple[str, ...] = ("kite", "nse", "kite_adjusted")

#: Deep-history / bhavcopy seam (M29). Rows before this with source kite are retagged
#: ``kite_adjusted`` by migration 0044.
BHAVCOPY_SEAM: dt.date = dt.date(2024, 1, 1)


class OhlcvDaily(Base):
    """Daily bars. TimescaleDB hypertable on ``date`` (1-year chunks), compressed after 90 days.

    ``close`` is the adjusted series that every factor reads; ``close_raw`` is the exchange
    print used wherever the user expects a real price (docs/02 rule 2). ``open_raw`` /
    ``high_raw`` / ``low_raw`` are nullable: NULL when the exchange print is unknown (deep
    history, or an older row adjusted before those columns existed).
    """

    __tablename__ = "ohlcv_daily"
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "date"),
        CheckConstraint(
            "source IN ('kite', 'nse', 'kite_adjusted')", name="ohlcv_daily_source"
        ),
    )

    instrument_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instrument.id"))
    date: Mapped[dt.date] = mapped_column(Date)
    open: Mapped[Decimal] = mapped_column(PRICE_RAW, nullable=False)
    high: Mapped[Decimal] = mapped_column(PRICE_RAW, nullable=False)
    low: Mapped[Decimal] = mapped_column(PRICE_RAW, nullable=False)
    close: Mapped[Decimal] = mapped_column(PRICE_RAW, nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    close_raw: Mapped[Decimal] = mapped_column(PRICE_RAW, nullable=False)
    volume_raw: Mapped[int] = mapped_column(BigInteger, nullable=False)
    open_raw: Mapped[Decimal | None] = mapped_column(PRICE_RAW)
    high_raw: Mapped[Decimal | None] = mapped_column(PRICE_RAW)
    low_raw: Mapped[Decimal | None] = mapped_column(PRICE_RAW)
    turnover: Mapped[Decimal | None] = mapped_column(MONEY)
    adj_factor: Mapped[Decimal] = mapped_column(ADJ_FACTOR, nullable=False, server_default="1")
    upper_circuit: Mapped[Decimal | None] = mapped_column(PRICE_RAW)
    lower_circuit: Mapped[Decimal | None] = mapped_column(PRICE_RAW)
    source: Mapped[str] = mapped_column(String, nullable=False)


class CorporateAction(Base):
    __tablename__ = "corporate_action"
    __table_args__ = (
        UniqueConstraint("instrument_id", "action_type", "ex_date"),
        CheckConstraint(
            "action_type IN ('dividend', 'split', 'bonus', 'rights', 'demerger')",
            name="corporate_action_type",
        ),
    )

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.id"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String, nullable=False)
    ex_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    # split 10:1 -> from=10, to=1 ; bonus 4:1 -> from=4, to=1
    ratio_from: Mapped[Decimal | None] = mapped_column(RATIO_6DP)
    ratio_to: Mapped[Decimal | None] = mapped_column(RATIO_6DP)
    amount: Mapped[Decimal | None] = mapped_column(PRICE_RAW)
    raw: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)


class IndexDef(Base):
    """A selectable universe or a dashboard-only index.

    ``id`` is load-bearing: ``factor_daily.universe_mask`` sets one bit per ``index_def.id``
    (docs/04), and a PostgreSQL ``integer`` has 31 usable bits. Universe ids are therefore
    allocated in 1..31 and dashboard-only index ids from 100 upward. See
    ``baskfy_core.universes.UNIVERSE_MASK_BIT``.
    """

    __tablename__ = "index_def"

    id: Mapped[SmallIntPk]
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    is_universe: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)


#: How a membership row was established (docs/09 §Backfill).
#:   ``nse_file``      read from a published NSE constituent file for that date. Certain.
#:   ``reconstructed`` carried back from the earliest available file, because NSE publishes no
#:                     constituent file for the date. docs/09: "record the reconstruction in
#:                     `index_member_daily.source` so backtests can exclude uncertain periods".
#:   ``derived``       resolved by rule rather than by a file — `nifty-allcap` and `etf`
#:                     (docs/06 §"Step 2"). Certain, but not from a constituent file.
MEMBERSHIP_SOURCES: tuple[str, ...] = ("nse_file", "reconstructed", "derived")


class IndexMemberDaily(Base):
    """Point-in-time index membership — the anti-look-ahead spine (docs/04, docs/06 §2).

    ``source`` is an addition to docs/04's DDL. docs/09 §Backfill requires it by name: pre-2018
    constituent files do not exist, so early membership must be reconstructed from the earliest
    file available — and a backtest has to be able to tell reconstructed history from observed
    history, or it will report confidence it has not earned. See
    docs/04b-pipeline-tables-addendum.md.
    """

    __tablename__ = "index_member_daily"
    __table_args__ = (
        PrimaryKeyConstraint("index_id", "date", "instrument_id"),
        CheckConstraint(
            "source IN ('nse_file', 'reconstructed', 'derived')", name="index_member_source"
        ),
        Index("ix_index_member_daily_date_index_id", "date", "index_id"),
        # The factsheet's direction of the same question: which indices was *this instrument* in
        # on this date (docs/10a §4). Prompt 16 deliverable 2 — migration 0008.
        Index("ix_index_member_daily_date_instrument_id", "date", "instrument_id"),
    )

    index_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("index_def.id"))
    date: Mapped[dt.date] = mapped_column(Date)
    instrument_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instrument.id"))
    weight: Mapped[Decimal | None] = mapped_column(WEIGHT)
    source: Mapped[str] = mapped_column(String, nullable=False, server_default="nse_file")


class IndexSnapshotDaily(Base):
    """Powers /dashboard (docs/01 §7)."""

    __tablename__ = "index_snapshot_daily"
    __table_args__ = (PrimaryKeyConstraint("index_id", "date"),)

    index_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("index_def.id"))
    date: Mapped[dt.date] = mapped_column(Date)
    level: Mapped[Decimal | None] = mapped_column(PRICE_RAW)
    change_abs: Mapped[Decimal | None] = mapped_column(PRICE_RAW)
    change_pct: Mapped[Decimal | None] = mapped_column(YIELD)
    pe: Mapped[Decimal | None] = mapped_column(FUNDAMENTAL)
    pb: Mapped[Decimal | None] = mapped_column(FUNDAMENTAL)
    div_yield: Mapped[Decimal | None] = mapped_column(YIELD)


class FundamentalDaily(Base):
    """P/E lives here, not on the fact row (docs/13 §2 finding 12)."""

    __tablename__ = "fundamental_daily"
    __table_args__ = (PrimaryKeyConstraint("instrument_id", "date"),)

    instrument_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instrument.id"))
    date: Mapped[dt.date] = mapped_column(Date)
    # docs/13 §2 finding 7: marketcap is an integer in ₹ crore.
    marketcap_cr: Mapped[int | None] = mapped_column(BigInteger)
    pe: Mapped[Decimal | None] = mapped_column(FUNDAMENTAL)
    pb: Mapped[Decimal | None] = mapped_column(FUNDAMENTAL)
    div_yield: Mapped[Decimal | None] = mapped_column(YIELD)
    shares_outstanding: Mapped[int | None] = mapped_column(BigInteger)
