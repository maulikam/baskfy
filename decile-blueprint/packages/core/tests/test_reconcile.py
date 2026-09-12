"""The reconciliation checks, and the evidence that they have teeth (Prompt 19 §4).

A reconciliation report that passes because its checks cannot fail is worse than no report: it
converts "we did not look" into "we looked and it was fine". So this file asserts two things about
every check — that it runs clean against the committed reference export, and that it *stops*
running clean when a single published cell is perturbed past its tolerance.
"""

from __future__ import annotations

from decimal import Decimal

import polars as pl
import pytest

from baskfy_core.reconcile import (
    ALL_CHECKS,
    WINDOW_LENGTHS,
    CheckResult,
    check_away_from_high,
    check_blend_ordering,
    check_circuit_nesting,
    check_positive_days_denominator,
    check_price_level_ordering,
    check_sharpe_identity,
    check_top_risk_flag_monotonicity,
    check_turnover_is_exchange_value,
    check_universe_containment,
    check_value_ranges,
    reconcile,
    skip_month_evidence,
    total_disagreements,
)
from _law1_io import reference_export_path
from baskfy_core.reference_export import read_export


@pytest.fixture(scope="module")
def export() -> pl.DataFrame:
    return read_export(reference_export_path())


@pytest.fixture(scope="module")
def results(export: pl.DataFrame) -> list[CheckResult]:
    return reconcile(export)


class TestTheReportRunsClean:
    def test_no_disagreement_over_tolerance(self, results: list[CheckResult]) -> None:
        """Prompt 19's first acceptance criterion, in its cleanest form."""
        offenders = [
            f"{result.name}: {len(result.disagreements)} — first: {result.disagreements[0]}"
            for result in results
            if result.disagreements
        ]
        assert not offenders, "\n".join(offenders)
        assert total_disagreements(results) == 0

    def test_every_check_actually_examined_something(self, results: list[CheckResult]) -> None:
        """A check that examined zero cells is a check that did not run."""
        by_name = {result.name: result for result in results}
        # skip_month_momentum has no cells by construction: the export has no such column, which
        # is itself the finding. It is required to say so under UNRESOLVED instead.
        assert by_name["skip_month_momentum"].cells == 0
        assert by_name["skip_month_momentum"].unresolved
        for result in results:
            if result.name == "skip_month_momentum":
                continue
            assert result.cells > 0, f"{result.name} examined no cells"

    def test_the_headline_counts_match_the_documents(self, results: list[CheckResult]) -> None:
        """docs/05 §3 verified 1,355 sharpe cells and §10 verified 542 away-from-high cells."""
        by_name = {result.name: result for result in results}
        assert by_name["sharpe_identity"].cells == 1355
        assert by_name["away_from_high"].cells == 542

    def test_the_blend_inversions_are_the_documented_forty_eight(
        self, results: list[CheckResult]
    ) -> None:
        """docs/13 §2 finding 3: "only 48 inversions all <= 0.0075"."""
        note = next(r for r in results if r.name == "blend_ordering").notes[0]
        assert note.startswith("48 inversions, worst 0.0075")


class TestTheChecksHaveTeeth:
    """One perturbation per check, each just past the tolerance that check derives."""

    def _perturb(self, export: pl.DataFrame, column: str, delta: float) -> pl.DataFrame:
        first = export["symbol"][0]
        return export.with_columns(
            pl.when(pl.col("symbol") == first)
            .then(pl.col(column) + delta)
            .otherwise(pl.col(column))
            .alias(column)
        )

    def test_sharpe_identity_catches_a_moved_sharpe(self, export: pl.DataFrame) -> None:
        result = check_sharpe_identity(self._perturb(export, "sharpe_return_one_year", 0.05))
        assert len(result.disagreements) == 1
        assert result.disagreements[0].column == "sharpe_return_one_year"

    def test_away_from_high_catches_a_moved_close(self, export: pl.DataFrame) -> None:
        result = check_away_from_high(self._perturb(export, "close", 5.0))
        assert len(result.disagreements) == 2  # both the 1-year and the all-time cell move

    def test_positive_days_catches_a_wrong_denominator(self, export: pl.DataFrame) -> None:
        """If the trading calendar drifts, ``pos_days x N`` stops being a whole number.

        Perturbing the published value is the same experiment as perturbing ``N`` and needs no
        monkeypatching: 0.5 percentage points on a 22-bar window is a tenth of a day, which no
        ``k/22`` can be.
        """
        result = check_positive_days_denominator(
            self._perturb(export, "positive_days_percent_one_month", 0.5)
        )
        assert len(result.disagreements) == 1
        assert f"N={WINDOW_LENGTHS[1]}" in result.disagreements[0].column

    def test_blend_ordering_catches_a_row_out_of_order(self, export: pl.DataFrame) -> None:
        """Move one row's 12-month sharpe far enough that its blend belongs elsewhere."""
        scrambled = export.with_columns(
            pl.when(pl.col("symbol") == export["symbol"][200])
            .then(pl.col("sharpe_return_one_year") + 40.0)
            .otherwise(pl.col("sharpe_return_one_year"))
            .alias("sharpe_return_one_year")
        )
        assert check_blend_ordering(scrambled).disagreements

    def test_circuit_nesting_catches_an_unnested_count(self, export: pl.DataFrame) -> None:
        broken = export.with_columns(
            pl.when(pl.col("symbol") == export["symbol"][0])
            .then(pl.lit(99))
            .otherwise(pl.col("circuits_one_month"))
            .alias("circuits_one_month")
        )
        result = check_circuit_nesting(broken)
        # 99 exceeds the 22-bar window and also exceeds every longer window's count.
        assert len(result.disagreements) >= 2

    def test_turnover_check_catches_a_shifted_distribution(self, export: pl.DataFrame) -> None:
        """Halve every turnover: the mean ratio moves to ~0.5 and the check must notice."""
        halved = export.with_columns((pl.col("volume") // 2).alias("volume"))
        assert check_turnover_is_exchange_value(halved).disagreements

    def test_price_level_ordering_catches_a_high_below_its_own_moving_average(
        self, export: pl.DataFrame
    ) -> None:
        broken = export.with_columns(
            pl.when(pl.col("symbol") == export["symbol"][0])
            .then(pl.lit(1.0))
            .otherwise(pl.col("high_all_time"))
            .alias("high_all_time")
        )
        assert check_price_level_ordering(broken).disagreements

    def test_value_ranges_catches_volatility_stored_as_a_percentage(
        self, export: pl.DataFrame
    ) -> None:
        """docs/13 §2 finding 4 — a units regression is the single most likely factor bug."""
        as_percent = export.with_columns(
            (pl.col("volatility_one_year") * 100).alias("volatility_one_year")
        )
        assert check_value_ranges(as_percent).disagreements

    def test_universe_containment_catches_a_broken_nest(self, export: pl.DataFrame) -> None:
        broken = export.with_columns(
            pl.when(pl.col("is_nifty_50") == 1)
            .then(pl.lit(0))
            .otherwise(pl.col("is_nifty_100"))
            .alias("is_nifty_100")
        )
        assert check_universe_containment(broken).disagreements

    def test_top_risk_flag_monotonicity_catches_a_flag_on_the_wrong_row(
        self, export: pl.DataFrame
    ) -> None:
        """Flag the *lowest*-beta NIFTY 50 member: monotonicity breaks immediately."""
        members = [r for r in export.iter_rows(named=True) if r["is_nifty_50"] == 1]
        lowest = min(members, key=lambda r: float(r["beta"]))["symbol"]
        broken = export.with_columns(
            pl.when(pl.col("symbol") == lowest)
            .then(pl.lit(1))
            .otherwise(pl.col("is_nifty_50_top_beta"))
            .alias("is_nifty_50_top_beta")
        )
        assert check_top_risk_flag_monotonicity(broken).disagreements

    def test_every_check_is_exercised_by_one_of_the_tests_above(self) -> None:
        """A check added without a perturbation test is a check nobody has proved can fail."""
        exercised = {
            "sharpe_identity",
            "away_from_high",
            "positive_days_denominator",
            "blend_ordering",
            "circuit_nesting",
            "turnover_is_exchange_value",
            "price_level_ordering",
            "value_ranges",
            "universe_containment",
            "top_risk_flag_monotonicity",
        }
        # The two INFERRED checks report findings rather than compare cells, and are asserted
        # on their content in TestTheInferredAreas below.
        inferred = {"skip_month_momentum", "circuit_detection_rule"}
        implemented = {check.__name__.removeprefix("check_") for check in ALL_CHECKS}
        assert implemented == exercised | inferred


class TestTheInferredAreas:
    """docs/05 §8 and §12 — "Investigate and document each disagreement"."""

    def test_skip_month_finding_states_the_arithmetic_and_the_conclusion(
        self, export: pl.DataFrame
    ) -> None:
        evidence = skip_month_evidence(export)
        joined = "\n".join(evidence)
        # The refutation of candidate A must be quantified, not asserted.
        assert "521.31" in joined, "candidate A's implied value is not shown"
        assert "608.37" in joined, "the published value is not shown"
        assert "INCONSISTENT" in joined
        assert "NOT CONFIRMED" in joined, "candidate B must not be presented as settled"
        assert "SkipMonthDefinition.SKIP_END" in joined, "the shipped definition is not named"

    def test_candidate_a_really_is_refuted_by_the_published_figures(self) -> None:
        """Recomputed here from docs/05's own numbers, so the finding is checkable not quotable."""
        ret_12m = Decimal("753.00")
        ret_1m = Decimal("37.29")
        implied = ((1 + ret_12m / 100) / (1 + ret_1m / 100) - 1) * 100
        published = Decimal("608.37")
        assert implied.quantize(Decimal("0.01")) == Decimal("521.31")
        # The gap is far larger than the ~5% the 21-vs-22 / 247-vs-252 offset mismatch buys.
        assert (published - implied) / implied > Decimal("0.15")

    def test_the_export_carries_no_skip_month_column(self, export: pl.DataFrame) -> None:
        """The structural reason §8 cannot be settled here, asserted rather than claimed."""
        assert not [c for c in export.columns if "minus" in c or "skip" in c]

    def test_circuit_finding_names_what_would_settle_it(self, results: list[CheckResult]) -> None:
        text = "\n".join(next(r for r in results if r.name == "circuit_detection_rule").unresolved)
        assert "bhavcopy" in text.lower() or "band" in text.lower()
        assert "DECISION" in text
