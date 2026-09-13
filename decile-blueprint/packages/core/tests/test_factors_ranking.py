"""The stored ranking factors, asserted against docs/ranking/PLAN.md contract C1 — not the code.

Every C1 column is pinned three ways:

* a **hand-computed** case on a series built so the answer can be written down (a flat-range ATR
  is its range; a 10% dip inside the window is a 10.00 drawdown; three +10% days and nothing else
  leave nothing once removed);
* an **oracle**: a plain-Python loop that reads C1's definition literally, run on a noisy
  multi-instrument series, compared to the engine to 1e-9 — it shares no code with
  ``baskfy_core.factors_ranking`` (no Polars, no rolling kernels);
* **NULL when the window is short**: exactly the bars the definition needs gives a value, one
  fewer gives NULL.

Plus point-in-time (a bar or benchmark level dated after the row never changes it), storage (the
precision table, the ORM model, migration 0046 and docs/04 agree on every column), and the two
worker-side functions ``cross_sectional_pctile`` and ``rank_persistence``.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Final

import numpy as np
import polars as pl
import pytest
from sqlalchemy import Numeric, SmallInteger

from baskfy_core import factors_ranking as fr
from baskfy_core.factor_registry import FACTORS
from baskfy_core.factors import compute_factors, compute_factors_unrounded
from baskfy_core.models.facts import FactorDaily
from baskfy_core.precision import COLUMN_PRECISION, INTEGER_COLUMNS, unpriced_numeric_columns
from baskfy_core.windows import resolve_window

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
AS_OF: Final = dt.date(2026, 8, 18)
START: Final = dt.date(2024, 1, 1)
TOLERANCE: Final = 1e-9

#: The C1 table's key column, typed out rather than imported: the test is the spec's copy.
C1_COLUMNS: Final[tuple[str, ...]] = (
    "atr_14",
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
    "excess_ret_3m",
    "excess_ret_6m",
    "excess_ret_12m",
    "resid_ret_12m",
    "rs_persist_126",
    "mom_pctile",
    "rank_persist_20",
    "nse_mr6",
    "nse_mr12",
)

#: C1's "numeric" column, typed out: (precision, scale), or None for smallint.
C1_TYPES: Final[dict[str, tuple[int, int] | None]] = {
    "atr_14": (18, 4),
    "atr_ext_20": (10, 4),
    "ma50_slope_20": (14, 2),
    "eff_ratio_63": (10, 4),
    "max_dd_6m": (14, 2),
    "max_dd_12m": (14, 2),
    "downside_vol_6m": (18, 10),
    "downside_vol_12m": (18, 10),
    "sortino_6m": (14, 2),
    "sortino_12m": (14, 2),
    "underwater_12m": (7, 2),
    "ret_ex_top3_12m": (14, 2),
    "accel_21_105": (18, 10),
    "accel_21_105_vs": (10, 4),
    "vol_exp_21_126": (10, 4),
    "vol_persist_20": None,
    "excess_ret_3m": (14, 2),
    "excess_ret_6m": (14, 2),
    "excess_ret_12m": (14, 2),
    "resid_ret_12m": (14, 2),
    "rs_persist_126": (7, 2),
    "mom_pctile": (7, 2),
    "rank_persist_20": (7, 2),
    "nse_mr6": (18, 10),
    "nse_mr12": (18, 10),
}


# ---------------------------------------------------------------------------
# Fixtures: a weekday calendar, bars, benchmarks
# ---------------------------------------------------------------------------


def weekdays(start: dt.date = START, end: dt.date = AS_OF) -> list[dt.date]:
    days: list[dt.date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


DAYS: Final = weekdays()
N3: Final = resolve_window(AS_OF, 3, DAYS).length
N6: Final = resolve_window(AS_OF, 6, DAYS).length
N12: Final = resolve_window(AS_OF, 12, DAYS).length


def bars_frame(  # noqa: PLR0913 - one argument per bar column a test may shape
    days: list[dt.date],
    closes: list[float],
    *,
    instrument_id: int = 1,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    volumes: list[float] | None = None,
) -> pl.DataFrame:
    """``vol_day_val`` is ``close_raw x volume_raw`` here (no exchange turnover)."""
    n = len(closes)
    return pl.DataFrame(
        {
            "instrument_id": [instrument_id] * n,
            "date": days[-n:],
            "close": closes,
            "close_raw": closes,
            "high": highs if highs is not None else closes,
            "low": lows if lows is not None else closes,
            "volume_raw": volumes if volumes is not None else [1_000.0] * n,
        },
        schema_overrides={"instrument_id": pl.Int64, "date": pl.Date},
    )


def noisy(seed: int, n: int, drift: float = 0.0006, sigma: float = 0.02) -> list[float]:
    rng = np.random.default_rng(seed)
    return [float(v) for v in 100.0 * np.exp(np.cumsum(rng.normal(drift, sigma, n)))]


def noisy_bars(seed: int, n: int, instrument_id: int = 1) -> pl.DataFrame:
    rng = np.random.default_rng(seed + 1000)
    closes = noisy(seed, n)
    spread = rng.uniform(0.002, 0.03, n)
    volumes = [float(v) for v in rng.uniform(1_000, 50_000, n)]
    return bars_frame(
        DAYS,
        closes,
        instrument_id=instrument_id,
        highs=[c * (1 + s) for c, s in zip(closes, spread, strict=True)],
        lows=[c * (1 - s * 0.8) for c, s in zip(closes, spread, strict=True)],
        volumes=volumes,
    )


def levels_frame(seed: int, days: list[dt.date], column: str = "level") -> pl.DataFrame:
    return pl.DataFrame(
        {"date": days, column: noisy(seed, len(days), drift=0.0004, sigma=0.01)},
        schema_overrides={"date": pl.Date},
    )


NIFTY_50: Final = levels_frame(50, DAYS)
NIFTY_500: Final = levels_frame(500, DAYS)


def unrounded(
    bars: pl.DataFrame,
    *,
    days: list[dt.date] = DAYS,
    as_of: dt.date = AS_OF,
    benchmark: pl.DataFrame | None = NIFTY_50,
    market: pl.DataFrame | None = NIFTY_500,
) -> dict[int, dict[str, object]]:
    frame = compute_factors_unrounded(bars, as_of, days, benchmark, market_benchmark=market).frame
    return {int(str(r["instrument_id"])): r for r in frame.iter_rows(named=True)}


def value(row: dict[str, object], column: str) -> float | None:
    raw = row[column]
    return None if raw is None else float(str(raw))


def close_to(actual: float | None, expected: float | None, tol: float = TOLERANCE) -> bool:
    if actual is None or expected is None:
        return actual is None and expected is None
    return math.isclose(actual, expected, rel_tol=tol, abs_tol=tol)


# ---------------------------------------------------------------------------
# Oracles — C1's text as plain Python, one index t = the as-of bar
# ---------------------------------------------------------------------------


def oracle_atr(highs: list[float], lows: list[float], closes: list[float]) -> float | None:
    """Wilder ATR(14): TR needs c_{t-1}; first ATR = mean of the first 14 TRs; then recursion."""
    n = len(closes)
    true_ranges = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, n)
    ]
    if len(true_ranges) < 14:
        return None
    atr = sum(true_ranges[:14]) / 14
    for tr in true_ranges[14:]:
        atr = (atr * 13 + tr) / 14
    return atr


def oracle_mean(values: list[float]) -> float:
    return sum(values) / len(values)


def oracle_std(values: list[float], ddof: int) -> float:
    mean = oracle_mean(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - ddof))


def oracle_row(  # noqa: PLR0913 - the per-bar inputs plus the two engine values reused
    *,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    beta: float | None,
    ret: dict[int, float],
) -> dict[str, float | None]:
    """Every per-instrument C1 column at the last bar, from the definition text."""
    c = closes
    t = len(c) - 1
    r = [c[i] / c[i - 1] - 1 for i in range(1, len(c))]  # r[i-1] is r_i
    logs = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
    value_traded = [cl * v for cl, v in zip(c, volumes, strict=True)]
    out: dict[str, float | None] = {}

    atr = oracle_atr(highs, lows, c)
    out["atr_14"] = atr
    ma20 = oracle_mean(c[t - 19 : t + 1])
    out["atr_ext_20"] = None if atr is None else (c[t] - ma20) / atr
    ma50_t = oracle_mean(c[t - 49 : t + 1])
    ma50_back = oracle_mean(c[t - 69 : t - 19])
    out["ma50_slope_20"] = (ma50_t / ma50_back - 1) * 100
    path = sum(abs(c[i] - c[i - 1]) for i in range(t - 62, t + 1))
    out["eff_ratio_63"] = abs(c[t] - c[t - 63]) / path

    for months, n in ((6, N6), (12, N12)):
        window = c[-n:]
        peak = window[0]
        worst = 0.0
        under = 0
        for price in window:
            peak = max(peak, price)
            worst = max(worst, 1 - price / peak)
            under += price < peak
        out[f"max_dd_{months}m"] = worst * 100
        if months == 12:
            out["underwater_12m"] = under / n * 100
            window_logs = [math.log(window[i] / window[i - 1]) for i in range(1, n)]
            top3 = sum(sorted(window_logs)[-3:])
            out["ret_ex_top3_12m"] = (math.exp(sum(window_logs) - top3) - 1) * 100
        last_returns = r[-n:]
        downside = math.sqrt(sum(min(x, 0.0) ** 2 for x in last_returns) / n) * math.sqrt(252)
        out[f"downside_vol_{months}m"] = downside
        out[f"sortino_{months}m"] = ret[months] / (downside * 100)

    recent = logs[-21:]
    prior = logs[-126:-21]
    accel = oracle_mean(recent) - oracle_mean(prior)
    out["accel_21_105"] = accel
    out["accel_21_105_vs"] = accel / oracle_std(logs[-126:], ddof=1)

    out["vol_exp_21_126"] = oracle_mean(value_traded[-21:]) / oracle_mean(value_traded[-147:-21])
    baseline = oracle_mean(value_traded[-146:-20])
    out["vol_persist_20"] = float(sum(v > baseline for v in value_traded[-20:]))

    out["_beta"] = beta
    return out


def oracle_rs_persist(closes: list[float], bench: list[float]) -> float:
    """% of the last 126 sessions whose c_d/c_{d-20}-1 beat the index's L_d/L_{d-20}-1."""
    t = len(closes) - 1
    hits = 0
    for d in range(t - 125, t + 1):
        hits += (closes[d] / closes[d - 20] - 1) > (bench[d] / bench[d - 20] - 1)
    return hits / 126 * 100


def month_end(days: list[dt.date], as_of: dt.date, back: int) -> dt.date:
    year, month = as_of.year, as_of.month
    for _ in range(back):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return max(d for d in days if d.year == year and d.month == month)


# ---------------------------------------------------------------------------
# The oracle comparison, three noisy instruments, every per-instrument column
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def computed() -> tuple[pl.DataFrame, dict[int, dict[str, object]]]:
    bars = pl.concat([noisy_bars(seed, len(DAYS), instrument_id=seed) for seed in (1, 2, 3)])
    return bars, unrounded(bars)


class TestEveryColumnAgainstTheDefinition:
    @pytest.mark.parametrize("instrument_id", [1, 2, 3])
    @pytest.mark.parametrize(
        "column",
        [
            "atr_14",
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
        ],
    )
    def test_matches_the_c1_oracle(
        self,
        computed: tuple[pl.DataFrame, dict[int, dict[str, object]]],
        instrument_id: int,
        column: str,
    ) -> None:
        bars, rows = computed
        series = bars.filter(pl.col("instrument_id") == instrument_id)
        row = rows[instrument_id]
        ret = {6: value(row, "ret_6m"), 12: value(row, "ret_12m")}
        assert ret[6] is not None and ret[12] is not None
        expected = oracle_row(
            closes=series["close"].to_list(),
            highs=series["high"].to_list(),
            lows=series["low"].to_list(),
            volumes=series["volume_raw"].to_list(),
            beta=value(row, "beta_12m"),
            ret={6: ret[6], 12: ret[12]},
        )[column]
        actual = value(row, column)
        assert actual is not None
        assert close_to(actual, expected, 1e-8), f"{column}: engine {actual} vs C1 {expected}"

    @pytest.mark.parametrize("instrument_id", [1, 2, 3])
    def test_benchmark_relative_columns(
        self,
        computed: tuple[pl.DataFrame, dict[int, dict[str, object]]],
        instrument_id: int,
    ) -> None:
        bars, rows = computed
        row = rows[instrument_id]
        levels_500 = dict(
            zip(NIFTY_500["date"].to_list(), NIFTY_500["level"].to_list(), strict=True)
        )
        levels_50 = dict(zip(NIFTY_50["date"].to_list(), NIFTY_50["level"].to_list(), strict=True))
        for months in (3, 6, 12):
            start = resolve_window(AS_OF, months, DAYS).start
            index_return = (levels_500[AS_OF] / levels_500[start] - 1) * 100
            ret = value(row, f"ret_{months}m")
            assert ret is not None
            assert close_to(value(row, f"excess_ret_{months}m"), ret - index_return)

        start_12 = resolve_window(AS_OF, 12, DAYS).start
        n50 = (levels_50[AS_OF] / levels_50[start_12] - 1) * 100
        ret_12 = value(row, "ret_12m")
        beta = value(row, "beta_12m")
        assert ret_12 is not None and beta is not None
        assert close_to(value(row, "resid_ret_12m"), ret_12 - beta * n50)

        series = bars.filter(pl.col("instrument_id") == instrument_id)
        expected_rs = oracle_rs_persist(series["close"].to_list(), NIFTY_500["level"].to_list())
        assert close_to(value(row, "rs_persist_126"), expected_rs)

    @pytest.mark.parametrize("instrument_id", [1, 2, 3])
    def test_nse_momentum_ratios(
        self,
        computed: tuple[pl.DataFrame, dict[int, dict[str, object]]],
        instrument_id: int,
    ) -> None:
        """NSE §16: MR = (P(end M-1)/P(end M-7 or M-13) - 1) / (std(l, ddof=1, 252) x sqrt 252)."""
        bars, rows = computed
        series = bars.filter(pl.col("instrument_id") == instrument_id)
        prices = dict(zip(series["date"].to_list(), series["close"].to_list(), strict=True))
        closes = series["close"].to_list()
        end = month_end(DAYS, AS_OF, 1)
        index_end = DAYS.index(end)
        logs = [math.log(closes[i] / closes[i - 1]) for i in range(index_end - 251, index_end + 1)]
        sigma = oracle_std(logs, ddof=1) * math.sqrt(252)
        for column, back in (("nse_mr6", 7), ("nse_mr12", 13)):
            expected = (prices[end] / prices[month_end(DAYS, AS_OF, back)] - 1) / sigma
            assert close_to(value(rows[instrument_id], column), expected), column

    def test_the_cross_sectional_columns_are_left_null_for_the_worker(
        self, computed: tuple[pl.DataFrame, dict[int, dict[str, object]]]
    ) -> None:
        _, rows = computed
        for row in rows.values():
            assert row["mom_pctile"] is None
            assert row["rank_persist_20"] is None


# ---------------------------------------------------------------------------
# Hand-computed cases
# ---------------------------------------------------------------------------


def flat_then(n: int, tail: list[float], base: float = 100.0) -> list[float]:
    return [base] * (n - len(tail)) + tail


class TestHandComputed:
    def test_a_constant_true_range_is_the_atr_and_extension_is_distance_over_it(self) -> None:
        """h - l = 2 every bar, closes flat -> TR = 2 -> ATR = 2; the recursion keeps it at 2.

        Then the last close jumps to 104 with h = 105, l = 103: TR = max(2, 5, 3) = 5, so
        ATR = (2 x 13 + 5) / 14 = 31/14, and ma_20 = (19 x 100 + 104) / 20 = 100.2.
        """
        n = 60
        closes = [100.0] * (n - 1) + [104.0]
        highs = [101.0] * (n - 1) + [105.0]
        lows = [99.0] * (n - 1) + [103.0]
        row = unrounded(bars_frame(DAYS, closes, highs=highs, lows=lows))[1]
        assert close_to(value(row, "atr_14"), 31 / 14)
        assert close_to(value(row, "atr_ext_20"), (104 - 100.2) / (31 / 14))

    def test_the_atr_seed_is_the_mean_of_the_first_fourteen_true_ranges(self) -> None:
        """15 bars: TRs are 1..14 (h - l = i on bar i, closes flat), so ATR = 7.5 on bar 15."""
        closes = [50.0] * 15
        highs = [50.0] + [50.0 + i / 2 for i in range(1, 15)]
        lows = [50.0] + [50.0 - i / 2 for i in range(1, 15)]
        row = unrounded(bars_frame(DAYS, closes, highs=highs, lows=lows))[1]
        assert close_to(value(row, "atr_14"), 7.5)

    def test_ma50_slope(self) -> None:
        """Closes 1..100: ma_50 today is mean(51..100) = 75.5, 20 bars ago mean(31..80) = 55.5."""
        closes = [float(i) for i in range(1, 101)]
        row = unrounded(bars_frame(DAYS, closes))[1]
        assert close_to(value(row, "ma50_slope_20"), (75.5 / 55.5 - 1) * 100)

    def test_efficiency_is_one_on_a_straight_line_and_zero_on_a_round_trip(self) -> None:
        straight = unrounded(bars_frame(DAYS, [100.0 + i for i in range(80)]))[1]
        assert close_to(value(straight, "eff_ratio_63"), 1.0)
        # 64 bars alternating 100/101: net c_t - c_{t-63} = 1 over a path of 63 -> 1/63.
        zigzag = unrounded(bars_frame(DAYS, [100.0 + (i % 2) for i in range(64)]))[1]
        assert close_to(value(zigzag, "eff_ratio_63"), 1 / 63)

    def test_efficiency_is_null_on_a_flat_path(self) -> None:
        """C1: "NULL if denominator 0" — a flat path has no efficiency, not zero efficiency."""
        row = unrounded(bars_frame(DAYS, [100.0] * 80))[1]
        assert row["eff_ratio_63"] is None

    def test_a_ten_percent_dip_inside_the_window_is_a_ten_point_drawdown(self) -> None:
        """Up to 200, down to 180 (-10%), recover to 210: max_dd 10.00; days below the running
        high are the dip's own bars."""
        tail = [150.0, 200.0, 190.0, 180.0, 195.0, 205.0, 210.0]
        closes = flat_then(len(DAYS), tail)
        row = unrounded(bars_frame(DAYS, closes))[1]
        assert close_to(value(row, "max_dd_6m"), 10.0)
        assert close_to(value(row, "max_dd_12m"), 10.0)
        # 190, 180 and 195 closed below the running high of 200.
        assert close_to(value(row, "underwater_12m"), 3 / N12 * 100)

    def test_the_drawdown_is_measured_from_the_high_inside_the_window_only(self) -> None:
        """A 300 peak before the 6M window starts does not count against the 6M drawdown."""
        closes = [100.0] * len(DAYS)
        closes[-N6 - 5] = 300.0  # outside the 6M window, inside the 12M one
        row = unrounded(bars_frame(DAYS, closes))[1]
        assert close_to(value(row, "max_dd_6m"), 0.0)
        assert close_to(value(row, "max_dd_12m"), (1 - 100 / 300) * 100)

    def test_a_steady_riser_is_never_underwater_and_has_no_downside(self) -> None:
        closes = [100.0 * 1.001**i for i in range(len(DAYS))]
        row = unrounded(bars_frame(DAYS, closes))[1]
        assert close_to(value(row, "underwater_12m"), 0.0)
        assert close_to(value(row, "max_dd_12m"), 0.0)
        assert close_to(value(row, "downside_vol_12m"), 0.0)
        # sortino: "NULL if downside_vol < 0.5e-10 (mirrors sharpe)".
        assert row["sortino_12m"] is None
        assert row["sortino_6m"] is None

    def test_downside_volatility_divides_by_every_return(self) -> None:
        """One -10% day in the 6M window and otherwise flat: sqrt(0.01 / N6) x sqrt(252)."""
        closes = [100.0] * (len(DAYS) - 1) + [90.0]
        row = unrounded(bars_frame(DAYS, closes))[1]
        assert close_to(value(row, "downside_vol_6m"), math.sqrt(0.01 / N6) * math.sqrt(252))
        assert close_to(value(row, "downside_vol_12m"), math.sqrt(0.01 / N12) * math.sqrt(252))
        # ret_6m = -10 (base is the window's first bar, 100).
        expected = -10.0 / (math.sqrt(0.01 / N6) * math.sqrt(252) * 100)
        assert close_to(value(row, "sortino_6m"), expected)

    def test_three_jumps_removed_leave_a_flat_year(self) -> None:
        """Flat except three +10% days: sum(l) - top3(l) = 0 -> ret_ex_top3_12m = 0.00."""
        closes = [100.0] * len(DAYS)
        level = 100.0
        for i in range(len(DAYS) - N12 + 10, len(DAYS)):
            if i in (len(DAYS) - 200, len(DAYS) - 100, len(DAYS) - 50):
                level *= 1.1
            closes[i] = level
        row = unrounded(bars_frame(DAYS, closes))[1]
        assert close_to(value(row, "ret_ex_top3_12m"), 0.0)
        assert close_to(value(row, "ret_12m"), (1.1**3 - 1) * 100)

    def test_acceleration_of_two_constant_growth_rates(self) -> None:
        """105 sessions of +0.1% then 21 of +0.5%: accel = ln 1.005 - ln 1.001.

        The 126 logs are 105 x a and 21 x b, so their sample std is
        |b - a| x sqrt(105 x 21 / 126 / 125).
        """
        a, b = math.log(1.001), math.log(1.005)
        closes = [100.0]
        for i in range(126):
            closes.append(closes[-1] * (1.001 if i < 105 else 1.005))
        row = unrounded(bars_frame(DAYS, closes))[1]
        assert close_to(value(row, "accel_21_105"), b - a)
        spread = (b - a) * math.sqrt(105 * 21 / 126 / 125)
        assert close_to(value(row, "accel_21_105_vs"), (b - a) / spread)

    def test_acceleration_scaled_is_null_when_the_log_returns_do_not_vary(self) -> None:
        closes = [100.0 * 1.002**i for i in range(200)]
        row = unrounded(bars_frame(DAYS, closes))[1]
        assert close_to(value(row, "accel_21_105"), 0.0, 1e-12)
        assert row["accel_21_105_vs"] is None

    def test_volume_expansion_and_persistence(self) -> None:
        """Flat price; 126 sessions at 1,000 shares then 21 at 3,000 -> expansion 3.0.

        Persistence: base = mean of the 126 sessions before the last 20 = (125 x 1000 + 3000)
        / 126 x 100 ₹; the last 20 all trade 3,000 x 100 ₹ > base -> 20.
        """
        volumes = [1_000.0] * 126 + [3_000.0] * 21
        row = unrounded(bars_frame(DAYS, [100.0] * 147, volumes=volumes))[1]
        assert close_to(value(row, "vol_exp_21_126"), 3.0)
        assert value(row, "vol_persist_20") == 20.0

        alternating = [1_000.0] * 126 + [1_500.0 if i % 2 else 500.0 for i in range(20)]
        row = unrounded(bars_frame(DAYS, [100.0] * 146, volumes=alternating))[1]
        assert value(row, "vol_persist_20") == 10.0

    def test_persistence_is_strictly_above_the_baseline(self) -> None:
        """Every session equal to the baseline counts zero, not twenty."""
        row = unrounded(bars_frame(DAYS, [100.0] * 146, volumes=[1_000.0] * 146))[1]
        assert value(row, "vol_persist_20") == 0.0

    def test_relative_strength_persistence_against_a_flat_index(self) -> None:
        """A flat index returns 0; a steady riser beats it on every one of the 126 days."""
        flat_index = pl.DataFrame({"date": DAYS, "level": [10_000.0] * len(DAYS)})
        riser = unrounded(
            bars_frame(DAYS, [100.0 * 1.001**i for i in range(200)]), market=flat_index
        )[1]
        assert close_to(value(riser, "rs_persist_126"), 100.0)
        faller = unrounded(
            bars_frame(DAYS, [100.0 * 0.999**i for i in range(200)]), market=flat_index
        )[1]
        assert close_to(value(faller, "rs_persist_126"), 0.0)

    def test_excess_return_over_a_doubling_index(self) -> None:
        """Index 1,000 on the 12M window's first day and 2,000 on as-of: excess = ret - 100."""
        start = resolve_window(AS_OF, 12, DAYS).start
        index = pl.DataFrame(
            {"date": DAYS, "level": [2_000.0 if d > start else 1_000.0 for d in DAYS]}
        )
        closes = [100.0] * (len(DAYS) - 1) + [150.0]
        row = unrounded(bars_frame(DAYS, closes), market=index)[1]
        assert close_to(value(row, "excess_ret_12m"), 50.0 - 100.0)

    def test_nse_ratio_on_a_known_month_end_path(self) -> None:
        """Price 100 through the end of M-13, 150 at the end of M-7, 180 at the end of M-1.

        MR12 = (180/100 - 1)/sigma, MR6 = (180/150 - 1)/sigma, with sigma from the 252 logs
        ending at end M-1 (two jumps: ln 1.5 and ln 1.2, the rest zero).
        """
        e1, e7 = month_end(DAYS, AS_OF, 1), month_end(DAYS, AS_OF, 7)
        closes = [100.0 if d <= month_end(DAYS, AS_OF, 13) else 0.0 for d in DAYS]
        for i, d in enumerate(DAYS):
            if d > month_end(DAYS, AS_OF, 13):
                closes[i] = 180.0 if d > e7 else 150.0
        index_e1 = DAYS.index(e1)
        logs = [math.log(closes[i] / closes[i - 1]) for i in range(index_e1 - 251, index_e1 + 1)]
        sigma = oracle_std(logs, ddof=1) * math.sqrt(252)
        row = unrounded(bars_frame(DAYS, closes))[1]
        assert close_to(value(row, "nse_mr12"), (180 / 100 - 1) / sigma)
        assert close_to(value(row, "nse_mr6"), (180 / 150 - 1) / sigma)
        # The M-7 step is inside sigma's year, so sigma is a real (non-zero) number here.
        assert any(x != 0 for x in logs)

    def test_nse_ratio_is_null_when_prices_do_not_vary(self) -> None:
        row = unrounded(bars_frame(DAYS, [100.0] * len(DAYS)))[1]
        assert row["nse_mr6"] is None
        assert row["nse_mr12"] is None

    def test_month_end_is_the_last_trading_day_of_the_month(self) -> None:
        # July 2026 ends on a Friday the 31st; May 2026 ends on Sunday the 31st -> Friday 29th.
        assert fr.month_end_trading_day(DAYS, AS_OF, 1) == dt.date(2026, 7, 31)
        assert fr.month_end_trading_day(DAYS, AS_OF, 3) == dt.date(2026, 5, 29)
        # Across a year boundary: August 2026 - 13 months = July 2025.
        assert fr.month_end_trading_day(DAYS, AS_OF, 13) == dt.date(2025, 7, 31)
        assert fr.month_end_trading_day(DAYS, AS_OF, 40) is None


# ---------------------------------------------------------------------------
# NULL when the window is short: the exact bar count gives a value, one fewer gives NULL
# ---------------------------------------------------------------------------


def _nse_bars_needed(back: int) -> int:
    end = DAYS.index(month_end(DAYS, AS_OF, 1))
    base = DAYS.index(month_end(DAYS, AS_OF, back))
    return len(DAYS) - min(end - 252, base)


BARS_NEEDED: Final[dict[str, int]] = {
    "atr_14": 15,  # 14 TRs, and the first bar has none
    "atr_ext_20": 20,  # ma_20
    "ma50_slope_20": 70,  # ma_50 twenty bars ago
    "eff_ratio_63": 64,  # c_{t-63}
    "max_dd_6m": N6,
    "max_dd_12m": N12,
    "underwater_12m": N12,
    "ret_ex_top3_12m": N12,
    "downside_vol_6m": N6 + 1,  # N returns, as vol_Nm
    "downside_vol_12m": N12 + 1,
    "sortino_6m": N6 + 1,
    "sortino_12m": N12 + 1,
    "accel_21_105": 127,  # 126 log returns
    "accel_21_105_vs": 127,
    "vol_exp_21_126": 147,  # 126 + 21 sessions of traded value
    "vol_persist_20": 146,  # 126 + 20
    "excess_ret_3m": N3,  # ret_3m
    "excess_ret_6m": N6,
    "excess_ret_12m": N12,
    "resid_ret_12m": N12,  # ret_12m's window binds before beta's 200-return floor
    "rs_persist_126": 146,  # 126 comparisons, each a 20-session return
    "nse_mr6": _nse_bars_needed(7),
    "nse_mr12": _nse_bars_needed(13),
}


class TestNullWhenTheWindowIsShort:
    def test_every_computed_c1_column_has_a_boundary_case(self) -> None:
        assert set(BARS_NEEDED) == set(C1_COLUMNS) - {"mom_pctile", "rank_persist_20"}

    @pytest.mark.parametrize(("column", "needed"), sorted(BARS_NEEDED.items()))
    def test_exactly_enough_bars_gives_a_value_and_one_fewer_gives_null(
        self, column: str, needed: int
    ) -> None:
        full = noisy_bars(7, len(DAYS))
        enough = unrounded(full.tail(needed))[1]
        short = unrounded(full.tail(needed - 1))[1]
        assert enough[column] is not None, f"{column} is NULL with {needed} bars"
        assert short[column] is None, f"{column} has a value with {needed - 1} bars"

    def test_a_calendar_that_does_not_reach_the_offset_nulls_the_month_windows(self) -> None:
        """windows.py's rule, reused: a calendar starting inside the 12M window is not a year."""
        days = weekdays(dt.date(2025, 9, 1))
        bars = bars_frame(days, noisy(3, len(days)))
        row = unrounded(bars, days=days)[1]
        for column in (
            "max_dd_12m",
            "underwater_12m",
            "ret_ex_top3_12m",
            "downside_vol_12m",
            "sortino_12m",
            "excess_ret_12m",
            "resid_ret_12m",
        ):
            assert row[column] is None, column
        assert row["max_dd_6m"] is not None

    def test_benchmark_columns_are_null_without_benchmarks(self) -> None:
        row = unrounded(noisy_bars(9, len(DAYS)), benchmark=None, market=None)[1]
        for column in (
            "excess_ret_3m",
            "excess_ret_6m",
            "excess_ret_12m",
            "resid_ret_12m",
            "rs_persist_126",
        ):
            assert row[column] is None, column
        assert row["atr_14"] is not None

    def test_a_missing_index_level_on_the_window_start_is_null_not_a_neighbour(self) -> None:
        start = resolve_window(AS_OF, 6, DAYS).start
        gapped = NIFTY_500.filter(pl.col("date") != start)
        row = unrounded(noisy_bars(9, len(DAYS)), market=gapped)[1]
        assert row["excess_ret_6m"] is None
        assert row["excess_ret_12m"] is not None


# ---------------------------------------------------------------------------
# Point-in-time
# ---------------------------------------------------------------------------


class TestPointInTime:
    def test_future_bars_and_levels_never_change_an_earlier_row(self) -> None:
        """Compute as-of D with history ending on D, then with ten wild bars after D appended to
        every series (bars, both benchmarks, the calendar): the D row must not move at all."""
        future_days = weekdays(AS_OF + dt.timedelta(days=1), AS_OF + dt.timedelta(days=16))
        all_days = DAYS + future_days
        instruments = [noisy_bars(seed, len(DAYS), instrument_id=seed) for seed in (11, 12)]
        extended = []
        for bars in instruments:
            last = float(bars["close"][-1])
            wild = [last * (3.0 if i % 2 else 0.2) for i in range(len(future_days))]
            extended.append(
                pl.concat(
                    [
                        bars,
                        bars_frame(
                            future_days,
                            wild,
                            instrument_id=int(bars["instrument_id"][0]),
                            volumes=[1e9] * len(future_days),
                        ),
                    ]
                )
            )

        def with_future(levels: pl.DataFrame) -> pl.DataFrame:
            tail = pl.DataFrame({"date": future_days, "level": [1e7] * len(future_days)})
            return pl.concat([levels, tail.with_columns(pl.col("date").cast(pl.Date))])

        before = compute_factors(
            pl.concat(instruments), AS_OF, DAYS, NIFTY_50, market_benchmark=NIFTY_500
        ).frame
        after = compute_factors(
            pl.concat(extended),
            AS_OF,
            all_days,
            with_future(NIFTY_50),
            market_benchmark=with_future(NIFTY_500),
        ).frame
        columns = ["instrument_id", *C1_COLUMNS]
        assert (
            before.select(columns).sort("instrument_id").to_dicts()
            == after.select(columns).sort("instrument_id").to_dicts()
        )
        populated = [c for c in C1_COLUMNS if before[c].null_count() < before.height]
        assert len(populated) == len(C1_COLUMNS) - 2, "the comparison must not be NULL vs NULL"


# ---------------------------------------------------------------------------
# Storage: precision, ORM, migration, docs, the range guard
# ---------------------------------------------------------------------------


class TestStorage:
    def test_the_module_lists_exactly_the_c1_columns(self) -> None:
        assert fr.RANKING_FACTOR_COLUMNS == C1_COLUMNS
        numeric = {k: v for k, v in C1_TYPES.items() if v is not None}
        assert numeric == fr.RANKING_NUMERIC_TYPES
        assert fr.RANKING_INTEGER_COLUMNS == ("vol_persist_20",)

    @pytest.mark.parametrize("column", C1_COLUMNS)
    def test_the_orm_column_has_the_c1_type(self, column: str) -> None:
        declared = FactorDaily.__table__.columns[column]
        expected = C1_TYPES[column]
        assert declared.nullable
        if expected is None:
            assert isinstance(declared.type, SmallInteger)
            assert column in INTEGER_COLUMNS
            return
        assert isinstance(declared.type, Numeric)
        assert (declared.type.precision, declared.type.scale) == expected
        assert COLUMN_PRECISION[column] == expected[1]

    def test_migration_0046_adds_every_column_at_the_c1_type(self) -> None:
        path = REPO_ROOT / "services" / "api" / "alembic" / "versions" / "0046_ranking_factors.py"
        text = path.read_text(encoding="utf-8")
        assert 'revision: str = "0046_ranking_factors"' in text
        assert 'down_revision: str | None = "0045_instrument_watch_and_prefs"' in text
        declared = dict(
            re.findall(r'\("(\w+)", sa\.(Numeric\(\d+, \d+\)|SmallInteger\(\))\)', text)
        )
        expected = {
            column: "SmallInteger()" if kind is None else f"Numeric({kind[0]}, {kind[1]})"
            for column, kind in C1_TYPES.items()
        }
        assert declared == expected

    def test_docs_04_lists_every_ranking_column(self) -> None:
        """House rule 4: behaviour that grows the schema updates docs/ in the same change."""
        text = (REPO_ROOT / "docs" / "04-data-model.md").read_text(encoding="utf-8")
        missing = [c for c in C1_COLUMNS if not re.search(rf"\b{c}\b", text)]
        assert missing == []

    def test_the_stored_row_is_rounded_to_each_columns_scale(self) -> None:
        frame = compute_factors(
            noisy_bars(21, len(DAYS)), AS_OF, DAYS, NIFTY_50, market_benchmark=NIFTY_500
        ).frame
        row = frame.to_dicts()[0]
        for column, kind in C1_TYPES.items():
            raw = row[column]
            if raw is None:
                continue
            places = 0 if kind is None else kind[1]
            exponent = Decimal(str(raw)).normalize().as_tuple().exponent
            assert isinstance(exponent, int)
            assert -exponent <= places, f"{column}={raw} has more than {places} dp"
        assert frame.schema["vol_persist_20"] == pl.Int64

    def test_no_working_column_and_no_unpriced_number_leaves_the_engine(self) -> None:
        frame = compute_factors(
            noisy_bars(22, len(DAYS)), AS_OF, DAYS, NIFTY_50, market_benchmark=NIFTY_500
        ).frame
        assert [c for c in frame.columns if c.startswith("_")] == []
        working = {
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
        assert [c for c in unpriced_numeric_columns(frame) if c not in working] == []

    def test_a_value_the_column_cannot_hold_is_null_rather_than_an_insert_failure(self) -> None:
        """numeric(10,4) holds < 10^6 once rounded; numeric(7,2) holds < 10^5."""
        frame = pl.DataFrame(
            {
                "vol_exp_21_126": [999_999.9999, 1_000_000.0, float("nan"), float("inf"), 2.5],
                "rs_persist_126": [99_999.99, 100_000.0, 1.0, -100_000.0, None],
            }
        )
        guarded = fr.null_unrepresentable(frame)
        assert guarded["vol_exp_21_126"].to_list() == [999_999.9999, None, None, None, 2.5]
        assert guarded["rs_persist_126"].to_list() == [99_999.99, None, 1.0, None, None]

    def test_a_benchmark_is_read_as_level_or_close_and_nothing_else(self) -> None:
        by_close = levels_frame(5, DAYS, column="close")
        by_level = levels_frame(5, DAYS, column="level")
        bars = noisy_bars(5, len(DAYS))
        assert (
            unrounded(bars, benchmark=by_close, market=by_close)[1]
            == unrounded(bars, benchmark=by_level, market=by_level)[1]
        )
        with pytest.raises(ValueError, match="level"):
            fr.benchmark_levels(pl.DataFrame({"date": DAYS, "value": [1.0] * len(DAYS)}))
        doubled = pl.concat([by_level, by_level.head(1)])
        with pytest.raises(ValueError, match="same date twice"):
            fr.benchmark_levels(doubled)


# ---------------------------------------------------------------------------
# mom_pctile — cross_sectional_pctile
# ---------------------------------------------------------------------------


def day_rows(values: list[float | None], masks: list[int]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "instrument_id": list(range(1, len(values) + 1)),
            "avg_sharpe_12_6_3_1": values,
            "universe_mask": masks,
        },
        schema_overrides={"avg_sharpe_12_6_3_1": pl.Float64, "instrument_id": pl.Int64},
    )


class TestMomentumPercentile:
    def test_ties_take_the_average_rank(self) -> None:
        """Values 1, 2, 2, 4: average ranks 1, 2.5, 2.5, 4 -> (r - 1)/(n - 1) x 100."""
        out = fr.cross_sectional_pctile(day_rows([1.0, 2.0, 2.0, 4.0], [1, 1, 1, 1]))
        assert out["mom_pctile"].to_list() == [0.0, 50.0, 50.0, 100.0]

    def test_rows_outside_every_universe_and_null_values_are_excluded_and_null(self) -> None:
        """n counts only mask != 0 rows with a value: 10, 30, 20 rank 0, 100, 50."""
        out = fr.cross_sectional_pctile(day_rows([10.0, 99.0, 30.0, None, 20.0], [4, 0, 4, 4, 2]))
        assert out["mom_pctile"].to_list() == [0.0, None, 100.0, None, 50.0]

    def test_a_population_of_one_is_the_top(self) -> None:
        out = fr.cross_sectional_pctile(day_rows([3.0, 7.0], [1, 0]))
        assert out["mom_pctile"].to_list() == [100.0, None]

    def test_it_is_rounded_at_write_time(self) -> None:
        """Four names: 1/3 x 100 = 33.333... is stored as 33.33."""
        out = fr.cross_sectional_pctile(day_rows([1.0, 2.0, 3.0, 4.0], [1, 1, 1, 1]))
        assert out["mom_pctile"].to_list() == [0.0, 33.33, 66.67, 100.0]

    def test_the_blend_is_computed_from_components_when_absent(self) -> None:
        components = pl.DataFrame(
            {
                "instrument_id": [1, 2, 3],
                "universe_mask": [1, 1, 1],
                "sharpe_12m": [1.0, 2.0, 5.0],
                "sharpe_6m": [1.0, 2.0, None],
                "sharpe_3m": [1.0, 2.0, 5.0],
                "sharpe_1m": [1.0, 6.0, 5.0],
            }
        )
        out = fr.cross_sectional_pctile(components)
        # Blends 1.0 and 3.0; the third has a NULL component and so no blend (docs/05 §4).
        assert out["mom_pctile"].to_list() == [0.0, 100.0, None]

    def test_the_components_are_the_registry_blend(self) -> None:
        assert FACTORS[fr.MOM_PCTILE_SOURCE].components == fr.MOM_PCTILE_COMPONENTS

    def test_it_refuses_a_frame_without_the_mask(self) -> None:
        with pytest.raises(ValueError, match="universe_mask"):
            fr.cross_sectional_pctile(pl.DataFrame({"instrument_id": [1]}))


# ---------------------------------------------------------------------------
# rank_persist_20 — rank_persistence
# ---------------------------------------------------------------------------


def history(dates: list[dt.date], series: Mapping[int, Sequence[float | None]]) -> pl.DataFrame:
    rows = [
        {"instrument_id": instrument_id, "date": day, "mom_pctile": pct}
        for instrument_id, values in series.items()
        for day, pct in zip(dates, values, strict=True)
    ]
    return pl.DataFrame(
        rows, schema={"instrument_id": pl.Int64, "date": pl.Date, "mom_pctile": pl.Float64}
    )


DATES_20: Final = DAYS[-20:]


class TestRankPersistence:
    def test_percent_of_the_last_twenty_dates_at_or_above_eighty(self) -> None:
        """15 of 20 dates >= 80 -> 75.00. Exactly 80 counts; 79.99 does not."""
        a = [90.0] * 14 + [80.0] + [79.99] * 5
        out = fr.rank_persistence(history(DATES_20, {1: a}), AS_OF)
        assert out.to_dicts() == [{"instrument_id": 1, "rank_persist_20": 75.0}]

    def test_null_unless_all_twenty_dates_carry_a_percentile(self) -> None:
        one_null: list[float | None] = [95.0] * 19 + [None]
        out = fr.rank_persistence(history(DATES_20, {1: [95.0] * 20, 2: one_null}), AS_OF)
        assert dict(zip(out["instrument_id"], out["rank_persist_20"], strict=True)) == {
            1: 100.0,
            2: None,
        }

    def test_a_missing_date_is_null(self) -> None:
        frame = history(DATES_20, {1: [95.0] * 20, 2: [95.0] * 20}).filter(
            ~((pl.col("instrument_id") == 2) & (pl.col("date") == DATES_20[3]))
        )
        out = fr.rank_persistence(frame, AS_OF)
        assert dict(zip(out["instrument_id"], out["rank_persist_20"], strict=True))[2] is None

    def test_fewer_than_twenty_dates_is_null(self) -> None:
        out = fr.rank_persistence(history(DAYS[-19:], {1: [99.0] * 19}), AS_OF)
        assert out["rank_persist_20"].to_list() == [None]

    def test_only_the_last_twenty_dates_count(self) -> None:
        """A hit on the 21st date back is outside the window."""
        values = [100.0] + [0.0] * 20
        out = fr.rank_persistence(history(DAYS[-21:], {1: values}), AS_OF)
        assert out["rank_persist_20"].to_list() == [0.0]

    def test_rows_after_as_of_are_ignored(self) -> None:
        """Point-in-time: tomorrow's percentile cannot enter today's persistence."""
        as_of = DAYS[-2]
        frame = history(DAYS[-21:], {1: [100.0] * 20 + [0.0]})
        out = fr.rank_persistence(frame, as_of)
        assert out.to_dicts() == [{"instrument_id": 1, "rank_persist_20": 100.0}]

    def test_only_instruments_with_a_row_on_as_of_are_returned(self) -> None:
        frame = history(DATES_20, {1: [90.0] * 20, 2: [90.0] * 20}).filter(
            ~((pl.col("instrument_id") == 2) & (pl.col("date") == AS_OF))
        )
        assert fr.rank_persistence(frame, AS_OF)["instrument_id"].to_list() == [1]

    def test_it_is_rounded_at_write_time(self) -> None:
        out = fr.rank_persistence(history(DATES_20, {1: [85.0] * 7 + [10.0] * 13}), AS_OF)
        assert out["rank_persist_20"].to_list() == [35.0]

    def test_a_duplicated_row_is_refused(self) -> None:
        frame = history(DATES_20, {1: [90.0] * 20})
        with pytest.raises(ValueError, match="twice"):
            fr.rank_persistence(pl.concat([frame, frame.tail(1)]), AS_OF)
