"""Specification tests for immutable curated-basket version drafts."""

from __future__ import annotations

import datetime as dt
import time
import tracemalloc
from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import cast

import pytest

from baskfy_core.curated_baskets import (
    BasketValidationError,
    ConstituentDraft,
    VersionDraft,
    validate_version_draft,
)


def version(*constituents: ConstituentDraft) -> VersionDraft:
    return VersionDraft(effective_date=dt.date(2026, 8, 21), constituents=constituents)


def constituent(
    instrument_id: int = 1,
    segment: str = "Largecap",
    weight: Decimal = Decimal("1.0000"),
) -> ConstituentDraft:
    return ConstituentDraft(instrument_id=instrument_id, segment=segment, weight=weight)


def mutate(instance: object, attribute: str, value: object) -> None:
    """Attempt a runtime mutation without asking the type checker to accept invalid source."""
    setattr(instance, attribute, value)


def test_an_exact_decimal_allocation_is_valid() -> None:
    validate_version_draft(
        version(
            constituent(1, "Largecap", Decimal("0.3333")),
            constituent(2, "Midcap", Decimal("0.3333")),
            constituent(3, "Smallcap", Decimal("0.3334")),
        )
    )


@pytest.mark.parametrize(
    "draft, message",
    [
        (version(), "at least one"),
        (version(constituent(weight=Decimal("0.9999"))), "total exactly"),
        (version(constituent(weight=Decimal("0"))), "greater than 0"),
        (version(constituent(weight=Decimal("1.0001"))), "at most 1"),
        (version(constituent(instrument_id=0)), "positive integer"),
        (version(constituent(instrument_id=-1)), "positive integer"),
        (version(constituent(instrument_id=True)), "positive integer"),
        (version(constituent(segment=" \t")), "nonblank"),
        (version(constituent(weight=Decimal("NaN"))), "finite Decimal"),
        (version(constituent(weight=Decimal("Infinity"))), "finite Decimal"),
    ],
)
def test_invalid_drafts_raise_the_typed_error(draft: VersionDraft, message: str) -> None:
    with pytest.raises(BasketValidationError, match=message):
        validate_version_draft(draft)


def test_duplicate_instrument_ids_are_rejected() -> None:
    with pytest.raises(BasketValidationError, match="duplicate instrument_id: 7"):
        validate_version_draft(
            version(
                constituent(7, weight=Decimal("0.4000")),
                constituent(7, weight=Decimal("0.6000")),
            )
        )


def test_weights_must_be_exactly_storable_at_four_decimal_places() -> None:
    with pytest.raises(BasketValidationError, match="exactly representable at 4 decimal places"):
        validate_version_draft(
            version(
                constituent(1, weight=Decimal("0.50005")),
                constituent(2, weight=Decimal("0.49995")),
            )
        )


@pytest.mark.parametrize(
    "draft, message",
    [
        (
            VersionDraft(
                effective_date=dt.datetime(2026, 8, 21, 9, 15),
                constituents=(constituent(),),
            ),
            "date, not a datetime",
        ),
        (
            VersionDraft(
                effective_date=dt.date(2026, 8, 21),
                constituents=(constituent(),),
                notes_md=cast("str | None", 42),
            ),
            "notes_md",
        ),
        (
            VersionDraft(
                effective_date=dt.date(2026, 8, 21),
                constituents=(constituent(),),
                source_scan_run_id=0,
            ),
            "source_scan_run_id",
        ),
        (
            VersionDraft(
                effective_date=dt.date(2026, 8, 21),
                constituents=(constituent(),),
                source_scan_run_id=True,
            ),
            "source_scan_run_id",
        ),
        (
            version(constituent(segment=cast("str", None))),
            "nonblank string",
        ),
    ],
)
def test_malformed_runtime_fields_raise_typed_errors(
    draft: VersionDraft,
    message: str,
) -> None:
    with pytest.raises(BasketValidationError, match=message):
        validate_version_draft(draft)


def test_drafts_and_constituents_are_frozen() -> None:
    holding = constituent()
    draft = version(holding)

    with pytest.raises(FrozenInstanceError):
        mutate(holding, "weight", Decimal("0.5000"))
    with pytest.raises(FrozenInstanceError):
        mutate(draft, "notes_md", "changed")


def test_large_version_validation_is_linear_and_bounded() -> None:
    draft = version(
        *(constituent(index, "Largecap", Decimal("0.0001")) for index in range(1, 10_001))
    )

    tracemalloc.start()
    started = time.perf_counter()
    validate_version_draft(draft)
    elapsed = time.perf_counter() - started
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert elapsed < 2.0
    assert peak_bytes < 4 * 1024 * 1024
