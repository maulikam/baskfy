"""``ScreenDefinition`` — the single source of truth for a saved screen's JSON.

Shape: docs/04-data-model.md §Screens ("screen.definition JSON shape").
Semantics: docs/06-screener-semantics.md §"Step 4 — filters".
Mirrored in TypeScript at packages/api-client/src/screen-definition.ts (kept honest by
packages/api-client/test/parity.test.ts and tests/test_screen_definition_parity.py).

Sentinel values
---------------
The reference product encodes "this filter is off" inside the value itself, so a naive reader
will silently apply a filter that the user meant to disable. Every sentinel is therefore given a
named constant and an ``is_active`` predicate; nothing downstream should compare to a literal.

    away_from_high.ath / .one_year   100  = ignore   (docs/01 §2.4)
    positive_days.m*                   0  = ignore   (docs/01 §2.5)
    circuits.m*                    > 250  = ignore   (docs/01 §2.6)
    ignore_above_beta                100  = ignore   (docs/01 §2.10)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from baskfy_core.factor_registry import CUSTOM_FILTER_OPERANDS, FACTORS

# --- Sentinels (docs/01 §2.4-§2.10, docs/06 step 4) --------------------------
AWAY_FROM_HIGH_IGNORE: Final = 100
POSITIVE_DAYS_IGNORE: Final = 0
CIRCUITS_IGNORE_ABOVE: Final = 250
IGNORE_ABOVE_BETA_IGNORE: Final = 100

#: docs/01 §2.14 — the screener exposes exactly three custom-filter slots.
MAX_CUSTOM_FILTERS: Final = 3

#: docs/ranking/PLAN.md C3 — the explicit ranking-term list is capped at eight terms.
MAX_RANKING_TERMS: Final = 8

#: docs/ranking/PLAN.md C3 — at most ten factor-range eligibility filters.
MAX_FACTOR_RANGES: Final = 10

#: docs/ranking/PLAN.md C3 — a term weight lies in ``(0, MAX_TERM_WEIGHT]``.
MAX_TERM_WEIGHT: Final = 100

#: The Wasserstein regime labels ``factor_daily.regime`` carries (C1 ``regime_priority``).
REGIME_VALUES: Final[tuple[str, ...]] = ("BULL", "NEUTRAL", "BEAR")

#: A percentage filter's upper bound. Positive-days values are a share of the window.
PERCENT_MAX: Final = 100

#: docs/01 §2.9 — the NSE series the screener switches on. `EQ`/`BE` are the main board; `SM`,
#: `ST` and `SZ` are the Emerge (SME) platform, added by M59 so an SME screen is expressible.
#: Widening this is a public API contract change: the Zod mirror in
#: `packages/api-client/src/screen-definition.ts` carries the same list and the parity tests fail
#: if the two drift. Default stays `["EQ"]`, so no existing screen changes meaning.
SERIES_VALUES: Final[tuple[str, ...]] = ("EQ", "BE", "SM", "ST", "SZ")

#: The subset of :data:`SERIES_VALUES` that is the Emerge platform rather than the main board.
SME_SERIES_VALUES: Final[tuple[str, ...]] = ("SM", "ST", "SZ")

SortDirection = Literal["asc", "desc"]
RankingModeName = Literal["single", "sequential", "composite"]
RankingScopeName = Literal["filtered_results", "fixed_universe", "within_sector"]
ApplyFiltersOn = Literal[
    "all", "decile_1", "decile_2", "decile_3", "decile_4", "decile_5", "top_50", "top_100"
]
CustomFilterOp = Literal[">=", "<=", "="]
TermPreference = Literal["higher", "lower", "target_range"]
MissingDataPolicy = Literal["penalize", "neutral", "exclude"]
RegimeLabel = Literal["BULL", "NEUTRAL", "BEAR"]

#: A finite float. JSON cannot carry NaN or infinity, and a Python caller must not smuggle one in:
#: a NaN bound would make every comparison false and silently empty a screen.
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]

#: A factor / column key. The shape check is the outer gate; the whitelist check is
#: :func:`_known_factor` / :func:`_known_operand` below, which reject anything the factor registry
#: does not name. Kept as a constrained ``str`` rather than a ``Literal`` of 64 values so that the
#: generated JSON Schema — and therefore the Zod mirror in ``packages/api-client`` — stays the
#: shape ``packages/api-client/test/parity.test.ts`` already pins. The registry is served to the
#: client at runtime by ``GET /meta/factors`` (docs/07 §Metadata).
FactorKey = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]


def _known_factor(key: str, field: str) -> str:
    """Reject a sort key the factor registry does not define.

    docs/06 §"Reference SQL skeleton": "`<factorN_expr>` is produced by a **whitelisted** factor
    registry ... never string-interpolated from user input." Rejecting here rather than only in
    ``baskfy_core.screener`` is what turns a bad key into a 422 at the edge instead of a failure
    deeper in, and it means a definition can never be *persisted* with a key that has no SQL.
    """
    if key not in FACTORS:
        raise ValueError(
            f"{field}: {key!r} is not one of the {len(FACTORS)} factor-registry keys "
            "(docs/01 §3; see GET /meta/factors)"
        )
    return key


def _rankable_factor(key: str, field: str) -> str:
    """A ranking term must name a registry factor that can reorder a list.

    ``rankable=False`` factors (a return in excess of one common index, a percentile that only
    gates) are refused with a pointer to ``factor_ranges``, which is where they mean something.
    """
    _known_factor(key, field)
    return key


def _known_operand(key: str, field: str) -> str:
    """Reject a custom-filter operand outside docs/01 §2.14's list.

    Narrower than the sort-factor whitelist on purpose: a blend or a beta-guarded ratio is a
    legitimate sort key but not a legitimate operand for a field-to-field comparison.
    """
    if key not in CUSTOM_FILTER_OPERANDS:
        raise ValueError(
            f"{field}: {key!r} is not one of the {len(CUSTOM_FILTER_OPERANDS)} custom-filter "
            "operands (docs/01 §2.14)"
        )
    return key


#: The 14 selectable universes (docs/01 §2.1). Spelled out rather than derived from
#: ``UNIVERSE_SLUGS`` because ``Literal`` cannot be built from a runtime tuple, and the generated
#: OpenAPI schema — and therefore the TS client and the ``sort_by`` dropdown — needs the values
#: inline. ``test_screen_definition.py::test_universe_slug_literal_matches_registry`` fails if
#: this list and ``baskfy_core.universes.UNIVERSES`` ever disagree.
UniverseSlug = Literal[
    "nifty-50",
    "nifty-next-50",
    "nifty-100",
    "nifty-200",
    "nifty-500",
    "nifty-total-market",
    "nifty-large-mid-250",
    "nifty-midcap-150",
    "nifty-smallcap-250",
    "nifty-microcap-250",
    "nifty-mid-small-400",
    "nifty-allcap",
    "nifty-fno",
    "etf",
    "nse-sme-emerge",
]


class _Model(BaseModel):
    """Every node rejects unknown keys (docs/07: ``extra="forbid"``)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


class MovingAverageFilter(_Model):
    """Eight independent switches, AND-combined (docs/01 §2.3)."""

    enabled: bool = False
    above_200: bool = False
    above_100: bool = False
    above_50: bool = False
    above_20: bool = False
    below_200: bool = False
    below_100: bool = False
    below_50: bool = False
    below_20: bool = False

    @model_validator(mode="after")
    def _reject_contradictions(self) -> MovingAverageFilter:
        """``above_K`` and ``below_K`` together can never match a row."""
        if not self.enabled:
            return self
        both = [
            k
            for k in (200, 100, 50, 20)
            if getattr(self, f"above_{k}") and getattr(self, f"below_{k}")
        ]
        if both:
            windows = ", ".join(str(k) for k in both)
            raise ValueError(
                f"moving_average: above and below cannot both be set for MA {windows}; "
                "no stock can satisfy both."
            )
        return self

    def is_active(self) -> bool:
        if not self.enabled:
            return False
        return any(
            getattr(self, f"{side}_{k}") for side in ("above", "below") for k in (200, 100, 50, 20)
        )


class AwayFromHighFilter(_Model):
    """ "Within X% of high". ``100`` means ignore (docs/01 §2.4)."""

    ath: int = AWAY_FROM_HIGH_IGNORE
    one_year: int = AWAY_FROM_HIGH_IGNORE

    @field_validator("ath", "one_year")
    @classmethod
    def _in_range(cls, v: int) -> int:
        if not 0 <= v <= AWAY_FROM_HIGH_IGNORE:
            raise ValueError(
                f"away_from_high must be between 0 and {AWAY_FROM_HIGH_IGNORE} "
                f"({AWAY_FROM_HIGH_IGNORE} = ignore); got {v}"
            )
        return v

    def ath_is_active(self) -> bool:
        return self.ath != AWAY_FROM_HIGH_IGNORE

    def one_year_is_active(self) -> bool:
        return self.one_year != AWAY_FROM_HIGH_IGNORE


class PositiveDaysFilter(_Model):
    """Minimum % of trading days that closed up. ``0`` means ignore (docs/01 §2.5)."""

    m12: int = POSITIVE_DAYS_IGNORE
    m9: int = POSITIVE_DAYS_IGNORE
    m6: int = POSITIVE_DAYS_IGNORE
    m3: int = POSITIVE_DAYS_IGNORE
    m1: int = POSITIVE_DAYS_IGNORE

    @field_validator("m12", "m9", "m6", "m3", "m1")
    @classmethod
    def _in_range(cls, v: int) -> int:
        if not 0 <= v <= PERCENT_MAX:
            raise ValueError(
                f"positive_days is a percentage between 0 and {PERCENT_MAX} "
                f"({POSITIVE_DAYS_IGNORE} = ignore); got {v}"
            )
        return v

    def active_windows(self) -> tuple[str, ...]:
        return tuple(
            w for w in ("m12", "m9", "m6", "m3", "m1") if getattr(self, w) != POSITIVE_DAYS_IGNORE
        )


class CircuitsFilter(_Model):
    """Maximum permitted circuit-hit days in the window. ``> 250`` means ignore (docs/01 §2.6).

    Note the asymmetry with the other sentinels: the ignore value is a *threshold*, not a single
    magic number, because any cap above a full year of trading days cannot bind.
    """

    m12: int = 999
    m9: int = 999
    m6: int = 999
    m3: int = 999
    m1: int = 999

    @field_validator("m12", "m9", "m6", "m3", "m1")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(
                f"circuits cap cannot be negative (> {CIRCUITS_IGNORE_ABOVE} = ignore); got {v}"
            )
        return v

    def active_windows(self) -> tuple[str, ...]:
        return tuple(
            w for w in ("m12", "m9", "m6", "m3", "m1") if getattr(self, w) <= CIRCUITS_IGNORE_ABOVE
        )


class RangeFilter(_Model):
    """An inclusive ``[from, to]`` band; both ``None`` means ignore (docs/06 step 4)."""

    from_: Decimal | None = Field(default=None, alias="from")
    to: Decimal | None = None

    @model_validator(mode="after")
    def _ordered(self) -> RangeFilter:
        if self.from_ is not None and self.to is not None and self.from_ > self.to:
            raise ValueError(f"range is inverted: from={self.from_} > to={self.to}")
        return self

    def is_active(self) -> bool:
        return self.from_ is not None or self.to is not None


class PeFilter(RangeFilter):
    """P/E band behind its own switch. Undefined P/E is excluded when on (docs/01 §2.8)."""

    enabled: bool = False

    def is_active(self) -> bool:
        return self.enabled and super().is_active()


class TopRiskFilter(_Model):
    """ "Ignore Top Beta / Top Volatility" — a precomputed per-universe flag (docs/06 step 4)."""

    enabled: bool = False
    count: int = 0

    @field_validator("count")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"count cannot be negative; got {v}")
        return v

    def is_active(self) -> bool:
        return self.enabled


class ExtraFactor(_Model):
    """Factor two / factor three of the combined ranking (docs/01 §2.12)."""

    enabled: bool = False
    sort_by: FactorKey | None = None
    sort_direction: SortDirection = "desc"

    @field_validator("sort_by")
    @classmethod
    def _in_registry(cls, v: str | None) -> str | None:
        return None if v is None else _known_factor(v, "sort_by")

    @model_validator(mode="after")
    def _needs_a_factor_when_enabled(self) -> ExtraFactor:
        if self.enabled and self.sort_by is None:
            raise ValueError("sort_by is required when the extra factor is enabled")
        return self

    def is_active(self) -> bool:
        return self.enabled and self.sort_by is not None


class CustomFilter(_Model):
    """A field-to-field comparison, e.g. ``ma_50 >= ma_200`` (docs/01 §2.14)."""

    enabled: bool = True
    left: FactorKey
    op: CustomFilterOp
    right: FactorKey

    @field_validator("left", "right")
    @classmethod
    def _in_operand_list(cls, v: str, info: ValidationInfo) -> str:
        return _known_operand(v, info.field_name or "operand")

    def is_active(self) -> bool:
        return self.enabled


class RankingTerm(_Model):
    """One explicit ranking term (docs/ranking/PLAN.md C3).

    ``preference`` says how a raw value becomes a score in ``[0, 1]`` (C4 step 3): ``higher`` and
    ``lower`` are percentiles, ``target_range`` scores distance from ``[target_min, target_max]``.
    ``weight`` is only meaningful in ``composite`` mode, so any other mode refuses a non-default
    weight rather than silently ignoring it.
    """

    factor: FactorKey
    preference: TermPreference
    weight: Annotated[float, Field(gt=0, le=MAX_TERM_WEIGHT, allow_inf_nan=False)] = 1.0
    target_min: FiniteFloat | None = None
    target_max: FiniteFloat | None = None

    @field_validator("factor")
    @classmethod
    def _in_registry(cls, v: str) -> str:
        return _rankable_factor(v, "ranking_terms.factor")

    @model_validator(mode="after")
    def _bounds_match_preference(self) -> RankingTerm:
        has_bound = self.target_min is not None or self.target_max is not None
        if self.preference == "target_range":
            if not has_bound:
                raise ValueError(
                    f"ranking_terms[{self.factor}]: target_range needs target_min, target_max "
                    "or both"
                )
            if (
                self.target_min is not None
                and self.target_max is not None
                and self.target_min > self.target_max
            ):
                raise ValueError(
                    f"ranking_terms[{self.factor}]: target range is inverted: "
                    f"target_min={self.target_min} > target_max={self.target_max}"
                )
        elif has_bound:
            raise ValueError(
                f"ranking_terms[{self.factor}]: target_min/target_max only apply to "
                f"preference='target_range', not {self.preference!r}"
            )
        return self


class FactorRange(_Model):
    """An inclusive ``[min, max]`` eligibility filter on one factor (docs/ranking/PLAN.md C3).

    This is where a non-rankable factor belongs: ``excess_ret_12m >= 0`` is a meaningful gate even
    though a common-index subtraction can never reorder a list (PLAN correction 1). NULL never
    satisfies a range (docs/06 step 4).
    """

    enabled: bool = True
    factor: FactorKey
    min: FiniteFloat | None = None
    max: FiniteFloat | None = None

    @field_validator("factor")
    @classmethod
    def _sql_factor(cls, v: str) -> str:
        key = _known_factor(v, "factor_ranges.factor")
        if FACTORS[key].is_computed:
            raise ValueError(
                f"factor_ranges.factor: {key!r} is computed in-process, not stored, so it cannot "
                "be a SQL eligibility filter"
            )
        return key

    @model_validator(mode="after")
    def _bounded_and_ordered(self) -> FactorRange:
        if self.min is None and self.max is None:
            raise ValueError(f"factor_ranges[{self.factor}]: set min, max or both")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(
                f"factor_ranges[{self.factor}]: range is inverted: min={self.min} > max={self.max}"
            )
        return self

    def is_active(self) -> bool:
        return self.enabled


class FamilyWeights(_Model):
    """Relative weight per weight family for ``composite`` (docs/ranking/PLAN.md C3/C4).

    ``None`` means "no opinion": the ranking engine gives that family the mean of the explicit
    weights of the families present (or 1 when none is explicit), then normalises the shares of the
    families that actually have terms to sum to one. That makes the scale irrelevant — ``60/40`` and
    ``0.6/0.4`` mean the same — and three momentum terms cannot triple momentum's share.
    """

    momentum: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    path_quality: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    trend_structure: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    participation: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    risk_execution: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None

    def as_mapping(self) -> dict[str, float | None]:
        return {
            "momentum": self.momentum,
            "path_quality": self.path_quality,
            "trend_structure": self.trend_structure,
            "participation": self.participation,
            "risk_execution": self.risk_execution,
        }


class ScreenDefinition(_Model):
    """The persisted screen configuration.

    Two definitions that mean the same thing produce the same :meth:`canonical_json`, and
    therefore the same :meth:`definition_hash` — which is what makes the Redis cache key
    (docs/06 §Caching) and ``screen_run.definition_hash`` trustworthy.
    """

    index: UniverseSlug
    sort_by: FactorKey
    sort_direction: SortDirection = "desc"
    apply_filters_on: ApplyFiltersOn = "all"
    #: docs/ranking/PLAN.md — default ``composite`` preserves docs/01 §2.12 sum-of-ranks.
    ranking_mode: RankingModeName = "composite"
    #: docs/ranking/PLAN.md — default ``filtered_results`` preserves docs/06 today.
    ranking_scope: RankingScopeName = "filtered_results"

    min_return_1y: Decimal | None = None
    #: Minimum median daily traded value over 1 year, in rupees (docs/13 §2 finding 6).
    median_volume_1y: int | None = None

    moving_average: MovingAverageFilter = Field(default_factory=MovingAverageFilter)
    away_from_high: AwayFromHighFilter = Field(default_factory=AwayFromHighFilter)
    positive_days: PositiveDaysFilter = Field(default_factory=PositiveDaysFilter)
    circuits: CircuitsFilter = Field(default_factory=CircuitsFilter)
    marketcap: RangeFilter = Field(default_factory=RangeFilter)
    pe: PeFilter = Field(default_factory=PeFilter)
    series: list[str] = Field(default_factory=lambda: ["EQ"])
    ignore_top_beta: TopRiskFilter = Field(default_factory=TopRiskFilter)
    ignore_top_volatility: TopRiskFilter = Field(default_factory=TopRiskFilter)
    ignore_above_beta: int = IGNORE_ABOVE_BETA_IGNORE
    price: RangeFilter = Field(default_factory=RangeFilter)
    factor_two: ExtraFactor = Field(default_factory=ExtraFactor)
    factor_three: ExtraFactor = Field(default_factory=ExtraFactor)
    historical_date: dt.date | None = None
    custom_filters: list[CustomFilter] = Field(default_factory=list)

    # --- Phase 2 (docs/ranking/PLAN.md C3). Each is omitted from canonical_json at its default,
    # so every definition saved before these existed keeps its hash. -------------------------
    #: Non-empty switches the screen from the legacy sort_by/factor_two/factor_three path to the
    #: ranking engine (C4). ``ranking_terms[0].factor`` must equal ``sort_by``.
    ranking_terms: list[RankingTerm] = Field(default_factory=list)
    #: Composite only. ``None`` = equal share across the families the terms use.
    family_weights: FamilyWeights | None = None
    #: What a missing raw value scores: 0.0, 0.5, or the row is dropped (C4 step 3).
    missing_data: MissingDataPolicy = "penalize"
    #: Eligibility filters on any stored factor, AND-combined with the docs/06 filters.
    factor_ranges: list[FactorRange] = Field(default_factory=list)
    #: Keep only rows whose ``factor_daily.regime`` is one of these. ``None`` = no regime filter.
    regime_in: list[RegimeLabel] | None = None

    @field_validator("sort_by")
    @classmethod
    def _sort_by_in_registry(cls, v: str) -> str:
        return _known_factor(v, "sort_by")

    @field_validator("series")
    @classmethod
    def _known_series(cls, v: list[str]) -> list[str]:
        unknown = [s for s in v if s not in SERIES_VALUES]
        if unknown:
            raise ValueError(f"unknown series {unknown}; expected any of {list(SERIES_VALUES)}")
        if len(set(v)) != len(v):
            raise ValueError(f"series contains duplicates: {v}")
        return v

    @field_validator("ignore_above_beta")
    @classmethod
    def _beta_ceiling(cls, v: int) -> int:
        if v < 0:
            raise ValueError(
                "ignore_above_beta cannot be negative "
                f"({IGNORE_ABOVE_BETA_IGNORE} = ignore); got {v}"
            )
        return v

    @field_validator("ranking_terms")
    @classmethod
    def _term_limit(cls, v: list[RankingTerm]) -> list[RankingTerm]:
        if len(v) > MAX_RANKING_TERMS:
            raise ValueError(
                f"at most {MAX_RANKING_TERMS} ranking terms are supported; got {len(v)}"
            )
        factors = [term.factor for term in v]
        duplicated = sorted({f for f in factors if factors.count(f) > 1})
        if duplicated:
            raise ValueError(
                f"ranking_terms name {duplicated} more than once; use one term with a larger "
                "weight instead"
            )
        return v

    @field_validator("factor_ranges")
    @classmethod
    def _range_limit(cls, v: list[FactorRange]) -> list[FactorRange]:
        if len(v) > MAX_FACTOR_RANGES:
            raise ValueError(
                f"at most {MAX_FACTOR_RANGES} factor ranges are supported; got {len(v)}"
            )
        return v

    @field_validator("regime_in")
    @classmethod
    def _regimes(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        if not v:
            raise ValueError(
                "regime_in cannot be empty (no row could match); use null for no filter"
            )
        if len(set(v)) != len(v):
            raise ValueError(f"regime_in contains duplicates: {v}")
        return v

    @field_validator("custom_filters")
    @classmethod
    def _slot_limit(cls, v: list[CustomFilter]) -> list[CustomFilter]:
        if len(v) > MAX_CUSTOM_FILTERS:
            raise ValueError(
                f"at most {MAX_CUSTOM_FILTERS} custom filters are supported; got {len(v)}"
            )
        return v

    @model_validator(mode="after")
    def _factor_three_requires_two(self) -> ScreenDefinition:
        """docs/01 §2.12: factor three is revealed by, and ranks after, factor two."""
        if self.factor_three.enabled and not self.factor_two.enabled:
            raise ValueError("factor_three cannot be enabled while factor_two is disabled")
        if self.ranking_mode == "single" and (
            self.factor_two.is_active() or self.factor_three.is_active()
        ):
            raise ValueError(
                "ranking_mode='single' cannot combine factor_two or factor_three; "
                "use sequential or composite"
            )
        # desk_score is the book's SCORE read from desk_score_daily (C2). Summing its row number
        # with another factor's would invent a second ranking story (PLAN Phase 1.2); a composite
        # of explicit ranking_terms is the supported way to blend it.
        if self.sort_by == "desk_score" and (
            self.factor_two.is_active() or self.factor_three.is_active()
        ):
            raise ValueError(
                "desk_score cannot combine with factor_two or factor_three; "
                "it is a single computed SCORE"
            )
        if self.ranking_terms:
            self._check_ranking_terms()
        else:
            if self.ranking_scope == "within_sector":
                raise ValueError(
                    "ranking_scope='within_sector' needs explicit ranking_terms; the legacy "
                    "sort_by/factor_two/factor_three path ranks in SQL without sectors"
                )
            if self.family_weights is not None:
                raise ValueError("family_weights needs ranking_terms in composite mode")
            if self.missing_data != "penalize":
                raise ValueError(
                    "missing_data applies to ranking_terms; the legacy path always sorts "
                    "missing values last"
                )
        return self

    def _check_ranking_terms(self) -> None:
        """C3's rules for a definition that ranks with explicit terms."""
        first = self.ranking_terms[0]
        if self.sort_by != first.factor:
            raise ValueError(
                f"sort_by must equal ranking_terms[0].factor ({first.factor!r}); "
                f"got {self.sort_by!r}"
            )
        if self.factor_two.is_active() or self.factor_three.is_active():
            raise ValueError("ranking_terms replace factor_two and factor_three; disable them")
        if self.ranking_mode == "single" and len(self.ranking_terms) != 1:
            raise ValueError(
                f"ranking_mode='single' ranks by exactly one term; got {len(self.ranking_terms)}"
            )
        if self.ranking_mode != "composite":
            weighted = [t.factor for t in self.ranking_terms if t.weight != 1.0]
            if weighted:
                raise ValueError(
                    f"term weights only apply to ranking_mode='composite'; {weighted} carry one "
                    f"under {self.ranking_mode!r}"
                )
            if self.family_weights is not None:
                raise ValueError("family_weights only apply to ranking_mode='composite'")
            if self.ranking_scope == "within_sector":
                raise ValueError(
                    "ranking_scope='within_sector' only changes a composite score; "
                    f"{self.ranking_mode!r} orders by raw values, so it would do nothing"
                )

    def ignore_above_beta_is_active(self) -> bool:
        return self.ignore_above_beta != IGNORE_ABOVE_BETA_IGNORE

    def ranking_factors(self) -> tuple[tuple[str, SortDirection], ...]:
        """Factor one, plus any enabled extras, in the order their ranks are summed (docs/06 §5)."""
        factors: list[tuple[str, SortDirection]] = [(self.sort_by, self.sort_direction)]
        for extra in (self.factor_two, self.factor_three):
            if extra.is_active() and extra.sort_by is not None:
                factors.append((extra.sort_by, extra.sort_direction))
        return tuple(factors)

    def canonical_json(self) -> str:
        """A byte-stable serialisation, suitable as a hash input.

        Deterministic by construction: JSON mode (dates and decimals become strings), aliases
        applied (``from_`` -> ``from``), defaults materialised so an omitted key and an explicit
        default hash alike, keys sorted recursively, and no insignificant whitespace. List order
        is preserved because ``custom_filters`` order is semantic.

        One deliberate exception to "defaults materialised": the Phase-2 fields
        (:data:`PHASE_2_DEFAULTS`) are dropped while they hold their default, so a definition
        written before they existed hashes exactly as it did (docs/ranking/PLAN.md C3, pinned by
        ``test_screen_definition.py::test_hash_stability_*``). An explicit default still hashes
        like an omitted one, because both are dropped.
        """
        payload = self.model_dump(mode="json", by_alias=True)
        for key, default in PHASE_2_DEFAULTS.items():
            if payload.get(key) == default:
                del payload[key]
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def definition_hash(self) -> str:
        """sha256 of :meth:`canonical_json` — ``screen_run.definition_hash`` and the cache key."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


#: The JSON value of each Phase-2 field at its default; :meth:`ScreenDefinition.canonical_json`
#: omits a field that equals it. Adding a field to ``ScreenDefinition`` later means adding it here,
#: or every saved screen's hash changes.
PHASE_2_DEFAULTS: Final[dict[str, object]] = {
    "ranking_terms": [],
    "family_weights": None,
    "missing_data": "penalize",
    "factor_ranges": [],
    "regime_in": None,
}
