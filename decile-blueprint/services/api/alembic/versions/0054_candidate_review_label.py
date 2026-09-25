"""``candidate_review_label`` — a person's label on a row's attention opinion, the fine-tuning set.

`baskfy_core.candidate_review` asks Laya "how much does this row deserve a look" over the row's
technicals in words plus its filing, and measured the base checkpoint at a coin flip (25 Sep
2026): the labels a person puts on rows are what the fine-tune trains on. This is where they are
collected — the twin of ``catalyst_tag_correction`` (0053) for the row question.

One row per ``(user_id, review_key)``, where the key is ``candidate_review.review_key(state)`` —
the same content address the Laya sidecar caches under — so a label applies to that state
wherever it appears. The ``state`` (the ``setup`` and ``filing`` words the model saw) is kept
verbatim as JSONB for the export; ``instrument_id``/``symbol`` say which row was on the page;
``laya_label``/``laya_confidence`` are what the model had cached at labelling time, which is the
training signal and cannot be reconstructed once the checkpoint has moved. ``label`` is
constrained to the three settled words.

The downgrade drops the table: a label is display context, never an input to a rank, a size or
an order, so nothing else depends on it.

Revision ID: 0054_candidate_review_label
Revises: 0053_catalyst_tag_correction
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0054_candidate_review_label"
down_revision: str | None = "0053_catalyst_tag_correction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `baskfy_core.candidate_review.ReviewLabel`, written out because a migration is frozen.
REVIEW_LABELS: tuple[str, ...] = ("look_first", "worth_a_look", "skip")

TABLE = "candidate_review_label"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("review_key", sa.Text(), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("laya_label", sa.String(), nullable=True),
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
            "label IN (" + ", ".join(f"'{value}'" for value in REVIEW_LABELS) + ")",
            name=op.f(f"ck_{TABLE}_label_known"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f(f"fk_{TABLE}_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f(f"fk_{TABLE}_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{TABLE}")),
        sa.UniqueConstraint("user_id", "review_key", name=op.f(f"uq_{TABLE}_user_id_review_key")),
    )


def downgrade() -> None:
    op.drop_table(TABLE)
