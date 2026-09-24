"""``catalyst_tag_correction`` — a person's word on a headline tag, the fine-tuning set.

`baskfy_core.catalyst_tags` runs a keyword baseline and Laya's cached answer over the NSE
headlines `/build/overlap` links, and settled the order (Maulik, 25 Sep 2026): corrections are
collected against both before any model is fine-tuned. This is where they are collected.

One row per ``(user_id, headline_key)``, where the key is ``catalyst_tags.cache_key(headline)``
— the same content address the Laya sidecar caches under — so a correction applies to that
headline wherever it appears. The headline is kept verbatim for the export; ``rules_event_type``
and ``laya_event_type``/``laya_confidence`` are what the two readers said at correction time,
which is the training signal and cannot be reconstructed once the phrase list or the checkpoint
has moved. ``event_type`` is constrained to the eight settled types.

The downgrade drops the table: a correction is a label on display context, never an input to a
rank, a size or an order, so nothing else depends on it.

Revision ID: 0053_catalyst_tag_correction
Revises: 0052_fno
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053_catalyst_tag_correction"
down_revision: str | None = "0052_fno"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `baskfy_core.catalyst_tags.EventType`, written out because a migration is frozen.
EVENT_TYPES: tuple[str, ...] = (
    "earnings",
    "order",
    "approval",
    "fundraising",
    "governance",
    "corporate_action",
    "routine",
    "other",
)

TABLE = "catalyst_tag_correction"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("headline_key", sa.Text(), nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("rules_event_type", sa.String(), nullable=True),
        sa.Column("laya_event_type", sa.String(), nullable=True),
        sa.Column("laya_confidence", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN (" + ", ".join(f"'{value}'" for value in EVENT_TYPES) + ")",
            name=op.f(f"ck_{TABLE}_event_type_known"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f(f"fk_{TABLE}_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{TABLE}")),
        sa.UniqueConstraint(
            "user_id", "headline_key", name=op.f(f"uq_{TABLE}_user_id_headline_key")
        ),
    )


def downgrade() -> None:
    op.drop_table(TABLE)
