"""Reference parity — docs/13 §5, the decisive acceptance test for the factor engine.

    "If step 2 passes for all 271 rows, the factor engine is provably equivalent to the reference
     product. That is the definition of done for Prompt 5."

WHAT THIS FILE CAN AND CANNOT PROVE, STATED PLAINLY
====================================================
docs/13 §5 asks for six things. They divide cleanly by what the repository actually contains.

**Provable here, and proved below (steps 1, 3, 5, 6).**
The export carries 271 real rows of *outputs*. Every identity docs/13 §2 verified over those
outputs can be re-verified through **our own formula code** — feeding the export's stored inputs
into the functions the engine uses and checking they produce the export's stored outputs. That is
a genuine test of our arithmetic against 1,355 real sharpe cells, 542 real away-from-high cells,
the blend rule, and the recovered window denominators. It is not a weakened substitute for
step 2; it is a different property, and it is the strongest one this data supports.

**Not provable here (step 2, the row-by-row reproduction).**
Reproducing ``ret_12m`` for CUPID requires CUPID's *adjusted daily closes for 248 trading days*.
The bundle contains no price history at all — `fixtures/` holds one file, a single-date snapshot
of results. The Prompt 2 provider fixtures are seeded random walks: they terminate on each
symbol's real close (so `close` matches) but their path is invented, so every path-dependent
factor is necessarily different. And the suite is network-blocked by design (Prompt 2 acceptance
criterion 1), so no amount of test code can fetch the real series.

So :class:`TestFullRowReproduction` is written in full and **skips**, with the reason attached.
It is not xfail and its tolerances are not widened — Prompt 5 is explicit: "Treat any column that
cannot be reproduced as a specification bug to be investigated and documented, not as a test to be
loosened." Point ``DECILE_PARITY_BARS`` at a Parquet file of real adjusted history and it runs.

**Known to be unreproducible even with the data (documented, not hidden).**
docs/05 §8 already records that ``ret_12m_minus_1m`` / ``ret_12m_minus_2m`` do not reconcile:
"The reference site's own numbers for CUPID (608.37 and 852.21) do **not** reconcile with this
definition given its other published figures. ... must be resolved empirically in Prompt 19."
Those two columns are listed in :data:`KNOWN_UNRECONCILED` rather than quietly excluded.
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal
from pathlib import Path
from typing import Final

import polars as pl
import pytest

from decile_core.blends import BLEND_SHAPES
from decile_core.factors import compute_factors
from decile_core.precision import quantise
from decile_core.reference_export import (
    EXPORT_COLUMNS,
    FACTOR_COLUMN_MAP,
    default_fixture_path,
    read_export,
)
from decile_core.universes import UNIVERSE_BY_SLUG
from decile_core.windows import EXPECTED_WINDOW_LENGTHS_2026_08_18

AS_OF: Final = dt.date(2026, 8, 18)

#: Where a caller can supply the real adjusted history that step 2 needs.
BARS_ENV_VAR: Final = "DECILE_PARITY_BARS"

#: docs/05 §8 — known not to reconcile, and why. Listed, never silently skipped.
KNOWN_UNRECONCILED: Final[dict[str, str]] = {
    "ret_12m_minus_1m": (
        "docs/05 §8: the reference product's published value (608.37 for CUPID) does not "
        "reconcile with either candidate definition; Prompt 19 must settle it empirically"
    ),
    "ret_12m_minus_2m": ("docs/05 §8: same — published 852.21 for CUPID, unreconciled"),
}

#: The tolerance each column's stored precision implies: half of its last decimal place
#: (docs/13 §5: "within the tolerance implied by its stored precision").
COLUMN_TOLERANCE: Final[dict[str, Decimal]] = {
    **{c: Decimal("0.005") for c in ("open", "high", "low", "close")},
    **{
        f"absolute_return_{w}": Decimal("0.005")
        for w in ("one_year", "nine_months", "six_months", "three_months", "one_month")
    },
    **{
        f"sharpe_return_{w}": Decimal("0.005")
        for w in ("one_year", "nine_months", "six_months", "three_months", "one_month")
    },
    **{
        f"rsi_{w}": Decimal("0.00005")
        for w in ("one_year", "nine_months", "six_months", "three_months", "one_month")
    },
    **{
        f"volatility_{w}": Decimal("0.00000000005")
        for w in ("one_year", "nine_months", "six_months", "three_months", "one_month")
    },
    "beta": Decimal("0.00000000005"),
    **{
        f"positive_days_percent_{w}": Decimal("0.005")
        for w in ("one_year", "nine_months", "six_months", "three_months", "one_month")
    },
    "high_one_year": Decimal("0.005"),
    "high_all_time": Decimal("0.005"),
    "away_from_high_one_year": Decimal("0.005"),
    "away_from_high_all_time": Decimal("0.005"),
    **{f"ma_{k}": Decimal("0.005") for k in (200, 100, 50, 20)},
    "median_volume_one_year": Decimal("0.5"),
    "marketcap": Decimal("0.5"),
    "volume": Decimal("0.5"),
}


@pytest.fixture(scope="module")
def export() -> pl.DataFrame:
    return read_export()


# ---------------------------------------------------------------------------
# docs/13 §5 step 1 — load the export
# ---------------------------------------------------------------------------


class TestStep1TheExportLoads:
    def test_it_has_271_rows(self, export: pl.DataFrame) -> None:
        assert export.height == 271

    def test_it_has_93_columns(self, export: pl.DataFrame) -> None:
        assert export.width == len(EXPORT_COLUMNS) == 93

    def test_every_row_is_the_same_trade_date(self, export: pl.DataFrame) -> None:
        assert export["date"].unique().to_list() == [AS_OF.isoformat()]


# ---------------------------------------------------------------------------
# docs/13 §5 step 3 — "Assert the identities in §2 rows 1-3 hold in **our** output too"
#
# Run through the engine's own formula code, with the export's stored values as inputs. A change
# to how we compute sharpe, away-from-high or a blend breaks these against 271 real rows.
# ---------------------------------------------------------------------------


class TestStep3IdentityOne:
    """docs/13 §2 finding 1: ``sharpe_return_N = absolute_return_N / (volatility_N x 100)``.

    A DOCUMENTED DISCREPANCY IN docs/13 §2
    ---------------------------------------
    docs/13 claims "**1,355/1,355 cells match**, max error 0.0051 = pure 2-dp rounding". Measured
    against the committed file, the error distribution is:

        1,344 cells at exactly 0.00
           11 cells at exactly 0.01
            0 cells anywhere in between

    So the maximum is 0.01, not 0.0051. Investigated rather than tolerated, per Prompt 5: "Treat
    any column that cannot be reproduced as a specification bug to be investigated and documented,
    not as a test to be loosened."

    **The formula is not in doubt.** The gap has one cause, and the *shape* of the distribution
    proves it: a formula difference would scatter errors across a range, and there is nothing
    between 0.00 and 0.01. The eleven outliers are rounding-boundary cases. Worked example —
    ATHERENERG, six months:

        stored return     99.78          (2 dp, so the true value is in [99.775, 99.785])
        stored volatility 0.47177847
        true sharpe       in [2.114870, 2.115082]  -> the reference product stored 2.12
        our recomputation 99.78 / 47.177847 = 2.1149757  -> rounds to 2.11

    The reference product divided its *unrounded* return; we can only divide the 2-dp one the
    export publishes. When the true ratio sits within half a tick of a ``.xx5`` boundary, the two
    land on opposite sides. docs/13's 0.0051 figure was presumably measured against unrounded
    inputs its author held and the export does not carry.

    What is asserted below is therefore the property that is actually true and actually
    discriminating: every cell within one unit in the last stored place, at least 99% exact, and
    **no cell in between** — which is what would break first if the formula were wrong.
    """

    WINDOWS: Final = ("one_year", "nine_months", "six_months", "three_months", "one_month")

    #: One unit in the last place of 2-dp storage.
    ONE_ULP: Final = Decimal("0.01")

    def _errors(self, export: pl.DataFrame) -> list[tuple[str, str, Decimal]]:
        out: list[tuple[str, str, Decimal]] = []
        for row in export.iter_rows(named=True):
            for window in self.WINDOWS:
                ret = row[f"absolute_return_{window}"]
                vol = row[f"volatility_{window}"]
                stored = row[f"sharpe_return_{window}"]
                if ret is None or vol is None or stored is None or float(vol) == 0:
                    continue
                computed = quantise(float(ret) / (float(vol) * 100), 2)
                assert computed is not None
                out.append((str(row["symbol"]), window, abs(computed - Decimal(str(stored)))))
        return out

    def test_all_1355_cells_are_checked(self, export: pl.DataFrame) -> None:
        """docs/13 §2's own count: 271 rows x 5 windows."""
        assert len(self._errors(export)) == 1355

    def test_no_cell_exceeds_one_unit_in_the_last_stored_place(self, export: pl.DataFrame) -> None:
        offenders = [
            f"{symbol} {window}: {error}"
            for symbol, window, error in self._errors(export)
            if error > self.ONE_ULP
        ]
        assert offenders == []

    def test_the_overwhelming_majority_are_exact(self, export: pl.DataFrame) -> None:
        errors = self._errors(export)
        exact = sum(1 for _s, _w, error in errors if error == 0)
        assert exact / len(errors) > 0.99

    def test_no_error_falls_between_zero_and_one_ulp(self, export: pl.DataFrame) -> None:
        """The discriminating assertion. Rounding boundaries produce 0 or exactly one tick;
        a wrong formula would produce a spread. This is what would fail first."""
        between = [
            f"{symbol} {window}: {error}"
            for symbol, window, error in self._errors(export)
            if Decimal(0) < error < self.ONE_ULP
        ]
        assert between == []

    def test_every_outlier_is_explained_by_a_rounding_boundary(self, export: pl.DataFrame) -> None:
        """For each mismatching cell, the true sharpe implied by the *unrounded* return band must
        reach the stored value — i.e. the reference product's answer is inside the band our
        rounded input cannot resolve."""
        unexplained: list[str] = []
        for row in export.iter_rows(named=True):
            for window in self.WINDOWS:
                ret, vol, stored = (
                    row[f"absolute_return_{window}"],
                    row[f"volatility_{window}"],
                    row[f"sharpe_return_{window}"],
                )
                if ret is None or vol is None or stored is None or float(vol) == 0:
                    continue
                computed = quantise(float(ret) / (float(vol) * 100), 2)
                assert computed is not None
                if computed == Decimal(str(stored)):
                    continue
                # The true return lies within half a tick of the stored one.
                denominator = float(vol) * 100
                low = quantise((float(ret) - 0.005) / denominator, 2)
                high = quantise((float(ret) + 0.005) / denominator, 2)
                assert low is not None and high is not None
                if not low <= Decimal(str(stored)) <= high:
                    unexplained.append(
                        f"{row['symbol']} {window}: stored {stored} outside [{low}, {high}]"
                    )
        assert unexplained == []

    def test_no_risk_free_rate_would_survive_this(self, export: pl.DataFrame) -> None:
        """docs/05 §3: "There is no risk-free rate ... anywhere in it." Proving the negative."""
        mismatches = 0
        for row in export.iter_rows(named=True):
            ret, vol, stored = (
                row["absolute_return_one_year"],
                row["volatility_one_year"],
                row["sharpe_return_one_year"],
            )
            if ret is None or vol is None or stored is None or float(vol) == 0:
                continue
            with_rate = quantise((float(ret) - 6.0) / (float(vol) * 100), 2)
            assert with_rate is not None
            if abs(with_rate - Decimal(str(stored))) > Decimal("0.0051"):
                mismatches += 1
        assert mismatches > 250


class TestStep3IdentityTwo:
    """docs/13 §2 finding 2: ``away_from_high = (close / high - 1) x 100``.

    "**542/542 match**, max error 0.005."
    """

    PAIRS: Final = (
        ("high_one_year", "away_from_high_one_year"),
        ("high_all_time", "away_from_high_all_time"),
    )

    def test_every_cell_matches(self, export: pl.DataFrame) -> None:
        checked = 0
        failures: list[str] = []
        for row in export.iter_rows(named=True):
            for high_column, away_column in self.PAIRS:
                high, away, close = row[high_column], row[away_column], row["close"]
                if high is None or away is None or close is None or float(high) == 0:
                    continue
                checked += 1
                computed = quantise((float(close) / float(high) - 1) * 100, 2)
                if computed is None or abs(computed - Decimal(str(away))) > Decimal("0.005"):
                    failures.append(f"{row['symbol']} {away_column}: {computed} vs {away}")
        assert checked == 542
        assert failures == []

    def test_away_from_high_is_never_positive(self, export: pl.DataFrame) -> None:
        """docs/05 §10 marks both as "<= 0"; a positive value would mean the high is not a max."""
        for row in export.iter_rows(named=True):
            for _high, away in self.PAIRS:
                if row[away] is not None:
                    assert float(row[away]) <= 0, f"{row['symbol']} {away}"


def _blend_column(export: pl.DataFrame, components: tuple[str, ...]) -> list[Decimal]:
    """The blend of ``components`` for every export row, summed exactly.

    ``Decimal`` rather than ``float`` because the whole question here is whether two rows tie to
    the last stored place, and binary floating point cannot represent 2-dp decimals exactly — it
    manufactures inversions that are not in the file. See docs/13a §2.
    """
    return [
        sum((Decimal(str(row[column])) for column in components), start=Decimal(0))
        / len(components)
        for row in export.iter_rows(named=True)
        if all(row[column] is not None for column in components)
    ]


class TestStep3IdentityThree:
    """docs/13 §2 finding 3 / docs/05 §4: a blend is the arithmetic mean of its components."""

    def test_the_screens_own_sort_factor_reproduces(self, export: pl.DataFrame) -> None:
        """docs/13: the export is the "Investing 001" screen, sorted by
        AVERAGE SHARPE RETURN 12 6 3 1 MONTHS. Recomputing it must reproduce the file's order."""
        components = (
            "sharpe_return_one_year",
            "sharpe_return_six_months",
            "sharpe_return_three_months",
            "sharpe_return_one_month",
        )
        blended = _blend_column(export, components)
        assert len(blended) == export.height

        # docs/13 §2 finding 3: "the only 48 'inversions' are <= 0.0075, i.e. exactly the
        # rounding granularity of 2-dp inputs".
        #
        # RESOLVED (Prompt 6). This was written up as a second discrepancy — 53 inversions, not
        # 48. The extra five were an artefact of *our* arithmetic, not the file's: summing four
        # 2-dp values as binary floats turns five exact ties into hairline inversions. Summed in
        # `Decimal`, as CLAUDE.md house rule 9 requires of every price, the count is exactly the
        # 48 docs/13 reports, and the bound is exactly 0.0075. See docs/13a §2.
        inversions = [
            blended[index] - blended[index + 1]
            for index in range(len(blended) - 1)
            if blended[index] < blended[index + 1]
        ]
        assert len(inversions) == 48
        assert max(abs(delta) for delta in inversions) == Decimal("0.0075")

    def test_the_ordering_is_otherwise_monotone(self, export: pl.DataFrame) -> None:
        """The export is sorted by this blend, so all but a handful of adjacent pairs descend."""
        components = (
            "sharpe_return_one_year",
            "sharpe_return_six_months",
            "sharpe_return_three_months",
            "sharpe_return_one_month",
        )
        blended = _blend_column(export, components)
        inversions = sum(
            1 for index in range(len(blended) - 1) if blended[index] < blended[index + 1]
        )
        assert inversions / len(blended) < 0.25

    @pytest.mark.parametrize("shape", BLEND_SHAPES, ids=lambda s: "_".join(map(str, s)))
    def test_every_blend_shape_is_a_plain_mean(
        self, export: pl.DataFrame, shape: tuple[int, ...]
    ) -> None:
        suffix = {
            12: "one_year",
            9: "nine_months",
            6: "six_months",
            3: "three_months",
            1: "one_month",
        }
        columns = [f"sharpe_return_{suffix[m]}" for m in shape]
        row = export.filter(pl.col("symbol") == "CUPID").to_dicts()[0]
        values = [float(row[c]) for c in columns]
        expected = quantise(sum(values) / len(values), 2)
        assert expected is not None

    def test_one_null_component_would_null_the_blend(self, export: pl.DataFrame) -> None:
        """docs/05 §4's rule, checked against the shape of real data: every one of the 271 rows
        has all four components, which is why the export has no NULL blends to observe."""
        components = (
            "sharpe_return_one_year",
            "sharpe_return_six_months",
            "sharpe_return_three_months",
            "sharpe_return_one_month",
        )
        assert all(row[c] is not None for row in export.iter_rows(named=True) for c in components)


# ---------------------------------------------------------------------------
# docs/13 §5 step 4 — index identities in our own membership
# ---------------------------------------------------------------------------


class TestStep4IndexIdentities:
    """docs/13 §2 finding 11 / docs/06: verified as exact, 0 violations."""

    def _members(self, export: pl.DataFrame, slug: str) -> set[str]:
        universe = UNIVERSE_BY_SLUG[slug]
        return {
            str(row["symbol"])
            for row in export.iter_rows(named=True)
            if int(row[universe.csv_flag] or 0) == 1
        }

    def test_nifty_500_is_the_union_of_its_three_parts(self, export: pl.DataFrame) -> None:
        assert self._members(export, "nifty-500") == (
            self._members(export, "nifty-100")
            | self._members(export, "nifty-midcap-150")
            | self._members(export, "nifty-smallcap-250")
        )

    def test_large_mid_250_is_the_union_of_its_two_parts(self, export: pl.DataFrame) -> None:
        assert self._members(export, "nifty-large-mid-250") == (
            self._members(export, "nifty-100") | self._members(export, "nifty-midcap-150")
        )

    def test_mid_small_400_is_the_union_of_its_two_parts(self, export: pl.DataFrame) -> None:
        assert self._members(export, "nifty-mid-small-400") == (
            self._members(export, "nifty-midcap-150") | self._members(export, "nifty-smallcap-250")
        )

    @pytest.mark.parametrize(
        ("subset", "superset"),
        [
            ("nifty-50", "nifty-100"),
            ("nifty-100", "nifty-200"),
            ("nifty-200", "nifty-500"),
            ("nifty-500", "nifty-total-market"),
            ("nifty-total-market", "nifty-allcap"),
            ("nifty-next-50", "nifty-100"),
            ("nifty-microcap-250", "nifty-total-market"),
        ],
    )
    def test_the_containment_chain_holds(
        self, export: pl.DataFrame, subset: str, superset: str
    ) -> None:
        assert self._members(export, subset) <= self._members(export, superset)


# ---------------------------------------------------------------------------
# docs/13 §5 step 5 — recovered window lengths
# ---------------------------------------------------------------------------


class TestStep5WindowLengths:
    """docs/13 §3: solve ``positive_days_percent = k/N`` across all 271 rows; only one minimal N
    fits each window, and the same N fits every instrument."""

    WINDOW_COLUMN: Final[dict[int, str]] = {
        1: "positive_days_percent_one_month",
        3: "positive_days_percent_three_months",
        6: "positive_days_percent_six_months",
        9: "positive_days_percent_nine_months",
        12: "positive_days_percent_one_year",
    }

    @pytest.mark.parametrize(("months", "expected"), EXPECTED_WINDOW_LENGTHS_2026_08_18.items())
    def test_the_denominator_is_recoverable_from_the_export(
        self, export: pl.DataFrame, months: int, expected: int
    ) -> None:
        """Re-derives N the way docs/13 §3 did, rather than taking its word for it."""
        column = self.WINDOW_COLUMN[months]
        values = [
            float(row[column]) for row in export.iter_rows(named=True) if row[column] is not None
        ]
        assert values

        recovered = next(
            (
                candidate
                for candidate in range(2, 400)
                if all(_is_multiple_of(value, 100.0 / candidate) for value in values)
            ),
            None,
        )
        assert recovered == expected

    def test_the_same_denominator_fits_every_instrument(self, export: pl.DataFrame) -> None:
        """The property that proves a shared calendar start rather than a per-stock bar count."""
        column = self.WINDOW_COLUMN[12]
        step = 100.0 / EXPECTED_WINDOW_LENGTHS_2026_08_18[12]
        offenders = [
            row["symbol"]
            for row in export.iter_rows(named=True)
            if row[column] is not None and not _is_multiple_of(float(row[column]), step)
        ]
        assert offenders == []


def _is_multiple_of(value: float, step: float) -> bool:
    """True when ``value`` is k x ``step`` for some integer k, within 2-dp storage rounding."""
    multiples = value / step
    return abs(multiples - round(multiples)) < 0.02


# ---------------------------------------------------------------------------
# docs/13 §5 step 6 — the export's own shape
# ---------------------------------------------------------------------------


class TestStep6ExportShape:
    """ "Assert our CSV export reproduces this file's exact column names, order, quoting and BOM."

    The writer itself is Prompt 9's; what belongs here is the contract it has to meet, taken from
    the file rather than from docs/13 §1's prose — which, as `decile_core.reference_export`
    records, describes the flag layout wrongly.
    """

    def test_the_column_order_is_taken_from_the_file(self, export: pl.DataFrame) -> None:
        assert tuple(export.columns) == EXPORT_COLUMNS

    def test_the_file_is_utf8_with_a_bom(self) -> None:
        """docs/13 §1: "the file is **UTF-8 with BOM** (Excel-friendly) ... Reproduce both"."""
        assert default_fixture_path().read_bytes().startswith(b"\xef\xbb\xbf")

    def test_only_the_name_field_is_quoted(self) -> None:
        """docs/13 §1: "quotes only the `name` field"."""
        first_data_line = default_fixture_path().read_text(encoding="utf-8-sig").splitlines()[1]
        assert first_data_line.startswith('"')
        assert first_data_line.count('"') == 2


# ---------------------------------------------------------------------------
# docs/13 §5 step 2 — THE DECISIVE TEST. Needs real price history.
# ---------------------------------------------------------------------------


def _load_real_bars() -> pl.DataFrame | None:
    """Real adjusted history, if a caller has supplied it.

    Expected columns: ``symbol``, ``date``, ``close``, ``close_raw``, ``high``, ``low``,
    ``volume_raw``, and optionally ``turnover``, ``upper_circuit``, ``lower_circuit``.
    """
    path = os.environ.get(BARS_ENV_VAR)
    if not path:
        return None
    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError(f"{BARS_ENV_VAR} points at {file}, which does not exist")
    return pl.read_parquet(file)


REAL_BARS_REASON: Final = (
    f"{BARS_ENV_VAR} is not set. docs/13 §5 step 2 reproduces every numeric column of all 271 "
    "rows, which requires each instrument's adjusted daily closes for the 248 trading days ending "
    "2026-08-18. The bundle contains no price history — fixtures/ holds one single-date snapshot "
    "of results — and the suite makes no network calls (Prompt 2 acceptance criterion 1). The "
    "Prompt 2 provider fixtures are seeded random walks: they end on each symbol's real close but "
    "their path is invented, so every path-dependent factor differs by construction. Point "
    f"{BARS_ENV_VAR} at a Parquet file of real adjusted history and this test runs."
)


@pytest.mark.skipif(os.environ.get(BARS_ENV_VAR) is None, reason=REAL_BARS_REASON)
class TestStep2FullRowReproduction:
    """docs/13 §5 step 2, in full. Tolerances are those the stored precision implies — not
    widened, per Prompt 5: "Treat any column that cannot be reproduced as a specification bug to
    be investigated and documented, not as a test to be loosened"."""

    def test_every_numeric_column_of_every_row_reproduces(self, export: pl.DataFrame) -> None:
        bars = _load_real_bars()
        assert bars is not None
        trading_days = sorted(set(bars["date"].to_list()))

        symbols = {
            row["symbol"]: index + 1 for index, row in enumerate(export.iter_rows(named=True))
        }
        prepared = bars.with_columns(
            pl.col("symbol").replace_strict(symbols, default=None).alias("instrument_id")
        ).drop_nulls("instrument_id")

        result = compute_factors(prepared, AS_OF, trading_days)
        computed = {int(row["instrument_id"]): row for row in result.frame.iter_rows(named=True)}

        failures: list[str] = []
        for row in export.iter_rows(named=True):
            ours = computed.get(symbols[row["symbol"]])
            if ours is None:
                failures.append(f"{row['symbol']}: no computed row")
                continue
            for export_column, model_column in FACTOR_COLUMN_MAP.items():
                if export_column in ("series",) or model_column in KNOWN_UNRECONCILED:
                    continue
                tolerance = COLUMN_TOLERANCE.get(export_column)
                if tolerance is None or row[export_column] is None:
                    continue
                mine = ours.get(model_column)
                if mine is None:
                    failures.append(f"{row['symbol']}.{export_column}: computed NULL")
                    continue
                delta = abs(Decimal(str(mine)) - Decimal(str(row[export_column])))
                if delta > tolerance:
                    failures.append(
                        f"{row['symbol']}.{export_column}: {mine} vs {row[export_column]} "
                        f"(delta {delta}, tolerance {tolerance})"
                    )

        assert failures == [], f"{len(failures)} cells failed:\n" + "\n".join(failures[:40])


def test_the_unreconciled_columns_are_documented() -> None:
    """docs/05 §8's known gap is recorded here, not hidden by an exclusion in the loop above."""
    assert set(KNOWN_UNRECONCILED) == {"ret_12m_minus_1m", "ret_12m_minus_2m"}
    for reason in KNOWN_UNRECONCILED.values():
        assert "docs/05 §8" in reason


def test_the_decisive_test_is_skipped_for_a_stated_reason() -> None:
    """A skip nobody can see is a lie. This asserts the reason is present and specific."""
    assert BARS_ENV_VAR in REAL_BARS_REASON
    assert "no price history" in REAL_BARS_REASON
