"""Cross-validation: Polars engine vs the naive pandas oracle (Prompt 19 §3).

    "A cross-validation harness: pick 25 real instruments, compute every factor with an
     independent, deliberately naive pandas implementation, and assert agreement with the Polars
     implementation to 4 decimal places. Keep both implementations; the naive one is the oracle."

Read ``factor_oracle.py`` for what makes the oracle independent and ``factor_corpus.py`` for what
"real instruments" means when the repository holds no real price history.

THE 4-DECIMAL-PLACE TOLERANCE, AND WHERE IT IS NOT 4 DP
-------------------------------------------------------
Prompt 19 says "to 4 decimal places", which is an absolute tolerance and the right one for the
factors docs/05 states in percent or as a ratio: returns, volatility, sharpe, RSI, beta, positive
days, distance from high, moving averages, circuit counts.

It is the wrong shape for the two families whose values are *rupees*: ``vol_avg_*`` and
``median_vol_12m`` run to 10^10, where the last representable bit of a float64 is already
~2 x 10^-6 and a 252-term summation done in two different orders cannot agree to 10^-4. Those are
compared at a **relative** 1e-12 — roughly a thousand times tighter than 4 dp would be in the
units that actually matter, and stated here rather than buried in a dict.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Final

import pandas as pd
import polars as pl
import pytest
from factor_corpus import (
    AS_OF,
    CORPUS_SIZE,
    corpus_benchmark,
    corpus_calendar,
    corpus_frame,
    corpus_pandas,
)
from factor_oracle import blend_pandas, compute_factors_pandas, to_float_or_none

from baskfy_core.blends import BLEND_SHAPES, SHARPE_ONLY_SHAPES, blend_expr, shape_suffix
from baskfy_core.factors import compute_factors, compute_factors_unrounded
from baskfy_core.precision import apply_storage_precision
from baskfy_core.windows import VOL_AVG_BARS, WINDOW_MONTHS, window_lengths

#: Prompt 19 §3's tolerance, applied to every factor whose unit is a percent, a ratio or a count.
ABSOLUTE_TOLERANCE: Final = 1e-4

#: The rupee-valued factors, compared relatively. See the module docstring.
RUPEE_COLUMNS: Final[tuple[str, ...]] = (
    *(f"vol_avg_{label}" for label in VOL_AVG_BARS),
    "median_vol_12m",
)
RELATIVE_TOLERANCE: Final = 1e-12


def _window_columns() -> list[str]:
    columns: list[str] = []
    for months in WINDOW_MONTHS:
        key = f"{months}m"
        columns += [f"ret_{key}", f"vol_{key}", f"sharpe_{key}", f"pos_days_{key}"]
        columns += [f"rsi_{key}", f"circuits_{key}"]
    return columns


#: Every docs/05 factor the oracle covers. `regime` is excluded and the reason is in
#: ``factor_oracle``'s docstring — it is a design, not a formula, so a pandas copy of it would
#: prove nothing.
CROSS_VALIDATED_COLUMNS: Final[tuple[str, ...]] = (
    *_window_columns(),
    "ma_20",
    "ma_50",
    "ma_100",
    "ma_200",
    "high_1y",
    "high_ath",
    "away_high_1y",
    "away_high_ath",
    "ret_12m_minus_1m",
    "ret_12m_minus_2m",
    *RUPEE_COLUMNS,
    "beta_12m",
)

#: docs/05 §7's two ratios. Not columns of ``compute_factors`` — docs/04 keeps them out of
#: ``factor_daily`` and the registry derives them in SQL — so they are cross-validated the same
#: way the blends are: derived from the engine's own output, compared against the oracle's.
BETA_SCALED_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("abs_div_beta_12m", "ret_12m"),
    ("sharpe_div_beta_12m", "sharpe_12m"),
)


#: ``(engine, oracle)``, each keyed by instrument id then by factor name.
Comparison = tuple[dict[int, dict[str, float | None]], dict[int, dict[str, float | None]]]


@pytest.fixture(scope="module")
def cross_validation() -> Comparison:
    """Run both implementations once, and index each by instrument."""
    frame = corpus_frame()
    calendar = corpus_calendar(frame)
    benchmark = corpus_benchmark(calendar)
    lengths = window_lengths(AS_OF, calendar)

    # The engine rounds to storage precision on the way out (CLAUDE.md house rule 8), which would
    # make a 4-dp comparison a comparison of two roundings. `raw` is the unrounded frame.
    engine = compute_factors_unrounded(frame, AS_OF, calendar, benchmark=benchmark)
    raw = engine.frame

    oracle = compute_factors_pandas(
        corpus_pandas(frame), AS_OF, lengths, benchmark=corpus_pandas(benchmark)
    )

    ours: dict[int, dict[str, float | None]] = {}
    for row in raw.iter_rows(named=True):
        values = {column: to_float_or_none(row.get(column)) for column in CROSS_VALIDATED_COLUMNS}
        # docs/05 §7: "beta <= 0 -> NULL (the ratio is meaningless and would invert the ranking)".
        beta = values["beta_12m"]
        for name, source in BETA_SCALED_COLUMNS:
            numerator = values[source]
            values[name] = (
                numerator / beta
                if beta is not None and beta > 0 and numerator is not None
                else None
            )
        ours[int(row["instrument_id"])] = values

    theirs: dict[int, dict[str, float | None]] = {}
    for _, oracle_row in oracle.iterrows():
        theirs[int(oracle_row["instrument_id"])] = {
            column: to_float_or_none(oracle_row.get(column))
            for column in (*CROSS_VALIDATED_COLUMNS, *(n for n, _ in BETA_SCALED_COLUMNS))
        }
    return ours, theirs


def _disagreement(column: str, mine: float | None, theirs: float | None) -> str | None:
    if mine is None and theirs is None:
        return None
    if (mine is None) != (theirs is None):
        return f"{column}: engine={mine!r} oracle={theirs!r} (one is NULL, the other is not)"
    assert mine is not None and theirs is not None
    if column in RUPEE_COLUMNS:
        scale = max(abs(mine), abs(theirs), 1.0)
        if abs(mine - theirs) > RELATIVE_TOLERANCE * scale:
            relative = abs(mine - theirs) / scale
            return f"{column}: engine={mine!r} oracle={theirs!r} (relative {relative:.3e})"
        return None
    if not math.isclose(mine, theirs, abs_tol=ABSOLUTE_TOLERANCE, rel_tol=0.0):
        return f"{column}: engine={mine!r} oracle={theirs!r} (absolute {abs(mine - theirs):.3e})"
    return None


class TestCrossValidation:
    def test_the_corpus_is_twenty_five_real_symbols(self) -> None:
        frame = corpus_frame()
        assert frame["instrument_id"].n_unique() == CORPUS_SIZE
        assert frame["symbol"].n_unique() == CORPUS_SIZE
        # Real symbols, from the docs/13 export by way of the Prompt 2 fixtures.
        assert "CUPID" in frame["symbol"].to_list()

    def test_every_factor_agrees_to_four_decimal_places(
        self,
        cross_validation: Comparison,
    ) -> None:
        ours, theirs = cross_validation
        assert set(ours) == set(theirs), "the two implementations disagree on which rows exist"

        failures: list[str] = []
        comparable = (*CROSS_VALIDATED_COLUMNS, *(n for n, _ in BETA_SCALED_COLUMNS))
        for instrument_id in sorted(ours):
            for column in comparable:
                message = _disagreement(
                    column, ours[instrument_id][column], theirs[instrument_id][column]
                )
                if message is not None:
                    failures.append(f"instrument {instrument_id}: {message}")
        assert not failures, "\n".join(failures[:40])

    def test_the_comparison_is_not_vacuous(
        self,
        cross_validation: Comparison,
    ) -> None:
        """A harness that compares NULL to NULL 25 times would also pass. This says it does not."""
        ours, _ = cross_validation
        populated = {
            column
            for values in ours.values()
            for column, value in values.items()
            if value is not None
        }
        expected = {*CROSS_VALIDATED_COLUMNS, *(n for n, _ in BETA_SCALED_COLUMNS)}
        missing = sorted(expected - populated)
        assert not missing, f"these columns were NULL for all 25 instruments: {missing}"

    def test_circuit_detection_fired_on_both_paths(
        self,
        cross_validation: Comparison,
    ) -> None:
        """docs/05 §12 has two branches; a corpus that only exercises one proves half a rule."""
        ours, _ = cross_validation
        counts = [values["circuits_12m"] for values in ours.values()]
        assert any(c is not None and c > 0 for c in counts), (
            "no instrument in the corpus recorded a circuit hit — the §12 comparison is vacuous"
        )

    def test_blends_agree_with_the_oracle(
        self,
        cross_validation: Comparison,
    ) -> None:
        """docs/05 §4 — computed by ``blend_expr`` on one side, by a Python mean on the other."""
        ours, theirs = cross_validation
        instruments = sorted(ours)
        engine_frame = pl.DataFrame(
            [{"instrument_id": i, **ours[i]} for i in instruments], strict=False
        )

        failures: list[str] = []
        for family in ("ret", "sharpe", "rsi"):
            shapes = BLEND_SHAPES + (SHARPE_ONLY_SHAPES if family == "sharpe" else ())
            for shape in shapes:
                components = [f"{family}_{m}m" for m in shape]
                name = f"avg_{family}_{shape_suffix(shape)}"
                computed = engine_frame.select("instrument_id", blend_expr(components).alias(name))
                for row in computed.iter_rows(named=True):
                    instrument_id = int(row["instrument_id"])
                    expected = blend_pandas(pd.Series(theirs[instrument_id]), family, shape)
                    message = _disagreement(
                        name, to_float_or_none(row[name]), to_float_or_none(expected)
                    )
                    if message is not None:
                        failures.append(f"instrument {instrument_id}: {message}")
        assert not failures, "\n".join(failures[:40])

    def test_beta_scaled_factors_respect_the_non_positive_beta_rule(
        self,
        cross_validation: Comparison,
    ) -> None:
        """docs/05 §7: "beta <= 0 -> NULL (the ratio ... would invert the ranking)"."""
        ours, _ = cross_validation
        for values in ours.values():
            beta = values["beta_12m"]
            if beta is not None and beta <= 0:
                for name, _source in BETA_SCALED_COLUMNS:
                    assert values[name] is None


class TestPrecisionSeam:
    """``compute_factors_unrounded`` is a harness affordance, not a production one."""

    def test_the_default_still_rounds_at_write_time(self) -> None:
        frame = corpus_frame()
        calendar = corpus_calendar(frame)
        rounded = compute_factors(frame, AS_OF, calendar).frame
        for value in rounded["ret_12m"].drop_nulls().to_list():
            scaled = float(value) * 100
            assert abs(scaled - round(scaled)) < 1e-6, f"{value} is not stored at 2 dp"

    def test_no_source_file_switches_the_rounding_off(self) -> None:
        """CLAUDE.md house rule 8. A writer that skipped rounding would break the CSV/API/UI tie."""
        root = Path(__file__).resolve().parents[3]
        trees = (
            "packages/core/src",
            "packages/providers/src",
            "services/api/src",
            "services/worker/src",
        )
        offenders: list[str] = []
        for tree in trees:
            for path in (root / tree).rglob("*.py"):
                if path.name == "factors.py":
                    continue  # where it is defined
                if "compute_factors_unrounded" in path.read_text():
                    offenders.append(str(path.relative_to(root)))
        assert not offenders, f"these write paths bypass storage precision: {offenders}"

    def test_the_two_implementations_agree_on_the_stored_row(self) -> None:
        """The strongest form of the comparison: identical rows *after* rounding, not just close.

        4-dp agreement on unrounded values is the criterion Prompt 19 states; this is what it
        buys — the row the worker would INSERT is the row the oracle would.
        """
        frame = corpus_frame()
        calendar = corpus_calendar(frame)
        benchmark = corpus_benchmark(calendar)
        lengths = window_lengths(AS_OF, calendar)

        stored = compute_factors(frame, AS_OF, calendar, benchmark=benchmark).frame
        oracle = compute_factors_pandas(
            corpus_pandas(frame), AS_OF, lengths, benchmark=corpus_pandas(benchmark)
        )
        oracle_rounded = apply_storage_precision(
            pl.DataFrame(oracle.to_dict(orient="records"), strict=False)
        )

        by_id = {int(r["instrument_id"]): r for r in oracle_rounded.iter_rows(named=True)}
        mismatches: list[str] = []
        for row in stored.iter_rows(named=True):
            theirs = by_id[int(row["instrument_id"])]
            for column in CROSS_VALIDATED_COLUMNS:
                mine = to_float_or_none(row.get(column))
                other = to_float_or_none(theirs.get(column))
                if mine != other:
                    mismatches.append(
                        f"instrument {row['instrument_id']} {column}: {mine!r} != {other!r}"
                    )
        assert not mismatches, "\n".join(mismatches[:20])
