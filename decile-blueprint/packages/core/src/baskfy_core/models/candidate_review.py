"""A person's label on a candidate row's attention opinion — the fine-tuning set, one row at a time.

`baskfy_core.candidate_review` asks Laya "how much does this row deserve a look" over the row's
technicals in words and its filing, and measured the base checkpoint at a coin flip (25 Sep
2026). The labels a person puts on rows are what the fine-tune trains on. This is the table
they land in — the twin of `CatalystTagCorrection`, for the row question instead of the
headline one.

CONTENT-ADDRESSED, LIKE THE CACHE
---------------------------------
``review_key`` is ``candidate_review.review_key(state)`` — the same sha256 the Laya sidecar
keys its answer on — so a label applies wherever that exact state appears: two names with the
same facts and the same filing, or the same name on two mornings the scans wrote identically.
The ``state`` itself (the ``setup`` and ``filing`` words the model was shown) is kept verbatim
beside the key because the export is a training set, and a training set of hashes trains
nothing. ``instrument_id`` and ``symbol`` say which row the person was looking at when they
labelled it; they are provenance, not part of the key.

WHAT THE MODEL SAID
-------------------
``laya_label``/``laya_confidence`` are what the sidecar had cached **at the moment of
labelling**. They are the signal: a label that agrees with a sure model is a different lesson
from one that overrules it, and neither can be reconstructed once the checkpoint has moved.

It is one row per ``(user_id, review_key)``: a second label on the same state updates the row
rather than voting against the first. Display context only, like the opinion it labels —
nothing here enters a rank, a filter, a size or an order.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.candidate_review import ReviewLabel
from baskfy_core.models.base import Base, BigIntPk, CreatedAt, JsonObject, UpdatedAt

#: The three attention labels, as the CHECK constraint spells them.
REVIEW_LABELS: tuple[str, ...] = tuple(member.value for member in ReviewLabel)

#: ``laya_confidence`` — a probability, four decimals, as `opinion_from_laya` rounds it.
CONFIDENCE = Numeric(6, 4)


def _in_check(name: str, column: str, values: tuple[str, ...]) -> CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)


def _user_fk() -> Mapped[int]:
    """``user_id``, non-null, cascading — the sole tenant's, like every ``sw_`` table."""
    return mapped_column(BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False)


class CandidateReviewLabel(Base):
    """One person's word on one row state. Upserted on ``(user_id, review_key)``."""

    __tablename__ = "candidate_review_label"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "review_key",
            name="uq_candidate_review_label_user_id_review_key",
        ),
        _in_check("label_known", "label", REVIEW_LABELS),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    #: ``candidate_review.review_key(state)``: content-addressed on the words the model saw.
    review_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: The ``{"setup", "filing"}`` words shown to the model — the export's input, verbatim.
    state: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    #: Which row the person was looking at. Provenance; the key is the state alone.
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    #: The person's word: one of the three ``ReviewLabel`` values — the export's label.
    label: Mapped[str] = mapped_column(String, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    #: What Laya had cached at labelling time, and how sure it was; null when it had nothing.
    laya_label: Mapped[str | None] = mapped_column(String)
    laya_confidence: Mapped[Decimal | None] = mapped_column(CONFIDENCE)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]
