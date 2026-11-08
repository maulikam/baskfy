"""The catalyst feed (SW11B; ``docs/swing/STANDING-ANSWERS.md`` A3, DECISIONS-SW MD4).

**``sw_catalyst``** — a headline, a stamp and a link per watched name, from NSE's free
corporate-announcements and event-calendar reads. Never the filing's text. Unique on
``(user_id, instrument_id, url)`` so the 09:10 job is idempotent: the same filing is one row
however many mornings see it, and two filings never collide because the URL is kept verbatim.
``source`` names the read (``NSE_ANNOUNCEMENT`` rows carry ``published_at``;
``NSE_EVENT_CALENDAR`` rows carry ``earnings_date``). Cascades from the instrument and the user.

**``sw_watch.earnings_date``** — the flag: the nearest result date the calendar lists on or
after the morning the feed ran, refreshed every run, NULL when none. ``sw_watch.catalyst`` is
not touched here; the job fills it from the newest headline only while it is empty.

Revision ID: 0032_swing_catalyst
Revises: 0031_swing_review_corrections
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_swing_catalyst"
down_revision: str | None = "0031_swing_review_corrections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `baskfy_core.models.swing.SW_CATALYST_SOURCES`, written out because a migration is frozen.
SOURCES = ("NSE_ANNOUNCEMENT", "NSE_EVENT_CALENDAR")


def upgrade() -> None:
    op.create_table(
        "sw_catalyst",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("earnings_date", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN (" + ", ".join(f"'{value}'" for value in SOURCES) + ")",
            name=op.f("ck_sw_catalyst_source_known"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_sw_catalyst_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_sw_catalyst_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sw_catalyst")),
        sa.UniqueConstraint(
            "user_id",
            "instrument_id",
            "url",
            name=op.f("uq_sw_catalyst_user_id_instrument_id_url"),
        ),
    )
    # The read models' one query per page: the newest row per watched instrument.
    op.create_index(
        "ix_sw_catalyst_user_id_instrument_id_published_at",
        "sw_catalyst",
        ["user_id", "instrument_id", "published_at"],
        unique=False,
    )
    op.add_column("sw_watch", sa.Column("earnings_date", sa.Date(), nullable=True))


def downgrade() -> None:
    """Drop the table and the flag. A typed `sw_watch.catalyst` is text and stays; an
    auto-filled one is indistinguishable from typed and stays too — a downgrade loses the
    links, never a person's note."""
    op.drop_column("sw_watch", "earnings_date")
    op.drop_table("sw_catalyst")
