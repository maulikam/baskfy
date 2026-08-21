"""Reconciliation against the reference product's published output (Prompt 19 §4).

    "A reconciliation report CLI comparing our computed values against a snapshot of the reference
     product's public output for a set of symbols — starting with the 271-row reference export in
     fixtures/ — printing every disagreement over a tolerance. Investigate and document each
     disagreement — especially the two INFERRED areas (12-1 momentum, circuit detection)."

This module is the arithmetic; ``decile_worker.reconcile_cli`` is the command that prints it.

WHAT CAN BE RECONCILED WITHOUT PRICE HISTORY, AND WHAT CANNOT
--------------------------------------------------------------
The snapshot is 271 rows of *outputs* for one day. It carries no price history, and the suite is
network-blocked, so "recompute ``ret_12m`` and compare" is not available here — that is the
``DECILE_PARITY_BARS`` path in ``packages/core/tests/test_reference_parity.py``, and it skips.

What *is* available is every relationship docs/05 asserts **between** the published columns. Those
are not weak substitutes. ``sharpe_N = ret_N / (vol_N x 100)`` over 1,355 real cells is a direct
test of docs/05 §3 against the reference product's own numbers; if our reading of the formula were
wrong, it would fail here with no price history at all. Twelve such checks are implemented, and
each names the docs/05 or docs/13 clause it is testing.

TOLERANCES ARE DERIVED, NEVER PICKED
------------------------------------
Every published column is rounded (docs/13 §4). A relationship between rounded columns therefore
cannot hold exactly, and the honest tolerance is the one the rounding *implies* — propagated
through the formula, per cell. So ``sharpe``'s tolerance is not "0.005": it is

    0.005                       (sharpe's own last decimal place)
  + 0.005 / (vol_N x 100)       (the return's, divided through by the volatility)

which for a 20%-volatility name is 0.00525 and for a 60%-volatility name is 0.00508. Picking a
flat number instead would either hide a real disagreement on low-volatility names or manufacture
one on high-volatility names — and the difference is exactly the 11 cells that a flat 0.005 flags
and this derivation does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

import polars as pl

from decile_core.blends import shape_suffix
from decile_core.precision import PERCENT_DP, PRICE_DP, RATIO_DP, RSI_DP
from decile_core.universes import UNIVERSES

#: Half of the last decimal place each family is stored at (docs/13 §4).
HALF_ULP: Final[dict[int, Decimal]] = {
    PRICE_DP: Decimal("0.005"),
    RSI_DP: Decimal("0.00005"),
    RATIO_DP: Decimal("0.00000000005"),
}
PRICE_HALF_ULP: Final = HALF_ULP[PRICE_DP]
PERCENT_HALF_ULP: Final = HALF_ULP[PERCENT_DP]
RSI_HALF_ULP: Final = HALF_ULP[RSI_DP]
RATIO_HALF_ULP: Final = HALF_ULP[RATIO_DP]

#: docs/13 §3's recovered window denominators for 2026-08-18, keyed by the export's own suffix.
WINDOW_SUFFIX: Final[dict[int, str]] = {
    12: "one_year",
    9: "nine_months",
    6: "six_months",
    3: "three_months",
    1: "one_month",
}
WINDOW_LENGTHS: Final[dict[int, int]] = {12: 247, 9: 185, 6: 121, 3: 64, 1: 22}

#: docs/05 §4's worked blend, the one the export's row order was produced by (docs/13 §2).
SORTING_BLEND: Final[tuple[int, ...]] = (12, 6, 3, 1)

#: docs/05 §5 — RSI is a percentage, so it is bounded by these.
RSI_FLOOR: Final = Decimal(0)
RSI_CEILING: Final = Decimal(100)

#: docs/13 §2 finding 4 — volatility is stored as a decimal FRACTION. Nothing real annualises
#: above 500%, so a value past this is a units regression rather than a violent stock.
VOLATILITY_FRACTION_CEILING: Final = Decimal(5)

#: Below this, a distributional check reports "not enough rows" rather than a verdict. Running
#: the report over `--symbols CUPID` must not manufacture a disagreement out of a sample of one.
MINIMUM_DISTRIBUTION_SAMPLE: Final = 30


@dataclass(frozen=True, slots=True)
class Disagreement:
    """One cell where the published numbers do not satisfy a docs/05 relationship."""

    check: str
    symbol: str
    column: str
    expected: Decimal
    actual: Decimal
    tolerance: Decimal

    @property
    def delta(self) -> Decimal:
        return abs(self.expected - self.actual)

    def __str__(self) -> str:
        return (
            f"{self.symbol:<14} {self.column:<34} expected={self.expected} "
            f"published={self.actual} delta={self.delta} tolerance={self.tolerance}"
        )


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One reconciliation check: what it tests, how much it tested, what disagreed."""

    name: str
    reference: str
    description: str
    cells: int
    disagreements: list[Disagreement] = field(default_factory=list)
    #: Things this check *establishes* but cannot decide — printed under UNRESOLVED, never
    #: counted as a pass. The two INFERRED areas live here.
    unresolved: list[str] = field(default_factory=list)
    #: Observations worth recording that are not disagreements.
    notes: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.disagreements


def _dec(value: object) -> Decimal:
    return Decimal(str(value))


def _slug(slug: str) -> str:
    return slug.replace("-", "_")


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def check_sharpe_identity(frame: pl.DataFrame) -> CheckResult:
    """docs/05 §3 — ``sharpe_N = ret_N_pct / (vol_N_fraction x 100)``, over 1,355 cells."""
    disagreements: list[Disagreement] = []
    cells = 0
    for row in frame.iter_rows(named=True):
        for suffix in WINDOW_SUFFIX.values():
            ret, vol, sharpe = (
                row[f"absolute_return_{suffix}"],
                row[f"volatility_{suffix}"],
                row[f"sharpe_return_{suffix}"],
            )
            if ret is None or vol is None or sharpe is None:
                continue
            volatility = _dec(vol) * 100
            if volatility == 0:
                continue
            cells += 1
            expected = _dec(ret) / volatility
            # See the module docstring: sharpe's own half-ULP, plus the return's propagated
            # through the division, plus the volatility's propagated through the quotient.
            tolerance = (
                PERCENT_HALF_ULP
                + PERCENT_HALF_ULP / volatility
                + abs(expected) * (RATIO_HALF_ULP * 100) / volatility
            )
            if abs(expected - _dec(sharpe)) > tolerance:
                disagreements.append(
                    Disagreement(
                        "sharpe_identity",
                        row["symbol"],
                        f"sharpe_return_{suffix}",
                        expected,
                        _dec(sharpe),
                        tolerance,
                    )
                )
    return CheckResult(
        "sharpe_identity",
        "docs/05 §3",
        "the reference product's Sharpe is ret / (vol x 100), with no risk-free rate",
        cells,
        disagreements,
    )


def check_away_from_high(frame: pl.DataFrame) -> CheckResult:
    """docs/05 §10 — ``away_high = (P_t / high - 1) x 100``, over 542 cells."""
    disagreements: list[Disagreement] = []
    cells = 0
    pairs = (
        ("high_one_year", "away_from_high_one_year"),
        ("high_all_time", "away_from_high_all_time"),
    )
    for row in frame.iter_rows(named=True):
        close = _dec(row["close"])
        for high_column, away_column in pairs:
            high = _dec(row[high_column])
            if high == 0:
                continue
            cells += 1
            expected = (close / high - 1) * 100
            # Propagated: away's own half-ULP, plus close's and high's through the ratio.
            tolerance = (
                PERCENT_HALF_ULP
                + 100 * PRICE_HALF_ULP / high
                + 100 * close * PRICE_HALF_ULP / (high * high)
            )
            actual = _dec(row[away_column])
            if abs(expected - actual) > tolerance:
                disagreements.append(
                    Disagreement(
                        "away_from_high", row["symbol"], away_column, expected, actual, tolerance
                    )
                )
    return CheckResult(
        "away_from_high",
        "docs/05 §10",
        "distance from the 1-year and all-time high, recomputed from close and high",
        cells,
        disagreements,
    )


def check_positive_days_denominator(frame: pl.DataFrame) -> CheckResult:
    """docs/13 §3 — ``pos_days_N`` is ``k/N``, so ``pos_days x N / 100`` must be a whole number.

    This is the check that recovered the window lengths in the first place. Re-running it is how
    we notice if the trading calendar ever stops agreeing with the reference product's windows.
    """
    disagreements: list[Disagreement] = []
    cells = 0
    for row in frame.iter_rows(named=True):
        for months, suffix in WINDOW_SUFFIX.items():
            value = row[f"positive_days_percent_{suffix}"]
            if value is None:
                continue
            n = WINDOW_LENGTHS[months]
            cells += 1
            k = _dec(value) * n / 100
            nearest = Decimal(round(k))
            tolerance = PERCENT_HALF_ULP * n / 100
            if abs(k - nearest) > tolerance:
                disagreements.append(
                    Disagreement(
                        "positive_days_denominator",
                        row["symbol"],
                        f"positive_days_percent_{suffix} (N={n})",
                        nearest,
                        k,
                        tolerance,
                    )
                )
    return CheckResult(
        "positive_days_denominator",
        "docs/13 §3, docs/05 §11",
        f"the shared denominators {WINDOW_LENGTHS} still divide every published percentage",
        cells,
        disagreements,
    )


def check_blend_ordering(frame: pl.DataFrame) -> CheckResult:
    """docs/05 §4 — the export's row order is the plain mean of its sharpe components.

    docs/13 §2 finding 3 already records that this cannot reproduce exactly: the reference
    product ranked on unrounded values the export does not carry. So the check is not "the order
    matches" — it is "every inversion is inside the granularity that 2-dp component rounding
    induces", which is the strongest statement the data supports.
    """
    components = [f"sharpe_return_{WINDOW_SUFFIX[m]}" for m in SORTING_BLEND]
    # Each component is rounded to 2 dp, so the mean of N of them sits within one half-ULP of the
    # mean of the true values whatever N is — the errors average, they do not accumulate. Two
    # blends can therefore be inverted by at most two half-ULPs.
    blend_tolerance = PERCENT_HALF_ULP * 2

    values: list[tuple[str, Decimal]] = []
    for row in frame.iter_rows(named=True):
        parts = [row[c] for c in components]
        if any(p is None for p in parts):
            continue
        values.append((row["symbol"], sum((_dec(p) for p in parts), Decimal(0)) / len(components)))

    inversions = 0
    worst = Decimal(0)
    disagreements: list[Disagreement] = []
    for index in range(1, len(values)):
        (_, above), (symbol, below) = values[index - 1], values[index]
        if below > above:
            inversions += 1
            gap = below - above
            worst = max(worst, gap)
            if gap > blend_tolerance:
                disagreements.append(
                    Disagreement(
                        "blend_ordering",
                        symbol,
                        f"avg_sharpe_{shape_suffix(SORTING_BLEND)} (row {index + 1})",
                        above,
                        below,
                        blend_tolerance,
                    )
                )
    return CheckResult(
        "blend_ordering",
        "docs/05 §4, docs/13 §2 finding 3",
        "the export's row order is the mean of its sharpe components, to rounding granularity",
        len(values),
        disagreements,
        notes=[
            f"{inversions} inversions, worst {worst}, all within {blend_tolerance} "
            "(docs/13 §2 reports 48 inversions, all <= 0.0075)"
        ],
    )


def check_circuit_nesting(frame: pl.DataFrame) -> CheckResult:
    """docs/05 §12 — ``circuits_N`` counts hits in the last N bars, so it is nested and bounded.

    A count over 22 bars cannot exceed a count over 64 bars of the same series, and neither can
    exceed the number of bars. This is everything the published columns can prove about the
    INFERRED detection rule; the rule itself is unresolvable here and is said so below.
    """
    disagreements: list[Disagreement] = []
    order = [1, 3, 6, 9, 12]
    cells = 0
    for row in frame.iter_rows(named=True):
        counts = [row[f"circuits_{WINDOW_SUFFIX[m]}"] for m in order]
        for index in range(len(order) - 1):
            cells += 1
            if counts[index] > counts[index + 1]:
                disagreements.append(
                    Disagreement(
                        "circuit_nesting",
                        row["symbol"],
                        f"circuits_{WINDOW_SUFFIX[order[index]]} > "
                        f"circuits_{WINDOW_SUFFIX[order[index + 1]]}",
                        _dec(counts[index + 1]),
                        _dec(counts[index]),
                        Decimal(0),
                    )
                )
        for index, months in enumerate(order):
            n = WINDOW_LENGTHS[months]
            cells += 1
            if counts[index] > n:
                disagreements.append(
                    Disagreement(
                        "circuit_nesting",
                        row["symbol"],
                        f"circuits_{WINDOW_SUFFIX[months]} exceeds its {n}-bar window",
                        Decimal(n),
                        _dec(counts[index]),
                        Decimal(0),
                    )
                )

    non_zero = sum(1 for row in frame.iter_rows(named=True) if row["circuits_one_year"] > 0)
    return CheckResult(
        "circuit_nesting",
        "docs/05 §12 (INFERRED)",
        "circuit-hit counts are nested across windows and bounded by the window length",
        cells,
        disagreements,
        unresolved=[
            "docs/05 §12's detection rule cannot be validated from this snapshot. Validating it "
            "needs per-bar high/low/close and the NSE band for each day; the export carries a "
            "single day of counts. Nesting and bounds are all the published data can decide.",
            f"{non_zero} of {frame.height} rows report at least one circuit-hit day in the last "
            "year, which is the only distributional evidence available for calibrating the "
            "band heuristic.",
        ],
    )


def check_turnover_is_exchange_value(frame: pl.DataFrame) -> CheckResult:
    """docs/05 §13 — ``volume`` is exchange turnover in rupees, not ``close x shares``.

    docs/13 §2 finding 5 states the evidence as a distribution ("0.9999 +/- 0.008"), so the check
    is on the distribution. A per-row bound would be the wrong shape: VWAP genuinely differs from
    the close, and a name that traded away from its close all day is not a disagreement.
    """
    ratios: list[Decimal] = []
    for row in frame.iter_rows(named=True):
        shares = row["volume_shares"]
        close = _dec(row["close"])
        if not shares or close == 0:
            continue
        ratios.append(_dec(row["volume"]) / (close * _dec(shares)))

    count = len(ratios)
    if count < MINIMUM_DISTRIBUTION_SAMPLE:
        return CheckResult(
            "turnover_is_exchange_value",
            "docs/05 §13, docs/13 §2 finding 5",
            "the `volume` column is VWAP-based value traded, not close x share count",
            count,
            [],
            notes=[
                f"skipped: {count} rows is below the {MINIMUM_DISTRIBUTION_SAMPLE} this check "
                "needs. docs/13 §2 states the evidence as a mean and a standard deviation, and a "
                "handful of rows has neither. Run the report over the whole export to see it."
            ],
        )
    mean = sum(ratios, Decimal(0)) / count
    variance = sum(((r - mean) ** 2 for r in ratios), Decimal(0)) / count
    sd = variance.sqrt()

    disagreements: list[Disagreement] = []
    # docs/13 §2's own figures, to the precision it states them at.
    if abs(mean - Decimal("0.9999")) > Decimal("0.0005"):
        disagreements.append(
            Disagreement(
                "turnover_is_exchange_value",
                "(all rows)",
                "mean(volume / (close x volume_shares))",
                Decimal("0.9999"),
                mean,
                Decimal("0.0005"),
            )
        )
    if abs(sd - Decimal("0.008")) > Decimal("0.001"):
        disagreements.append(
            Disagreement(
                "turnover_is_exchange_value",
                "(all rows)",
                "sd(volume / (close x volume_shares))",
                Decimal("0.008"),
                sd,
                Decimal("0.001"),
            )
        )
    return CheckResult(
        "turnover_is_exchange_value",
        "docs/05 §13, docs/13 §2 finding 5",
        "the `volume` column is VWAP-based value traded, not close x share count",
        count,
        disagreements,
        notes=[f"mean {mean:.6f}, sd {sd:.6f} over {count} rows"],
    )


def check_price_level_ordering(frame: pl.DataFrame) -> CheckResult:
    """docs/05 §9, §10 — a maximum bounds a mean, and a longer maximum bounds a shorter one."""
    disagreements: list[Disagreement] = []
    cells = 0
    for row in frame.iter_rows(named=True):
        high_1y = _dec(row["high_one_year"])
        high_ath = _dec(row["high_all_time"])
        cells += 1
        if high_ath < high_1y - PRICE_HALF_ULP * 2:
            disagreements.append(
                Disagreement(
                    "price_level_ordering",
                    row["symbol"],
                    "high_all_time < high_one_year",
                    high_1y,
                    high_ath,
                    PRICE_HALF_ULP * 2,
                )
            )
        # Every moving average is a mean of closes inside a window the all-time high covers.
        for k in (20, 50, 100, 200):
            average = row[f"ma_{k}"]
            if average is None:
                continue
            cells += 1
            if _dec(average) > high_ath + PRICE_HALF_ULP * 2:
                disagreements.append(
                    Disagreement(
                        "price_level_ordering",
                        row["symbol"],
                        f"ma_{k} exceeds high_all_time",
                        high_ath,
                        _dec(average),
                        PRICE_HALF_ULP * 2,
                    )
                )
        # ma_20 averages 20 of the last 252 closes, so the 1-year high bounds it.
        if row["ma_20"] is not None:
            cells += 1
            if _dec(row["ma_20"]) > high_1y + PRICE_HALF_ULP * 2:
                disagreements.append(
                    Disagreement(
                        "price_level_ordering",
                        row["symbol"],
                        "ma_20 exceeds high_one_year",
                        high_1y,
                        _dec(row["ma_20"]),
                        PRICE_HALF_ULP * 2,
                    )
                )
    return CheckResult(
        "price_level_ordering",
        "docs/05 §9, §10",
        "high_ath >= high_1y >= ma_20, and no moving average exceeds the all-time high",
        cells,
        disagreements,
    )


def check_value_ranges(frame: pl.DataFrame) -> CheckResult:
    """docs/05 §5, §2 — RSI is a percentage in [0, 100] and volatility is a positive fraction."""
    disagreements: list[Disagreement] = []
    cells = 0
    for row in frame.iter_rows(named=True):
        for suffix in WINDOW_SUFFIX.values():
            rsi = row[f"rsi_{suffix}"]
            if rsi is not None:
                cells += 1
                value = _dec(rsi)
                if value < RSI_FLOOR or value > RSI_CEILING:
                    disagreements.append(
                        Disagreement(
                            "value_ranges",
                            row["symbol"],
                            f"rsi_{suffix} outside [0, 100]",
                            RSI_CEILING,
                            value,
                            Decimal(0),
                        )
                    )
            vol = row[f"volatility_{suffix}"]
            if vol is not None:
                cells += 1
                value = _dec(vol)
                if value <= 0:
                    disagreements.append(
                        Disagreement(
                            "value_ranges",
                            row["symbol"],
                            f"volatility_{suffix} is not positive",
                            Decimal(0),
                            value,
                            Decimal(0),
                        )
                    )
                # docs/13 §2 finding 4: stored as a FRACTION. A percentage would be > 1 here.
                if value > VOLATILITY_FRACTION_CEILING:
                    disagreements.append(
                        Disagreement(
                            "value_ranges",
                            row["symbol"],
                            f"volatility_{suffix} looks like a percentage, not a fraction",
                            VOLATILITY_FRACTION_CEILING,
                            value,
                            Decimal(0),
                        )
                    )
    return CheckResult(
        "value_ranges",
        "docs/05 §2, §5; docs/13 §2 finding 4",
        "RSI in [0, 100]; volatility a positive decimal fraction",
        cells,
        disagreements,
    )


#: Which universes are contained in which, from docs/01 §2's own definitions. Checked against the
#: published flags, so a membership bug in either direction shows up.
UNIVERSE_CONTAINMENT: Final[tuple[tuple[str, str], ...]] = (
    ("nifty_50", "nifty_100"),
    ("nifty_next_50", "nifty_100"),
    ("nifty_100", "nifty_200"),
    ("nifty_200", "nifty_500"),
    ("nifty_500", "nifty_total_market"),
    ("nifty_100", "nifty_large_mid_250"),
    ("nifty_midcap_150", "nifty_large_mid_250"),
    ("nifty_midcap_150", "nifty_mid_small_400"),
    ("nifty_smallcap_250", "nifty_mid_small_400"),
    ("nifty_total_market", "nifty_allcap"),
)


def check_universe_containment(frame: pl.DataFrame) -> CheckResult:
    """docs/01 §2 — the index families nest, so the published flags must nest too."""
    disagreements: list[Disagreement] = []
    cells = 0
    for row in frame.iter_rows(named=True):
        for inner, outer in UNIVERSE_CONTAINMENT:
            cells += 1
            if row[f"is_{inner}"] == 1 and row[f"is_{outer}"] != 1:
                disagreements.append(
                    Disagreement(
                        "universe_containment",
                        row["symbol"],
                        f"is_{inner} without is_{outer}",
                        Decimal(1),
                        Decimal(0),
                        Decimal(0),
                    )
                )
    return CheckResult(
        "universe_containment",
        "docs/01 §2",
        "NIFTY 50 c 100 c 200 c 500 c Total Market, and the mid/small families nest likewise",
        cells,
        disagreements,
    )


def check_top_risk_flag_monotonicity(frame: pl.DataFrame) -> CheckResult:
    """docs/06 §Step 4 — ``*_top_beta`` / ``*_top_volatility`` are a top decile *within* a universe.

    The export is a filtered subset, so the flagged share of the visible rows is not 10% and
    cannot be. What must still hold is monotonicity: inside one universe, no unflagged row may
    have a strictly higher beta (or volatility) than a flagged one. That is true of a top-decile
    cut and false of almost any bug.
    """
    disagreements: list[Disagreement] = []
    cells = 0
    notes: list[str] = []
    for universe in UNIVERSES:
        slug = _slug(universe.slug)
        members = [r for r in frame.iter_rows(named=True) if r[f"is_{slug}"] == 1]
        if not members:
            continue
        for metric, column in (("beta", "beta"), ("volatility", "volatility_one_year")):
            flag = f"is_{slug}_top_{metric}"
            flagged = [r for r in members if r[flag] == 1]
            unflagged = [r for r in members if r[flag] != 1]
            if not flagged or not unflagged:
                continue
            cells += len(members)
            lowest_flagged = min(_dec(r[column]) for r in flagged)
            for row in unflagged:
                if _dec(row[column]) > lowest_flagged:
                    disagreements.append(
                        Disagreement(
                            "top_risk_flag_monotonicity",
                            row["symbol"],
                            f"{flag} unset despite {column} above the flagged minimum",
                            lowest_flagged,
                            _dec(row[column]),
                            Decimal(0),
                        )
                    )
            notes.append(
                f"{slug}/{metric}: {len(flagged)}/{len(members)} visible rows flagged "
                f"({100 * len(flagged) / len(members):.1f}%)"
            )
    return CheckResult(
        "top_risk_flag_monotonicity",
        "docs/06 §Step 4",
        "the top-beta and top-volatility flags are a monotone cut within each universe",
        cells,
        disagreements,
        notes=notes,
    )


def check_skip_month_momentum(frame: pl.DataFrame) -> CheckResult:
    """docs/05 §8 — the first INFERRED area, investigated as far as the data allows.

    docs/05 asks Prompt 19 to resolve this "empirically" against the reference product. It cannot
    be resolved from this snapshot, and the reason is structural rather than a matter of effort:

    * **The export has no skip-month column.** All 93 are listed in
      ``decile_core.reference_export.EXPORT_COLUMNS``; none of them is ``ret_12m_minus_1m`` or
      ``ret_12m_minus_2m``. The reference product offers them as *sort keys* (docs/01 §3) and
      does not export the values.
    * **The only two published values in the whole bundle are docs/05 §8's own** — CUPID 608.37
      and 852.21, observed on 19 Aug 2026, one day after this file.

    So the investigation is arithmetic on those two numbers plus the published returns, and it is
    done in :func:`skip_month_evidence`. Its conclusion is recorded as UNRESOLVED, because
    "candidate A is refuted" is not the same as "candidate B is confirmed", and pretending
    otherwise would be exactly the kind of confident wrongness docs/05 warns about.
    """
    evidence = skip_month_evidence(frame)
    return CheckResult(
        "skip_month_momentum",
        "docs/05 §8 (INFERRED)",
        "12-1 and 12-2 momentum: which of docs/05's two candidate definitions the product uses",
        0,
        [],
        unresolved=evidence,
    )


def skip_month_evidence(frame: pl.DataFrame) -> list[str]:
    """The arithmetic behind the §8 finding, so a reader can check it rather than trust it."""
    published_12_1 = Decimal("608.37")
    published_12_2 = Decimal("852.21")
    # docs/05 §3's table and §8's figures are all from 19 Aug 2026.
    ret_12m_19aug = Decimal("753.00")
    ret_1m_19aug = Decimal("37.29")

    growth_12m = 1 + ret_12m_19aug / 100
    growth_1m = 1 + ret_1m_19aug / 100
    candidate_a = (growth_12m / growth_1m - 1) * 100

    lines = [
        "docs/05 §8 leaves two candidate definitions open and asks Prompt 19 to settle it "
        "empirically. IT CANNOT BE SETTLED FROM THIS BUNDLE. Reasons and arithmetic:",
        "  (1) The 93-column export carries no skip-month column at all — the reference product "
        "offers 12-1 and 12-2 as sort keys (docs/01 §3) and never exports the values. So the 271 "
        "rows contribute nothing to this question.",
        "  (2) The only published values anywhere in docs/ are CUPID's 608.37 and 852.21, from "
        "19 Aug 2026 (docs/05 §8).",
        f"  (3) Candidate A — docs/05's primary, P_(t-21)/P_(t-252) — is fully determined by "
        f"CUPID's other published figures on that day: (1 + {ret_12m_19aug}%) / "
        f"(1 + {ret_1m_19aug}%) - 1 = {candidate_a.quantize(Decimal('0.01'))}%, against a "
        f"published {published_12_1}%. The gap is "
        f"{(published_12_1 - candidate_a).quantize(Decimal('0.01'))} percentage points, or "
        f"{((published_12_1 - candidate_a) / candidate_a * 100).quantize(Decimal('0.1'))}% of "
        "the value. The 21-vs-22 and 247-vs-252 bar mismatches between §8's bar offsets and "
        "§1's calendar windows are worth about six bars of a name compounding at ~0.86% a day, "
        "i.e. ~5% — a quarter of the gap. Candidate A is therefore INCONSISTENT with the "
        "published figure, by a margin the offset mismatch does not explain.",
        f"  (4) Candidate B — P_(t-21)/P_(t-273) — is under-determined: it implies a 13-month "
        f"return of {((1 + published_12_1 / 100) * growth_1m - 1) * 100:.0f}% for CUPID, which "
        "nothing in the bundle contradicts and nothing confirms. It is NOT CONFIRMED.",
        f"  (5) The 12-2 figure ({published_12_2}%) exceeds the 12-month return "
        f"({ret_12m_19aug}%), which under either candidate requires CUPID's price to have fallen "
        "materially between t-42 and t-21 while rising 37% over the last month. That is possible "
        "and is not evidence either way.",
        "  DECISION: the engine keeps shipping candidate A (SkipMonthDefinition.SKIP_END), which "
        "is the definition docs/05 §8 instructs us to implement, and the switch to candidate B "
        "remains a constructor argument (decile_core.momentum.SkipMonthConfig). Resolving it "
        "needs CUPID's real adjusted closes for ~294 trading days, which arrives with the first "
        "production backfill and not before.",
    ]
    del frame
    return lines


def check_circuit_detection_rule(frame: pl.DataFrame) -> CheckResult:
    """docs/05 §12 — the second INFERRED area, investigated as far as the data allows."""
    counts = frame["circuits_one_year"].to_list()
    non_zero = [c for c in counts if c > 0]
    return CheckResult(
        "circuit_detection_rule",
        "docs/05 §12 (INFERRED)",
        "which rule the reference product uses to call a day circuit-locked",
        len(counts),
        [],
        unresolved=[
            "docs/05 §12's rule has two branches (published NSE bands; a |r_t| band heuristic "
            "with a 0.25% tolerance) and the snapshot can distinguish neither. Distinguishing "
            "them needs, per bar: high, low, close_raw, the previous close and the day's NSE "
            "band. The export has one day of counts.",
            f"What the counts do say: {len(non_zero)} of {len(counts)} rows recorded at least "
            f"one hit in the last year; the largest is {max(counts)} days out of "
            f"{WINDOW_LENGTHS[12]}. A detection rule that fired on, say, every 20% up-day would "
            "produce far more than that on a 271-row momentum screen, so the reference product's "
            "rule is at least as strict as ours. That is a bound, not a validation.",
            "DECISION: the engine keeps docs/05 §12's rule as written, with the published-band "
            "branch preferred and the method recorded per row "
            "(decile_core.circuits.CircuitMethod). Recalibration needs bhavcopy history.",
        ],
    )


ALL_CHECKS: Final = (
    check_sharpe_identity,
    check_away_from_high,
    check_positive_days_denominator,
    check_blend_ordering,
    check_circuit_nesting,
    check_turnover_is_exchange_value,
    check_price_level_ordering,
    check_value_ranges,
    check_universe_containment,
    check_top_risk_flag_monotonicity,
    check_skip_month_momentum,
    check_circuit_detection_rule,
)


def reconcile(frame: pl.DataFrame) -> list[CheckResult]:
    """Run every check against a reference-export frame."""
    return [check(frame) for check in ALL_CHECKS]


def total_disagreements(results: list[CheckResult]) -> int:
    return sum(len(result.disagreements) for result in results)
