"""Deterministic SCAN → curated-basket genesis projection (docs/smallcase/03 rule 5).

Same ranked scan in → same version constituents out. No I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from baskfy_core.curated_baskets import WEIGHT_QUANTIZE, assert_weights_sum_to_one

__all__ = [
    "DEFAULT_SCAN_TOP_N",
    "FIXTURE_SCAN_SYMBOLS",
    "MOMENTUM_SCAN_BASKET_SLUG",
    "MOMENTUM_SCAN_STRATEGY_KEY",
    "ScanConstituentSpec",
    "ScanGenesisProjection",
    "equal_weights",
    "project_scan_top_n",
]

#: Engine strategy key stored on ``cb_basket.scan_strategy_key``.
MOMENTUM_SCAN_STRATEGY_KEY: Final = "momentum-scan"

#: Public catalog slug for the first SCAN basket (SC2 seed).
MOMENTUM_SCAN_BASKET_SLUG: Final = "momentum-scan"

#: Desk-style top-N for the genesis cut when the fixture does not say otherwise.
DEFAULT_SCAN_TOP_N: Final = 15

#: Fixture MomentumScan-like ranking used by the SC2 seed and its golden test.
FIXTURE_SCAN_SYMBOLS: Final[tuple[str, ...]] = (
    "RELIANCE",
    "TCS",
    "INFY",
    "HDFCBANK",
    "ICICIBANK",
    "SBIN",
    "BHARTIARTL",
    "ITC",
    "LT",
    "AXISBANK",
    "KOTAKBANK",
    "HINDUNILVR",
    "BAJFINANCE",
    "MARUTI",
    "SUNPHARMA",
    "TITAN",
    "NTPC",
    "POWERGRID",
)


@dataclass(frozen=True, slots=True)
class ScanConstituentSpec:
    """One row of a projected version — symbol + weight + display segment."""

    symbol: str
    weight: Decimal
    segment: str = "Equity"


@dataclass(frozen=True, slots=True)
class ScanGenesisProjection:
    """Everything needed to write a ``source=SCAN`` basket + genesis version."""

    slug: str
    name: str
    strategy_key: str
    version_no: int
    label: str
    constituents: tuple[ScanConstituentSpec, ...]
    source_scan_run_id: str | None

    @property
    def weights(self) -> tuple[Decimal, ...]:
        return tuple(c.weight for c in self.constituents)


def equal_weights(n: int) -> tuple[Decimal, ...]:
    """n equal weights quantized to 4 dp that sum to 1.0000."""
    if n < 1:
        raise ValueError("need at least one constituent")
    base = (Decimal("1") / Decimal(n)).quantize(WEIGHT_QUANTIZE, rounding=ROUND_HALF_UP)
    weights = [base] * n
    # Push residual onto the last name so the domain assert passes.
    residual = Decimal("1.0000") - sum(weights)
    weights[-1] = (weights[-1] + residual).quantize(WEIGHT_QUANTIZE, rounding=ROUND_HALF_UP)
    assert_weights_sum_to_one(weights)
    return tuple(weights)


def project_scan_top_n(  # noqa: PLR0913 - explicit projection knobs
    ranked_symbols: Sequence[str],
    *,
    top_n: int = DEFAULT_SCAN_TOP_N,
    scan_run_id: str | None = None,
    slug: str = MOMENTUM_SCAN_BASKET_SLUG,
    name: str = "Momentum Scan",
    strategy_key: str = MOMENTUM_SCAN_STRATEGY_KEY,
    segments: Mapping[str, str] | None = None,
) -> ScanGenesisProjection:
    """Cut equal-weight top-N from a ranked scan. Deterministic for identical inputs."""
    if top_n < 1:
        raise ValueError("top_n must be ≥ 1")
    cleaned = [s.strip().upper() for s in ranked_symbols if s and s.strip()]
    if len(cleaned) < top_n:
        raise ValueError(f"scan has {len(cleaned)} symbols; need at least top_n={top_n}")
    # Preserve first-seen order; drop later duplicates.
    seen: set[str] = set()
    ordered: list[str] = []
    for symbol in cleaned:
        if symbol in seen:
            continue
        seen.add(symbol)
        ordered.append(symbol)
        if len(ordered) == top_n:
            break
    if len(ordered) < top_n:
        raise ValueError(f"only {len(ordered)} unique symbols after de-dupe; need {top_n}")

    weights = equal_weights(top_n)
    seg_map = segments or {}
    constituents = tuple(
        ScanConstituentSpec(
            symbol=symbol,
            weight=weight,
            segment=str(seg_map.get(symbol, "Equity")),
        )
        for symbol, weight in zip(ordered, weights, strict=True)
    )
    return ScanGenesisProjection(
        slug=slug,
        name=name,
        strategy_key=strategy_key,
        version_no=1,
        label="GENESIS",
        constituents=constituents,
        source_scan_run_id=scan_run_id,
    )
