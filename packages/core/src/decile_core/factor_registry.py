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

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from decile_core.blends import (
    BETA_BLEND_SHAPES,
    BLEND_SHAPES,
    SHARPE_ONLY_SHAPES,
    blend_sql,
    shape_label,
    shape_suffix,
)
from decile_core.windows import WINDOW_MONTHS


class FactorFamily(StrEnum):
    """docs/01 §3's own grouping. docs/08 §"Sort By" wants the dropdown grouped by family."""

    ABSOLUTE_RETURN = "absolute_return"
    SHARPE_RETURN = "sharpe_return"
    RSI = "rsi"
    RISK_ADJUSTED = "risk_adjusted"
    SKIP_MONTH = "skip_month"
    NON_MOMENTUM = "non_momentum"


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
    for key, label, expr, unit, higher, policy in non_momentum:
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
            )
        )

    return registry


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
    return get(key).sql_expr


def by_family() -> dict[FactorFamily, tuple[Factor, ...]]:
    """docs/08 §"Sort By": "use a searchable combobox grouped by family"."""
    grouped: dict[FactorFamily, list[Factor]] = {family: [] for family in FactorFamily}
    for factor in FACTORS.values():
        grouped[factor.family].append(factor)
    return {family: tuple(items) for family, items in grouped.items()}


def family_counts() -> dict[str, int]:
    return {family.value: len(items) for family, items in by_family().items()}
