"""VB2: the core reproduces the study, or says exactly where it does not.

``research/volume-breakout/STRATEGY.md`` is the spec; ``out/final_trades.csv`` (761 trades) and
``out/final_metrics.json`` are its answers. This file runs the **production** core over the
**same** bars and compares. ``docs/vbt/06`` VB2.

**It skips when the export is absent, and says so loudly.** ``research/volume-breakout/aws/`` is
68 MB and gitignored; regenerate it with ``export_bars_aws.sh``. What runs everywhere is
``test_vbt_backtest.py``'s planted trade, whose arithmetic is worked by hand.

The expectations are read from the study's own files rather than transcribed, so a re-run of the
research that moved a number would be visible here as a failure rather than invisible as a
transcription that nobody updated.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Protocol, cast

import numpy as np
import polars as pl
import pytest

from baskfy_core.vbt.backtest import (
    BacktestParams,
    BacktestResult,
    gate_vector,
    panel_from_frame,
    run_backtest,
    summarise,
    yearly,
)
from baskfy_core.vbt.breadth import breadth_series
from baskfy_core.vbt.calendar import SessionCalendar
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, EntryConfig
from baskfy_core.vbt.indicators import with_vbt_indicators
from baskfy_core.vbt.signals import with_signal_columns

REPO = Path(__file__).resolve().parents[4]
RESEARCH = REPO / "research" / "volume-breakout"
EXPORT = RESEARCH / "aws"
RESULTS = RESEARCH / "out"

# ``tools/`` is not a package on the core's import path; the loader lives there because VB9's
# plant run uses the same one, and a second copy under ``tests/`` would be the thing that drifts.
sys.path.insert(0, str(REPO / "tools" / "vbt"))

#: The study's frame: the first 200 sessions are warm-up.
FIRST_SESSION = dt.date(2017, 10, 16)

#: STRATEGY §1's two data findings, as dates. Found by the **rule**, never by this list.
THIN_SESSIONS = (
    dt.date(2017, 10, 19),
    dt.date(2018, 11, 7),
    dt.date(2024, 1, 20),
    dt.date(2024, 3, 2),
    dt.date(2024, 5, 18),
    dt.date(2025, 2, 1),
)

#: How close each headline number has to be. Three of them are stored rounded to one decimal, so
#: half of that last place is the honest tolerance.
TOLERANCE = {
    "cagr_pct": 0.01,
    "max_dd_pct": 0.01,
    "trades": 0.0,
    "win_rate_pct": 0.05,
    "profit_factor": 0.005,
    "avg_hold": 0.05,
    "exposure_pct": 0.05,
}

pytestmark = pytest.mark.skipif(
    not (EXPORT / "ohlcv_daily.csv.gz").exists(),
    reason=(
        "the research export is absent. It is 68 MB and gitignored; regenerate it with "
        "research/volume-breakout/export_bars_aws.sh (SSM to the AWS Phase-A box, then S3). "
        "Without it the study cannot be reproduced and this file proves nothing — which is why "
        "it skips rather than passes."
    ),
)


from research_panel import load  # noqa: E402 - the sys.path insert above has to come first


class Panel(Protocol):
    """What `research_panel.load` returns, stated here because mypy cannot follow it.

    The loader lives in `tools/vbt/`, outside every package, and reaches this file through the
    `sys.path` insert above — so its `LoadedPanel` is an untyped import and every attribute read
    would otherwise need an escape hatch (house rule 3). A structural type is the honest fix: it
    says what this test depends on, and it fails if the loader stops providing it.
    """

    @property
    def bars(self) -> pl.DataFrame: ...

    @property
    def calendar(self) -> SessionCalendar: ...

    @property
    def universe(self) -> pl.DataFrame: ...


@pytest.fixture(scope="module")
def loaded() -> Panel:
    return cast(Panel, load(DEFAULT_VBT_CONFIG))


@pytest.fixture(scope="module")
def tagged(loaded: Panel) -> pl.DataFrame:
    frame = with_vbt_indicators(loaded.bars, loaded.calendar, DEFAULT_VBT_CONFIG)
    return with_signal_columns(frame, DEFAULT_VBT_CONFIG)


@pytest.fixture(scope="module")
def result(tagged: pl.DataFrame) -> BacktestResult:
    panel = panel_from_frame(tagged, "state")
    gate = gate_vector(breadth_series(tagged, DEFAULT_VBT_CONFIG), panel.sessions)
    return run_backtest(panel, gate, BacktestParams(start=FIRST_SESSION, config=DEFAULT_VBT_CONFIG))


def study_trades() -> list[dict[str, str]]:
    with (RESULTS / "final_trades.csv").open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def study_metrics() -> dict[str, float]:
    stored = json.loads((RESULTS / "final_metrics.json").read_text(encoding="utf-8"))
    return {name: float(stored[name]) for name in TOLERANCE}


class TestTheDataPlantFindings:
    """STRATEGY §1, as tests. `06` VB2 AC 3."""

    def test_the_rule_finds_exactly_the_six_thin_sessions(self, loaded: Panel) -> None:
        assert loaded.calendar.dropped == THIN_SESSIONS

    def test_a_thin_session_really_was_thin(self, loaded: Panel) -> None:
        """~200 names against ~1,900 — muhurat and special Saturdays, not trading days for a
        daily strategy, and a single such column poisons every window that spans it."""
        counts = loaded.calendar.counts
        ordinary = sorted(counts.values())[len(counts) // 2]
        for session in THIN_SESSIONS:
            assert counts[session] < 0.25 * ordinary, session

    def test_the_universe_is_the_cash_segment_without_etfs(self, loaded: Panel) -> None:
        """`04` §1 — 4,186 instruments in the research's panel, 349 of them ETFs."""
        assert loaded.universe.height >= 3_800
        symbols = set(loaded.universe["symbol"].to_list())
        assert not {"NIFTYBEES", "BANKBEES", "GOLDBEES"} & symbols
        # The ETF patterns are narrow on purpose: a wider one would take GOLDIAM with GOLDBEES.
        assert "GOLDIAM" in symbols

    def test_a_window_tolerates_a_tenth_of_its_bars(self, tagged: pl.DataFrame) -> None:
        """`04` §2.2 — the plant is not dense, and a rule that demanded a full window would
        quietly shrink the universe to the most liquid names."""
        with_average = tagged.filter(pl.col("vol_sma").is_not_null()).height
        with_close = tagged.filter(pl.col("close").is_not_null()).height
        assert with_average > 0
        assert with_average < with_close  # warm-up alone accounts for the difference


class TestTheSignals:
    def test_the_raw_chartink_scan_is_thirty_two_thousand_nine_hundred_and_twenty_nine(
        self, tagged: pl.DataFrame
    ) -> None:
        """`06` VB2 AC 1, and STRATEGY §0's own count."""
        assert int(tagged["scan_hit"].sum()) == 32_929

    def test_vbt_one_is_six_thousand_two_hundred_and_ninety_three(
        self, tagged: pl.DataFrame
    ) -> None:
        """The **six**-filter reading. DECISIONS-VB VB0.2: ``grid2.py``'s eight give 6,254."""
        assert int((tagged["state"] == "SIGNAL").sum()) == 6_293

    def test_the_breadth_series_agrees_with_the_research_file_to_one_name(
        self, tagged: pl.DataFrame
    ) -> None:
        """`06` VB2 AC 2, as measured rather than as hoped.

        The two series differ by at most **one name in the numerator** — about 0.1 of a
        percentage point out of some 1,100 measured names. The cause is a genuine tie and is
        worth naming: a penny stock whose adjusted close has been ₹0.10 for months has a 200-day
        average of ₹0.10, and Polars' rolling mean returns 0.09999999999999999 where pandas'
        returns 0.1. "Is the close above its own average" then has two defensible answers.
        DECISIONS-VB VB2.2.
        """
        ours = breadth_series(tagged, DEFAULT_VBT_CONFIG).sort("date")
        theirs = pl.read_csv(RESULTS / "breadth200.csv").rename({"": "date", "0": "fraction"})
        theirs = theirs.with_columns(pl.col("date").str.to_date()).sort("date")
        joined = ours.join(theirs, on="date", how="inner")
        assert joined.height == ours.height
        gap = (joined["pct_above_dma"] / 100.0 - joined["fraction"]).abs().max()
        assert isinstance(gap, float)
        assert gap < 0.001

    def test_the_forty_percent_gate_verdict_is_identical_on_every_session(
        self, tagged: pl.DataFrame
    ) -> None:
        """And this is the assertion that matters: the strategy's own threshold is nowhere near
        a tie, so not one of 2,396 sessions changes what the book was allowed to do."""
        ours = breadth_series(tagged, DEFAULT_VBT_CONFIG).sort("date")
        theirs = pl.read_csv(RESULTS / "breadth200.csv").rename({"": "date", "0": "fraction"})
        theirs = theirs.with_columns(pl.col("date").str.to_date()).sort("date")
        joined = ours.join(theirs, on="date", how="inner")
        threshold = DEFAULT_VBT_CONFIG.breadth.min_pct_above_dma
        mine = joined["pct_above_dma"] > threshold
        study = joined["fraction"] > threshold / 100.0
        assert int((mine != study).sum()) == 0


class TestTheTrades:
    """`06` VB2 AC 4 — every trade, on identity and then on price."""

    def test_there_are_seven_hundred_and_sixty_one(self, result: BacktestResult) -> None:
        assert len(result.trades) == len(study_trades()) == 761

    def test_every_trade_matches_on_identity(self, result: BacktestResult) -> None:
        reason_of = {
            "ema_close": "EMA_EXIT",
            "stop": "STOP_HIT",
            "stop_day0": "STOP_HIT",
            "stop_gap": "STOP_GAP",
            "no_bar": "NO_BAR",
            "end": "END_OF_RUN",
        }
        differences = [
            (their["symbol"], their["entry_date"], their["qty"], their["reason"])
            for their, our in zip(study_trades(), result.trades, strict=True)
            if not (
                their["symbol"] == our.symbol
                and their["entry_date"] == our.entry_date.isoformat()
                and their["exit_date"] == our.exit_date.isoformat()
                and int(their["qty"]) == our.quantity
                and reason_of[their["reason"]] == our.reason.value
            )
        ]
        assert differences == []

    def test_every_price_agrees_to_the_paisa(self, result: BacktestResult) -> None:
        worst = max(
            max(
                abs(float(our.entry_price) - float(their["entry"])),
                abs(float(our.exit_price) - float(their["exit"])),
            )
            for their, our in zip(study_trades(), result.trades, strict=True)
        )
        assert worst <= 0.01

    def test_the_exits_are_the_ones_strategy_describes(self, result: BacktestResult) -> None:
        """688 left on the EMA and 62 hit the stop — the shape of the whole strategy."""
        stats = summarise(result)
        assert stats is not None
        assert stats.by_reason["EMA_EXIT"] == 688
        assert stats.by_reason["STOP_HIT"] == 62


class TestTheMetrics:
    """`06` VB2 AC 5."""

    def test_every_headline_number_is_within_its_tolerance(self, result: BacktestResult) -> None:
        stats = summarise(result)
        assert stats is not None
        ours = {
            "cagr_pct": stats.cagr_pct,
            "max_dd_pct": stats.max_drawdown_pct,
            "trades": float(stats.trades),
            "win_rate_pct": stats.win_rate_pct,
            "profit_factor": stats.profit_factor or 0.0,
            "avg_hold": stats.avg_hold_sessions,
            "exposure_pct": stats.exposure_pct,
        }
        theirs = study_metrics()
        outside = {
            name: (theirs[name], ours[name])
            for name in TOLERANCE
            if abs(ours[name] - theirs[name]) > TOLERANCE[name]
        }
        assert outside == {}

    def test_the_headline_is_eighteen_two_at_minus_twenty_eight(
        self, result: BacktestResult
    ) -> None:
        """The number STRATEGY §0 leads with, spelled out so a reader of this file sees it."""
        stats = summarise(result)
        assert stats is not None
        assert round(stats.cagr_pct, 1) == 18.2
        assert round(stats.max_drawdown_pct, 1) == -27.9

    def test_the_yearly_table_reproduces_strategy_section_four(
        self, result: BacktestResult
    ) -> None:
        published = {
            2017: 17.3,
            2018: -19.3,
            2019: -3.8,
            2020: 20.4,
            2021: 45.6,
            2022: 16.1,
            2023: 64.7,
            2024: 35.1,
            2025: 1.2,
            2026: 6.3,
        }
        ours = {row.year: round(row.return_pct, 1) for row in yearly(result)}
        assert ours == published

    def test_the_book_is_invested_about_sixty_three_percent_of_the_time(
        self, result: BacktestResult
    ) -> None:
        """The gate's whole point: it *leaves*. An always-invested book is a different product."""
        stats = summarise(result)
        assert stats is not None
        assert 62.0 < stats.exposure_pct < 64.0


class TestTheNeighbourhood:
    """`06` VB2 AC 6 — the entry window's cliff, and that nothing else falls apart."""

    @pytest.mark.parametrize(("sessions", "expected"), [(2, 11.4), (3, 18.2), (5, 17.1)])
    def test_the_entry_window_is_the_parameter_with_a_cliff(
        self, tagged: pl.DataFrame, sessions: int, expected: float
    ) -> None:
        config = dataclasses.replace(DEFAULT_VBT_CONFIG, entry=EntryConfig(valid_sessions=sessions))
        panel = panel_from_frame(tagged, "state")
        gate = gate_vector(breadth_series(tagged, config), panel.sessions)
        run = run_backtest(panel, gate, BacktestParams(start=FIRST_SESSION, config=config))
        stats = summarise(run)
        assert stats is not None
        assert round(stats.cagr_pct, 1) == expected

    def test_the_gate_halves_the_drawdown_for_the_same_return(self, tagged: pl.DataFrame) -> None:
        """STRATEGY §4: no gate is 18.5% at -48.8%. The gate does not buy return; it buys the
        drawdown, and that is the argument for it."""
        panel = panel_from_frame(tagged, "state")
        off = run_backtest(
            panel,
            np.ones(panel.sessions_count, dtype=bool),
            BacktestParams(start=FIRST_SESSION, config=DEFAULT_VBT_CONFIG),
        )
        stats = summarise(off)
        assert stats is not None
        assert round(stats.cagr_pct, 1) == 18.5
        assert round(stats.max_drawdown_pct, 1) == -48.8

    def test_the_raw_chartink_scan_traded_the_same_way_is_flat(self, tagged: pl.DataFrame) -> None:
        """STRATEGY §0's central claim: **the scan as given is not a strategy.** 0.8% a year at
        a 48% drawdown, against 18.2% at 27.9% for the same five lines plus six filters."""
        panel = panel_from_frame(tagged, "scan_hit")
        gate = gate_vector(breadth_series(tagged, DEFAULT_VBT_CONFIG), panel.sessions)
        run = run_backtest(
            panel, gate, BacktestParams(start=FIRST_SESSION, config=DEFAULT_VBT_CONFIG)
        )
        stats = summarise(run)
        assert stats is not None
        assert round(stats.cagr_pct, 1) == 0.8
