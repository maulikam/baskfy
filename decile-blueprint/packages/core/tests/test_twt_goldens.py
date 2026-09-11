"""TW2: the core is the study — and where it is not, the difference is named (``docs/twt/06`` TW2).

``research/tight-close/STRATEGY.md`` is the spec; ``out/final_trades.csv`` (164 trades) and
``out/final_metrics.json`` are its answers, and both are **committed here as fixtures** so that the
grading corpus is self-contained: ``fixtures/twt/golden_trades.csv`` and
``fixtures/twt/golden_metrics.json`` are byte-for-byte copies, and a test in this file proves the
copy whenever ``research/`` is present.

What runs where
---------------
* **Everywhere** — the fixtures are the study's own; the comparison machinery reports every
  difference by kind and size; the seam says whether TW1's engine has landed.
* **Wherever the panel is** (``research/volume-breakout/data/panel.pkl``, 1.7 GB, gitignored) — the
  loader converts it to the plant's frame shape, and the sleeve's own scan is compared against the
  research's reading over 3.58 million bars.
* **Wherever the panel *and* ``baskfy_core.twt.backtest`` are** — the study is re-run and graded.
  The engine is TW1's sibling's and did not exist when this harness was written;
  :class:`TestTheSeam` asserts the failure is loud rather than silent, and the reproduction skips
  with the seam's own reason rather than passing on nothing.
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Final

import polars as pl
import pytest

from baskfy_core.twt.calendar import thin_sessions
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG
from baskfy_core.twt.indicators import REQUIRED_COLUMNS

REPO: Final = Path(__file__).resolve().parents[4]
HARNESS: Final = REPO / "tools" / "twt"
FIXTURES: Final = Path(__file__).resolve().parent / "fixtures" / "twt"
RESEARCH_RESULTS: Final = REPO / "research" / "tight-close" / "out"

sys.path.insert(0, str(HARNESS))

from twt_compare import (  # noqa: E402 - after the sys.path insert above
    DEFAULT_METRIC_TOLERANCE,
    DEFAULT_TRADE_TOLERANCE,
    RESEARCH_EXIT_REASONS,
    DifferenceKind,
    Trade,
    compare_metrics,
    compare_trades,
    read_golden_metrics,
    read_golden_trades,
    trades_from,
)
from twt_goldens import (  # noqa: E402
    GOLDEN_CONFIG,
    GOLDEN_FLOOR_INR,
    GOLDEN_METRICS,
    GOLDEN_SIGNAL_COLUMN,
    GOLDEN_START,
    GOLDEN_TRADES,
    SEAM_MODULE,
    SEAM_NAMES,
    GoldenRun,
    SeamNotReady,
    grade,
    seam,
    seam_status,
    tagged_frame,
)
from twt_panel import PANEL, LoadedPanel, load  # noqa: E402

#: The five modules the harness is (G1).
HARNESS_MODULES: Final[tuple[str, ...]] = (
    "twt_compare.py",
    "twt_goldens.py",
    "twt_panel.py",
    "twt_recall.py",
    "twt_scan.py",
)

#: Calls that put bytes on disk. The harness is read-only against ``research/``, and the only
#: writer it has is the fixture builder.
_WRITE_CALLS: Final[frozenset[str]] = frozenset(
    {"write_text", "write_bytes", "mkdir", "unlink", "touch", "rename", "replace"}
)
#: The one function allowed to contain them, and the directory it writes to.
_THE_ONE_WRITER: Final = ("twt_scan.py", "write")

#: ``01`` §2's own counts over the research panel, and the shape of the panel itself.
PANEL_INSTRUMENTS: Final = 4_186
PANEL_SESSIONS: Final = 2_396
PANEL_BARS: Final = 3_578_815
PANEL_ETFS: Final = 349
STATE_STOCK_DAYS: Final = 115_551
ENTRY_EVENTS: Final = 26_767
SIGNALS_AT_THE_RESEARCH_FLOOR: Final = 21_378

#: ``04`` §2.1's six. The research removed them when it built the panel; the rule must agree.
THIN_SESSIONS: Final[tuple[dt.date, ...]] = (
    dt.date(2017, 10, 19),
    dt.date(2018, 11, 7),
    dt.date(2024, 1, 20),
    dt.date(2024, 3, 2),
    dt.date(2024, 5, 18),
    dt.date(2025, 2, 1),
)

#: STRATEGY §4's headline, spelled out so a reader of this file sees the numbers being graded.
PUBLISHED_METRICS: Final[dict[str, float]] = {
    "cagr_pct": 20.92,
    "max_dd_pct": -24.7,
    "trades": 164.0,
    "win_rate_pct": 40.9,
    "profit_factor": 2.71,
    "avg_hold": 104.6,
}

_PANEL_REASON: Final = (
    f"{PANEL} is absent. It is 1.7 GB and gitignored; rebuild it with "
    "`cd research/volume-breakout && python run_research.py`. The committed goldens under "
    "packages/core/tests/fixtures/twt/ do not need it, and everything that can be asserted "
    "without it is asserted above."
)
_SEAM_READY, _SEAM_WHY = seam_status()

needs_panel = pytest.mark.skipif(not PANEL.exists(), reason=_PANEL_REASON)
needs_engine = pytest.mark.skipif(not _SEAM_READY, reason=_SEAM_WHY)


@pytest.fixture(scope="module")
def loaded() -> LoadedPanel:
    return load(GOLDEN_CONFIG)


@pytest.fixture(scope="module")
def tagged(loaded: LoadedPanel) -> pl.DataFrame:
    return tagged_frame(loaded, GOLDEN_CONFIG)


#: One plausible trade, for the comparison's own unit tests.
A_TRADE: Final = Trade(
    symbol="ACME",
    entry_date=dt.date(2024, 1, 2),
    exit_date=dt.date(2024, 6, 3),
    entry_price=Decimal("100.00"),
    exit_price=Decimal("150.00"),
    quantity=100,
    pnl_inr=Decimal("5000.00"),
    return_pct=Decimal("50.00"),
    hold_sessions=104,
    reason="STOP_HIT",
)


#: The eight field differences the comparison must be able to name, each as a typed mutation of
#: :data:`A_TRADE`. A ``**kwargs`` helper would be shorter and would need an escape hatch to type
#: (house rule 3); a mutator per field says exactly what is being planted.
PLANTED: Final[tuple[tuple[str, Callable[[Trade], Trade], str], ...]] = (
    ("exit_date", lambda t: dataclasses.replace(t, exit_date=dt.date(2024, 6, 4)), "2024-06-04"),
    ("entry_price", lambda t: dataclasses.replace(t, entry_price=Decimal("100.05")), "100.05"),
    ("exit_price", lambda t: dataclasses.replace(t, exit_price=Decimal("149.00")), "149.00"),
    ("quantity", lambda t: dataclasses.replace(t, quantity=99), "99"),
    ("pnl_inr", lambda t: dataclasses.replace(t, pnl_inr=Decimal("4000.00")), "4000.00"),
    ("return_pct", lambda t: dataclasses.replace(t, return_pct=Decimal("40.00")), "40.00"),
    ("hold_sessions", lambda t: dataclasses.replace(t, hold_sessions=103), "103"),
    ("reason", lambda t: dataclasses.replace(t, reason="STOP_GAP"), "STOP_GAP"),
)


class TestTheHarnessIsWhereItSaysItIs:
    """G1."""

    def test_the_five_modules_are_on_disk(self) -> None:
        assert sorted(path.name for path in HARNESS.glob("*.py")) == list(HARNESS_MODULES)

    def test_the_harness_is_read_only_against_research(self) -> None:
        """A source scan, not a promise: the only call in the harness that puts bytes on disk is
        in ``twt_scan.write``, and it writes to the fixtures directory. ``research/`` is the
        answer key and nothing here may touch it."""
        offenders: list[str] = []
        for path in sorted(HARNESS.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                for inner in ast.walk(node):
                    if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Attribute):
                        continue
                    where = (path.name, node.name)
                    if inner.func.attr in _WRITE_CALLS and where != _THE_ONE_WRITER:
                        offenders.append(f"{path.name}:{node.name} calls {inner.func.attr}")
        assert offenders == []

    def test_nothing_in_the_harness_opens_a_file_for_writing(self) -> None:
        offenders: list[str] = []
        for path in sorted(HARNESS.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if name != "open":
                    continue
                modes = [
                    argument.value
                    for argument in node.args
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                ]
                if any("w" in mode or "a" in mode or "+" in mode for mode in modes):
                    offenders.append(f"{path.name}: open({modes})")
        assert offenders == []


class TestTheGoldenFixtures:
    """G4 and G5 — committed copies, and what they say."""

    def test_both_goldens_are_committed_and_small(self) -> None:
        assert GOLDEN_TRADES.is_file() and GOLDEN_METRICS.is_file()
        assert GOLDEN_TRADES.stat().st_size < 100_000
        assert GOLDEN_METRICS.stat().st_size < 10_000

    def test_the_panel_is_not_committed(self) -> None:
        """TW2's own instruction: the goldens live here and the 1.7 GB panel does not."""
        assert not (FIXTURES / "panel.pkl").exists()
        assert all(path.stat().st_size < 200_000 for path in FIXTURES.iterdir())

    @pytest.mark.skipif(
        not RESEARCH_RESULTS.exists(), reason="research/tight-close/out/ is not in this checkout"
    )
    @pytest.mark.parametrize(
        ("fixture", "original"),
        [("golden_trades.csv", "final_trades.csv"), ("golden_metrics.json", "final_metrics.json")],
    )
    def test_each_fixture_is_byte_for_byte_the_researchs_own_file(
        self, fixture: str, original: str
    ) -> None:
        """**The fixtures are copies, never regenerations.** A golden this repository computed for
        itself would only prove the code agrees with itself."""
        assert (FIXTURES / fixture).read_bytes() == (RESEARCH_RESULTS / original).read_bytes()

    def test_there_are_one_hundred_and_sixty_four_trades(self) -> None:
        assert len(read_golden_trades(GOLDEN_TRADES)) == 164

    def test_every_headline_number_is_the_studys_own(self) -> None:
        stored = read_golden_metrics(GOLDEN_METRICS)
        for name, published in PUBLISHED_METRICS.items():
            assert stored[name] == pytest.approx(published), name

    def test_the_exits_are_the_five_the_study_reports(self) -> None:
        """137 stops (the trail, which the research simulator reports as ``stop``), 15 gaps, two
        on the entry session's own low, and ten still open when the history ran out."""
        counts: dict[str, int] = {}
        for trade in read_golden_trades(GOLDEN_TRADES):
            counts[trade.reason] = counts.get(trade.reason, 0) + 1
        assert counts == {"STOP_HIT": 137, "STOP_GAP": 15, "STOP_DAY0": 2, "END_OF_RUN": 10}
        assert set(RESEARCH_EXIT_REASONS.values()) >= set(counts)

    def test_the_trade_list_and_the_metrics_agree_with_each_other(self) -> None:
        """Two files, one study: the trade count in the metrics is the row count of the trades."""
        metrics = read_golden_metrics(GOLDEN_METRICS)
        assert metrics["trades"] == len(read_golden_trades(GOLDEN_TRADES))

    def test_the_average_hold_is_the_position_book_the_note_describes(self) -> None:
        trades = read_golden_trades(GOLDEN_TRADES)
        average = sum(trade.hold_sessions for trade in trades) / len(trades)
        assert average == pytest.approx(read_golden_metrics(GOLDEN_METRICS)["avg_hold"], abs=0.05)


class TestTheComparisonNamesEveryDifference:
    """G6 and G7 — the machinery, on planted differences."""

    def test_two_identical_lists_are_clean(self) -> None:
        trades = read_golden_trades(GOLDEN_TRADES)
        outcome = compare_trades(trades, trades)
        assert outcome.clean
        assert outcome.matched_count == 164
        assert "identical" in outcome.render()

    def test_a_missing_trade_is_reported_as_missing_and_not_as_wrong_prices(self) -> None:
        expected = [A_TRADE, dataclasses.replace(A_TRADE, symbol="BETA")]
        outcome = compare_trades([expected[0]], expected)
        assert outcome.by_kind() == {"missing": 1}
        assert outcome.differences[0].symbol == "BETA"

    def test_an_extra_trade_is_reported_as_extra(self) -> None:
        outcome = compare_trades([A_TRADE, dataclasses.replace(A_TRADE, symbol="BETA")], [A_TRADE])
        assert outcome.by_kind() == {"extra": 1}

    @pytest.mark.parametrize(("field", "mutate", "shown"), PLANTED, ids=[p[0] for p in PLANTED])
    def test_each_field_difference_is_named_by_its_own_name(
        self, field: str, mutate: Callable[[Trade], Trade], shown: str
    ) -> None:
        outcome = compare_trades([mutate(A_TRADE)], [A_TRADE])
        assert outcome.by_field() == {field: 1}
        difference = outcome.differences[0]
        assert difference.kind is DifferenceKind.FIELD
        assert difference.field == field
        assert shown in difference.render()

    def test_a_numeric_difference_carries_its_size(self) -> None:
        outcome = compare_trades(
            [dataclasses.replace(A_TRADE, exit_price=Decimal("151.25"))], [A_TRADE]
        )
        assert outcome.differences[0].size == Decimal("1.25")
        assert "(by 1.25)" in outcome.differences[0].render()

    def test_a_date_difference_has_no_size_and_that_is_not_a_small_one(self) -> None:
        outcome = compare_trades(
            [dataclasses.replace(A_TRADE, exit_date=dt.date(2025, 1, 1))], [A_TRADE]
        )
        assert outcome.differences[0].size is None
        assert not outcome.differences[0].within_tolerance
        assert not outcome.passes

    def test_one_paisa_in_a_hundred_and_sixty_four_trades_is_still_a_difference(self) -> None:
        """G7. There is no mean, no RMS and no "close enough": the single changed trade is in the
        report by name and by size, and ``clean`` is false."""
        golden = read_golden_trades(GOLDEN_TRADES)
        nudged = list(golden)
        nudged[7] = dataclasses.replace(
            nudged[7], exit_price=nudged[7].exit_price + Decimal("0.01")
        )
        outcome = compare_trades(nudged, golden)
        assert not outcome.clean
        assert len(outcome.differences) == 1
        assert outcome.differences[0].size == Decimal("0.01")
        assert golden[7].symbol in outcome.render()

    def test_a_tolerance_marks_a_difference_and_never_removes_one(self) -> None:
        golden = read_golden_trades(GOLDEN_TRADES)
        nudged = list(golden)
        nudged[7] = dataclasses.replace(
            nudged[7], exit_price=nudged[7].exit_price + Decimal("0.01")
        )
        outcome = compare_trades(nudged, golden, DEFAULT_TRADE_TOLERANCE)
        assert outcome.passes  # inside the paisa tolerance
        assert not outcome.clean  # and still reported
        assert outcome.differences[0].within_tolerance
        assert "[inside tolerance]" in outcome.differences[0].render()

    def test_a_duplicated_key_is_refused_rather_than_guessed(self) -> None:
        with pytest.raises(ValueError, match="same \\(symbol, entry_date\\)"):
            compare_trades([A_TRADE, A_TRADE], [A_TRADE])

    def test_the_render_lists_the_differences_and_says_how_many_it_did_not(self) -> None:
        golden = read_golden_trades(GOLDEN_TRADES)
        outcome = compare_trades(golden[:5], golden)
        text = outcome.render(limit=2)
        assert "159 differences" in text
        assert "and 157 more" in text

    def test_the_metrics_comparison_reports_each_number_against_its_tolerance(self) -> None:
        study = read_golden_metrics(GOLDEN_METRICS)
        outcome = compare_metrics(study, study)
        assert outcome.passes
        assert len(outcome.metrics) == len(DEFAULT_METRIC_TOLERANCE)
        assert "cagr_pct" in outcome.render()

    def test_a_metric_the_run_did_not_compute_is_outside_every_tolerance(self) -> None:
        """A reproduction that forgot a number has not reproduced it: a missing key is ``nan``,
        never a skip."""
        study = read_golden_metrics(GOLDEN_METRICS)
        outcome = compare_metrics({}, study)
        assert not outcome.passes
        assert len(outcome.outside()) == len(DEFAULT_METRIC_TOLERANCE)

    def test_a_metric_outside_its_tolerance_is_named_and_signed(self) -> None:
        study = read_golden_metrics(GOLDEN_METRICS)
        outcome = compare_metrics({**study, "cagr_pct": 18.0}, study)
        assert [metric.name for metric in outcome.outside()] == ["cagr_pct"]
        assert outcome.outside()[0].delta == pytest.approx(-2.92)
        assert "OUT" in outcome.render()

    def test_a_produced_trade_missing_a_field_says_which_one(self) -> None:
        class Partial:
            symbol = "ACME"

        with pytest.raises(AttributeError, match="entry_date"):
            trades_from([Partial()])


class TestTheSeam:
    """G12 — where TW1's engine plugs in, and what happens while it is not there."""

    def test_the_seam_names_the_module_and_every_function_it_needs(self) -> None:
        assert SEAM_MODULE == "baskfy_core.twt.backtest"
        assert set(SEAM_NAMES) == {
            "panel_from_frame",
            "gate_vector",
            "run_backtest",
            "summarise",
            "BacktestParams",
        }

    def test_the_status_is_a_sentence_a_person_can_act_on(self) -> None:
        ready, why = seam_status()
        assert SEAM_MODULE in why
        if not ready:
            assert "does not exist yet" in why or "missing" in why

    @pytest.mark.skipif(_SEAM_READY, reason="the engine has landed; there is nothing to fail")
    def test_without_the_engine_the_runner_raises_rather_than_returning_nothing(self) -> None:
        """``produce`` asks :func:`goldens.seam` for the engine before it touches a frame, so this
        is the failure every caller gets — loud, named, and never an empty trade list."""
        with pytest.raises(SeamNotReady) as raised:
            seam()
        assert SEAM_MODULE in str(raised.value)
        for name in SEAM_NAMES:
            assert name in str(raised.value)

    def test_the_golden_set_is_the_studys_floor_and_the_sleeves_everything_else(self) -> None:
        """``04`` §3.5: the study ran at ₹2 crore and the sleeve ships at ₹5 crore, and the floor
        is passed as ``signal_mask``'s ``floor_inr`` rather than by editing the config."""
        assert GOLDEN_CONFIG is DEFAULT_TWT_CONFIG
        assert DEFAULT_TWT_CONFIG.entry.research_min_turnover_inr == GOLDEN_FLOOR_INR
        assert Decimal("20000000") == GOLDEN_FLOOR_INR
        assert DEFAULT_TWT_CONFIG.entry.min_turnover_inr == Decimal("50000000")

    def test_the_study_window_is_the_one_final_tc_ran(self) -> None:
        assert dt.date(2017, 10, 16) == GOLDEN_START


@needs_panel
class TestThePanelLoader:
    """G2 and G3 — the research's panel, in the plant's shape."""

    def test_the_frame_has_every_column_the_indicators_require(self, loaded: LoadedPanel) -> None:
        assert set(REQUIRED_COLUMNS) <= set(loaded.bars.columns)
        assert loaded.bars.schema["date"] == pl.Date
        assert loaded.bars.schema["instrument_id"] == pl.Int64
        assert loaded.bars.schema["close"] == pl.Float64

    def test_it_is_the_studys_own_panel(self, loaded: LoadedPanel) -> None:
        assert loaded.universe.height == PANEL_INSTRUMENTS
        assert len(loaded.calendar.sessions) == PANEL_SESSIONS
        assert loaded.bars.height == PANEL_BARS
        assert int(loaded.universe["is_etf"].sum()) == PANEL_ETFS

    def test_the_research_package_is_never_imported(self) -> None:
        """The pickle names two research dataclasses; the loader rebinds them rather than
        importing ``research/volume-breakout/vbt/``, so loading the panel cannot execute research
        code or put it on the import path."""
        assert "vbt" not in sys.modules
        assert "vbt.data" not in sys.modules
        assert not [entry for entry in sys.path if entry.endswith("volume-breakout")]

    def test_the_thin_session_rule_finds_nothing_left_to_drop(self, loaded: LoadedPanel) -> None:
        """G3. The research dropped its six when it built the panel and ``04`` §2.1's rule agrees:
        two implementations, one answer."""
        assert loaded.dropped_sessions == ()
        assert thin_sessions(loaded.bars, DEFAULT_TWT_CONFIG.data) == []

    def test_the_six_thin_sessions_are_absent_from_the_panel(self, loaded: LoadedPanel) -> None:
        sessions = set(loaded.calendar.sessions)
        assert not sessions & set(THIN_SESSIONS)

    def test_a_missing_bar_is_a_missing_row_and_never_a_forward_fill(
        self, loaded: LoadedPanel
    ) -> None:
        """3.58 M rows out of 4,186 x 2,396 possible: the panel is sparse, and the loader keeps it
        sparse. A dense frame with carried-forward closes would make every fill price a fiction."""
        assert loaded.bars.height < PANEL_INSTRUMENTS * PANEL_SESSIONS // 2
        assert loaded.bars["close"].null_count() == 0


@needs_panel
class TestTheSleevesScanIsTheStudys:
    """The half of TW2 that needs no engine — and the strongest evidence available before one."""

    def test_the_state_is_the_studys_own_count(self, tagged: pl.DataFrame) -> None:
        assert int(tagged["tight_state"].sum()) == STATE_STOCK_DAYS

    def test_the_entry_events_are_the_studys_own_count(self, tagged: pl.DataFrame) -> None:
        """``01`` §2: 26,767 since 2017. **DECISIONS-TW TW0.6 predicted a difference here and the
        measurement is that there is none** — no name's listing day is an entry on this panel."""
        assert int(tagged["entry_event"].sum()) == ENTRY_EVENTS

    def test_the_signals_at_the_research_floor_are_the_studys_own_count(
        self, tagged: pl.DataFrame
    ) -> None:
        """21,378 at ₹2 crore — the funnel ``01`` §2 reports, and the set the study traded from."""
        assert int(tagged[GOLDEN_SIGNAL_COLUMN].sum()) == SIGNALS_AT_THE_RESEARCH_FLOOR

    def test_the_breadth_gate_verdict_is_the_studys_on_every_session(
        self, tagged: pl.DataFrame
    ) -> None:
        """The gate decides whether the book may enter at all, so the assertion that matters is
        not the percentage but the verdict: not one of 2,396 sessions disagrees.

        The percentages differ by at most 0.096 of a point — about one name in a thousand — for the
        reason DECISIONS-VB VB2.2 records: a rolling mean computed by Polars and by pandas can
        disagree in the last bit, and "is the close above its own average" then has two defensible
        answers. DECISIONS-TW **TW2.5**.
        """
        from baskfy_core.twt.breadth import breadth_series  # noqa: PLC0415

        ours = breadth_series(tagged, GOLDEN_CONFIG).sort("date")
        theirs = pl.read_csv(RESEARCH_RESULTS / "breadth200.csv").rename(
            {"": "date", "0": "fraction"}
        )
        theirs = theirs.with_columns(pl.col("date").str.to_date()).sort("date")
        joined = ours.join(theirs, on="date", how="inner")
        assert joined.height == PANEL_SESSIONS
        threshold = GOLDEN_CONFIG.breadth.min_pct_above_dma
        gap = (joined["pct_above_dma"] / 100.0 - joined["fraction"]).abs().max()
        assert isinstance(gap, float)
        assert gap < 0.001
        disagreements = (
            (joined["pct_above_dma"] > threshold) != (joined["fraction"] > float(threshold) / 100.0)
        ).sum()
        assert int(disagreements) == 0


@pytest.fixture(scope="module")
def outcome(loaded: LoadedPanel) -> GoldenRun:
    """The study, re-run by the sleeve's own functions and graded. Needs the engine."""
    from twt_goldens import execute, produce, produce_metrics  # noqa: PLC0415 - seam-gated

    result = execute(loaded, GOLDEN_CONFIG)
    return grade(produce(result), produce_metrics(result))


@needs_panel
@needs_engine
class TestTheStudyIsReproduced:
    """TW2's AC, in full. Runs when both the panel and TW1's engine are present."""

    def test_the_trade_list_reproduces_the_study(self, outcome: GoldenRun) -> None:
        """Every difference is in the report, by kind and by size. A difference that survives
        investigation is a DECISIONS-TW entry and a blocker for TW9, never a footnote."""
        assert outcome.trade_comparison.passes, outcome.trade_comparison.render()

    def test_there_are_one_hundred_and_sixty_four_of_them(self, outcome: GoldenRun) -> None:
        assert outcome.trade_comparison.produced_count == 164

    def test_every_headline_number_is_inside_its_tolerance(self, outcome: GoldenRun) -> None:
        assert outcome.metric_comparison.passes, outcome.metric_comparison.render()

    def test_the_headline_is_twenty_point_nine_at_minus_twenty_four_seven(
        self, outcome: GoldenRun
    ) -> None:
        assert round(outcome.metrics["cagr_pct"], 2) == 20.92
        assert round(outcome.metrics["max_dd_pct"], 1) == -24.7
