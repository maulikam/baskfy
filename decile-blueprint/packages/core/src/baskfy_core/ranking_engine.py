"""The ranking engine — docs/ranking/PLAN.md contract C4. Pure pandas.

Input is one frame: the selected universe for ``as_of`` **after** the ``apply_filters_on`` bucket
and **before** any filter, carrying ``passes_filters`` and the per-clause ``fail__*`` flags, the
point-in-time sector, every term's raw value, the book's desk score columns and the NSE momentum
inputs (``baskfy_core.screener.build_ranking_frame_query`` builds exactly this). Output is the
ranked rows plus everything :func:`explain` needs to say why a row sits where it does.

The six steps, each a function below:

1. **Computed factors** — ``desk_score`` is the joined ``desk_score_daily.score`` (never re-scored
   here, C2); ``nse_momentum_score`` comes from :mod:`baskfy_core.nse_momentum`.
2. **Scope set** — ``fixed_universe``: every row; ``filtered_results``: the rows that will be
   results (pass the filters and are not dropped for data); ``within_sector``: every row, grouped
   by sector, NULL sector → ``"unclassified"``.
3. **Transform** each term inside its scope set (per group for ``within_sector``) to ``[0, 1]``.
4. **Combine** — composite: family-weighted mean of term scores x 100; sequential / single: raw
   values in term order.
5. **Output** — rows that pass the filters and are not excluded, ranked ``1..n``.
6. **Explain** one row as a :class:`RankExplanation`.

Where this file reads C4 more precisely than its wording — each recorded in
``docs/DECISIONS-MERGE.md`` "Ranking 2.D":

* ``target_range``: a value inside the range scores exactly 1.0. C4's "1 - percentile of distance"
  with average ties would give in-range rows ``1 - (k-1)/2/(n-1)`` whenever *some* rows are
  outside, so a stock could drop from 1.0 to 0.5 without moving because a neighbour left the band;
  C4 itself special-cases the all-inside group to 1.0, which this generalises. Rows outside score
  ``1 - percentile`` of their distance among the group's distances.
* ``missing_data="exclude"`` removes the row from the *results*, so under ``filtered_results`` it
  is also outside the scope set; under ``fixed_universe`` and ``within_sector`` the universe is the
  universe and it still counts toward other terms' percentiles.
* A term on ``desk_score`` never ranks a row the book rejected (or has no score for), whatever the
  missing-data policy (C2: "Rejected rows are never ranked").
* In ``sequential``/``single``, missing raw values sort last under both ``penalize`` and
  ``neutral``: a raw-value order has no "middle" to put them in.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal

import numpy as np
import pandas as pd

from baskfy_core import nse_momentum

RANKING_ENGINE_VERSION: Final = "ranking-2.0.0"

ModeName = Literal["single", "sequential", "composite"]
ScopeName = Literal["filtered_results", "fixed_universe", "within_sector"]
PreferenceName = Literal["higher", "lower", "target_range"]
MissingPolicy = Literal["penalize", "neutral", "exclude"]

#: C1's weight families, in the order the UI lists them.
WEIGHT_FAMILIES: Final[tuple[str, ...]] = (
    "momentum",
    "path_quality",
    "trend_structure",
    "participation",
    "risk_execution",
)

# --- Frame column contract (shared with screener.build_ranking_frame_query) ------------------
INSTRUMENT_ID: Final = "instrument_id"
SYMBOL: Final = "symbol"
PASSES_FILTERS: Final = "passes_filters"
SECTOR: Final = "sector"
#: Prefix of one boolean per filter clause; ``True`` means the row fails that clause.
FAIL_PREFIX: Final = "fail__"
UNCLASSIFIED_SECTOR: Final = "unclassified"

DESK_SCORE_KEY: Final = "desk_score"
DESK_SCORE_RANK: Final = "desk_score_rank"
DESK_REJECT: Final = "desk_reject"
#: Desk component column → (display name, the component's maximum in ``score.score``).
DESK_COMPONENTS: Final[dict[str, tuple[str, float]]] = {
    "desk_a_trend": ("A trend", 25.0),
    "desk_b_momentum": ("B momentum", 25.0),
    "desk_c_sharpe": ("C Sharpe", 20.0),
    "desk_d_consistency": ("D consistency", 10.0),
    "desk_e_liquidity": ("E liquidity", 10.0),
}
DESK_F_PENALTY: Final = "desk_f_penalty"
#: ``desk_score_daily.ext_over_20dma``: ``(close / 20-DMA - 1) x 100``, the one raw input the book
#: stores beside its grades (A adds points for it, F deducts for it — ``score.score``).
DESK_EXT_OVER_20DMA: Final = "desk_ext_over_20dma"
#: ``desk_score_daily.score_version`` on the row itself.
DESK_ROW_SCORE_VERSION: Final = "desk_score_version"
#: F's floor in ``score.score`` (``np.maximum(f, -10)``).
DESK_F_FLOOR: Final = -10.0

NSE_MOMENTUM_KEY: Final = "nse_momentum_score"
NSE_MR6: Final = "nse_mr6"
NSE_MR12: Final = "nse_mr12"
IN_NIFTY_200: Final = "in_nifty_200"
IS_FNO: Final = "is_fno"
#: Optional constant columns: Z statistics over the whole eligible universe (see nse_momentum).
NSE_POPULATION_COLUMNS: Final[tuple[str, str, str, str]] = (
    "nse_mr6_mean",
    "nse_mr6_std",
    "nse_mr12_mean",
    "nse_mr12_std",
)

LAST_BAR_DATE: Final = "last_bar_date"
RECENT_CORPORATE_ACTION: Final = "recent_corporate_action"

# --- Output columns --------------------------------------------------------------------------
RANK: Final = "rank"
COMPOSITE_SCORE: Final = "composite_score"
TERM_SCORE_PREFIX: Final = "term_score__"
TERM_CONTRIB_PREFIX: Final = "term_contrib__"
TERM_DISTANCE_PREFIX: Final = "term_distance__"
IN_SCOPE: Final = "in_scope"
EXCLUDED: Final = "excluded"
EXCLUDED_REASON: Final = "excluded_reason"

#: C4 step 3: the score a missing raw value receives.
MISSING_SCORE: Final[dict[str, float]] = {"penalize": 0.0, "neutral": 0.5}

#: C4 step 6: positive / deduction thresholds on a transformed score, and on a desk component.
POSITIVE_AT: Final = 0.8
DEDUCTION_AT: Final = 0.2

_NULL_POLICY_INSUFFICIENT_HISTORY: Final = "insufficient_history"


# ---------------------------------------------------------------------------
# Specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TermSpec:
    """One ranking term, resolved against the registry (label, family, null policy)."""

    key: str
    label: str
    weight_family: str
    preference: PreferenceName
    weight: float = 1.0
    target_min: float | None = None
    target_max: float | None = None
    null_policy: str | None = None

    def __post_init__(self) -> None:
        if self.weight_family not in WEIGHT_FAMILIES:
            raise ValueError(f"{self.key}: unknown weight family {self.weight_family!r}")
        if not (math.isfinite(self.weight) and self.weight > 0):
            raise ValueError(f"{self.key}: weight must be a positive finite number")
        if self.preference == "target_range":
            if self.target_min is None and self.target_max is None:
                raise ValueError(f"{self.key}: target_range needs at least one bound")
            if (
                self.target_min is not None
                and self.target_max is not None
                and self.target_min > self.target_max
            ):
                raise ValueError(f"{self.key}: target range is inverted")


@dataclass(frozen=True, slots=True)
class RankingSpec:
    """Everything the engine needs from a ``ScreenDefinition``, registry already resolved."""

    terms: tuple[TermSpec, ...]
    mode: ModeName
    scope: ScopeName
    missing_data: MissingPolicy = "penalize"
    #: Family → weight, ``None`` = no opinion (see :func:`family_shares`). ``None`` overall = equal.
    family_weights: Mapping[str, float | None] | None = None

    def __post_init__(self) -> None:
        if not self.terms:
            raise ValueError("a ranking needs at least one term")
        keys = [t.key for t in self.terms]
        if len(set(keys)) != len(keys):
            raise ValueError(f"duplicate ranking terms: {keys}")
        if self.mode == "single" and len(self.terms) != 1:
            raise ValueError("single mode ranks by exactly one term")
        if self.family_weights is not None:
            unknown = sorted(set(self.family_weights) - set(WEIGHT_FAMILIES))
            if unknown:
                raise ValueError(f"unknown weight families: {unknown}")
            if any(
                w is not None and not (math.isfinite(w) and w >= 0)
                for w in self.family_weights.values()
            ):
                raise ValueError("family weights must be finite and >= 0")


def family_shares(spec: RankingSpec) -> dict[str, float]:
    """Each present family's share of the composite, summing to 1 (C4 step 4).

    A family with no explicit weight gets the mean of the explicit weights of the families present
    (1.0 when none is explicit), so the scale of the weights is irrelevant and an unset family is
    neither silenced nor dominant. Families with no term get no share at all.
    """
    present = list(dict.fromkeys(t.weight_family for t in spec.terms))
    given = spec.family_weights or {}
    explicit = [w for f in present if (w := given.get(f)) is not None]
    default = sum(explicit) / len(explicit) if explicit else 1.0
    raw = {f: (w if (w := given.get(f)) is not None else default) for f in present}
    total = sum(raw.values())
    if not total > 0:
        raise ValueError(
            "family_weights give every family used by the terms a zero share; nothing would rank"
        )
    return {f: w / total for f, w in raw.items()}


def effective_weights(spec: RankingSpec) -> dict[str, float]:
    """Term key → effective weight = family share x term weight / sum term weights in the family.

    Three correlated momentum terms therefore split momentum's share instead of tripling it.
    """
    shares = family_shares(spec)
    family_totals: dict[str, float] = {}
    for term in spec.terms:
        family_totals[term.weight_family] = family_totals.get(term.weight_family, 0.0) + term.weight
    return {
        t.key: shares[t.weight_family] * t.weight / family_totals[t.weight_family]
        for t in spec.terms
    }


# ---------------------------------------------------------------------------
# Step 3 — transforms
# ---------------------------------------------------------------------------


def percentile(values: pd.Series) -> pd.Series:
    """``(average-tie rank - 1) / (n - 1)`` ascending over the non-missing values; ``n = 1`` ⇒ 1.

    Missing values stay missing and do not count toward ``n``.
    """
    numeric = values.astype("float64")
    n = int(numeric.notna().sum())
    if n == 0:
        return pd.Series(np.nan, index=values.index, dtype="float64")
    if n == 1:
        return numeric.where(numeric.isna(), 1.0)
    ranks = numeric.rank(method="average", na_option="keep")
    return (ranks - 1.0) / (n - 1)


def target_distance(values: pd.Series, lower: float | None, upper: float | None) -> pd.Series:
    """0 inside ``[lower, upper]`` (either bound optional), else the gap to the nearest bound."""
    numeric = values.astype("float64")
    below = (lower - numeric).clip(lower=0.0) if lower is not None else 0.0
    above = (numeric - upper).clip(lower=0.0) if upper is not None else 0.0
    distance = pd.Series(below + above, index=values.index, dtype="float64")
    return distance.where(numeric.notna())


def target_score(distances: pd.Series) -> pd.Series:
    """Inside the range ⇒ 1.0; outside ⇒ ``1 - percentile`` of the distance among the group."""
    scored = 1.0 - percentile(distances)
    return scored.mask(distances == 0.0, 1.0)


def transform(values: pd.Series, term: TermSpec) -> pd.Series:
    """One term's raw values → scores in ``[0, 1]`` over exactly the rows passed in."""
    numeric = values.astype("float64")
    if term.preference == "higher":
        return percentile(numeric)
    if term.preference == "lower":
        return percentile(-numeric)
    return target_score(target_distance(numeric, term.target_min, term.target_max))


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RankingResult:
    """``ranked`` is the output (C4 step 5); ``scored`` is every input row, for explain."""

    ranked: pd.DataFrame
    scored: pd.DataFrame
    spec: RankingSpec
    #: Term key → effective weight; empty unless the mode is composite.
    weights: Mapping[str, float]


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    """A column as float64; ``None``/``Decimal`` are accepted, anything non-numeric raises."""
    return pd.to_numeric(frame[column].astype("object")).astype("float64")


def _require(frame: pd.DataFrame, columns: Sequence[str], why: str) -> None:
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise ValueError(f"ranking frame lacks {missing}, needed for {why}")


def add_nse_momentum_score(frame: pd.DataFrame) -> pd.DataFrame:
    """Step 1 for ``nse_momentum_score``: NSE's normalised score, NaN for ineligible rows.

    If the frame carries :data:`NSE_POPULATION_COLUMNS` (statistics over the whole eligible
    universe on the date), Z is taken against those; otherwise against the eligible rows present.
    """
    _require(frame, (NSE_MR6, NSE_MR12, IN_NIFTY_200, IS_FNO), NSE_MOMENTUM_KEY)
    population: nse_momentum.NsePopulation | None = None
    if all(c in frame.columns for c in NSE_POPULATION_COLUMNS) and len(frame):
        six_mean, six_std, twelve_mean, twelve_std = (
            _numeric(frame, c).iloc[0] for c in NSE_POPULATION_COLUMNS
        )
        if not any(pd.isna(v) for v in (six_mean, six_std, twelve_mean, twelve_std)):
            population = nse_momentum.NsePopulation(
                mr6=nse_momentum.HorizonStats(mean=float(six_mean), std=float(six_std)),
                mr12=nse_momentum.HorizonStats(mean=float(twelve_mean), std=float(twelve_std)),
            )
    out = frame.copy()
    out[NSE_MOMENTUM_KEY] = nse_momentum.scores(
        _numeric(frame, NSE_MR6),
        _numeric(frame, NSE_MR12),
        frame[IN_NIFTY_200].astype("object").fillna(False).astype(bool),
        frame[IS_FNO].astype("object").fillna(False).astype(bool),
        population=population,
    )
    return out


def _sort_key(values: pd.Series, term: TermSpec) -> pd.Series:
    """An ascending sort key that orders raw ``values`` best-first with missing values last."""
    numeric = values.astype("float64")
    if term.preference == "higher":
        key = -numeric
    elif term.preference == "lower":
        key = numeric
    else:
        key = target_distance(numeric, term.target_min, term.target_max)
    return key.fillna(np.inf)


def _round2(value: float) -> float:
    return float(Decimal(repr(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _exclusions(raw: Mapping[str, pd.Series], spec: RankingSpec, index: pd.Index) -> pd.Series:
    """Why a row is dropped from the results regardless of filters; ``""`` when it is not."""
    keys = [t.key for t in spec.terms]
    reasons = pd.Series("", index=index, dtype="object")
    if DESK_SCORE_KEY in raw:
        reasons = reasons.mask(raw[DESK_SCORE_KEY].isna(), "desk_score_ineligible")
    if spec.missing_data == "exclude":
        any_missing = pd.concat([raw[k].isna() for k in keys], axis=1).any(axis=1)
        reasons = reasons.mask((reasons == "") & any_missing, "missing_data")
    return reasons


def _groups(scored: pd.DataFrame, spec: RankingSpec) -> pd.Series:
    """Step 2's grouping: the sector (NULL → unclassified) for within_sector, else one group."""
    if spec.scope != "within_sector":
        return pd.Series("all", index=scored.index, dtype="object")
    if SECTOR not in scored.columns:
        return pd.Series(UNCLASSIFIED_SECTOR, index=scored.index, dtype="object")
    column = scored[SECTOR].astype("object")
    return column.where(column.notna(), UNCLASSIFIED_SECTOR)


def _transform_terms(
    scored: pd.DataFrame,
    raw: Mapping[str, pd.Series],
    spec: RankingSpec,
    in_scope: pd.Series,
    groups: pd.Series,
) -> None:
    """Step 3, writing ``term_score__*`` (and ``term_distance__*``) onto ``scored``."""
    members_by_group = [members.index for _, members in groups[in_scope].groupby(groups[in_scope])]
    for term in spec.terms:
        values = raw[term.key]
        transformed = pd.Series(np.nan, index=scored.index, dtype="float64")
        for members in members_by_group:
            transformed.loc[members] = transform(values.loc[members], term)
        if spec.missing_data in MISSING_SCORE:
            transformed = transformed.mask(
                in_scope & values.isna(), MISSING_SCORE[spec.missing_data]
            )
        scored[f"{TERM_SCORE_PREFIX}{term.key}"] = transformed
        if term.preference == "target_range":
            scored[f"{TERM_DISTANCE_PREFIX}{term.key}"] = target_distance(
                values, term.target_min, term.target_max
            )


def _combine(
    scored: pd.DataFrame, raw: Mapping[str, pd.Series], spec: RankingSpec
) -> tuple[dict[str, float], pd.DataFrame]:
    """Step 4: composite score and contributions, plus the ascending sort keys for the mode."""
    keys = pd.DataFrame(index=scored.index)
    if spec.mode != "composite":
        for position, term in enumerate(spec.terms):
            keys[f"k{position}"] = _sort_key(raw[term.key], term)
        return {}, keys
    weights = effective_weights(spec)
    total = pd.Series(0.0, index=scored.index, dtype="float64")
    for term in spec.terms:
        contribution = 100.0 * weights[term.key] * scored[f"{TERM_SCORE_PREFIX}{term.key}"]
        scored[f"{TERM_CONTRIB_PREFIX}{term.key}"] = contribution
        total = total + contribution
    scored[COMPOSITE_SCORE] = total.map(lambda v: np.nan if pd.isna(v) else _round2(v))
    first = spec.terms[0]
    keys["k0"] = (-scored[COMPOSITE_SCORE]).fillna(np.inf)
    keys["k1"] = _sort_key(raw[first.key], first)
    return weights, keys


def rank_frame(frame: pd.DataFrame, spec: RankingSpec) -> RankingResult:
    """Run C4 steps 1-5 over ``frame`` (one row per instrument)."""
    _require(frame, (INSTRUMENT_ID, PASSES_FILTERS), "ranking")
    if frame[INSTRUMENT_ID].duplicated().any():
        raise ValueError("ranking frame has duplicate instrument_id rows")
    keys = [t.key for t in spec.terms]

    # Step 1: computed factors (desk_score arrives joined; nse_momentum_score is computed here).
    scored = frame.copy().reset_index(drop=True)
    if NSE_MOMENTUM_KEY in keys and NSE_MOMENTUM_KEY not in scored.columns:
        scored = add_nse_momentum_score(scored)
    _require(scored, keys, "the ranking terms")
    raw = {key: _numeric(scored, key) for key in keys}
    passes = scored[PASSES_FILTERS].astype("object").fillna(False).astype(bool)

    reasons = _exclusions(raw, spec, scored.index)
    excluded = reasons != ""
    scored[EXCLUDED] = excluded
    scored[EXCLUDED_REASON] = reasons

    # Step 2: scope set. filtered_results ranks among the results; the other two, the universe.
    if spec.scope == "filtered_results":
        in_scope = passes & ~excluded
    else:
        in_scope = pd.Series(True, index=scored.index)
    scored[IN_SCOPE] = in_scope
    groups = _groups(scored, spec)
    if spec.scope == "within_sector":
        scored[SECTOR] = groups

    _transform_terms(scored, raw, spec, in_scope, groups)
    weights, sort_keys = _combine(scored, raw, spec)

    # Step 5: output rows, best first, ties broken by instrument_id.
    output = passes & ~excluded
    order = (
        sort_keys.loc[output]
        .assign(_instrument_id=scored.loc[output, INSTRUMENT_ID])
        .sort_values([*sort_keys.columns, "_instrument_id"], kind="mergesort")
        .index
    )
    scored[RANK] = pd.Series(pd.NA, index=scored.index, dtype="Int64")
    scored.loc[order, RANK] = range(1, len(order) + 1)
    ranked = scored.loc[order].reset_index(drop=True)
    ranked = ranked[[RANK, *[c for c in ranked.columns if c != RANK]]]
    return RankingResult(ranked=ranked, scored=scored, spec=spec, weights=weights)


# ---------------------------------------------------------------------------
# Step 6 — explain
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TermExplanation:
    factor: str
    label: str
    weight_family: str
    preference: str
    raw: float | None
    transformed: float | None
    effective_weight: float | None
    contribution: float | None
    missing: bool


@dataclass(frozen=True, slots=True)
class EligibilityFailure:
    filter: str
    detail: str


@dataclass(frozen=True, slots=True)
class Eligibility:
    passed: bool
    failures: tuple[EligibilityFailure, ...]


@dataclass(frozen=True, slots=True)
class DataQuality:
    missing_factors: tuple[str, ...]
    insufficient_history: bool
    stale_price: bool
    recent_corporate_action: bool


@dataclass(frozen=True, slots=True)
class DeskInput:
    """One stored raw value a desk grade was computed from."""

    name: str
    label: str
    value: float | None


@dataclass(frozen=True, slots=True)
class DeskComponent:
    """One A-F grade: its points, the range ``score.score`` clips it to, and its stored inputs.

    ``inputs`` holds only what ``desk_score_daily`` stores. The book keeps one raw input,
    ``ext_over_20dma``, which A and F both read; B-E's inputs (returns, Sharpe, positive days,
    median value) are not stored with the row, so their ``inputs`` are empty rather than
    re-derived from another table on another basis.
    """

    grade: str
    key: str
    label: str
    points: float | None
    min_points: float
    max_points: float
    inputs: tuple[DeskInput, ...]


@dataclass(frozen=True, slots=True)
class DeskBlock:
    score: float | None
    rank: int | None
    a_trend: float | None
    b_momentum: float | None
    c_sharpe: float | None
    d_consistency: float | None
    e_liquidity: float | None
    f_penalty: float | None
    reject: str
    eligible: bool
    ext_over_20dma: float | None
    score_version: str | None
    components: tuple[DeskComponent, ...]


@dataclass(frozen=True, slots=True)
class RankHistory:
    """Today's rank beside the previous session's rank for the same definition.

    ``previous_as_of`` is the trading day before ``as_of`` (``None`` when there is none);
    ``previous`` is the row's rank in that day's run, ``None`` when it was not ranked then.
    ``change`` is ``previous - today``: rank 1 is best, so a positive change is places gained.
    It is ``None`` unless both ranks exist.
    """

    today: int | None
    previous: int | None
    previous_as_of: str | None
    change: int | None


@dataclass(frozen=True, slots=True)
class Provenance:
    universe: str
    as_of: str
    data_version: int | None
    ranking_engine_version: str
    desk_score_version: str | None
    nse_momentum_version: str
    scope: str
    mode: str


@dataclass(frozen=True, slots=True)
class RankExplanation:
    """Why one row ranks where it does (C4 step 6). ``to_dict`` is JSON-able."""

    instrument_id: int
    symbol: str | None
    rank: int | None
    total: float | None
    terms: tuple[TermExplanation, ...]
    positives: tuple[str, ...]
    deductions: tuple[str, ...]
    eligibility: Eligibility
    data_quality: DataQuality
    desk: DeskBlock | None
    provenance: Provenance
    rank_history: RankHistory

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExplainContext:
    """The run-level facts an explanation cites."""

    result: RankingResult
    universe: str
    as_of: dt.date
    data_version: int | None = None
    desk_score_version: str | None = None
    #: ``fail__<clause>`` suffix → human-readable filter text from the query builder.
    clause_details: Mapping[str, str] = field(default_factory=dict)
    #: The trading day before ``as_of``, and the row's rank in the same definition's run on it —
    #: the caller ranks that day's point-in-time frame; core only reports it.
    previous_as_of: dt.date | None = None
    previous_rank: int | None = None


def _opt_float(value: object) -> float | None:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (int, float, np.integer, np.floating, Decimal)):
        number = float(value)
        return None if math.isnan(number) else number
    raise TypeError(f"expected a number, got {type(value).__name__}")


def _opt_int(value: object) -> int | None:
    number = _opt_float(value)
    return None if number is None else int(number)


def _is_null(value: object) -> bool:
    return value is None or value is pd.NA or (isinstance(value, float) and math.isnan(value))


def _flag(value: object) -> bool:
    """A ``fail__*`` cell: NULL counts as failed (docs/06: NULL never satisfies a predicate)."""
    return True if _is_null(value) else bool(value)


def _scope_phrase(context: ExplainContext, row: pd.Series) -> str:
    scope = context.result.spec.scope
    if scope == "filtered_results":
        return "the filtered results"
    if scope == "fixed_universe":
        return context.universe
    return f"sector {row.get(SECTOR, UNCLASSIFIED_SECTOR)}"


def _terms(row: pd.Series, context: ExplainContext) -> tuple[TermExplanation, ...]:
    result = context.result
    out: list[TermExplanation] = []
    for term in result.spec.terms:
        raw = _opt_float(row.get(term.key))
        in_scope = bool(row.get(IN_SCOPE, False))
        transformed = _opt_float(row.get(f"{TERM_SCORE_PREFIX}{term.key}")) if in_scope else None
        weight = result.weights.get(term.key)
        contribution = _opt_float(row.get(f"{TERM_CONTRIB_PREFIX}{term.key}")) if in_scope else None
        out.append(
            TermExplanation(
                factor=term.key,
                label=term.label,
                weight_family=term.weight_family,
                preference=term.preference,
                raw=raw,
                transformed=transformed,
                effective_weight=weight,
                contribution=contribution,
                missing=raw is None,
            )
        )
    return tuple(out)


def _term_notes(
    row: pd.Series, context: ExplainContext, terms: Sequence[TermExplanation]
) -> tuple[list[str], list[str]]:
    positives: list[str] = []
    deductions: list[str] = []
    where = _scope_phrase(context, row)
    specs = {t.key: t for t in context.result.spec.terms}
    policy = context.result.spec.missing_data
    for explained in terms:
        spec = specs[explained.factor]
        if explained.missing:
            deductions.append(f"Missing {spec.label} (missing_data={policy})")
            continue
        if spec.preference == "target_range":
            distance = _opt_float(row.get(f"{TERM_DISTANCE_PREFIX}{spec.key}"))
            if distance == 0.0:
                positives.append(f"Inside the target range on {spec.label}")
            elif distance is not None:
                deductions.append(f"Outside the target range on {spec.label} by {distance:.4g}")
            continue
        if explained.transformed is None:
            continue
        if explained.transformed >= POSITIVE_AT:
            positives.append(f"Top 20% on {spec.label} within {where}")
        elif explained.transformed <= DEDUCTION_AT:
            deductions.append(f"Bottom 20% on {spec.label} within {where}")
    return positives, deductions


#: Grade letter, frame column and display label, in A-F order.
_DESK_GRADES: Final[tuple[tuple[str, str, str], ...]] = (
    ("A", "desk_a_trend", "Trend"),
    ("B", "desk_b_momentum", "Momentum"),
    ("C", "desk_c_sharpe", "Sharpe"),
    ("D", "desk_d_consistency", "Consistency"),
    ("E", "desk_e_liquidity", "Liquidity"),
    ("F", DESK_F_PENALTY, "Penalty"),
)
#: The grades whose formula reads ``ext_over_20dma``.
_EXT_GRADES: Final = frozenset({"A", "F"})


def _desk_components(row: pd.Series) -> tuple[DeskComponent, ...]:
    extension = DeskInput(
        name="ext_over_20dma",
        label="Extension over the 20-day moving average, %",
        value=_opt_float(row.get(DESK_EXT_OVER_20DMA)),
    )
    out: list[DeskComponent] = []
    for grade, column, label in _DESK_GRADES:
        if column == DESK_F_PENALTY:
            low, high = DESK_F_FLOOR, 0.0
        else:
            low, high = 0.0, DESK_COMPONENTS[column][1]
        out.append(
            DeskComponent(
                grade=grade,
                key=column.removeprefix("desk_"),
                label=label,
                points=_opt_float(row.get(column)),
                min_points=low,
                max_points=high,
                inputs=(extension,) if grade in _EXT_GRADES else (),
            )
        )
    return tuple(out)


def rank_history(
    today: int | None, previous: int | None, previous_as_of: dt.date | None
) -> RankHistory:
    """``RankHistory`` from two ranks; ``change`` only when both exist."""
    change = None if today is None or previous is None else previous - today
    return RankHistory(
        today=today,
        previous=previous,
        previous_as_of=None if previous_as_of is None else previous_as_of.isoformat(),
        change=change,
    )


def _desk(row: pd.Series) -> DeskBlock | None:
    if DESK_REJECT not in row.index:
        return None
    reject_cell = row.get(DESK_REJECT)
    score = _opt_float(row.get(DESK_SCORE_KEY))
    if _is_null(reject_cell) and score is None:
        # No desk_score_daily row for this instrument on the date.
        return None
    reject = "" if not isinstance(reject_cell, str) else reject_cell
    return DeskBlock(
        score=score,
        rank=_opt_int(row.get(DESK_SCORE_RANK)),
        a_trend=_opt_float(row.get("desk_a_trend")),
        b_momentum=_opt_float(row.get("desk_b_momentum")),
        c_sharpe=_opt_float(row.get("desk_c_sharpe")),
        d_consistency=_opt_float(row.get("desk_d_consistency")),
        e_liquidity=_opt_float(row.get("desk_e_liquidity")),
        f_penalty=_opt_float(row.get(DESK_F_PENALTY)),
        reject=reject,
        eligible=reject == "" and score is not None,
        ext_over_20dma=_opt_float(row.get(DESK_EXT_OVER_20DMA)),
        score_version=(
            version if isinstance(version := row.get(DESK_ROW_SCORE_VERSION), str) else None
        ),
        components=_desk_components(row),
    )


def _desk_notes(row: pd.Series, desk: DeskBlock | None) -> tuple[list[str], list[str]]:
    if desk is None:
        return [], []
    positives: list[str] = []
    deductions: list[str] = []
    for column, (name, maximum) in DESK_COMPONENTS.items():
        value = _opt_float(row.get(column))
        if value is not None and value >= POSITIVE_AT * maximum:
            positives.append(f"Desk {name} {value:.1f} of {maximum:g}")
    if desk.f_penalty is not None and desk.f_penalty < 0:
        deductions.append(f"Desk F penalty {desk.f_penalty:+.1f}")
    for token in (t for t in desk.reject.split(";") if t):
        deductions.append(f"Desk reject: {token}")
    return positives, deductions


def _eligibility(row: pd.Series, context: ExplainContext) -> Eligibility:
    failures: list[EligibilityFailure] = []
    for column in row.index:
        if not isinstance(column, str) or not column.startswith(FAIL_PREFIX):
            continue
        if _flag(row[column]):
            clause = column.removeprefix(FAIL_PREFIX)
            failures.append(
                EligibilityFailure(filter=clause, detail=context.clause_details.get(clause, clause))
            )
    reason = row.get(EXCLUDED_REASON, "")
    if reason == "desk_score_ineligible":
        failures.append(
            EligibilityFailure(
                filter="desk_score", detail="the book rejected this name or has no score for it"
            )
        )
    elif reason == "missing_data":
        missing = [t.label for t in context.result.spec.terms if pd.isna(row.get(t.key))]
        failures.append(
            EligibilityFailure(
                filter="missing_data",
                detail=f"missing_data=exclude drops rows missing {', '.join(missing)}",
            )
        )
    # A NULL passes_filters is not a pass: only an explicit true passes.
    passes = not _is_null(row.get(PASSES_FILTERS)) and bool(row.get(PASSES_FILTERS))
    return Eligibility(passed=passes and not failures, failures=tuple(failures))


def _data_quality(
    row: pd.Series, context: ExplainContext, terms: Sequence[TermExplanation]
) -> DataQuality:
    specs = {t.key: t for t in context.result.spec.terms}
    missing = tuple(t.factor for t in terms if t.missing)
    insufficient = any(
        specs[key].null_policy == _NULL_POLICY_INSUFFICIENT_HISTORY for key in missing
    )
    stale = False
    if LAST_BAR_DATE in row.index:
        # No bar at all is the stalest price there is.
        last = row.get(LAST_BAR_DATE)
        stale = _is_null(last) or pd.Timestamp(str(last)).date() < context.as_of
    action = row.get(RECENT_CORPORATE_ACTION, False)
    return DataQuality(
        missing_factors=missing,
        insufficient_history=insufficient,
        stale_price=stale,
        recent_corporate_action=not _is_null(action) and bool(action),
    )


def explain(row: pd.Series, context: ExplainContext) -> RankExplanation:
    """C4 step 6 for one row of ``context.result.scored`` (ranked or not)."""
    spec = context.result.spec
    terms = _terms(row, context)
    positives, deductions = _term_notes(row, context, terms)
    desk = _desk(row)
    desk_positives, desk_deductions = _desk_notes(row, desk)
    symbol = row.get(SYMBOL)
    rank = _opt_int(row.get(RANK))
    return RankExplanation(
        instrument_id=int(row[INSTRUMENT_ID]),
        symbol=symbol if isinstance(symbol, str) else None,
        rank=rank,
        total=_opt_float(row.get(COMPOSITE_SCORE)) if spec.mode == "composite" else None,
        terms=terms,
        positives=tuple([*positives, *desk_positives]),
        deductions=tuple([*deductions, *desk_deductions]),
        eligibility=_eligibility(row, context),
        data_quality=_data_quality(row, context, terms),
        desk=desk,
        provenance=Provenance(
            universe=context.universe,
            as_of=context.as_of.isoformat(),
            data_version=context.data_version,
            ranking_engine_version=RANKING_ENGINE_VERSION,
            desk_score_version=context.desk_score_version,
            nse_momentum_version=nse_momentum.NSE_MOMENTUM_VERSION,
            scope=spec.scope,
            mode=spec.mode,
        ),
        rank_history=rank_history(rank, context.previous_rank, context.previous_as_of),
    )


def explain_instrument(instrument_id: int, context: ExplainContext) -> RankExplanation:
    """:func:`explain` for one ``instrument_id``; ``KeyError`` if it is not in the frame."""
    scored = context.result.scored
    matches = scored.loc[scored[INSTRUMENT_ID] == instrument_id]
    if matches.empty:
        raise KeyError(f"instrument {instrument_id} is not in the ranking frame")
    return explain(matches.iloc[0], context)
