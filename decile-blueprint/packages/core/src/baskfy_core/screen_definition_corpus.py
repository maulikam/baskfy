"""The shared accept/reject corpus for ScreenDefinition.

Both ``pytest`` (Pydantic) and ``vitest`` (Zod) run every case in this list and must reach the
same verdict. A structural schema comparison alone would not catch a validator that only one side
implements — the sentinel rules and the cross-field rules live in code, not in the type shape —
so the behavioural corpus is the part of Prompt 1 deliverable 6 that actually bites.

Cases are derived from the specification, not from observed behaviour:
docs/04 §Screens (the shape), docs/01 §2.4-§2.14 (the sentinels and slot limits),
docs/06 §"Step 4" (filter semantics).
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from typing import Final

from baskfy_core.models.base import JsonObject
from baskfy_core.screen_definition import ScreenDefinition


@dataclass(frozen=True, slots=True)
class Case:
    name: str
    #: Why the spec says this verdict — quoted in test failures.
    reason: str
    valid: bool
    value: JsonObject
    #: For valid cases, the fully-defaulted object Pydantic produces. The Zod side must
    #: materialise the identical object, which is a far sharper check than comparing the two
    #: JSON Schemas' `default` keywords (they render container defaults differently).
    #: Populated by :func:`render`; ``None`` for invalid cases.
    materialised: JsonObject | None = None


def _base(**overrides: object) -> JsonObject:
    payload: JsonObject = {"index": "nifty-500", "sort_by": "ret_12m"}
    payload.update(overrides)
    return payload


CASES: Final[tuple[Case, ...]] = (
    Case(
        name="minimal",
        reason="index and sort_by are the only required keys; everything else defaults to 'off'",
        valid=True,
        value=_base(),
    ),
    Case(
        name="docs-04-full-example",
        reason="docs/04 §Screens prints this exact object as the definition shape",
        valid=True,
        value={
            "index": "nifty-total-market",
            "sort_by": "avg_sharpe_12_6_3_1",
            "sort_direction": "desc",
            "apply_filters_on": "all",
            "ranking_mode": "composite",
            "ranking_scope": "filtered_results",
            "min_return_1y": None,
            "median_volume_1y": 10000000,
            "moving_average": {
                "enabled": False,
                "above_200": False,
                "above_100": False,
                "above_50": False,
                "above_20": False,
                "below_200": False,
                "below_100": False,
                "below_50": False,
                "below_20": False,
            },
            "away_from_high": {"ath": 100, "one_year": 100},
            "positive_days": {"m12": 0, "m9": 0, "m6": 0, "m3": 0, "m1": 0},
            "circuits": {"m12": 999, "m9": 999, "m6": 999, "m3": 999, "m1": 999},
            "marketcap": {"from": None, "to": None},
            "pe": {"enabled": False, "from": None, "to": None},
            "series": ["EQ"],
            "ignore_top_beta": {"enabled": False, "count": 0},
            "ignore_top_volatility": {"enabled": False, "count": 0},
            "ignore_above_beta": 100,
            "price": {"from": None, "to": None},
            "factor_two": {"enabled": False, "sort_by": None, "sort_direction": "desc"},
            "factor_three": {"enabled": False, "sort_by": None, "sort_direction": "desc"},
            "historical_date": None,
            "custom_filters": [{"enabled": True, "left": "ma_50", "op": ">=", "right": "ma_200"}],
        },
    ),
    Case(
        name="unknown-top-level-key",
        reason='docs/07: "Unknown keys are rejected (extra=\\"forbid\\")"',
        valid=False,
        value=_base(sort_bee="ret_12m"),
    ),
    Case(
        name="unknown-nested-key",
        reason="extra=forbid applies to every nested node, not just the root",
        valid=False,
        value=_base(away_from_high={"ath": 25, "one_year": 25, "ath_pct": 25}),
    ),
    Case(
        name="unknown-universe",
        reason="docs/01 §2.1 fixes the 14 selectable universes",
        valid=False,
        value=_base(index="nifty-4000"),
    ),
    Case(
        name="away-from-high-sentinel",
        reason="docs/01 §2.4: 100 = ignore, and is therefore the top of the valid range",
        valid=True,
        value=_base(away_from_high={"ath": 100, "one_year": 25}),
    ),
    Case(
        name="away-from-high-above-sentinel",
        reason="a value above the 100 sentinel has no meaning",
        valid=False,
        value=_base(away_from_high={"ath": 101}),
    ),
    Case(
        name="away-from-high-negative",
        reason='"within X% of high" cannot be negative',
        valid=False,
        value=_base(away_from_high={"one_year": -1}),
    ),
    Case(
        name="positive-days-sentinel",
        reason="docs/01 §2.5: 0 = ignore",
        valid=True,
        value=_base(positive_days={"m12": 0, "m6": 55}),
    ),
    Case(
        name="positive-days-above-100",
        reason="a percentage of trading days cannot exceed 100",
        valid=False,
        value=_base(positive_days={"m3": 101}),
    ),
    Case(
        name="circuits-ignore-threshold",
        reason="docs/01 §2.6: any cap above 250 cannot bind, so 999 is the idiomatic 'off'",
        valid=True,
        value=_base(circuits={"m12": 999, "m1": 3}),
    ),
    Case(
        name="circuits-negative",
        reason="a maximum count of circuit days cannot be negative",
        valid=False,
        value=_base(circuits={"m6": -1}),
    ),
    Case(
        name="ignore-above-beta-sentinel",
        reason="docs/01 §2.10: 100 = ignore",
        valid=True,
        value=_base(ignore_above_beta=100),
    ),
    Case(
        name="ignore-above-beta-negative",
        reason="a beta ceiling cannot be negative",
        valid=False,
        value=_base(ignore_above_beta=-1),
    ),
    Case(
        name="series-unknown",
        reason="docs/01 §2.9 as widened by M59: EQ/BE main board, SM/ST/SZ Emerge, nothing else",
        valid=False,
        # `GS` is a government security. Non-negotiable #7 blocks SGB/G-sec at the lowest layer,
        # so a screen that could even name one must not validate.
        value=_base(series=["EQ", "GS"]),
    ),
    Case(
        name="series-sme",
        reason="M59: an Emerge-only screen is expressible",
        valid=True,
        value=_base(series=["SM", "ST"]),
    ),
    Case(
        name="series-sme-and-main-board",
        reason="M59: SME and main board screen together under `nifty-allcap`",
        valid=True,
        value=_base(series=["EQ", "SM"]),
    ),
    Case(
        name="series-duplicated",
        reason="a duplicated series would double-count in the IN predicate",
        valid=False,
        value=_base(series=["EQ", "EQ"]),
    ),
    Case(
        name="four-custom-filters",
        reason="docs/01 §2.14: the screener exposes exactly three slots",
        valid=False,
        value=_base(
            custom_filters=[
                {"enabled": True, "left": "ma_50", "op": ">=", "right": "ma_200"},
                {"enabled": True, "left": "ma_20", "op": ">=", "right": "ma_50"},
                {"enabled": True, "left": "close", "op": ">=", "right": "ma_20"},
                {"enabled": True, "left": "vol_avg_1w", "op": ">=", "right": "vol_avg_12m"},
            ]
        ),
    ),
    Case(
        name="custom-filter-bad-operator",
        reason="docs/01 §2.14: operator ∈ >=, <=, =",
        valid=False,
        value=_base(
            custom_filters=[{"enabled": True, "left": "ma_50", "op": ">", "right": "ma_200"}]
        ),
    ),
    Case(
        name="factor-two-enabled-without-key",
        reason="docs/01 §2.12: an enabled factor must name the factor it ranks by",
        valid=False,
        value=_base(factor_two={"enabled": True, "sort_by": None, "sort_direction": "desc"}),
    ),
    Case(
        name="factor-three-without-factor-two",
        reason="docs/01 §2.12: factor three is revealed by, and ranks after, factor two",
        valid=False,
        value=_base(factor_three={"enabled": True, "sort_by": "vol_12m", "sort_direction": "asc"}),
    ),
    Case(
        name="per-factor-direction",
        reason='docs/06 §5: "a user can rank by highest 12-month Sharpe and lowest volatility"',
        valid=True,
        value=_base(
            sort_direction="desc",
            factor_two={"enabled": True, "sort_by": "vol_12m", "sort_direction": "asc"},
        ),
    ),
    Case(
        name="moving-average-above-and-below-same-window",
        reason="no close is both above and below its 200-day MA; the screen would always be empty",
        valid=False,
        value=_base(moving_average={"enabled": True, "above_200": True, "below_200": True}),
    ),
    Case(
        name="inverted-marketcap-range",
        reason="docs/01 §2.7: the range is inclusive [from, to]",
        valid=False,
        value=_base(marketcap={"from": 5000, "to": 100}),
    ),
    Case(
        name="apply-filters-on-decile",
        reason="docs/06 step 3 enumerates all | decile_1..5 | top_50 | top_100",
        valid=True,
        value=_base(apply_filters_on="decile_1"),
    ),
    Case(
        name="apply-filters-on-unknown-bucket",
        reason="decile_6 is not one of the enumerated buckets",
        valid=False,
        value=_base(apply_filters_on="decile_6"),
    ),
    Case(
        name="historical-date",
        reason="docs/01 §2.13: re-runs the screen as of a past date",
        valid=True,
        value=_base(historical_date="2025-04-15"),
    ),
    Case(
        name="historical-date-not-a-date",
        reason="historical_date is a date input",
        valid=False,
        value=_base(historical_date="not-a-date"),
    ),
    Case(
        name="sort-direction-unknown",
        reason="docs/01 §2.1: Highest to Lowest | Lowest to Highest",
        valid=False,
        value=_base(sort_direction="sideways"),
    ),
    # --- docs/ranking/PLAN.md C3 — explicit ranking terms, ranges, regimes ------------------
    Case(
        name="ranking-terms-composite",
        reason="C3: composite of weighted terms, family weights, a missing-data policy and a scope",
        valid=True,
        value=_base(
            ranking_mode="composite",
            ranking_scope="fixed_universe",
            ranking_terms=[
                {"factor": "ret_12m", "preference": "higher", "weight": 2},
                {"factor": "vol_12m", "preference": "lower"},
                {
                    "factor": "ma_dist_20",
                    "preference": "target_range",
                    "target_min": 0,
                    "target_max": 5.5,
                },
            ],
            family_weights={"momentum": 60, "risk_execution": 40},
            missing_data="neutral",
        ),
    ),
    Case(
        name="ranking-terms-target-range-one-bound",
        reason="C3: target_range needs at least one bound, not both",
        valid=True,
        value=_base(
            ranking_terms=[
                {"factor": "ret_12m", "preference": "target_range", "target_max": 80},
            ],
        ),
    ),
    Case(
        name="ranking-terms-sequential",
        reason="C3: sequential orders by each term's raw value; weights stay at their default",
        valid=True,
        value=_base(
            sort_by="desk_score",
            ranking_mode="sequential",
            ranking_terms=[
                {"factor": "desk_score", "preference": "higher"},
                {"factor": "ma_dist_20", "preference": "lower"},
                {"factor": "vol_12m", "preference": "lower"},
            ],
            missing_data="exclude",
        ),
    ),
    Case(
        name="ranking-terms-single",
        reason="C3: single ranks by exactly one term",
        valid=True,
        value=_base(
            ranking_mode="single",
            ranking_terms=[{"factor": "ret_12m", "preference": "higher"}],
        ),
    ),
    Case(
        name="ranking-terms-within-sector",
        reason="C3: within_sector is allowed once terms rank in composite",
        valid=True,
        value=_base(
            ranking_scope="within_sector",
            ranking_terms=[
                {"factor": "ret_12m", "preference": "higher"},
                {"factor": "pos_days_6m", "preference": "higher", "weight": 0.5},
            ],
        ),
    ),
    Case(
        name="factor-ranges-and-regime-on-legacy-path",
        reason="C3: factor_ranges and regime_in are eligibility filters, valid without terms",
        valid=True,
        value=_base(
            factor_ranges=[
                {"factor": "ret_6m", "min": 0},
                {"enabled": False, "factor": "vol_12m", "min": 0.1, "max": 0.6},
            ],
            regime_in=["BULL", "NEUTRAL"],
        ),
    ),
    Case(
        name="desk-score-fixed-universe",
        reason="C2: desk_score is read from desk_score_daily, so scope cannot change its order",
        valid=True,
        value=_base(sort_by="desk_score", ranking_scope="fixed_universe"),
    ),
    Case(
        name="ranking-terms-sort-by-mismatch",
        reason="C3: sort_by must equal ranking_terms[0].factor",
        valid=False,
        value=_base(ranking_terms=[{"factor": "vol_12m", "preference": "lower"}]),
    ),
    Case(
        name="ranking-terms-with-factor-two",
        reason="C3: explicit terms replace factor_two/factor_three",
        valid=False,
        value=_base(
            ranking_terms=[{"factor": "ret_12m", "preference": "higher"}],
            factor_two={"enabled": True, "sort_by": "vol_12m", "sort_direction": "asc"},
        ),
    ),
    Case(
        name="ranking-terms-single-with-two-terms",
        reason="C3: single ⇒ exactly one term",
        valid=False,
        value=_base(
            ranking_mode="single",
            ranking_terms=[
                {"factor": "ret_12m", "preference": "higher"},
                {"factor": "vol_12m", "preference": "lower"},
            ],
        ),
    ),
    Case(
        name="ranking-terms-duplicate-factor",
        reason="a factor named twice would double its weight silently",
        valid=False,
        value=_base(
            ranking_terms=[
                {"factor": "ret_12m", "preference": "higher"},
                {"factor": "ret_12m", "preference": "lower"},
            ],
        ),
    ),
    Case(
        name="ranking-terms-nine",
        reason="C3: at most eight terms",
        valid=False,
        value=_base(
            ranking_terms=[
                {"factor": key, "preference": "higher"}
                for key in (
                    "ret_12m",
                    "ret_6m",
                    "sharpe_12m",
                    "vol_12m",
                    "ma_dist_20",
                    "ma_stack_score",
                    "pos_days_6m",
                    "avg_sharpe_12_6_3_1",
                    "vol_expansion_1w_12m",
                )
            ],
        ),
    ),
    Case(
        name="ranking-terms-target-range-without-bounds",
        reason="C3: target_range needs ≥1 bound",
        valid=False,
        value=_base(ranking_terms=[{"factor": "ret_12m", "preference": "target_range"}]),
    ),
    Case(
        name="ranking-terms-target-range-inverted",
        reason="C3: target_min ≤ target_max",
        valid=False,
        value=_base(
            ranking_terms=[
                {
                    "factor": "ret_12m",
                    "preference": "target_range",
                    "target_min": 10,
                    "target_max": 5,
                }
            ],
        ),
    ),
    Case(
        name="ranking-terms-bounds-on-higher",
        reason="a bound on a monotonic preference would be silently ignored",
        valid=False,
        value=_base(
            ranking_terms=[{"factor": "ret_12m", "preference": "higher", "target_min": 1}],
        ),
    ),
    Case(
        name="ranking-terms-weight-zero",
        reason="C3: 0 < weight",
        valid=False,
        value=_base(ranking_terms=[{"factor": "ret_12m", "preference": "higher", "weight": 0}]),
    ),
    Case(
        name="ranking-terms-weight-above-100",
        reason="C3: weight ≤ 100",
        valid=False,
        value=_base(
            ranking_terms=[{"factor": "ret_12m", "preference": "higher", "weight": 100.5}],
        ),
    ),
    Case(
        name="ranking-terms-unknown-preference",
        reason="C3: preference ∈ higher | lower | target_range (eligibility is a filter)",
        valid=False,
        value=_base(ranking_terms=[{"factor": "ret_12m", "preference": "eligibility"}]),
    ),
    Case(
        name="ranking-terms-unknown-key",
        reason="extra=forbid applies to ranking terms too",
        valid=False,
        value=_base(ranking_terms=[{"factor": "ret_12m", "preference": "higher", "wieght": 2}]),
    ),
    Case(
        name="ranking-terms-weight-in-sequential",
        reason="C3: weights only meaningful in composite",
        valid=False,
        value=_base(
            ranking_mode="sequential",
            ranking_terms=[
                {"factor": "ret_12m", "preference": "higher", "weight": 3},
                {"factor": "vol_12m", "preference": "lower"},
            ],
        ),
    ),
    Case(
        name="family-weights-in-sequential",
        reason="C3: family_weights are composite only",
        valid=False,
        value=_base(
            ranking_mode="sequential",
            ranking_terms=[{"factor": "ret_12m", "preference": "higher"}],
            family_weights={"momentum": 1},
        ),
    ),
    Case(
        name="family-weights-negative",
        reason="C3: family weights are ≥ 0",
        valid=False,
        value=_base(
            ranking_terms=[{"factor": "ret_12m", "preference": "higher"}],
            family_weights={"momentum": -1},
        ),
    ),
    Case(
        name="family-weights-unknown-family",
        reason="C3 names exactly five weight families",
        valid=False,
        value=_base(
            ranking_terms=[{"factor": "ret_12m", "preference": "higher"}],
            family_weights={"value": 1},
        ),
    ),
    Case(
        name="family-weights-without-terms",
        reason="family weights mean nothing to the legacy sum-of-ranks path",
        valid=False,
        value=_base(family_weights={"momentum": 1}),
    ),
    Case(
        name="missing-data-without-terms",
        reason="the legacy path always sorts NULLs last; a policy there would be ignored",
        valid=False,
        value=_base(missing_data="neutral"),
    ),
    Case(
        name="missing-data-unknown",
        reason="C3: penalize | neutral | exclude",
        valid=False,
        value=_base(
            ranking_terms=[{"factor": "ret_12m", "preference": "higher"}],
            missing_data="impute",
        ),
    ),
    Case(
        name="within-sector-without-terms",
        reason="the legacy SQL path has no sector; within_sector needs the ranking engine",
        valid=False,
        value=_base(ranking_scope="within_sector"),
    ),
    Case(
        name="within-sector-sequential",
        reason="sequential orders by raw values, so a sector scope would change nothing",
        valid=False,
        value=_base(
            ranking_mode="sequential",
            ranking_scope="within_sector",
            ranking_terms=[{"factor": "ret_12m", "preference": "higher"}],
        ),
    ),
    Case(
        name="factor-range-without-bounds",
        reason="C3: a range needs min, max or both",
        valid=False,
        value=_base(factor_ranges=[{"factor": "ret_6m"}]),
    ),
    Case(
        name="factor-range-inverted",
        reason="C3: min ≤ max",
        valid=False,
        value=_base(factor_ranges=[{"factor": "ret_6m", "min": 5, "max": 1}]),
    ),
    Case(
        name="factor-ranges-eleven",
        reason="C3: at most ten factor ranges",
        valid=False,
        value=_base(factor_ranges=[{"factor": "ret_6m", "min": i} for i in range(11)]),
    ),
    Case(
        name="regime-in-empty",
        reason="an empty regime list matches no row; null is the 'off' value",
        valid=False,
        value=_base(regime_in=[]),
    ),
    Case(
        name="regime-in-unknown-label",
        reason="C1: regimes are BULL | NEUTRAL | BEAR",
        valid=False,
        value=_base(regime_in=["BULL", "SIDEWAYS"]),
    ),
    Case(
        name="regime-in-duplicated",
        reason="a duplicated regime would double-count in the IN predicate",
        valid=False,
        value=_base(regime_in=["BULL", "BULL"]),
    ),
    Case(
        name="desk-score-with-factor-two",
        reason="PLAN Phase 1.2: desk_score never sums row numbers with another factor",
        valid=False,
        value=_base(
            sort_by="desk_score",
            factor_two={"enabled": True, "sort_by": "ret_12m", "sort_direction": "desc"},
        ),
    ),
    Case(
        name="single-with-factor-two",
        reason="ranking_mode='single' cannot combine an active factor_two",
        valid=False,
        value=_base(
            ranking_mode="single",
            factor_two={"enabled": True, "sort_by": "vol_12m", "sort_direction": "asc"},
        ),
    ),
)


def render() -> str:
    """The corpus as JSON, for the TypeScript side to consume.

    Valid cases are annotated with the object Pydantic materialises from them.

    One known asymmetry, recorded rather than hidden: Pydantic serialises ``Decimal`` to a JSON
    *string* in JSON mode, because that is the only lossless rendering and ``canonical_json`` is a
    hash input. A JavaScript client sends the same value as a number. No case below carries a
    non-null decimal, so the comparison is exact today; when Prompt 7 adds one, the TS side must
    stringify decimal-typed fields before comparing.
    """
    payload = []
    for case in CASES:
        record = asdict(case)
        if case.valid:
            model = ScreenDefinition.model_validate(case.value)
            record["materialised"] = model.model_dump(mode="json", by_alias=True)
        payload.append(record)
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    sys.stdout.write(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
