"""``desk_score_daily`` - the weekly book's Momentum Quality Score, stored per day (PLAN.md C2).

The screener reads this table and never re-scores. Every row is what
:func:`baskfy_core.desk_score_service.score_day` returned for that instrument on that date: the
book's own scan over the whole ``nse_cash`` universe, scored by ``score.score`` with
``DESK_CONFIG``. Phase 1 recomputed the SCORE over the screener's filtered survivors, which ran the
percentiles and the 1/99 clips over a different population and so produced a second, different
book; storing the book's number once is the fix.

Rejected rows are stored too - with ``score`` and ``score_rank`` NULL and the reject tokens in
``reject`` - because "why is this name not ranked" is a question the explain panel answers from
this row. They are never ranked: ``score_rank`` is 1..n over unrejected names only.

Precision is the storage contract (house rule 8): ``score`` at 1 dp, as the book rounds it; A-F
and ``ext_over_20dma`` at 4 dp. ``score_version`` names the formula (``DESK_SCORE_VERSION``), so a
row written before a scoring change can be told apart from one written after.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.models.base import Base

#: numeric(6,1): the book's SCORE is a 0-100 sum rounded to one place.
DESK_SCORE = Numeric(6, 1)
#: numeric(8,4): each A-F part and the 20-DMA extension, at the precision the service rounds to.
DESK_PART = Numeric(8, 4)
DESK_EXTENSION = Numeric(10, 4)


class DeskScoreDaily(Base):
    """One row per scanned instrument per trading day."""

    __tablename__ = "desk_score_daily"
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "date"),
        # The screener's read: one date's ranked rows, best first.
        Index("ix_desk_score_daily_date_score_rank", "date", "score_rank"),
    )

    instrument_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instrument.id"))
    date: Mapped[dt.date] = mapped_column(Date)
    score: Mapped[Decimal | None] = mapped_column(DESK_SCORE)
    score_rank: Mapped[int | None] = mapped_column(Integer)
    a_trend: Mapped[Decimal | None] = mapped_column(DESK_PART)
    b_momentum: Mapped[Decimal | None] = mapped_column(DESK_PART)
    c_sharpe: Mapped[Decimal | None] = mapped_column(DESK_PART)
    d_consistency: Mapped[Decimal | None] = mapped_column(DESK_PART)
    e_liquidity: Mapped[Decimal | None] = mapped_column(DESK_PART)
    f_penalty: Mapped[Decimal | None] = mapped_column(DESK_PART)
    ext_over_20dma: Mapped[Decimal | None] = mapped_column(DESK_EXTENSION)
    reject: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    score_version: Mapped[str] = mapped_column(String(32), nullable=False)
