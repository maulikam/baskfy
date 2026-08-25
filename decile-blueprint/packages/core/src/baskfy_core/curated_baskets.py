"""Pure domain rules for the curated-basket product layer (docs/smallcase/03).

No database, no network, no clock — house rule 1.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

#: Environment variable holding the sole tenant's ``app_user.id`` until a second tenant
#: is admitted. Every user-scoped ``cb_*`` row must carry an explicit ``user_id``; nothing
#: infers "the user".
SOLE_USER_ENV: str = "BASKFY_SOLE_USER_ID"

#: Seed rows written by ``baskfy_api.curated_seed`` (docs/smallcase/03).
MANAGER_SLUG_BASKFY_ENGINE: str = "baskfy-engine"
MANAGER_SLUG_MAULIK: str = "maulik"

MANAGER_SEED_SLUGS: frozenset[str] = frozenset({MANAGER_SLUG_BASKFY_ENGINE, MANAGER_SLUG_MAULIK})

#: Constituent weights are stored at four decimal places; the sum must reconcile to one.
WEIGHT_SUM_TARGET: Decimal = Decimal("1.0000")
WEIGHT_SUM_TOLERANCE: Decimal = Decimal("0.00005")
WEIGHT_QUANTIZE: Decimal = Decimal("0.0001")


class VersionImmutableError(Exception):
    """Raised when a caller attempts to mutate an existing basket version.

    ``cb_basket_version`` and ``cb_constituent`` are append-only facts (docs/smallcase/03).
    Corrections ship as a new version; the service layer refuses UPDATEs on these rows.
    """


def assert_weights_sum_to_one(weights: Sequence[Decimal]) -> None:
    """Raise ``ValueError`` unless *weights* sum to 1.0000 within tolerance.

    Each weight is quantized to four decimal places before summing, matching write-time
    rounding (house rule 8) and ``cb_constituent.weight`` storage precision.
    """
    if not weights:
        raise ValueError("constituent weights cannot be empty")
    total = sum(w.quantize(WEIGHT_QUANTIZE) for w in weights)
    delta = abs(total - WEIGHT_SUM_TARGET)
    if delta > WEIGHT_SUM_TOLERANCE:
        raise ValueError(
            f"constituent weights must sum to {WEIGHT_SUM_TARGET}, got {total} "
            f"(delta {delta} exceeds tolerance {WEIGHT_SUM_TOLERANCE})"
        )


def assert_version_is_new(*, existing_version_nos: Sequence[int], new_version_no: int) -> None:
    """Refuse creating a version whose number already exists for the basket."""
    if new_version_no in existing_version_nos:
        raise VersionImmutableError(
            f"version_no {new_version_no} already exists; "
            "versions are immutable — publish a new version_no instead"
        )


def refuse_version_mutation() -> None:
    """Document the immutability contract; the service layer raises on UPDATE.

    SQLAlchemy models deliberately expose no update helpers for version rows. Callers that
    need to change a basket cut a new ``cb_basket_version`` instead of mutating an old one.
    """
    raise VersionImmutableError(
        "cb_basket_version and cb_constituent rows are append-only; "
        "corrections must be a new version"
    )
