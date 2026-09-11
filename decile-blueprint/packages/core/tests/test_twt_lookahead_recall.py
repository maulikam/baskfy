"""TW2: how much of Chartink's export each reading reproduces, and why the sleeve ships one of them.

``research/tight-close/STRATEGY.md`` §1 is the finding. Chartink's **backtester** evaluates a
weekly candle as a completed candle, so its historical export of this scan knows Friday's close on
Monday. Read point-in-time the panel reproduces **64.9 %** of its stock-days at **61.5 %**
precision; read with look-ahead, **83.1 %** at **97.8 %**. House rule 5 is why the sleeve ships the
first: the second is not a better reproduction, it is the *size of the look-ahead*.

**This file needs neither the 1.7 GB panel nor a running backtest engine.** The two readings, over
the window Chartink's export covers, are committed under ``fixtures/twt/`` as stock-day lists, and
``tools/twt/twt_scan.py --verify`` rebuilds them from the panel byte for byte (the
panel-gated class at the end of this file is that check as a test). So the arithmetic that turns a
reading into a recall number is asserted everywhere, and the reading itself is asserted wherever
the panel is.

When TW1's core lands, the only new thing is **which reading is fed in**:
:class:`TestTheSleevesOwnReadingIsThePointInTimeOne` feeds the sleeve's ``tight_state`` and
``twt_lookahead.tight_state_lookahead`` through the same scorer and asserts the same two numbers.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Final

import polars as pl
import pytest
from twt_lookahead import tight_state_lookahead, with_lookahead_columns

import baskfy_core.twt as twt_package
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG
from baskfy_core.twt.signals import tight_state, with_twt_columns

REPO: Final = Path(__file__).resolve().parents[4]
FIXTURES: Final = Path(__file__).resolve().parent / "fixtures" / "twt"
SLEEVE_SOURCE: Final = REPO / "decile-blueprint" / "packages" / "core" / "src" / "baskfy_core"
SLEEVE_MODULES: Final = SLEEVE_SOURCE / "twt"

# ``tools/`` is not a package on the core's import path; the harness lives there beside the
# repository's other operator tools so that TW2's reproduction and TW9's plant run cannot drift
# apart by having two copies of it. Same arrangement as ``test_vbt_goldens.py``.
sys.path.insert(0, str(REPO / "tools" / "twt"))

from twt_panel import PANEL  # noqa: E402
from twt_recall import MissReasons, ScoreCard, score  # noqa: E402 - after the sys.path insert
from twt_scan import (  # noqa: E402
    MIN_CLOSE_RAW,
    MIN_VOL_SMA,
    MONTHS_AGO,
    TIGHT_PCT,
    TIGHT_WEEKS,
    VOL_SMA_BARS,
    Reading,
    Window,
    read_pairs,
    verify,
)

#: STRATEGY §1's four numbers, and how close a reproduction has to come (TW2's AC: ±0.5 pt).
PUBLISHED: Final[dict[str, tuple[float, float]]] = {
    #: reading -> (recall %, precision %)
    Reading.POINT_IN_TIME.value: (64.9, 61.5),
    Reading.LOOK_AHEAD.value: (83.1, 97.8),
}
RECALL_TOLERANCE_PT: Final = 0.5

#: What the scan must never be able to reach from inside the sleeve (DECISIONS-TW TW0.1).
FORBIDDEN_IN_THE_SLEEVE: Final[tuple[str, ...]] = (
    "tight_state_lookahead",
    "include_current_week",
    "lookahead",
    "look_ahead",
    "TightReading",
)


@pytest.fixture(scope="module")
def indicated() -> pl.DataFrame:
    """The research panel with every indicator column. Built once; only the panel-gated class
    asks for it, so a checkout without the panel never constructs it."""
    from twt_panel import load  # noqa: PLC0415

    from baskfy_core.twt.indicators import with_twt_indicators  # noqa: PLC0415

    loaded = load(DEFAULT_TWT_CONFIG)
    return with_twt_indicators(loaded.bars, loaded.calendar, DEFAULT_TWT_CONFIG)


def fixture_pairs(name: str) -> list[tuple[dt.date, str]]:
    return read_pairs(FIXTURES / f"{name}.csv.gz")


def manifest() -> dict[str, object]:
    loaded = json.loads((FIXTURES / "recall_manifest.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def window() -> Window:
    span = manifest()["window"]
    assert isinstance(span, dict)
    return Window(
        first=dt.date.fromisoformat(str(span["first"])),
        last=dt.date.fromisoformat(str(span["last"])),
    )


class TestTheScorerSaysWhatItMeasures:
    """G8 — a recall number is only as honest as its definitions, so the definitions are tests."""

    def test_a_match_is_the_session_and_the_symbol_and_nothing_else(self) -> None:
        day = dt.date(2026, 3, 2)
        card = score([(day, "ACME")], [(day, "ACME")])
        assert (card.matched, card.recall_pct, card.precision_pct) == (1, 100.0, 100.0)

    def test_the_same_symbol_on_another_session_is_not_a_match(self) -> None:
        card = score([(dt.date(2026, 3, 3), "ACME")], [(dt.date(2026, 3, 2), "ACME")])
        assert card.matched == 0
        assert card.missed == ((dt.date(2026, 3, 2), "ACME"),)
        assert card.spurious == ((dt.date(2026, 3, 3), "ACME"),)

    def test_recall_and_precision_are_each_others_mirror(self) -> None:
        """Symmetry, asserted rather than assumed: swap the arguments and the two swap."""
        day = dt.date(2026, 3, 2)
        mine = [(day, "A"), (day, "B"), (day, "C")]
        theirs = [(day, "B"), (day, "C"), (day, "D"), (day, "E")]
        forward, backward = score(mine, theirs), score(theirs, mine)
        assert forward.recall_pct == pytest.approx(backward.precision_pct)
        assert forward.precision_pct == pytest.approx(backward.recall_pct)
        assert forward.missed == backward.spurious
        assert forward.spurious == backward.missed

    def test_a_stock_day_nobody_could_produce_is_still_a_miss(self) -> None:
        """**The rule that keeps the number honest.** A name the panel has no bar for on that
        session — or does not carry at all — stays in the denominator. Dropping the unreachable
        rows would turn a data gap into a better score."""
        day = dt.date(2026, 3, 2)
        card = score([(day, "ACME")], [(day, "ACME"), (day, "DELISTED")])
        assert card.expected == 2
        assert card.recall_pct == pytest.approx(50.0)
        assert (day, "DELISTED") in card.missed

    def test_a_duplicate_is_one_stock_day(self) -> None:
        day = dt.date(2026, 3, 2)
        card = score([(day, "ACME"), (day, "ACME")], [(day, "ACME")])
        assert (card.produced, card.matched, card.precision_pct) == (1, 1, 100.0)

    def test_an_empty_answer_key_scores_zero_and_not_a_hundred(self) -> None:
        assert score([], []).recall_pct == 0.0
        assert score([(dt.date(2026, 3, 2), "A")], []).recall_pct == 0.0

    def test_the_card_renders_both_sides_and_a_sample_of_each(self) -> None:
        day = dt.date(2026, 3, 2)
        text = score([(day, "A")], [(day, "B")]).render_long(MissReasons({"no_bar": 1}))
        assert "recall 0.0%" in text
        assert "2026-03-02 B" in text  # the miss is named, not counted away
        assert "no_bar 1" in text


class TestTheFixturesAreTheResearchsOwnReading:
    """G9's inputs. Committed, small, and described by their own manifest."""

    def test_every_fixture_is_present_and_small_enough_to_live_here(self) -> None:
        for name in ("recall_chartink", "recall_point_in_time", "recall_look_ahead"):
            path = FIXTURES / f"{name}.csv.gz"
            assert path.is_file(), path
            assert path.stat().st_size < 200_000, f"{path} is too big to commit"

    def test_the_window_is_chartinks_own_export_span(self) -> None:
        assert window() == Window(first=dt.date(2026, 1, 21), last=dt.date(2026, 9, 9))

    def test_the_answer_key_is_the_nine_thousand_two_hundred_and_fifty_four(self) -> None:
        """``tscan_verify.py``'s own count of Chartink's export, index rows removed."""
        assert len(fixture_pairs("recall_chartink")) == 9_254
        assert manifest()["chartink_stock_days"] == 9_254

    def test_every_fixture_row_is_inside_the_window(self) -> None:
        span = window()
        for name in ("recall_chartink", "recall_point_in_time", "recall_look_ahead"):
            assert all(span.holds(session) for session, _ in fixture_pairs(name)), name

    def test_the_manifest_counts_are_the_rows_on_disk(self) -> None:
        counts = manifest()["reading_stock_days"]
        assert isinstance(counts, dict)
        for reading in Reading:
            assert counts[reading.value] == len(fixture_pairs(f"recall_{reading.value}"))


class TestTheResearchsFourNumbersAreReproduced:
    """G9 — TW2's AC, from the fixtures, with no panel and no engine."""

    @pytest.mark.parametrize("reading", list(Reading), ids=[r.value for r in Reading])
    def test_the_recall_and_precision_are_the_published_ones(self, reading: Reading) -> None:
        card = score(fixture_pairs(f"recall_{reading.value}"), fixture_pairs("recall_chartink"))
        recall, precision = PUBLISHED[reading.value]
        assert card.recall_pct == pytest.approx(recall, abs=RECALL_TOLERANCE_PT)
        assert card.precision_pct == pytest.approx(precision, abs=RECALL_TOLERANCE_PT)

    def test_the_look_ahead_reading_is_the_one_that_agrees_with_chartink(self) -> None:
        """The whole finding in one assertion: knowing Friday's close on Monday buys 18 points of
        recall and 36 of precision. That is the size of the look-ahead, not the size of an edge."""
        answer_key = fixture_pairs("recall_chartink")
        point_in_time = score(fixture_pairs("recall_point_in_time"), answer_key)
        look_ahead = score(fixture_pairs("recall_look_ahead"), answer_key)
        assert look_ahead.recall_pct > point_in_time.recall_pct + 15
        assert look_ahead.precision_pct > point_in_time.precision_pct + 30

    def test_the_point_in_time_reading_produces_more_names_and_finds_fewer(self) -> None:
        """Why precision falls: the partial-week reading calls a base tight that the finished week
        widens. 9,769 produced against Chartink's 9,254, and only 6,005 of them agreed."""
        card = score(fixture_pairs("recall_point_in_time"), fixture_pairs("recall_chartink"))
        assert card.produced == 9_769
        assert card.matched == 6_005

    def test_the_misses_are_explained_and_the_explanation_adds_up(self) -> None:
        """STRATEGY §1 names two of the buckets — 832 no-bar days and 602 names without a clean
        50-session volume window. The classification is a description of the misses, never a
        deduction from them: the reasons sum to the miss count exactly."""
        reasons = manifest()["miss_reasons"]
        assert isinstance(reasons, dict)
        for reading in Reading:
            card = score(fixture_pairs(f"recall_{reading.value}"), fixture_pairs("recall_chartink"))
            counted = MissReasons(reasons[reading.value])
            assert counted.total == len(card.missed), reading.value
        point_in_time = reasons[Reading.POINT_IN_TIME.value]
        assert point_in_time["no_bar"] == 832
        assert point_in_time["no_volume_average"] == 602


class TestTheLookAheadReadingIsNotReachableFromTheSleeve:
    """G11 — ``04`` §3.2 and DECISIONS-TW TW0.1, asserted rather than trusted."""

    def test_the_package_exports_nothing_that_names_it(self) -> None:
        exported = set(dir(twt_package))
        for name in FORBIDDEN_IN_THE_SLEEVE:
            assert name not in exported, f"baskfy_core.twt exports {name}"

    def test_no_module_of_the_sleeve_mentions_it_outside_a_docstring(self) -> None:
        """A source scan, because an attribute scan only sees what a module chose to export."""
        offenders: list[str] = []
        for path in sorted(SLEEVE_MODULES.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name | ast.Attribute | ast.arg):
                    spelled = (
                        node.id
                        if isinstance(node, ast.Name)
                        else (node.attr if isinstance(node, ast.Attribute) else node.arg)
                    )
                    if spelled in FORBIDDEN_IN_THE_SLEEVE:
                        offenders.append(f"{path.name}: {spelled}")
        assert offenders == []

    def test_tight_state_takes_no_switch(self) -> None:
        """The function's own signature: a frame and a config, and there is no third thing."""
        import inspect  # noqa: PLC0415 - the one test that needs it

        parameters = list(inspect.signature(tight_state).parameters)
        assert parameters == ["framed", "config"]

    def test_the_reading_lives_in_the_test_tree(self) -> None:
        """And it is here — the file this test module imports it from is under ``tests/``."""
        assert Path(tight_state_lookahead.__module__.replace(".", "/")).name == "twt_lookahead"
        assert (Path(__file__).parent / "twt_lookahead.py").is_file()


class TestTheResearchsThresholdsAndTheSleevesAgree:
    """The harness re-implements the research's reading with its own copies of the thresholds, so
    that a change to the sleeve's config cannot silently move the *research's* half of the
    comparison. That the two are equal is therefore a thing to assert, not to assume."""

    def test_every_threshold_of_the_scan_is_the_same_number_on_both_sides(self) -> None:
        scan = DEFAULT_TWT_CONFIG.scan
        assert float(scan.min_close_raw) == MIN_CLOSE_RAW
        assert float(scan.min_vol_sma) == MIN_VOL_SMA
        assert scan.vol_sma_bars == VOL_SMA_BARS
        assert float(scan.tight_band_pct) == TIGHT_PCT
        assert scan.tight_weeks == TIGHT_WEEKS
        assert scan.month_low_months_back == MONTHS_AGO


@pytest.mark.skipif(
    not PANEL.exists(),
    reason=(
        f"{PANEL} is absent. It is 1.7 GB and gitignored; rebuild it with "
        "`cd research/volume-breakout && python run_research.py`. Everything above this point "
        "runs without it — what is skipped here is the re-derivation of the committed fixtures "
        "from the bars, and the check that the sleeve's own reading is the point-in-time one."
    ),
)
class TestTheSleevesOwnReadingIsThePointInTimeOne:
    """G10, and TW2's AC in full: the same scorer, fed the sleeve's reading instead of a fixture."""

    def _stock_days(self, frame: pl.DataFrame) -> list[tuple[dt.date, str]]:
        span = window()
        rows = frame.filter(
            pl.col("tight_state") & (pl.col("date") >= span.first) & (pl.col("date") <= span.last)
        ).select(["date", "symbol"])
        return [(session, str(symbol)) for session, symbol in rows.iter_rows()]

    def _card(self, frame: pl.DataFrame) -> ScoreCard:
        return score(self._stock_days(frame), fixture_pairs("recall_chartink"))

    def test_the_committed_fixtures_are_re_derivable_from_the_panel(self) -> None:
        """``tools/twt/twt_scan.py --verify``, as a test. The fixtures are a copy of a
        computation, and this is the check that the copy is still the computation."""
        assert verify() is True

    def test_the_sleeves_reading_is_the_point_in_time_fixture(
        self, indicated: pl.DataFrame
    ) -> None:
        from baskfy_core.twt.signals import month_low_back, weekly_closes  # noqa: PLC0415

        framed = month_low_back(weekly_closes(indicated, DEFAULT_TWT_CONFIG), DEFAULT_TWT_CONFIG)
        mine = self._stock_days(tight_state(framed, DEFAULT_TWT_CONFIG))
        card = score(mine, fixture_pairs("recall_point_in_time"))
        assert card.missed == ()
        assert card.spurious == ()

    def test_the_sleeves_reading_scores_the_published_sixty_four_nine(
        self, indicated: pl.DataFrame
    ) -> None:
        from baskfy_core.twt.signals import month_low_back, weekly_closes  # noqa: PLC0415

        framed = month_low_back(weekly_closes(indicated, DEFAULT_TWT_CONFIG), DEFAULT_TWT_CONFIG)
        card = self._card(tight_state(framed, DEFAULT_TWT_CONFIG))
        recall, precision = PUBLISHED[Reading.POINT_IN_TIME.value]
        assert card.recall_pct == pytest.approx(recall, abs=RECALL_TOLERANCE_PT)
        assert card.precision_pct == pytest.approx(precision, abs=RECALL_TOLERANCE_PT)

    def test_the_test_modules_look_ahead_reading_scores_the_published_eighty_three_one(
        self, indicated: pl.DataFrame
    ) -> None:
        card = self._card(tight_state_lookahead(indicated, DEFAULT_TWT_CONFIG))
        recall, precision = PUBLISHED[Reading.LOOK_AHEAD.value]
        assert card.recall_pct == pytest.approx(recall, abs=RECALL_TOLERANCE_PT)
        assert card.precision_pct == pytest.approx(precision, abs=RECALL_TOLERANCE_PT)

    def test_the_two_readings_differ_only_in_the_current_weeks_close(
        self, indicated: pl.DataFrame
    ) -> None:
        """The claim the whole finding rests on: ``w1`` and ``w2`` are identical under both, so
        the 18 points of recall come from ``w0`` and from nothing else."""
        from twt_lookahead import weekly_closes_lookahead  # noqa: PLC0415

        from baskfy_core.twt.signals import weekly_closes  # noqa: PLC0415

        mine = weekly_closes(indicated, DEFAULT_TWT_CONFIG).sort(["instrument_id", "date"])
        theirs = weekly_closes_lookahead(indicated, DEFAULT_TWT_CONFIG).sort(
            ["instrument_id", "date"]
        )
        for column in ("weekly_close_1", "weekly_close_2"):
            assert mine[column].equals(theirs[column]), column
        assert not mine["weekly_close_0"].equals(theirs["weekly_close_0"])

    def test_with_lookahead_columns_is_the_same_reading_from_bars(self) -> None:
        """The mirror of ``with_twt_columns``, so a caller cannot accidentally build the
        look-ahead state a different way and get a different number."""
        from twt_panel import load  # noqa: PLC0415

        loaded = load(DEFAULT_TWT_CONFIG)
        card = self._card(with_lookahead_columns(loaded.bars, loaded.calendar, DEFAULT_TWT_CONFIG))
        assert card.recall_pct == pytest.approx(83.1, abs=RECALL_TOLERANCE_PT)

    def test_the_sleeve_detects_the_same_state_as_the_research_over_the_whole_history(self) -> None:
        """Not the window — every session of 2017 → 2026. ``01`` §2's own count is 115,551."""
        from twt_panel import load  # noqa: PLC0415

        loaded = load(DEFAULT_TWT_CONFIG)
        frame = with_twt_columns(loaded.bars, loaded.calendar, DEFAULT_TWT_CONFIG)
        assert int(frame["tight_state"].sum()) == 115_551
        assert int(frame["entry_event"].sum()) == 26_767
