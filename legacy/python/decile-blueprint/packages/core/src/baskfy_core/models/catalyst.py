"""A person's correction of a headline tag — the fine-tuning set, one row at a time.

`baskfy_core.catalyst_tags` settled the order (Maulik, 25 Sep 2026): a keyword baseline first,
Laya's answer beside it, **corrections collected against both**, and only then a fine-tuned
model shadowed against the baseline. This is the table the corrections land in.

CONTENT-ADDRESSED, LIKE THE CACHE
---------------------------------
``headline_key`` is ``catalyst_tags.cache_key(headline)`` — the same sha256 the Laya sidecar
keys its answer on — so a correction applies wherever that exact headline appears: two names
that filed the same notice, or the same filing on two mornings, read the same word. The
headline itself is kept verbatim beside the key because the export is a training set, and a
training set of hashes trains nothing.

WHAT THE TWO READERS SAID
-------------------------
``rules_event_type`` and ``laya_event_type``/``laya_confidence`` are what the baseline and the
model answered **at the moment of correction**. They are the signal: a correction that agrees
with the rules and overrules a sure model is a different lesson from one that overrules both,
and neither can be reconstructed later once the phrase list or the checkpoint has moved.

It is one row per ``(user_id, headline_key)``: a second correction of the same headline updates
the row rather than voting against the first. Display context only, like the tag it corrects —
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
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.catalyst_tags import EventType
from baskfy_core.models.base import Base, BigIntPk, CreatedAt, UpdatedAt

#: The eight event types, as the CHECK constraint spells them.
CATALYST_EVENT_TYPES: tuple[str, ...] = tuple(member.value for member in EventType)

#: ``laya_confidence`` — a probability, four decimals, as `tag_from_laya` rounds it.
CONFIDENCE = Numeric(6, 4)


def _in_check(name: str, column: str, values: tuple[str, ...]) -> CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)


def _user_fk() -> Mapped[int]:
    """``user_id``, non-null, cascading — the sole tenant's, like every ``sw_`` table."""
    return mapped_column(BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False)


class CatalystTagCorrection(Base):
    """One person's word on one headline. Upserted on ``(user_id, headline_key)``."""

    __tablename__ = "catalyst_tag_correction"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "headline_key",
            name="uq_catalyst_tag_correction_user_id_headline_key",
        ),
        _in_check("event_type_known", "event_type", CATALYST_EVENT_TYPES),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    #: ``catalyst_tags.cache_key(headline)``: content-addressed, case- and space-blind.
    headline_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: The headline as the exchange published it — the export's input text.
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    #: The person's word: one of the eight ``EventType`` values — the export's label.
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    #: What the rules said at correction time; null only if there was no headline to read.
    rules_event_type: Mapped[str | None] = mapped_column(String)
    #: What Laya had cached at correction time, and how sure it was; null when it had nothing.
    laya_event_type: Mapped[str | None] = mapped_column(String)
    laya_confidence: Mapped[Decimal | None] = mapped_column(CONFIDENCE)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]
