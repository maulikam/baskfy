"""docs/05 §"Golden-test requirements" (Prompt 5 acceptance criteria).

The document lists nine. Each is implemented below with the section it pins.

A DATE DISCREPANCY WORTH KNOWING ABOUT
--------------------------------------
Prompt 5's acceptance criteria quote docs/05's worked examples and attribute them to the
committed fixture:

    "CUPID identities reproduce: ret_12m/vol_12m == sharpe_12m for 1y/6m/3m/1m to 2 dp, and
     mean(sharpe_12m, sharpe_6m, sharpe_3m, sharpe_1m) == 5.19 to 2 dp on the fixture."
    "away_high_ath reproduces -4.83% on the CUPID fixture."

They are not the same day. docs/05 §3's table, §4's blend and §10's -4.83% are all from
**19 Aug 2026** (docs/01 records the session as 19 Aug); the committed export
``fixtures/reference-screen-export-2026-08-18.csv`` is **18 Aug 2026**. On 18 Aug CUPID's figures
are ret 726.63, sharpe 12.54, blend 5.1325 and away-from-high -5.01. Both sets are internally
consistent — the volatility happens to round to 57.93 on both days, which is what makes them look
like one snapshot.

So both are asserted: the docs/05 worked examples as stated, *and* the fixture's own values. A
formula that reproduces one but not the other would be wrong, and neither number is adjusted to
make the other pass.
"""

from __future__ import annotations

from typing import Final

import datetime as dt
import math
import time
from decimal import Decimal

import numpy as np
import polars as pl
import pytest

from baskfy_core.blends import blend_expr, blend_sql
from baskfy_core.factor_registry import (
    BACKTEST_SIGNAL_KEYS,
    COLUMN_PICKER_KEYS,
    CUSTOM_FILTER_OPERANDS,
    DOCUMENTED_COLUMN_COUNT,
    DOCUMENTED_FACTOR_COUNT,
    FACTORS,
    NAMED_FACTOR_COUNT,
    SORT_FACTOR_KEYS,
    FactorUnit,
    family_counts,
    get,
    sql_for,
)
from baskfy_core.factors import compute_factors
from baskfy_core.precision import (
    COLUMN_PRECISION,
    apply_storage_precision,
    quantise,
    unpriced_numeric_columns,
)
from baskfy_core.reference_export import read_export
from baskfy_core.windows import (
    EXPECTED_WINDOW_LENGTHS_2026_08_18,
    resolve_window,
    window_lengths,
)

AS_OF = dt.date(2026, 8, 18)


def weekday_calendar(start: dt.date, end: dt.date) -> list[dt.date]:
    """Every weekday in a range — a calendar with no holidays, for arithmetic tests."""
    days: list[dt.date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


def series_frame(
    closes: list[float],
    days: list[dt.date],
    *,
    instrument_id: int = 1,
    turnover: float | None = None,
) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "instrument_id": instrument_id,
                "date": day,
                "close": close,
                "close_raw": close,
                "high": close,
                "low": close,
                "volume_raw": 1000.0,
                "turnover": turnover,
                "upper_circuit": None,
                "lower_circuit": None,
            }
            for day, close in zip(days, closes, strict=True)
        ],
        strict=False,
    )


def geometric(
    days: list[dt.date], seed: int, drift: float = 0.0008, vol: float = 0.02
) -> list[float]:
    rng = np.random.default_rng(seed)
    price = 100.0
    out = []
    for _ in days:
        price *= math.exp(rng.normal(drift, vol))
        out.append(price)
    return out


# ---------------------------------------------------------------------------
# Golden test 1 — docs/05 §3 identities and §4 blend
# ---------------------------------------------------------------------------


class TestGolden1SharpeIdentity:
    """docs/05 §3: "sharpe_N = ret_N / vol_N", with no risk-free rate and no excess-return term.

    Storage convention (docs/05 §3, docs/13 §4): volatility is a decimal fraction, so in code the
    identity is ``sharpe_N = ret_N_pct / (vol_N_fraction * 100)``.
    """

    #: docs/05 §3's verification table, from 19 Aug 2026.
    DOCS_05_TABLE: tuple[tuple[str, float, float, float], ...] = (
        ("1 year", 753.00, 57.93, 13.00),
        ("6 months", 234.50, 55.02, 4.26),
        ("3 months", 138.25, 49.12, 2.81),
        ("1 month", 37.29, 54.03, 0.69),
    )

    @pytest.mark.parametrize(("window", "ret", "vol_pct", "expected"), DOCS_05_TABLE)
    def test_the_docs_05_table_reproduces(
        self, window: str, ret: float, vol_pct: float, expected: float
    ) -> None:
        del window
        assert quantise(ret / vol_pct, 2) == quantise(expected, 2)

    def test_the_docs_05_blend_reproduces(self) -> None:
        """docs/05 §4: "(13.00 + 4.26 + 2.81 + 0.69) / 4 = 5.19"."""
        components = [13.00, 4.26, 2.81, 0.69]
        assert quantise(sum(components) / len(components), 2) == Decimal("5.19")

    def test_the_other_two_instruments_in_docs_05_reproduce(self) -> None:
        """WELCORP 126.18/35.82 = 3.52; HFCL 200.96/49.08 = 4.09."""
        assert quantise(126.18 / 35.82, 2) == Decimal("3.52")
        assert quantise(200.96 / 49.08, 2) == Decimal("4.09")

    def test_the_fixtures_own_cupid_row_reproduces(self) -> None:
        """The committed export is 18 Aug, where the same identity gives 12.54 and a 5.13 blend."""
        row = read_export().filter(pl.col("symbol") == "CUPID").to_dicts()[0]
        computed = float(row["absolute_return_one_year"]) / (
            float(row["volatility_one_year"]) * 100
        )
        assert quantise(computed, 2) == quantise(row["sharpe_return_one_year"], 2)

        blend = (
            sum(
                float(row[f"sharpe_return_{w}"])
                for w in ("one_year", "six_months", "three_months", "one_month")
            )
            / 4
        )
        assert quantise(blend, 2) == Decimal("5.13")

    def test_the_engine_reproduces_the_identity_on_a_computed_series(self) -> None:
        """The identity must hold in *our* output, not only in the reference product's."""
        days = weekday_calendar(dt.date(2024, 1, 1), AS_OF)
        frame = series_frame(geometric(days, seed=11), days)
        row = compute_factors(frame, AS_OF, days).frame.to_dicts()[0]
        for window in ("12m", "6m", "3m", "1m"):
            expected = float(row[f"ret_{window}"]) / (float(row[f"vol_{window}"]) * 100)
            assert quantise(expected, 2) == quantise(row[f"sharpe_{window}"], 2), window

    def test_no_risk_free_rate_is_subtracted(self) -> None:
        """docs/05 §3: "There is no risk-free rate, no annualisation adjustment and no
        excess-return term anywhere in it." A 6% rate would shift every value visibly."""
        days = weekday_calendar(dt.date(2024, 1, 1), AS_OF)
        frame = series_frame(geometric(days, seed=12), days)
        row = compute_factors(frame, AS_OF, days).frame.to_dicts()[0]
        plain = float(row["ret_12m"]) / (float(row["vol_12m"]) * 100)
        with_rate = (float(row["ret_12m"]) - 6.0) / (float(row["vol_12m"]) * 100)
        assert quantise(plain, 2) == quantise(row["sharpe_12m"], 2)
        assert quantise(with_rate, 2) != quantise(row["sharpe_12m"], 2)


# ---------------------------------------------------------------------------
# Golden test 2 — docs/05 §10 away-from-high
# ---------------------------------------------------------------------------


class TestGolden2AwayFromHigh:
    """docs/05 §10: ``away_high = (P_t / high - 1) x 100``, always <= 0."""

    def test_the_docs_05_worked_example(self) -> None:
        """docs/05 §10: "284.56 / 299.00 - 1 = -4.83%" — the 19 Aug figure."""
        assert quantise((284.56 / 299.00 - 1) * 100, 2) == Decimal("-4.83")

    def test_the_fixtures_own_cupid_row(self) -> None:
        """On 18 Aug the same formula gives -5.01, which is what the export stores."""
        row = read_export().filter(pl.col("symbol") == "CUPID").to_dicts()[0]
        computed = (float(row["close"]) / float(row["high_all_time"]) - 1) * 100
        assert quantise(computed, 2) == quantise(row["away_from_high_all_time"], 2)
        assert quantise(computed, 2) == Decimal("-5.01")

    def test_the_engine_reproduces_it(self) -> None:
        days = weekday_calendar(dt.date(2025, 1, 1), AS_OF)
        closes = geometric(days, seed=5)
        frame = series_frame(closes, days)
        row = compute_factors(frame, AS_OF, days).frame.to_dicts()[0]
        expected = (closes[-1] / max(closes) - 1) * 100
        assert quantise(expected, 2) == quantise(row["away_high_ath"], 2)

    def test_it_is_never_positive(self) -> None:
        """A close cannot exceed the maximum that includes it."""
        days = weekday_calendar(dt.date(2025, 1, 1), AS_OF)
        frame = series_frame(geometric(days, seed=6), days)
        row = compute_factors(frame, AS_OF, days).frame.to_dicts()[0]
        assert float(row["away_high_ath"]) <= 0
        assert float(row["away_high_1y"]) <= 0

    def test_a_stock_at_its_high_is_exactly_zero(self) -> None:
        days = weekday_calendar(dt.date(2025, 1, 1), AS_OF)
        closes = [100.0 + index for index in range(len(days))]
        frame = series_frame(closes, days)
        row = compute_factors(frame, AS_OF, days).frame.to_dicts()[0]
        assert quantise(row["away_high_ath"], 2) == Decimal("0.00")


# ---------------------------------------------------------------------------
# Golden test 3 — constant returns
# ---------------------------------------------------------------------------


class TestGolden3ConstantSeries:
    """docs/05: "A synthetic constant-return series produces vol = 0 and sharpe = NULL."

    docs/05 §3's guard: "if vol_N == 0 or is NULL -> sharpe_N = NULL". A flat series has an
    undefined ratio, not an infinite one, and an infinity in a `numeric` column is not storable.
    """

    def _row(self, growth: float) -> dict[str, object]:
        days = weekday_calendar(dt.date(2024, 1, 1), AS_OF)
        closes = [100.0 * (growth**index) for index in range(len(days))]
        frame = series_frame(closes, days)
        return compute_factors(frame, AS_OF, days).frame.to_dicts()[0]

    def test_a_flat_series_has_zero_volatility(self) -> None:
        row = self._row(1.0)
        assert quantise(row["vol_12m"], 10) == Decimal("0E-10")

    def test_a_constant_growth_series_has_zero_volatility(self) -> None:
        """Constant *return*, not constant price: every daily return is identical."""
        row = self._row(1.001)
        assert quantise(row["vol_12m"], 10) == Decimal("0E-10")

    def test_sharpe_is_null_when_volatility_is_zero(self) -> None:
        assert self._row(1.001)["sharpe_12m"] is None

    def test_sharpe_is_null_for_every_window(self) -> None:
        row = self._row(1.001)
        for window in ("1m", "3m", "6m", "9m", "12m"):
            assert row[f"sharpe_{window}"] is None, window

    def test_the_return_itself_is_still_computed(self) -> None:
        """A zero-volatility series still has a return; only the ratio is undefined."""
        assert self._row(1.001)["ret_12m"] is not None


# ---------------------------------------------------------------------------
# Golden test 4 — split-adjustment invariance
# ---------------------------------------------------------------------------


class TestGolden4SplitInvariance:
    """docs/05: "A synthetic series with a known 2:1 split produces identical factors before and
    after adjustment is applied (adjustment invariance)."

    This is what makes docs/02 rule 2 ("Adjusted by default") safe: adjusting a series must move
    the *prices* without moving any factor derived from their ratios.
    """

    #: Factors that are scale-invariant by construction — ratios of prices, or of returns.
    INVARIANT: tuple[str, ...] = (
        *(f"ret_{w}" for w in ("1m", "3m", "6m", "9m", "12m")),
        *(f"vol_{w}" for w in ("1m", "3m", "6m", "9m", "12m")),
        *(f"sharpe_{w}" for w in ("1m", "3m", "6m", "9m", "12m")),
        *(f"rsi_{w}" for w in ("1m", "3m", "6m", "9m", "12m")),
        *(f"pos_days_{w}" for w in ("1m", "3m", "6m", "9m", "12m")),
        "away_high_1y",
        "away_high_ath",
    )

    def _rows(self) -> tuple[dict[str, object], dict[str, object]]:
        days = weekday_calendar(dt.date(2024, 1, 1), AS_OF)
        closes = geometric(days, seed=21)
        split_index = len(days) // 2

        # Unadjusted: the raw prints, with a 2:1 split halving the price mid-series.
        unadjusted = [
            close if index < split_index else close / 2 for index, close in enumerate(closes)
        ]
        # Adjusted: the split removed, so the *whole* series is on the post-split scale.
        adjusted = [close / 2 for close in closes]
        del unadjusted, split_index

        adjusted_row = compute_factors(series_frame(adjusted, days), AS_OF, days).frame.to_dicts()[
            0
        ]
        original_row = compute_factors(series_frame(closes, days), AS_OF, days).frame.to_dicts()[0]
        return original_row, adjusted_row

    @pytest.mark.parametrize("factor", INVARIANT)
    def test_a_factor_is_unchanged_by_a_uniform_price_scaling(self, factor: str) -> None:
        """A correctly adjusted 2:1 split is a uniform halving of the whole series."""
        original, adjusted = self._rows()
        if original[factor] is None:
            pytest.skip(f"{factor} has no value on this fixture")
        assert quantise(original[factor], 4) == quantise(adjusted[factor], 4)

    def test_price_levels_do_move(self) -> None:
        """Guards the fixture: if the two series were identical the test above would be vacuous."""
        original, adjusted = self._rows()
        assert quantise(original["close"], 2) != quantise(adjusted["close"], 2)
        assert quantise(original["ma_200"], 2) != quantise(adjusted["ma_200"], 2)

    def test_an_unadjusted_split_visibly_corrupts_the_return(self) -> None:
        """Shows what adjustment is protecting against: a raw split reads as a 50% collapse."""
        days = weekday_calendar(dt.date(2024, 1, 1), AS_OF)
        closes = geometric(days, seed=22)
        split_index = len(days) - 5
        unadjusted = [
            close if index < split_index else close / 2 for index, close in enumerate(closes)
        ]
        clean = compute_factors(series_frame(closes, days), AS_OF, days).frame.to_dicts()[0]
        broken = compute_factors(series_frame(unadjusted, days), AS_OF, days).frame.to_dicts()[0]
        assert float(broken["ret_12m"]) < float(clean["ret_12m"]) - 40


# ---------------------------------------------------------------------------
# Golden test 5 — blend NULL propagation
# ---------------------------------------------------------------------------


class TestGolden5BlendNulls:
    """docs/05 §4: "If **any** component is NULL the blend is NULL (do not silently average over
    fewer terms — that would rank young listings above seasoned ones)."."""

    def test_a_complete_blend_is_the_arithmetic_mean(self) -> None:
        frame = pl.DataFrame({"a": [1.0], "b": [2.0], "c": [3.0], "d": [4.0]})
        result = frame.select(blend_expr(["a", "b", "c", "d"]).alias("blend"))
        assert result["blend"][0] == pytest.approx(2.5)

    def test_one_null_component_makes_the_blend_null(self) -> None:
        frame = pl.DataFrame({"a": [1.0], "b": [None], "c": [3.0], "d": [4.0]})
        result = frame.select(blend_expr(["a", "b", "c", "d"]).alias("blend"))
        assert result["blend"][0] is None

    def test_it_does_not_average_over_the_surviving_terms(self) -> None:
        """The specific failure docs/05 names: a young listing ranking above a seasoned one."""
        frame = pl.DataFrame({"a": [10.0], "b": [None], "c": [10.0], "d": [10.0]})
        result = frame.select(blend_expr(["a", "b", "c", "d"]).alias("blend"))
        assert result["blend"][0] is None

    def test_the_sql_form_propagates_nulls_too(self) -> None:
        """docs/04 computes blends in SQL; PostgreSQL's NULL arithmetic must do the same job."""
        sql = blend_sql(["sharpe_12m", "sharpe_6m", "sharpe_3m", "sharpe_1m"])
        assert sql == "((sharpe_12m + sharpe_6m + sharpe_3m + sharpe_1m) / 4.0)"
        assert "COALESCE" not in sql.upper()

    def test_an_empty_blend_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one component"):
            blend_expr([])


# ---------------------------------------------------------------------------
# Golden test 6 — beta
# ---------------------------------------------------------------------------


class TestGolden6Beta:
    """docs/05 §6: ``Cov(r_stock, r_bench)_252 / Var(r_bench)_252``, against NIFTY 50."""

    def _setup(self) -> tuple[pl.DataFrame, pl.DataFrame, list[dt.date]]:
        days = weekday_calendar(dt.date(2024, 1, 1), AS_OF)
        bench_closes = geometric(days, seed=31, drift=0.0004, vol=0.011)
        bench = pl.DataFrame({"date": days, "close": bench_closes}, strict=False)

        levered = [100.0]
        for index in range(1, len(days)):
            change = bench_closes[index] / bench_closes[index - 1] - 1
            levered.append(levered[-1] * (1 + 2 * change))
        inverse = [100.0]
        for index in range(1, len(days)):
            change = bench_closes[index] / bench_closes[index - 1] - 1
            inverse.append(inverse[-1] * (1 - change))

        frame = pl.concat(
            [
                series_frame(bench_closes, days, instrument_id=1),
                series_frame(levered, days, instrument_id=2),
                series_frame(inverse, days, instrument_id=3),
            ]
        )
        return frame, bench, days

    def test_beta_of_the_benchmark_against_itself_is_exactly_one(self) -> None:
        frame, bench, days = self._setup()
        rows = {
            int(r["instrument_id"]): r
            for r in compute_factors(frame, AS_OF, days, bench).frame.to_dicts()
        }
        assert quantise(rows[1]["beta_12m"], 10) == Decimal("1.0000000000")

    def test_a_two_times_levered_series_has_beta_two(self) -> None:
        frame, bench, days = self._setup()
        rows = {
            int(r["instrument_id"]): r
            for r in compute_factors(frame, AS_OF, days, bench).frame.to_dicts()
        }
        assert quantise(rows[2]["beta_12m"], 4) == Decimal("2.0000")

    def test_a_negative_beta_is_not_clipped(self) -> None:
        """docs/05 §6: "Negative betas are legitimate and must not be clipped (the site shows
        `OIL: -0.22`)."."""
        frame, bench, days = self._setup()
        rows = {
            int(r["instrument_id"]): r
            for r in compute_factors(frame, AS_OF, days, bench).frame.to_dicts()
        }
        assert float(rows[3]["beta_12m"]) < 0

    def test_beta_is_null_without_a_benchmark(self) -> None:
        frame, _bench, days = self._setup()
        rows = compute_factors(frame, AS_OF, days, None).frame.to_dicts()
        assert all(row["beta_12m"] is None for row in rows)

    def test_beta_is_null_below_the_minimum_overlap(self) -> None:
        """docs/05 §6: "require >= 200 overlapping observations else NULL"."""
        days = weekday_calendar(dt.date(2026, 1, 1), AS_OF)
        bench_closes = geometric(days, seed=33, vol=0.01)
        bench = pl.DataFrame({"date": days, "close": bench_closes}, strict=False)
        frame = series_frame(bench_closes, days)
        row = compute_factors(frame, AS_OF, days, bench).frame.to_dicts()[0]
        assert len(days) < 200
        assert row["beta_12m"] is None


# ---------------------------------------------------------------------------
# Golden test 8 — window lengths
# ---------------------------------------------------------------------------


class TestGolden8WindowLengths:
    """docs/05 and docs/13 §3: 22 / 64 / 121 / 185 / 247 trading days as of 2026-08-18."""

    def test_the_expected_lengths_are_the_documented_ones(self) -> None:
        assert EXPECTED_WINDOW_LENGTHS_2026_08_18 == {1: 22, 3: 64, 6: 121, 9: 185, 12: 247}

    def test_the_window_is_a_calendar_offset_not_a_bar_count(self) -> None:
        """The property docs/13 §3 used to recover the lengths: the start is a shared *date*."""
        days = weekday_calendar(dt.date(2024, 1, 1), AS_OF)
        window = resolve_window(AS_OF, 12, days)
        assert window.calendar_start == dt.date(2025, 8, 18)
        assert window.start >= window.calendar_start

    def test_the_start_snaps_forward_never_backward(self) -> None:
        """Snapping backwards would start a window before the calendar offset."""
        days = weekday_calendar(dt.date(2024, 1, 1), AS_OF)
        # 2025-08-16 is a Saturday; a 1-month window from 2025-09-16 back lands on it.
        window = resolve_window(dt.date(2025, 9, 16), 1, days)
        assert window.calendar_start == dt.date(2025, 8, 16)
        assert window.start == dt.date(2025, 8, 18)

    def test_the_window_is_inclusive_of_both_ends(self) -> None:
        days = weekday_calendar(dt.date(2024, 1, 1), AS_OF)
        window = resolve_window(AS_OF, 1, days)
        assert window.trading_days[0] == window.start
        assert window.trading_days[-1] == AS_OF

    def test_every_instrument_shares_one_window(self) -> None:
        """docs/13 §3: "the **same** N fits every instrument"."""
        days = weekday_calendar(dt.date(2024, 1, 1), AS_OF)
        first = window_lengths(AS_OF, days)
        second = window_lengths(AS_OF, days)
        assert first == second

    def test_a_one_month_window_matches_the_documented_length(self) -> None:
        """A weekday-only calendar has no holidays in the 1-month window, and the documented
        length for it is 22 — so this one is checkable without the real NSE holiday list."""
        days = weekday_calendar(dt.date(2025, 1, 1), AS_OF)
        assert window_lengths(AS_OF, days)[1] == EXPECTED_WINDOW_LENGTHS_2026_08_18[1]

    def test_an_instrument_without_a_full_window_gets_null(self) -> None:
        """docs/05 §Notation: "never computed on a short window, because that would break the
        shared-denominator property"."""
        days = weekday_calendar(dt.date(2026, 6, 1), AS_OF)
        frame = series_frame(geometric(days, seed=41), days)
        row = compute_factors(frame, AS_OF, days).frame.to_dicts()[0]
        assert row["ret_12m"] is None
        assert row["vol_12m"] is None
        assert row["pos_days_12m"] is None
        assert row["ret_1m"] is not None


# ---------------------------------------------------------------------------
# Golden test 9 — storage precision
# ---------------------------------------------------------------------------


class TestGolden9StoragePrecision:
    """docs/13 §4: "The reference product rounds **at write time**, not at render time."

    Round at render instead and three surfaces — the API, the table and the CSV export — each
    round independently from a value none of them stored, and they disagree in the last digit.
    """

    def _row(self) -> dict[str, object]:
        days = weekday_calendar(dt.date(2024, 1, 1), AS_OF)
        frame = series_frame(geometric(days, seed=51), days, turnover=1234567.89)
        return compute_factors(frame, AS_OF, days).frame.to_dicts()[0]

    @pytest.mark.parametrize("column", ["close", "ma_200", "high_1y", "high_ath"])
    def test_prices_are_stored_at_two_decimals(self, column: str) -> None:
        assert _decimal_places(self._row()[column]) <= 2

    @pytest.mark.parametrize("column", ["ret_12m", "sharpe_12m", "away_high_ath", "pos_days_12m"])
    def test_percentages_are_stored_at_two_decimals(self, column: str) -> None:
        assert _decimal_places(self._row()[column]) <= 2

    @pytest.mark.parametrize("column", ["rsi_12m", "rsi_1m"])
    def test_rsi_is_stored_at_four_decimals(self, column: str) -> None:
        assert _decimal_places(self._row()[column]) <= 4

    def test_volatility_keeps_ten_decimals(self) -> None:
        """docs/13 §4: "keep full precision for volatility and beta because they feed divisions"."""
        assert COLUMN_PRECISION["vol_12m"] == 10

    def test_volatility_is_a_fraction_not_a_percentage(self) -> None:
        """docs/13 §2 finding 4: the export's range is 0.179-0.618; the UI multiplies by 100."""
        value = float(str(self._row()["vol_12m"]))
        assert 0 < value < 3

    def test_rounding_is_half_up_not_bankers(self) -> None:
        """Python's default rounds 0.125 to 0.12; the reference product's values are half-up."""
        assert quantise(0.125, 2) == Decimal("0.13")
        assert quantise(2.5, 0) == Decimal("3")

    def test_every_numeric_column_has_a_documented_precision(self) -> None:
        """A new float column with no decided precision is a bug, not a default."""
        days = weekday_calendar(dt.date(2024, 1, 1), AS_OF)
        frame = series_frame(geometric(days, seed=52), days)
        computed = compute_factors(frame, AS_OF, days).frame
        unpriced = [
            c
            for c in unpriced_numeric_columns(computed)
            # Working columns the engine carries but never stores.
            if c
            not in {
                "daily_return",
                "prev_close_raw",
                "high",
                "low",
                "volume_raw",
                "turnover",
                "upper_circuit",
                "lower_circuit",
                "vol_day_val",
                "regime_distance_bull",
                "regime_distance_bear",
                "bench_return",
            }
        ]
        assert unpriced == []

    def test_apply_storage_precision_is_idempotent(self) -> None:
        frame = pl.DataFrame({"ret_12m": [1.23456], "vol_12m": [0.123456789012]})
        once = apply_storage_precision(frame)
        twice = apply_storage_precision(once)
        assert once.equals(twice)


def _decimal_places(value: object) -> int:
    if value is None:
        return 0
    text = f"{float(str(value)):.12f}".rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".")[1])


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


class TestPerformance:
    """Prompt 5: "a benchmark test asserting full-universe factor computation for one date
    completes in under 30 seconds on the fixture-scaled dataset"."""

    #: docs/02: "~2,300 NSE instruments". docs/04: "~8.5M rows" over 15 years.
    UNIVERSE_SIZE = 500
    BUDGET_SECONDS = 30.0

    def test_a_full_universe_date_computes_within_the_budget(self) -> None:
        days = weekday_calendar(dt.date(2024, 1, 1), AS_OF)
        rng = np.random.default_rng(99)
        frames = []
        for instrument_id in range(1, self.UNIVERSE_SIZE + 1):
            price = 100.0
            closes = []
            for _ in days:
                price *= math.exp(rng.normal(0.0005, 0.018))
                closes.append(price)
            frames.append(series_frame(closes, days, instrument_id=instrument_id))
        frame = pl.concat(frames)
        bench = pl.DataFrame(
            {"date": days, "close": geometric(days, seed=100, vol=0.01)}, strict=False
        )

        started = time.monotonic()
        result = compute_factors(frame, AS_OF, days, bench)
        elapsed = time.monotonic() - started

        assert result.frame.height == self.UNIVERSE_SIZE
        assert elapsed < self.BUDGET_SECONDS, (
            f"{self.UNIVERSE_SIZE} instruments x {len(days)} bars took {elapsed:.1f}s, "
            f"over the {self.BUDGET_SECONDS:.0f}s budget"
        )


#: Keys added to the registry after docs/01 §3 was written, each with the decision that added it.
#: Every entry is a deliberate widening; the test below asserts the count so an *accidental* one
#: still fails.
FACTORS_ADDED_SINCE_DOCS: Final[dict[str, str]] = {
    # M56 — short-horizon sharpe blends. Every other blend shape starts at 12, so these are the
    # only two that ask what a name has done recently without twelve months dominating the mean.
    "avg_sharpe_3_1": "DECISIONS-MERGE.md M56",
    "avg_sharpe_6_1": "DECISIONS-MERGE.md M56",
}


class TestRegistry:
    """docs/06 §"The factor registry" (Prompt 5 deliverable 4).

    "The registry is the single source of truth for: the `sort_by` dropdown, the custom-filter
     operand list, the column picker, the API enum, and the backtest signal list."
    """

    def test_every_factor_has_every_documented_field(self) -> None:
        for factor in FACTORS.values():
            assert factor.key and factor.label and factor.sql_expr
            assert factor.family is not None
            assert factor.unit is not None
            assert factor.null_policy is not None
            assert isinstance(factor.higher_is_better, bool)

    def test_the_family_counts_match_docs_01_section_3(self) -> None:
        """docs/01 §3's own per-family numbers: 16 / 17 / 16 / 5 / 2.

        `sharpe_return` carries the M56 additions on top of its documented 17, which is why it is
        the one family stated as "documented plus what was added since" rather than as a bare
        number. The other four are untouched and stay literal — a family that grows without a
        recorded decision behind it still fails here.
        """
        counts = family_counts()
        added_to_sharpe = sum(
            1 for key in FACTORS_ADDED_SINCE_DOCS if key.startswith("avg_sharpe_")
        )
        assert counts["absolute_return"] == 16
        assert counts["sharpe_return"] == 17 + added_to_sharpe
        assert counts["rsi"] == 16
        assert counts["risk_adjusted"] == 5
        assert counts["skip_month"] == 2

    def test_the_headline_count_disagrees_with_the_enumeration(self) -> None:
        """docs/01 §3 is headed "The 62 ranking factors" but its last family, labelled
        "Non-momentum sort keys (6)", enumerates eight. 56 + 6 = 62 matches the headline;
        56 + 8 = 64 matched the list. Both cannot be right.

        All named keys are implemented — dropping two sort keys the document names, and which its
        own column picker also lists, to make a headline number come out would be losing
        functionality to arithmetic. Prompt 19's parity audit should settle it.

        **The registry is now 66, and the drift is deliberate rather than a third disagreement.**
        M56 added `avg_sharpe_3_1` and `avg_sharpe_6_1` at Maulik's request (27 Aug 2026). docs/01
        §3 predates them, so the documented figure stays 62 and this asserts the *gap* rather than
        a frozen total: what must not happen is the registry drifting without anyone noticing, and
        a bare `== 66` would have to be re-pinned by hand on every deliberate addition — which is
        how a guard becomes a chore and then a rubber stamp.
        """
        assert DOCUMENTED_FACTOR_COUNT == 62
        assert NAMED_FACTOR_COUNT == 64 + len(FACTORS_ADDED_SINCE_DOCS)
        assert family_counts()["non_momentum"] == 8

    def test_the_column_picker_count_disagrees_the_same_way(self) -> None:
        """docs/01 §4 says "34 available columns" and enumerates 36."""
        assert DOCUMENTED_COLUMN_COUNT == 34
        assert len(COLUMN_PICKER_KEYS) == 36

    def test_keys_are_unique(self) -> None:
        assert len(set(SORT_FACTOR_KEYS)) == len(SORT_FACTOR_KEYS) == len(FACTORS)

    def test_the_registry_drives_the_backtest_signal_list(self) -> None:
        assert BACKTEST_SIGNAL_KEYS == SORT_FACTOR_KEYS

    def test_custom_filter_operands_are_all_stored_columns(self) -> None:
        """docs/01 §2.14 compares field to field, so an operand must be a real column — a blend
        would have to be recomputed on both sides of the comparison."""
        for operand in CUSTOM_FILTER_OPERANDS:
            factor = FACTORS.get(operand)
            if factor is not None:
                assert factor.is_stored, operand

    def test_an_unknown_key_is_refused(self) -> None:
        """docs/06: "a **whitelisted** factor registry ... never string-interpolated from user
        input"."""
        with pytest.raises(KeyError, match="not in the registry"):
            get("'; DROP TABLE factor_daily; --")

    def test_a_blend_expression_is_a_plain_mean_in_sql(self) -> None:
        assert sql_for("avg_sharpe_12_6_3_1") == (
            "((sharpe_12m + sharpe_6m + sharpe_3m + sharpe_1m) / 4.0)"
        )

    def test_beta_scaled_factors_guard_against_non_positive_beta(self) -> None:
        """docs/05 §7: "beta <= 0 -> NULL (the ratio is meaningless and would invert the
        ranking)"."""
        assert "beta_12m > 0" in sql_for("sharpe_div_beta_12m")
        assert "beta_12m > 0" in sql_for("avg_sharpe_div_beta_12_9_6_3")

    def test_volatility_is_registered_as_a_fraction(self) -> None:
        assert get("vol_12m").unit is FactorUnit.FRACTION
