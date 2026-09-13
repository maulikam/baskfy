"""The factor registry (Prompt 5 deliverable 4).

docs/06 §"The factor registry":

    "One Python dict (mirrored to TS via generated constants) with, per factor: `key`, `label`,
     `family`, `sql_expr`, `unit`, `higher_is_better`, `null_policy`. The registry is the single
     source of truth for: the `sort_by` dropdown, the custom-filter operand list, the column
     picker, the API enum, and the backtest signal list."

Prompt 5 repeats the requirement: "This single registry must drive the sort_by enum, the
custom-filter operand list, the column picker, and the backtest signal list."

Why ``sql_expr`` is a string, and why it is safe
------------------------------------------------
docs/06 §"Reference SQL skeleton": "`<factorN_expr>` is produced by a **whitelisted** factor
registry that maps a factor key to a SQL expression — never string-interpolated from user input."
A user supplies a *key*; the key is looked up here; the expression that comes back was written in
this file. A `sort_by` that is not in this registry is rejected before any SQL is built.

A NOTE ON THE COUNT — a real inconsistency in docs/01 §3
--------------------------------------------------------
docs/01 §3 is headed "The 62 ranking factors" and its families sum as:

    Absolute return (16) + Sharpe return (17) + RSI (16)
  + Risk-adjusted-by-beta (5) + Skip-month momentum (2)          = 56

leaving 6 for the last family. But that family, labelled "**Non-momentum sort keys (6)**",
enumerates **eight**: Volatility 1 year, Beta, Price to earnings, Marketcap, Close, Close raw,
Away from high all time, Away from high 1 year.

So 56 + 6 = 62 matches the headline, and 56 + 8 = 64 matches the enumeration. Both cannot be
right. This registry implements all **64 named keys**, because dropping two sort keys the document
explicitly names — and which docs/01 §4's column picker also lists — to make a headline number
come out would be losing real functionality to arithmetic. ``NAMED_FACTOR_COUNT`` records the
discrepancy so it is visible rather than folklore, and Prompt 19's parity audit should settle it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from baskfy_core.blends import (
    BETA_BLEND_SHAPES,
    BLEND_SHAPES,
    SHARPE_ONLY_SHAPES,
    blend_sql,
    shape_label,
    shape_suffix,
)
from baskfy_core.ranking import FactorPreference
from baskfy_core.windows import WINDOW_MONTHS


class FactorFamily(StrEnum):
    """docs/01 §3's own grouping, plus Phase-1 ranking families (docs/ranking/PLAN.md).

    docs/08 §"Sort By" wants the dropdown grouped by family.
    """

    ABSOLUTE_RETURN = "absolute_return"
    SHARPE_RETURN = "sharpe_return"
    RSI = "rsi"
    RISK_ADJUSTED = "risk_adjusted"
    SKIP_MONTH = "skip_month"
    NON_MOMENTUM = "non_momentum"
    PATH_QUALITY = "path_quality"
    TREND_STRUCTURE = "trend_structure"
    PARTICIPATION = "participation"
    #: Ranking Phase 2 (C1): momentum measured as something other than a raw window return —
    #: benchmark-relative, residual, persistence, acceleration and NSE's momentum ratios.
    MOMENTUM_QUALITY = "momentum_quality"


class WeightFamily(StrEnum):
    """docs/ranking/PLAN.md C1 — the five families a composite ranking weights.

    Distinct from :class:`FactorFamily`, which is the display grouping of the Sort By dropdown
    (docs/08) and stays as it was. Every factor carries exactly one of these.
    """

    MOMENTUM = "momentum"
    PATH_QUALITY = "path_quality"
    TREND_STRUCTURE = "trend_structure"
    PARTICIPATION = "participation"
    RISK_EXECUTION = "risk_execution"


class ValidationStatus(StrEnum):
    """docs/ranking/PLAN.md C1 — what evidence stands behind a factor as a ranking input.

    Pre-Phase-2 factors are ``legacy``; Phase-2 factors are ``research`` until leaf F's hold-out
    evidence (C8) moves them to ``validated`` or ``rejected``.
    """

    LEGACY = "legacy"
    RESEARCH = "research"
    VALIDATED = "validated"
    REJECTED = "rejected"


class FactorUnit(StrEnum):
    PERCENT = "percent"
    #: A decimal fraction rendered as a percentage — volatility (docs/13 §2 finding 4).
    FRACTION = "fraction"
    RATIO = "ratio"
    INDEX = "index"
    RUPEES = "rupees"
    CRORE = "crore"
    PRICE = "price"
    #: A plain integer count — the circuit-hit day columns (docs/01 §4).
    COUNT = "count"
    #: Not a number at all — ``series`` is "EQ" or "BE".
    TEXT = "text"


class NullPolicy(StrEnum):
    """What a NULL means for this factor, which decides how it sorts and filters.

    docs/06 §"Step 4": "NULLs never satisfy a predicate. A stock listed 3 months ago has
    `ret_12m = NULL` and is therefore excluded by any 1-year filter."
    """

    #: NULL because the instrument lacks a full window (docs/05 §Notation).
    INSUFFICIENT_HISTORY = "insufficient_history"
    #: NULL because a component was NULL (docs/05 §4).
    NULL_IF_ANY_COMPONENT_NULL = "null_if_any_component_null"
    #: NULL because the denominator was zero or non-positive (docs/05 §3, §7).
    NULL_ON_UNDEFINED_RATIO = "null_on_undefined_ratio"
    #: NULL because the upstream source does not publish it (docs/05 §14).
    NULL_IF_UNPUBLISHED = "null_if_unpublished"


@dataclass(frozen=True, slots=True)
class Factor:
    """One entry, carrying every field docs/06 §"The factor registry" lists."""

    key: str
    label: str
    family: FactorFamily
    sql_expr: str
    unit: FactorUnit
    higher_is_better: bool
    null_policy: NullPolicy
    #: The stored ``factor_daily`` columns this reads. Empty for a stored column itself.
    components: tuple[str, ...] = ()
    #: True when the factor is a stored column rather than an expression over stored columns.
    is_stored: bool = False
    #: True when ranking is computed in-process (e.g. desk SCORE), not by SQL ``ROW_NUMBER``.
    is_computed: bool = False
    #: How the ranking engine should treat this key (docs/ranking/PLAN.md).
    preference: FactorPreference = FactorPreference.HIGHER
    #: C1: which composite weight family this factor contributes to.
    weight_family: WeightFamily = field(kw_only=True)
    #: C1: the evidence behind it as a ranking input.
    validation_status: ValidationStatus = field(kw_only=True)
    #: C1: one sentence saying what the number is.
    definition: str = field(kw_only=True)
    #: C1: False for filter/column-only factors a ranking term must refuse (default True).
    rankable: bool = field(default=True, kw_only=True)

    @property
    def is_blend(self) -> bool:
        return len(self.components) > 1


#: docs/07 and docs/01 render the 12-month window as "1 YEAR", not "12 MONTHS".
_MONTHS_IN_A_YEAR: Final = 12


def _window_label(months: int) -> str:
    """docs/07's own wording: "AVERAGE SHARPE RETURN 12 6 3 1 MONTHS", "1 YEAR"."""
    if months == _MONTHS_IN_A_YEAR:
        return "1 YEAR"
    return f"{months} MONTHS" if months > 1 else "1 MONTH"


def _build() -> dict[str, Factor]:
    registry: dict[str, Factor] = {}

    def add(factor: Factor) -> None:
        if factor.key in registry:
            raise ValueError(f"duplicate factor key {factor.key!r}")
        registry[factor.key] = factor

    # --- Absolute return: 5 singles + 11 blends = 16 (docs/01 §3) --------------
    for months in WINDOW_MONTHS:
        column = f"ret_{months}m"
        add(
            Factor(
                key=column,
                label=f"ABSOLUTE RETURN {_window_label(months)}",
                family=FactorFamily.ABSOLUTE_RETURN,
                sql_expr=column,
                unit=FactorUnit.PERCENT,
                higher_is_better=True,
                null_policy=NullPolicy.INSUFFICIENT_HISTORY,
                is_stored=True,
                weight_family=WeightFamily.MOMENTUM,
                validation_status=ValidationStatus.LEGACY,
                definition=(
                    f"Percent change of the adjusted close over the {months}-month calendar "
                    "window (docs/05 §1)."
                ),
            )
        )
    for shape in BLEND_SHAPES:
        components = tuple(f"ret_{m}m" for m in shape)
        add(
            Factor(
                key=f"avg_return_{shape_suffix(shape)}",
                label=f"AVERAGE ABSOLUTE RETURN {shape_label(shape)} MONTHS",
                family=FactorFamily.ABSOLUTE_RETURN,
                sql_expr=blend_sql(list(components)),
                unit=FactorUnit.PERCENT,
                higher_is_better=True,
                null_policy=NullPolicy.NULL_IF_ANY_COMPONENT_NULL,
                components=components,
                weight_family=WeightFamily.MOMENTUM,
                validation_status=ValidationStatus.LEGACY,
                definition=(
                    f"Mean of the {shape_label(shape)}-month absolute returns, NULL if any is "
                    "NULL (docs/05 §4)."
                ),
            )
        )

    # --- Sharpe return: 5 singles + 11 blends + 6·3 = 17 ----------------------
    for months in WINDOW_MONTHS:
        column = f"sharpe_{months}m"
        add(
            Factor(
                key=column,
                label=f"SHARPE RETURN {_window_label(months)}",
                family=FactorFamily.SHARPE_RETURN,
                sql_expr=column,
                unit=FactorUnit.RATIO,
                higher_is_better=True,
                null_policy=NullPolicy.NULL_ON_UNDEFINED_RATIO,
                is_stored=True,
                weight_family=WeightFamily.MOMENTUM,
                validation_status=ValidationStatus.LEGACY,
                definition=(
                    f"The {months}-month return divided by 100 x its annualised volatility "
                    "(docs/05 §3)."
                ),
            )
        )
    for shape in (*BLEND_SHAPES, *SHARPE_ONLY_SHAPES):
        components = tuple(f"sharpe_{m}m" for m in shape)
        add(
            Factor(
                key=f"avg_sharpe_{shape_suffix(shape)}",
                label=f"AVERAGE SHARPE RETURN {shape_label(shape)} MONTHS",
                family=FactorFamily.SHARPE_RETURN,
                sql_expr=blend_sql(list(components)),
                unit=FactorUnit.RATIO,
                higher_is_better=True,
                null_policy=NullPolicy.NULL_IF_ANY_COMPONENT_NULL,
                components=components,
                weight_family=WeightFamily.MOMENTUM,
                validation_status=ValidationStatus.LEGACY,
                definition=(
                    f"Mean of the {shape_label(shape)}-month sharpe returns, NULL if any is "
                    "NULL (docs/05 §4)."
                ),
            )
        )

    # --- RSI: 5 singles + 11 blends = 16 --------------------------------------
    for months in WINDOW_MONTHS:
        column = f"rsi_{months}m"
        add(
            Factor(
                key=column,
                label=f"RSI {_window_label(months)}",
                family=FactorFamily.RSI,
                sql_expr=column,
                unit=FactorUnit.INDEX,
                higher_is_better=True,
                null_policy=NullPolicy.INSUFFICIENT_HISTORY,
                is_stored=True,
                weight_family=WeightFamily.MOMENTUM,
                validation_status=ValidationStatus.LEGACY,
                definition=(
                    f"Cutler's RSI with a period of the {months}-month window's daily changes "
                    "(docs/05 §5)."
                ),
            )
        )
    for shape in BLEND_SHAPES:
        components = tuple(f"rsi_{m}m" for m in shape)
        add(
            Factor(
                key=f"avg_rsi_{shape_suffix(shape)}",
                label=f"AVERAGE RSI {shape_label(shape)} MONTHS",
                family=FactorFamily.RSI,
                sql_expr=blend_sql(list(components)),
                unit=FactorUnit.INDEX,
                higher_is_better=True,
                null_policy=NullPolicy.NULL_IF_ANY_COMPONENT_NULL,
                components=components,
                weight_family=WeightFamily.MOMENTUM,
                validation_status=ValidationStatus.LEGACY,
                definition=(
                    f"Mean of the {shape_label(shape)}-month RSIs, NULL if any is NULL "
                    "(docs/05 §4)."
                ),
            )
        )

    # --- Risk-adjusted by beta: 5 (docs/05 §7) --------------------------------
    # "beta <= 0 -> NULL (the ratio is meaningless and would invert the ranking)."
    beta_guard = "CASE WHEN beta_12m > 0 THEN {expr} / beta_12m END"
    add(
        Factor(
            key="abs_div_beta_12m",
            label="ABSOLUTE RETURN 1 YEAR / BETA",
            family=FactorFamily.RISK_ADJUSTED,
            sql_expr=beta_guard.format(expr="ret_12m"),
            unit=FactorUnit.RATIO,
            higher_is_better=True,
            null_policy=NullPolicy.NULL_ON_UNDEFINED_RATIO,
            components=("ret_12m", "beta_12m"),
            weight_family=WeightFamily.MOMENTUM,
            validation_status=ValidationStatus.LEGACY,
            definition="The 1-year return divided by beta, NULL when beta <= 0 (docs/05 §7).",
        )
    )
    add(
        Factor(
            key="sharpe_div_beta_12m",
            label="SHARPE RETURN 1 YEAR / BETA",
            family=FactorFamily.RISK_ADJUSTED,
            sql_expr=beta_guard.format(expr="sharpe_12m"),
            unit=FactorUnit.RATIO,
            higher_is_better=True,
            null_policy=NullPolicy.NULL_ON_UNDEFINED_RATIO,
            components=("sharpe_12m", "beta_12m"),
            weight_family=WeightFamily.MOMENTUM,
            validation_status=ValidationStatus.LEGACY,
            definition=(
                "The 1-year sharpe return divided by beta, NULL when beta <= 0 (docs/05 §7)."
            ),
        )
    )
    for shape in BETA_BLEND_SHAPES:
        components = tuple(f"sharpe_{m}m" for m in shape)
        add(
            Factor(
                key=f"avg_sharpe_div_beta_{shape_suffix(shape)}",
                label=f"AVERAGE SHARPE RETURN {shape_label(shape)} MONTHS / BETA",
                family=FactorFamily.RISK_ADJUSTED,
                sql_expr=beta_guard.format(expr=blend_sql(list(components))),
                unit=FactorUnit.RATIO,
                higher_is_better=True,
                null_policy=NullPolicy.NULL_ON_UNDEFINED_RATIO,
                components=(*components, "beta_12m"),
                weight_family=WeightFamily.MOMENTUM,
                validation_status=ValidationStatus.LEGACY,
                definition=(
                    f"Mean of the {shape_label(shape)}-month sharpe returns divided by beta, "
                    "NULL when beta <= 0 (docs/05 §7)."
                ),
            )
        )

    # --- Skip-month momentum: 2 (docs/05 §8, INFERRED) ------------------------
    add(
        Factor(
            key="ret_12m_minus_1m",
            label="RETURN 12 MINUS 1 MONTHS",
            family=FactorFamily.SKIP_MONTH,
            sql_expr="ret_12m_minus_1m",
            unit=FactorUnit.PERCENT,
            higher_is_better=True,
            null_policy=NullPolicy.INSUFFICIENT_HISTORY,
            is_stored=True,
            weight_family=WeightFamily.MOMENTUM,
            validation_status=ValidationStatus.LEGACY,
            definition="Twelve-month return excluding the most recent month (docs/05 §8).",
        )
    )
    add(
        Factor(
            key="ret_12m_minus_2m",
            label="RETURN 12 MINUS TWO MONTHS",
            family=FactorFamily.SKIP_MONTH,
            sql_expr="ret_12m_minus_2m",
            unit=FactorUnit.PERCENT,
            higher_is_better=True,
            null_policy=NullPolicy.INSUFFICIENT_HISTORY,
            is_stored=True,
            weight_family=WeightFamily.MOMENTUM,
            validation_status=ValidationStatus.LEGACY,
            definition="Twelve-month return excluding the most recent two months (docs/05 §8).",
        )
    )

    # --- Non-momentum sort keys: docs/01 §3 names eight -----------------------
    non_momentum: tuple[tuple[str, str, str, FactorUnit, bool, NullPolicy], ...] = (
        (
            "vol_12m",
            "VOLATILITY 1 YEAR",
            "vol_12m",
            FactorUnit.FRACTION,
            # Lower volatility is the desirable end for a risk sort — the reference product lets
            # the user choose the direction, but the registry has to state a default.
            False,
            NullPolicy.INSUFFICIENT_HISTORY,
        ),
        ("beta_12m", "BETA", "beta_12m", FactorUnit.RATIO, False, NullPolicy.INSUFFICIENT_HISTORY),
        ("pe", "PRICE TO EARNINGS", "pe", FactorUnit.RATIO, False, NullPolicy.NULL_IF_UNPUBLISHED),
        (
            "marketcap_cr",
            "MARKETCAP",
            "marketcap_cr",
            FactorUnit.CRORE,
            True,
            NullPolicy.NULL_IF_UNPUBLISHED,
        ),
        ("close", "CLOSE", "close", FactorUnit.PRICE, True, NullPolicy.INSUFFICIENT_HISTORY),
        (
            "close_raw",
            "CLOSE RAW",
            "close_raw",
            FactorUnit.PRICE,
            True,
            NullPolicy.INSUFFICIENT_HISTORY,
        ),
        (
            "away_high_ath",
            "AWAY FROM HIGH ALL TIME",
            "away_high_ath",
            FactorUnit.PERCENT,
            True,
            NullPolicy.INSUFFICIENT_HISTORY,
        ),
        (
            "away_high_1y",
            "AWAY FROM HIGH 1 YEAR",
            "away_high_1y",
            FactorUnit.PERCENT,
            True,
            NullPolicy.INSUFFICIENT_HISTORY,
        ),
    )
    non_momentum_meaning: dict[str, tuple[WeightFamily, str]] = {
        "vol_12m": (
            WeightFamily.RISK_EXECUTION,
            "Annualised population standard deviation of the 1-year window's daily returns, "
            "as a fraction (docs/05 §2).",
        ),
        "beta_12m": (
            WeightFamily.RISK_EXECUTION,
            "Covariance of 252 daily returns with NIFTY 50 over its variance (docs/05 §6).",
        ),
        "pe": (
            WeightFamily.RISK_EXECUTION,
            "Price to earnings as NSE publishes it (docs/05 §14).",
        ),
        "marketcap_cr": (
            WeightFamily.RISK_EXECUTION,
            "Market capitalisation in ₹ crore (docs/05 §14).",
        ),
        "close": (
            WeightFamily.RISK_EXECUTION,
            "The corporate-action-adjusted closing price.",
        ),
        "close_raw": (
            WeightFamily.RISK_EXECUTION,
            "The exchange's own closing print, unadjusted.",
        ),
        "away_high_ath": (
            WeightFamily.TREND_STRUCTURE,
            "Percent distance of the close below the all-time intraday high (docs/05 §10).",
        ),
        "away_high_1y": (
            WeightFamily.TREND_STRUCTURE,
            "Percent distance of the close below the 252-bar intraday high (docs/05 §10).",
        ),
    }
    for key, label, expr, unit, higher, policy in non_momentum:
        weight_family, definition = non_momentum_meaning[key]
        add(
            Factor(
                key=key,
                label=label,
                family=FactorFamily.NON_MOMENTUM,
                sql_expr=expr,
                unit=unit,
                higher_is_better=higher,
                null_policy=policy,
                is_stored=True,
                preference=(FactorPreference.HIGHER if higher else FactorPreference.LOWER),
                weight_family=weight_family,
                validation_status=ValidationStatus.LEGACY,
                definition=definition,
            )
        )

    # --- Path / trend / participation (ranking engine Phase 1, 13 Sep 2026) ---
    # Already on factor_daily; previously filter-only. See docs/ranking/PLAN.md.
    for months in WINDOW_MONTHS:
        column = f"pos_days_{months}m"
        add(
            Factor(
                key=column,
                label=f"POSITIVE DAYS {_window_label(months)}",
                family=FactorFamily.PATH_QUALITY,
                sql_expr=column,
                unit=FactorUnit.PERCENT,
                higher_is_better=True,
                null_policy=NullPolicy.INSUFFICIENT_HISTORY,
                is_stored=True,
                preference=FactorPreference.HIGHER,
                weight_family=WeightFamily.PATH_QUALITY,
                validation_status=ValidationStatus.LEGACY,
                definition=(
                    f"Percent of the {months}-month window's daily returns that were positive "
                    "(docs/05 §11)."
                ),
            )
        )
    for months in WINDOW_MONTHS:
        if months == _MONTHS_IN_A_YEAR:
            continue  # vol_12m already registered under non_momentum
        column = f"vol_{months}m"
        add(
            Factor(
                key=column,
                label=f"VOLATILITY {_window_label(months)}",
                family=FactorFamily.NON_MOMENTUM,
                sql_expr=column,
                unit=FactorUnit.FRACTION,
                higher_is_better=False,
                null_policy=NullPolicy.INSUFFICIENT_HISTORY,
                is_stored=True,
                preference=FactorPreference.LOWER,
                weight_family=WeightFamily.RISK_EXECUTION,
                validation_status=ValidationStatus.LEGACY,
                definition=(
                    f"Annualised population standard deviation of the {months}-month window's "
                    "daily returns, as a fraction (docs/05 §2)."
                ),
            )
        )
    add(
        Factor(
            key="vol_expansion_1w_12m",
            label="VOLUME EXPANSION 1 WEEK / 1 YEAR",
            family=FactorFamily.PARTICIPATION,
            sql_expr=(
                "CASE WHEN vol_avg_12m > 0 THEN "
                "CAST(vol_avg_1w AS numeric) / CAST(vol_avg_12m AS numeric) END"
            ),
            unit=FactorUnit.RATIO,
            higher_is_better=True,
            null_policy=NullPolicy.NULL_ON_UNDEFINED_RATIO,
            components=("vol_avg_1w", "vol_avg_12m"),
            preference=FactorPreference.HIGHER,
            weight_family=WeightFamily.PARTICIPATION,
            validation_status=ValidationStatus.LEGACY,
            definition="Mean traded value of the last week over that of the last 252 sessions.",
        )
    )
    for window in (20, 50, 100, 200):
        add(
            Factor(
                key=f"ma_dist_{window}",
                label=f"DISTANCE FROM MA {window}",
                family=FactorFamily.TREND_STRUCTURE,
                sql_expr=(f"CASE WHEN ma_{window} > 0 THEN (close / ma_{window} - 1) * 100 END"),
                unit=FactorUnit.PERCENT,
                # Not "higher is better": overextension is a risk. Preference is target_range.
                higher_is_better=False,
                null_policy=NullPolicy.NULL_ON_UNDEFINED_RATIO,
                components=("close", f"ma_{window}"),
                preference=FactorPreference.TARGET_RANGE,
                weight_family=WeightFamily.TREND_STRUCTURE,
                validation_status=ValidationStatus.LEGACY,
                definition=f"Percent distance of the close above its {window}-session average.",
            )
        )
    add(
        Factor(
            key="ma_stack_score",
            label="MA STACK SCORE",
            family=FactorFamily.TREND_STRUCTURE,
            # Mirrors the desk's A_trend structure without the extension bonus (0-21). NULL, not 0,
            # when any average is missing: the null policy below says so, and the desk itself
            # rejects a name with no 50/200 DMA (score.py) rather than scoring it 0. Before
            # 13 Sep 2026 the ELSE 0 branches turned a name with no history into a ranked 0.
            sql_expr=(
                "CASE WHEN close IS NOT NULL AND ma_20 IS NOT NULL AND ma_50 IS NOT NULL "
                "AND ma_100 IS NOT NULL AND ma_200 IS NOT NULL THEN "
                "(CASE WHEN close > ma_20 THEN 4 ELSE 0 END)"
                " + (CASE WHEN close > ma_50 THEN 4 ELSE 0 END)"
                " + (CASE WHEN close > ma_100 THEN 4 ELSE 0 END)"
                " + (CASE WHEN close > ma_200 THEN 4 ELSE 0 END)"
                " + (CASE WHEN close > ma_20 AND ma_20 > ma_50 AND ma_50 > ma_100 "
                "AND ma_100 > ma_200 THEN 5 ELSE 0 END) END"
            ),
            unit=FactorUnit.RATIO,
            higher_is_better=True,
            null_policy=NullPolicy.INSUFFICIENT_HISTORY,
            components=("close", "ma_20", "ma_50", "ma_100", "ma_200"),
            preference=FactorPreference.HIGHER,
            weight_family=WeightFamily.TREND_STRUCTURE,
            validation_status=ValidationStatus.LEGACY,
            definition=(
                "0-21 points: 4 for each moving average the close is above, 5 more for a full "
                "20 > 50 > 100 > 200 stack."
            ),
        )
    )

    # --- Desk SCORE (ranking engine Phase 1.2) — computed via baskfy_core.score, not SQL ---
    add(
        Factor(
            key="desk_score",
            label="DESK MOMENTUM QUALITY SCORE",
            family=FactorFamily.NON_MOMENTUM,
            # Sentinel only: :func:`sql_for` refuses computed keys. The live path is
            # ``build_survivors_query`` + ``rerank_survivors_by_desk_score``.
            sql_expr="__computed_not_sql__",
            unit=FactorUnit.RATIO,
            higher_is_better=True,
            null_policy=NullPolicy.INSUFFICIENT_HISTORY,
            is_stored=False,
            is_computed=True,
            preference=FactorPreference.HIGHER,
            weight_family=WeightFamily.MOMENTUM,
            validation_status=ValidationStatus.LEGACY,
            definition=(
                "The momentum book's own 0-100 SCORE for the name, from desk_score_daily "
                "(docs/ranking/PLAN.md C2)."
            ),
        )
    )

    _add_phase_two_factors(add)

    return registry


#: SQL for ``regime_priority``. C1: "the Wasserstein labels get an order, never a distance".
REGIME_PRIORITY_SQL: Final = (
    "(CASE regime WHEN 'BULL' THEN 2 WHEN 'NEUTRAL' THEN 1 WHEN 'BEAR' THEN 0 END)"
)

#: docs/ranking/VALIDATION.md §5 — the C8 promotion rule's outcome per Phase-2 factor, applied by
#: ``research/ranking-validation/render_validation.py`` to ``out/ablation.csv``. Only ``validated``
#: and ``rejected`` are listed; every other Phase-2 key stays ``research`` — the non-rankable C1
#: columns (never modelled) and the two the run could not measure (``rs_persist_126``: NIFTY 500
#: levels absent; ``nse_momentum_score``: membership only from 2021-08-02). Key-by-key parity with
#: the document: ``test_factor_registry_c1.py::test_validation_status_matches_validation_md``.
C8_VALIDATION_STATUS: Final[dict[str, ValidationStatus]] = {
    "rank_persist_20": ValidationStatus.VALIDATED,
    **{
        key: ValidationStatus.REJECTED
        for key in (
            "atr_ext_20",
            "ma50_slope_20",
            "eff_ratio_63",
            "max_dd_6m",
            "max_dd_12m",
            "downside_vol_6m",
            "downside_vol_12m",
            "sortino_6m",
            "sortino_12m",
            "underwater_12m",
            "ret_ex_top3_12m",
            "accel_21_105",
            "accel_21_105_vs",
            "vol_exp_21_126",
            "vol_persist_20",
            "resid_ret_12m",
            "regime_priority",
        )
    },
}


def _c8_status(key: str) -> ValidationStatus:
    return C8_VALIDATION_STATUS.get(key, ValidationStatus.RESEARCH)


#: docs/ranking/PLAN.md C1 — every stored ranking factor, as
#: ``(key, label, display family, unit, preference, weight family, rankable, null policy,
#: definition)``. ``higher_is_better`` follows from the preference: False for ``lower`` and
#: ``target_range``, True otherwise.
_PHASE_TWO_STORED: Final[
    tuple[
        tuple[
            str,
            str,
            FactorFamily,
            FactorUnit,
            FactorPreference,
            WeightFamily,
            bool,
            NullPolicy,
            str,
        ],
        ...,
    ]
] = (
    (
        "atr_14",
        "ATR 14",
        FactorFamily.NON_MOMENTUM,
        FactorUnit.PRICE,
        FactorPreference.ELIGIBILITY,
        WeightFamily.RISK_EXECUTION,
        False,
        NullPolicy.INSUFFICIENT_HISTORY,
        "Wilder's 14-session average true range of the adjusted bars, seeded by a simple mean.",
    ),
    (
        "atr_ext_20",
        "ATR EXTENSION FROM MA 20",
        FactorFamily.TREND_STRUCTURE,
        FactorUnit.RATIO,
        FactorPreference.TARGET_RANGE,
        WeightFamily.TREND_STRUCTURE,
        True,
        NullPolicy.NULL_ON_UNDEFINED_RATIO,
        "How many 14-session ATRs the close sits above its 20-session moving average.",
    ),
    (
        "ma50_slope_20",
        "MA 50 SLOPE 20 SESSIONS",
        FactorFamily.TREND_STRUCTURE,
        FactorUnit.PERCENT,
        FactorPreference.HIGHER,
        WeightFamily.TREND_STRUCTURE,
        True,
        NullPolicy.INSUFFICIENT_HISTORY,
        "Percent change of the 50-session moving average over the last 20 sessions.",
    ),
    (
        "eff_ratio_63",
        "EFFICIENCY RATIO 63 SESSIONS",
        FactorFamily.PATH_QUALITY,
        FactorUnit.RATIO,
        FactorPreference.HIGHER,
        WeightFamily.PATH_QUALITY,
        True,
        NullPolicy.NULL_ON_UNDEFINED_RATIO,
        "Net 63-session price move over the sum of its 63 absolute daily moves (1 = straight).",
    ),
    (
        "max_dd_6m",
        "MAX DRAWDOWN 6 MONTHS",
        FactorFamily.PATH_QUALITY,
        FactorUnit.PERCENT,
        FactorPreference.LOWER,
        WeightFamily.PATH_QUALITY,
        True,
        NullPolicy.INSUFFICIENT_HISTORY,
        "Deepest percent fall of the close from its running high inside the 6-month window.",
    ),
    (
        "max_dd_12m",
        "MAX DRAWDOWN 1 YEAR",
        FactorFamily.PATH_QUALITY,
        FactorUnit.PERCENT,
        FactorPreference.LOWER,
        WeightFamily.PATH_QUALITY,
        True,
        NullPolicy.INSUFFICIENT_HISTORY,
        "Deepest percent fall of the close from its running high inside the 1-year window.",
    ),
    (
        "downside_vol_6m",
        "DOWNSIDE VOLATILITY 6 MONTHS",
        FactorFamily.NON_MOMENTUM,
        FactorUnit.FRACTION,
        FactorPreference.LOWER,
        WeightFamily.RISK_EXECUTION,
        True,
        NullPolicy.INSUFFICIENT_HISTORY,
        "Annualised root mean square of the 6-month window's negative daily returns, over N.",
    ),
    (
        "downside_vol_12m",
        "DOWNSIDE VOLATILITY 1 YEAR",
        FactorFamily.NON_MOMENTUM,
        FactorUnit.FRACTION,
        FactorPreference.LOWER,
        WeightFamily.RISK_EXECUTION,
        True,
        NullPolicy.INSUFFICIENT_HISTORY,
        "Annualised root mean square of the 1-year window's negative daily returns, over N.",
    ),
    (
        "sortino_6m",
        "SORTINO RETURN 6 MONTHS",
        FactorFamily.NON_MOMENTUM,
        FactorUnit.RATIO,
        FactorPreference.HIGHER,
        WeightFamily.RISK_EXECUTION,
        True,
        NullPolicy.NULL_ON_UNDEFINED_RATIO,
        "The 6-month return divided by 100 x its downside volatility.",
    ),
    (
        "sortino_12m",
        "SORTINO RETURN 1 YEAR",
        FactorFamily.NON_MOMENTUM,
        FactorUnit.RATIO,
        FactorPreference.HIGHER,
        WeightFamily.RISK_EXECUTION,
        True,
        NullPolicy.NULL_ON_UNDEFINED_RATIO,
        "The 1-year return divided by 100 x its downside volatility.",
    ),
    (
        "underwater_12m",
        "UNDERWATER DAYS 1 YEAR",
        FactorFamily.PATH_QUALITY,
        FactorUnit.PERCENT,
        FactorPreference.LOWER,
        WeightFamily.PATH_QUALITY,
        True,
        NullPolicy.INSUFFICIENT_HISTORY,
        "Percent of the 1-year window's sessions that closed below the window's running high.",
    ),
    (
        "ret_ex_top3_12m",
        "RETURN 1 YEAR EXCLUDING TOP 3 DAYS",
        FactorFamily.PATH_QUALITY,
        FactorUnit.PERCENT,
        FactorPreference.HIGHER,
        WeightFamily.PATH_QUALITY,
        True,
        NullPolicy.INSUFFICIENT_HISTORY,
        "The 1-year return with its three largest daily log returns removed.",
    ),
    (
        "accel_21_105",
        "MOMENTUM ACCELERATION 21 / 105",
        FactorFamily.MOMENTUM_QUALITY,
        FactorUnit.RATIO,
        FactorPreference.HIGHER,
        WeightFamily.MOMENTUM,
        True,
        NullPolicy.INSUFFICIENT_HISTORY,
        "Mean daily log return of the last 21 sessions minus that of the 105 sessions before.",
    ),
    (
        "accel_21_105_vs",
        "MOMENTUM ACCELERATION 21 / 105 VOL-SCALED",
        FactorFamily.MOMENTUM_QUALITY,
        FactorUnit.RATIO,
        FactorPreference.HIGHER,
        WeightFamily.MOMENTUM,
        True,
        NullPolicy.NULL_ON_UNDEFINED_RATIO,
        "Momentum acceleration 21/105 divided by the sample std of the same 126 log returns.",
    ),
    (
        "vol_exp_21_126",
        "VOLUME EXPANSION 21 / 126",
        FactorFamily.PARTICIPATION,
        FactorUnit.RATIO,
        FactorPreference.HIGHER,
        WeightFamily.PARTICIPATION,
        True,
        NullPolicy.NULL_ON_UNDEFINED_RATIO,
        "Mean traded value of the last 21 sessions over that of the 126 sessions before them.",
    ),
    (
        "vol_persist_20",
        "VOLUME PERSISTENCE 20 SESSIONS",
        FactorFamily.PARTICIPATION,
        FactorUnit.COUNT,
        FactorPreference.HIGHER,
        WeightFamily.PARTICIPATION,
        True,
        NullPolicy.INSUFFICIENT_HISTORY,
        "How many of the last 20 sessions traded above the mean of the 126 sessions before.",
    ),
    (
        "excess_ret_3m",
        "EXCESS RETURN 3 MONTHS VS NIFTY 500",
        FactorFamily.MOMENTUM_QUALITY,
        FactorUnit.PERCENT,
        FactorPreference.ELIGIBILITY,
        WeightFamily.MOMENTUM,
        False,
        NullPolicy.NULL_IF_ANY_COMPONENT_NULL,
        "The 3-month return minus NIFTY 500's over the same window; a filter, not a rank key.",
    ),
    (
        "excess_ret_6m",
        "EXCESS RETURN 6 MONTHS VS NIFTY 500",
        FactorFamily.MOMENTUM_QUALITY,
        FactorUnit.PERCENT,
        FactorPreference.ELIGIBILITY,
        WeightFamily.MOMENTUM,
        False,
        NullPolicy.NULL_IF_ANY_COMPONENT_NULL,
        "The 6-month return minus NIFTY 500's over the same window; a filter, not a rank key.",
    ),
    (
        "excess_ret_12m",
        "EXCESS RETURN 1 YEAR VS NIFTY 500",
        FactorFamily.MOMENTUM_QUALITY,
        FactorUnit.PERCENT,
        FactorPreference.ELIGIBILITY,
        WeightFamily.MOMENTUM,
        False,
        NullPolicy.NULL_IF_ANY_COMPONENT_NULL,
        "The 1-year return minus NIFTY 500's over the same window; a filter, not a rank key.",
    ),
    (
        "resid_ret_12m",
        "RESIDUAL RETURN 1 YEAR",
        FactorFamily.MOMENTUM_QUALITY,
        FactorUnit.PERCENT,
        FactorPreference.HIGHER,
        WeightFamily.MOMENTUM,
        True,
        NullPolicy.NULL_IF_ANY_COMPONENT_NULL,
        "The 1-year return minus beta times NIFTY 50's 1-year return.",
    ),
    (
        "rs_persist_126",
        "RELATIVE STRENGTH PERSISTENCE 126 SESSIONS",
        FactorFamily.MOMENTUM_QUALITY,
        FactorUnit.PERCENT,
        FactorPreference.HIGHER,
        WeightFamily.MOMENTUM,
        True,
        NullPolicy.INSUFFICIENT_HISTORY,
        "Percent of the last 126 sessions whose 20-session return beat NIFTY 500's.",
    ),
    (
        "mom_pctile",
        "MOMENTUM PERCENTILE",
        FactorFamily.MOMENTUM_QUALITY,
        FactorUnit.PERCENT,
        FactorPreference.ELIGIBILITY,
        WeightFamily.MOMENTUM,
        False,
        NullPolicy.INSUFFICIENT_HISTORY,
        "Percentile (0-100) of AVERAGE SHARPE RETURN 12 6 3 1 among the day's universe rows.",
    ),
    (
        "rank_persist_20",
        "RANK PERSISTENCE 20 DAYS",
        FactorFamily.MOMENTUM_QUALITY,
        FactorUnit.PERCENT,
        FactorPreference.HIGHER,
        WeightFamily.MOMENTUM,
        True,
        NullPolicy.INSUFFICIENT_HISTORY,
        "Percent of the last 20 dates on which the momentum percentile was at least 80.",
    ),
    (
        "nse_mr6",
        "NSE MOMENTUM RATIO 6 MONTHS",
        FactorFamily.MOMENTUM_QUALITY,
        FactorUnit.RATIO,
        FactorPreference.ELIGIBILITY,
        WeightFamily.MOMENTUM,
        False,
        NullPolicy.NULL_ON_UNDEFINED_RATIO,
        "NSE's 6-month momentum ratio: month-end price return over annualised 1-year sigma.",
    ),
    (
        "nse_mr12",
        "NSE MOMENTUM RATIO 12 MONTHS",
        FactorFamily.MOMENTUM_QUALITY,
        FactorUnit.RATIO,
        FactorPreference.ELIGIBILITY,
        WeightFamily.MOMENTUM,
        False,
        NullPolicy.NULL_ON_UNDEFINED_RATIO,
        "NSE's 12-month momentum ratio: month-end price return over annualised 1-year sigma.",
    ),
)


def _add_phase_two_factors(add: Callable[[Factor], None]) -> None:
    """docs/ranking/PLAN.md C1 — the stored ranking factors, ``regime_priority`` and the
    NSE momentum score. ``research`` until leaf F's evidence (docs/ranking/VALIDATION.md) moved
    them: see :data:`C8_VALIDATION_STATUS`."""
    for (
        key,
        label,
        family,
        unit,
        preference,
        weight,
        rankable,
        policy,
        meaning,
    ) in _PHASE_TWO_STORED:
        add(
            Factor(
                key=key,
                label=label,
                family=family,
                sql_expr=key,
                unit=unit,
                # As ``ma_dist_*`` already does: a target range is not "higher is better".
                higher_is_better=preference
                not in (FactorPreference.LOWER, FactorPreference.TARGET_RANGE),
                null_policy=policy,
                is_stored=True,
                preference=preference,
                weight_family=weight,
                validation_status=_c8_status(key),
                definition=meaning,
                rankable=rankable,
            )
        )
    add(
        Factor(
            key="regime_priority",
            label="REGIME PRIORITY",
            family=FactorFamily.TREND_STRUCTURE,
            sql_expr=REGIME_PRIORITY_SQL,
            unit=FactorUnit.INDEX,
            higher_is_better=True,
            null_policy=NullPolicy.INSUFFICIENT_HISTORY,
            components=("regime",),
            preference=FactorPreference.HIGHER,
            weight_family=WeightFamily.TREND_STRUCTURE,
            validation_status=_c8_status("regime_priority"),
            definition=(
                "The Wasserstein regime as an explicit order: BULL 2, NEUTRAL 1, BEAR 0 — an "
                "order, never a distance."
            ),
        )
    )
    add(
        Factor(
            key="nse_momentum_score",
            label="NIFTY200 MOMENTUM 30 SCORE (NSE METHODOLOGY)",
            family=FactorFamily.MOMENTUM_QUALITY,
            # Sentinel only: :func:`sql_for` refuses computed keys. The ranking engine (C4)
            # computes it over the NSE-eligible set from nse_mr6/nse_mr12.
            sql_expr="__computed_not_sql__",
            unit=FactorUnit.RATIO,
            higher_is_better=True,
            null_policy=NullPolicy.INSUFFICIENT_HISTORY,
            components=("nse_mr6", "nse_mr12"),
            is_computed=True,
            preference=FactorPreference.HIGHER,
            weight_family=WeightFamily.MOMENTUM,
            validation_status=_c8_status("nse_momentum_score"),
            definition=(
                "NSE's normalised momentum score: 1 + Z (or 1/(1 - Z)) of 0.5 Z12 + 0.5 Z6 over "
                "the Nifty 200 F&O-eligible set."
            ),
        )
    )


FACTORS: Final[dict[str, Factor]] = _build()

#: docs/01 §3's headline. Recorded next to the real count so the gap cannot be forgotten.
DOCUMENTED_FACTOR_COUNT: Final = 62
#: What docs/01 §3 actually enumerates once its last family is counted item by item.
NAMED_FACTOR_COUNT: Final = len(FACTORS)

#: docs/06: "The registry is the single source of truth for: the `sort_by` dropdown, ..."
SORT_FACTOR_KEYS: Final[tuple[str, ...]] = tuple(FACTORS)

#: docs/01 §2.14 — the custom-filter operand list. Field-to-field comparisons only make sense
#: between stored columns, so blends and guarded ratios are excluded.
#:
#: The order is docs/01 §2.14's own, longest window first: "Absolute return 1 year · Volatility
#: 1y/9m/6m/3m/1m · ... · Volume 1y avg · Volume 9m avg · ...". It reads as an ordered list because
#: it *is* one — the reference product's dropdown — and `apps/web` renders it in this order, which
#: `packages/core/tests/test_operand_parity.py` pins. Note that this is the reverse of
#: ``WINDOW_MONTHS``, which ascends because the factor families are built from short to long.
CUSTOM_FILTER_OPERANDS: Final[tuple[str, ...]] = (
    "ret_12m",
    *(f"vol_{m}m" for m in reversed(WINDOW_MONTHS)),
    "beta_12m",
    "close",
    "close_raw",
    "away_high_ath",
    "away_high_1y",
    *(f"ma_{k}" for k in (200, 100, 50, 20)),
    "vol_day_val",
    *(f"vol_avg_{k}m" for k in (12, 9, 6, 3, 1)),
    "vol_avg_1w",
)

#: docs/01 §4 — the column picker.
#:
#: A SECOND COUNT DISCREPANCY, of the same kind as the 62/64 one above. docs/01 §4 is headed
#: "Column picker (`/screens/:id/columns/edit`) — **34 available columns**" and then enumerates
#: thirty-six: Series, Marketcap, P/E, five absolute returns, five sharpe returns, five RSIs,
#: Beta, Volatility 1Y, High 1Y, High ATH, Away From High 1Y, Away From High ATH, four MAs,
#: Median Volume 1Y, Close, Close Raw, and five circuits. All thirty-six are offered here, for the
#: same reason as before: dropping two columns the document names, to make a headline number come
#: out, would lose real functionality to arithmetic.
DOCUMENTED_COLUMN_COUNT: Final = 34

#: Labels and units for the picker columns that are **not** ranking factors, so that
#: :func:`columns` can answer for all thirty-six from one place. docs/06 §"The factor registry"
#: makes the registry "the single source of truth for ... the column picker", and a column the
#: registry could not name would break that. Labels follow docs/01 §4's own wording, in the
#: upper-case style the factor labels already use.
_NON_FACTOR_COLUMNS: Final[dict[str, tuple[str, FactorUnit]]] = {
    "series": ("SERIES", FactorUnit.TEXT),
    "high_1y": ("HIGH ONE YEAR", FactorUnit.PRICE),
    "high_ath": ("HIGH ALL TIME", FactorUnit.PRICE),
    "ma_200": ("MA 200", FactorUnit.PRICE),
    "ma_100": ("MA 100", FactorUnit.PRICE),
    "ma_50": ("MA 50", FactorUnit.PRICE),
    "ma_20": ("MA 20", FactorUnit.PRICE),
    "median_vol_12m": ("MEDIAN VOLUME ONE YEAR", FactorUnit.RUPEES),
    **{
        f"circuits_{months}m": (f"CIRCUITS {_window_label(months)}", FactorUnit.COUNT)
        for months in WINDOW_MONTHS
    },
}

COLUMN_PICKER_KEYS: Final[tuple[str, ...]] = (
    "series",
    "marketcap_cr",
    "pe",
    *(f"ret_{m}m" for m in (12, 9, 6, 3, 1)),
    *(f"sharpe_{m}m" for m in (12, 9, 6, 3, 1)),
    *(f"rsi_{m}m" for m in (12, 9, 6, 3, 1)),
    "beta_12m",
    "vol_12m",
    "high_1y",
    "high_ath",
    "away_high_1y",
    "away_high_ath",
    "ma_200",
    "ma_100",
    "ma_50",
    "ma_20",
    "median_vol_12m",
    "close",
    "close_raw",
    *(f"circuits_{m}m" for m in (12, 9, 6, 3, 1)),
)

#: docs/10 — every registry key is a candidate backtest signal.
BACKTEST_SIGNAL_KEYS: Final[tuple[str, ...]] = SORT_FACTOR_KEYS


@dataclass(frozen=True, slots=True)
class ResultColumn:
    """One entry of docs/01 §4's column picker, as ``GET /meta/columns`` returns it."""

    key: str
    label: str
    unit: FactorUnit
    #: True when the column is also a ranking factor, so the UI can offer it in both places.
    is_factor: bool


def columns() -> tuple[ResultColumn, ...]:
    """Every selectable result column, in docs/01 §4's order.

    Most are ranking factors and take their label straight from the registry; the rest are
    display-only columns named in :data:`_NON_FACTOR_COLUMNS`.
    """
    resolved: list[ResultColumn] = []
    for key in COLUMN_PICKER_KEYS:
        factor = FACTORS.get(key)
        if factor is not None:
            resolved.append(ResultColumn(key, factor.label, factor.unit, is_factor=True))
            continue
        label, unit = _NON_FACTOR_COLUMNS[key]
        resolved.append(ResultColumn(key, label, unit, is_factor=False))
    return tuple(resolved)


def get(key: str) -> Factor:
    """Look a factor up. The whitelist docs/06 requires before any SQL is built."""
    try:
        return FACTORS[key]
    except KeyError as exc:
        raise KeyError(
            f"unknown factor {key!r}; it is not in the registry, so no SQL will be built for it"
        ) from exc


def sql_for(key: str) -> str:
    factor = get(key)
    if factor.is_computed:
        raise ValueError(
            f"{key!r} is a computed ranking factor and has no SQL expression; it is ranked "
            "in-process (desk_score from desk_score_daily, nse_momentum_score by the ranking "
            "engine), never by SQL ROW_NUMBER"
        )
    return factor.sql_expr


def by_family() -> dict[FactorFamily, tuple[Factor, ...]]:
    """docs/08 §"Sort By": "use a searchable combobox grouped by family"."""
    grouped: dict[FactorFamily, list[Factor]] = {family: [] for family in FactorFamily}
    for factor in FACTORS.values():
        grouped[factor.family].append(factor)
    return {family: tuple(items) for family, items in grouped.items()}


def family_counts() -> dict[str, int]:
    return {family.value: len(items) for family, items in by_family().items()}
