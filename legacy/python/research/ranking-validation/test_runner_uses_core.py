"""G3 (gates/ranking-2.F-validation.md): the runner's factors are core's factors, not second formulas.

    cd decile-blueprint && uv run pytest ../research/ranking-validation/test_runner_uses_core.py

What is asserted, and why each is the spec rather than the code's current output:

1. **Same numbers as ``compute_factors``.** For sampled (instrument, signal date) pairs, the value
   the ablation ranked on — the runner's cached frame when a full run has written it, otherwise a
   fresh ``factors.factors_on`` over that date's whole universe (the call the cache is built from)
   — equals ``compute_factors`` run independently on that one instrument's *entire* history up to
   the date, within 1e-9, for several window-exact C1/docs-05 columns. That covers three things
   at once: the runner calls core, its 3-year worker lookback does not truncate a window, and
   computing instruments in chunks does not leak one instrument into another.
2. **The regime switch used for the 19 pre-signal sessions changes nothing ``mom_pctile`` reads.**
3. **No formulas in the runner.** Its source contains no rolling / std / log / pct-change / ewm
   arithmetic apart from the C8 universe rule's 63-bar median traded value.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import polars as pl
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from baskfy_core.factors import FactorConfig, compute_factors  # noqa: E402

from rvl import data, factors  # noqa: E402

TOLERANCE = 1e-9
#: Window-exact columns (no recursion seeded at the start of the history handed in).
COMPARED = (
    "ret_12m",
    "sharpe_12m",
    "vol_12m",
    "max_dd_12m",
    "downside_vol_12m",
    "sortino_12m",
    "eff_ratio_63",
    "ma50_slope_20",
    "accel_21_105",
    "vol_exp_21_126",
)
SAMPLE_DATE_POSITIONS = (6, 55, -1)
SAMPLES_PER_DATE = 4


@dataclass(frozen=True)
class World:
    bars: pl.DataFrame
    sessions: list[dt.date]
    calendar: tuple[dt.date, ...]
    series: pl.DataFrame
    benchmark: pl.DataFrame | None
    market: pl.DataFrame | None


@pytest.fixture(scope="module")
def world() -> World:
    data.bars_parquet()
    bars = data.load_bars([*factors.FACTOR_INPUTS, "mtv_63"])
    return World(
        bars=bars,
        sessions=sorted(bars["date"].unique().to_list()),
        calendar=data.calendar(),
        series=data.instruments(),
        benchmark=data.index_levels("NIFTY 50"),
        market=data.index_levels("NIFTY 500"),
    )


def _close(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if math.isnan(left) or math.isnan(right):
        return math.isnan(left) and math.isnan(right)
    return abs(left - right) <= TOLERANCE


def _runner_frame(world: World, day: dt.date) -> pl.DataFrame:
    cached = factors.run_dir(None) / "factors" / f"{day.isoformat()}.parquet"
    if cached.exists():
        return pl.read_parquet(cached)
    ids = factors.universe(world.bars, day, None)
    return factors.factors_on(
        world.bars, world.series, day, ids, world.calendar, world.benchmark, world.market
    )


def test_runner_columns_equal_compute_factors_on_sampled_instrument_dates(world: World) -> None:
    dates = factors.signal_dates(world.sessions)
    compared = dict.fromkeys(COMPARED, 0)
    for position in SAMPLE_DATE_POSITIONS:
        day = dates[position]
        runner = _runner_frame(world, day)
        ids = sorted(runner["instrument_id"].to_list())
        step = max(1, len(ids) // SAMPLES_PER_DATE)
        for instrument in ids[::step][:SAMPLES_PER_DATE]:
            history = (
                world.bars.filter(
                    (pl.col("instrument_id") == instrument) & (pl.col("date") <= day)
                )
                .select(factors.FACTOR_INPUTS)
                .join(world.series.select("instrument_id", "series"), on="instrument_id")
            )
            reference = compute_factors(
                history, day, list(world.calendar), world.benchmark, market_benchmark=world.market
            ).frame.row(0, named=True)
            mine = runner.filter(pl.col("instrument_id") == instrument).row(0, named=True)
            for column in COMPARED:
                assert _close(mine[column], reference[column]), (
                    f"{column} for instrument {instrument} on {day}: runner {mine[column]!r}, "
                    f"compute_factors {reference[column]!r}"
                )
                if reference[column] is not None:
                    compared[column] += 1
    populated = [column for column, count in compared.items() if count > 0]
    assert len(populated) >= 3, f"too few columns carried values to compare: {compared}"


def test_regime_switch_leaves_the_sharpe_inputs_to_mom_pctile_identical(world: World) -> None:
    day = factors.signal_dates(world.sessions)[40]
    prior = world.sessions[world.sessions.index(day) - 7]
    ids = factors.universe(world.bars, prior, None)[:40]

    def run(config: FactorConfig | None) -> pl.DataFrame:
        return factors.factors_on(
            world.bars,
            world.series,
            prior,
            ids,
            world.calendar,
            world.benchmark,
            world.market,
            config,
        ).sort("instrument_id")

    fast, full = run(factors.NO_REGIME), run(None)
    assert fast["instrument_id"].to_list() == full["instrument_id"].to_list()
    for column in factors.SHARPE_COMPONENTS:
        for a, b in zip(fast[column].to_list(), full[column].to_list(), strict=True):
            assert _close(a, b), f"{column}: {a!r} != {b!r}"


#: Factor arithmetic that must live in core; the allowed hits are not factors.
FORMULA = re.compile(r"rolling_|\.std\(|\.log\(|pct_change|ewm|\.shift\(|\.diff\(|cum_?sum")
ALLOWED = {
    # C8 universe rule (63-bar median traded value) and vbt's special-session detector.
    ("rvl/data.py", ".rolling_median(UNIVERSE_MEDIAN_SESSIONS"),
    ("rvl/data.py", ".rolling_median(THIN_SESSION_WINDOW"),
}


def test_runner_source_carries_no_factor_formulas() -> None:
    sources = [HERE / "run_validation.py", *sorted((HERE / "rvl").glob("*.py"))]
    hits = []
    for path in sources:
        relative = path.relative_to(HERE).as_posix()
        for line in path.read_text().splitlines():
            code = line.split("#", 1)[0]
            if FORMULA.search(code) and not any(
                relative == name and token in code for name, token in ALLOWED
            ):
                hits.append(f"{relative}: {line.strip()}")
    assert not hits, "factor arithmetic in the runner:\n" + "\n".join(hits)
