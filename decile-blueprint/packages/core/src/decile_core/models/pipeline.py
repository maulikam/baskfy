"""``ingest_cursor`` — the resumable-backfill cursor (PROMPTS.md Prompt 3 deliverable 6).

docs/09 §"Kite specifics" names it ("resume from ``ingest_cursor``") and §Backfill requires
``--resume``, but docs/04 does not define the table. See docs/04b-ingest-cursor-addendum.md.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from decile_core.models.base import Base, CreatedAt, JsonObject, UpdatedAt

#: A cursor's lifecycle. ``done`` rows are what make a resumed run skip completed work.
CURSOR_STATUSES: tuple[str, ...] = ("pending", "running", "done", "failed")

#: What is being backfilled. Kept open-ended (a text column, not an enum) because Prompt 15's
#: backtest fan-out will want its own cursor kind without a migration.
CURSOR_KIND_BARS: str = "bars"


class IngestCursor(Base):
    """One resumable unit of backfill work.

    Granularity is *(kind, instrument, window)* — one row per instrument per chunked date window,
    matching the slices ``KiteProvider.chunk_windows`` yields. That is the smallest unit that can
    be re-run safely, and it means an interrupted run resumes at the chunk boundary rather than
    restarting an instrument's whole fifteen-year history.

    ``attempts`` and ``last_error`` live here rather than only in ``pipeline_run_step`` because a
    backfill is not a pipeline run: it is a one-off that may span days and be resumed by a
    different process (docs/09 §Backfill: "Budget a weekend and a resumable cursor").
    """

    __tablename__ = "ingest_cursor"
    __table_args__ = (
        PrimaryKeyConstraint("kind", "instrument_id", "window_start"),
        CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')", name="ingest_cursor_status"
        ),
        CheckConstraint("window_end >= window_start", name="ingest_cursor_window_ordered"),
        Index("ix_ingest_cursor_kind_status", "kind", "status"),
    )

    kind: Mapped[str] = mapped_column(String)
    instrument_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instrument.id"))
    window_start: Mapped[dt.date] = mapped_column(Date)
    window_end: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    rows_written: Mapped[int | None] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_error: Mapped[JsonObject | None] = mapped_column(JSONB)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]
