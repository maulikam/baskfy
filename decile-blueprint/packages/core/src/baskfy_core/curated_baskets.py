"""Pure contracts for append-only curated-basket versions.

The service layer persists these values, but validation deliberately stays here so a draft can be
proved safe before a database transaction performs its first write.  Validation is one pass over
the constituents and uses one set of instrument identifiers: both time and auxiliary memory are
linear in the number of names.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

EXACT_TOTAL_WEIGHT = Decimal("1.0000")
WEIGHT_QUANTUM = Decimal("0.0001")


class BasketValidationError(ValueError):
    """A version draft violates the curated-basket domain contract."""


@dataclass(frozen=True, slots=True)
class ConstituentDraft:
    """One immutable target holding in a basket version."""

    instrument_id: int
    segment: str
    weight: Decimal


@dataclass(frozen=True, slots=True)
class VersionDraft:
    """All caller-supplied facts needed to append one basket version."""

    effective_date: dt.date
    constituents: tuple[ConstituentDraft, ...]
    notes_md: str | None = None
    source_scan_run_id: int | None = None


def _validate_draft_shape(draft: VersionDraft) -> None:
    """Reject runtime values that bypassed the annotated dataclass constructor contract."""
    if not isinstance(draft, VersionDraft):
        raise BasketValidationError("draft must be a VersionDraft")
    if type(draft.effective_date) is not dt.date:
        raise BasketValidationError("effective_date must be a date, not a datetime")
    if not isinstance(draft.constituents, tuple):
        raise BasketValidationError("constituents must be an immutable tuple")
    if draft.notes_md is not None and not isinstance(draft.notes_md, str):
        raise BasketValidationError("notes_md must be a string or None")
    if draft.source_scan_run_id is not None and (
        type(draft.source_scan_run_id) is not int or draft.source_scan_run_id <= 0
    ):
        raise BasketValidationError("source_scan_run_id must be a positive integer or None")
    if not draft.constituents:
        raise BasketValidationError("a basket version requires at least one constituent")


def _validated_weight(
    constituent: ConstituentDraft,
    instrument_ids: set[int],
) -> Decimal:
    """Validate one constituent and return its already-storage-safe weight."""
    if not isinstance(constituent, ConstituentDraft):
        raise BasketValidationError("each constituent must be a ConstituentDraft")
    instrument_id = constituent.instrument_id
    if type(instrument_id) is not int or instrument_id <= 0:
        raise BasketValidationError("instrument_id must be a positive integer")
    if instrument_id in instrument_ids:
        raise BasketValidationError(f"duplicate instrument_id: {instrument_id}")
    instrument_ids.add(instrument_id)

    if not isinstance(constituent.segment, str) or not constituent.segment.strip():
        raise BasketValidationError(
            f"segment must be a nonblank string for instrument_id {instrument_id}"
        )

    weight = constituent.weight
    if not isinstance(weight, Decimal) or not weight.is_finite():
        raise BasketValidationError(
            f"weight must be a finite Decimal for instrument_id {instrument_id}"
        )
    if weight <= 0 or weight > 1:
        raise BasketValidationError(
            f"weight must be greater than 0 and at most 1 for instrument_id {instrument_id}"
        )
    if weight.quantize(WEIGHT_QUANTUM) != weight:
        raise BasketValidationError(
            f"weight must be exactly representable at 4 decimal places for instrument_id "
            f"{instrument_id}"
        )
    return weight


def validate_version_draft(draft: VersionDraft) -> None:
    """Validate exact weights and constituent identity without touching ambient state.

    ``Decimal`` equality is intentional: there is no epsilon around a portfolio allocation.  The
    database stores four decimal places, so a caller must supply an allocation whose mathematical
    total is exactly ``1.0000`` before the repository writes anything.
    """
    _validate_draft_shape(draft)

    instrument_ids: set[int] = set()
    total_weight = sum(
        (_validated_weight(constituent, instrument_ids) for constituent in draft.constituents),
        start=Decimal(0),
    )

    if total_weight != EXACT_TOTAL_WEIGHT:
        raise BasketValidationError(
            f"constituent weights must total exactly {EXACT_TOTAL_WEIGHT}; got {total_weight}"
        )
