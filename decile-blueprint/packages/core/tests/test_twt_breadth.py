"""The regime gate (``docs/twt/04`` §4).

Two things are asserted here that a reader would otherwise have to trust, and both are named in
``docs/twt/06`` TW1's acceptance criteria: **the gate is strict** — ``> 40 %``, not ``>=`` — and **a
zero denominator is SHUT**. An empty universe must never read as a healthy market, which is exactly
what an "open when we cannot tell" default would make it.

The third thing is DECISIONS-TW **TW0.4**: the arithmetic is VBT-1's and the calibration is TWT's.
A test that only checked the numbers would pass on a copied implementation, and a test that only
checked the import would pass on inherited thresholds, so both are here.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl
import pytest

from baskfy_core.twt.breadth import (
    BreadthReading,
    as_shared_config,
    breadth_above_dma,
    breadth_series,
    gate_for,
)
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, BreadthConfig, Gate, TwtConfig

BREADTH = DEFAULT_TWT_CONFIG.breadth
SESSION = dt.date(2024, 6, 3)
LATER = dt.date(2024, 6, 4)


def universe(
    *,
    above: int,
    below: int,
    warming: int = 0,
    session: dt.date = SESSION,
    blank: int = 0,
) -> pl.DataFrame:
    """A session's panel: names above their average, names below it, names still warming up.

    ``warming`` names printed a bar but have no 200-session average yet; ``blank`` names have an
    average and did not print. Neither belongs in the reading, and which one a wrong denominator
    would swallow is the difference between a listing wave and a bear market.
    """
    rows: list[dict[str, object]] = []
    identifier = 0
    for _ in range(above):
        rows.append(
            {"instrument_id": identifier, "date": session, "close": 110.0, "sma_dma": 100.0}
        )
        identifier += 1
    for _ in range(below):
        rows.append({"instrument_id": identifier, "date": session, "close": 90.0, "sma_dma": 100.0})
        identifier += 1
    for _ in range(warming):
        rows.append({"instrument_id": identifier, "date": session, "close": 110.0, "sma_dma": None})
        identifier += 1
    for _ in range(blank):
        rows.append({"instrument_id": identifier, "date": session, "close": None, "sma_dma": 100.0})
        identifier += 1
    return pl.DataFrame(
        rows,
        schema={
            "instrument_id": pl.Int64,
            "date": pl.Date,
            "close": pl.Float64,
            "sma_dma": pl.Float64,
        },
    )


def reading(frame: pl.DataFrame, config: TwtConfig = DEFAULT_TWT_CONFIG) -> BreadthReading:
    return breadth_above_dma(frame, SESSION, config)


class TestTheGateIsStrict:
    """``04`` §4.3: ``OPEN`` when ``pct_above_dma > min_pct_above_dma``, **strictly**; ``SHUT`` at
    or below it. The research's own gate is ``breadth > 0.40`` and a session that reads exactly
    40 % is a session the book does not enter on."""

    def test_exactly_the_threshold_is_shut(self) -> None:
        assert gate_for(Decimal("40.0000")) is Gate.SHUT

    def test_one_ten_thousandth_above_the_threshold_is_open(self) -> None:
        assert gate_for(Decimal("40.0001")) is Gate.OPEN

    def test_one_ten_thousandth_below_the_threshold_is_shut(self) -> None:
        assert gate_for(Decimal("39.9999")) is Gate.SHUT

    def test_the_reading_carries_the_same_verdict_as_the_bare_comparison(self) -> None:
        forty = reading(universe(above=40, below=60))
        assert forty.pct_above_dma == Decimal("40.0000")
        assert forty.gate is Gate.SHUT
        assert forty.is_open is False
        forty_one = reading(universe(above=41, below=59))
        assert forty_one.pct_above_dma == Decimal("41.0000")
        assert forty_one.gate is Gate.OPEN

    def test_there_are_two_values_and_no_amber(self) -> None:
        """``04`` §4.3: the swing book's ladder uses a middle value to shrink exposure; this book
        has ten equal slots and no ladder."""
        assert {member.value for member in Gate} == {"OPEN", "SHUT"}


class TestAZeroDenominatorIsShut:
    """``04`` §4.2: ``measured_count = 0`` gives ``0.0000`` and a SHUT gate. A session the panel
    cannot measure is not a session the book enters on."""

    def test_an_empty_universe_reads_zero_and_shuts_the_gate(self) -> None:
        empty = reading(universe(above=0, below=0))
        assert empty.measured_count == 0
        assert empty.pct_above_dma == Decimal("0.0000")
        assert empty.gate is Gate.SHUT

    def test_a_universe_that_has_printed_but_not_warmed_up_also_shuts_it(self) -> None:
        """Every name above its average and the gate is still shut, because none of them *has* an
        average yet. The alternative — counting a warming name as "not above" — would report a
        listing wave as a bear market; the alternative to *that* — reading an unmeasurable session
        as open — would let the book enter a market nobody has looked at."""
        young = reading(universe(above=0, below=0, warming=50))
        assert young.universe_count == 50
        assert young.measured_count == 0
        assert young.gate is Gate.SHUT

    def test_the_series_shuts_an_unmeasurable_session_too(self) -> None:
        frame = universe(above=0, below=0, warming=50)
        row = breadth_series(frame).to_dicts()[0]
        assert row["measured_count"] == 0
        assert row["gate"] == Gate.SHUT.value


class TestWhatIsInTheDenominator:
    """``04`` §4.2: the names that **printed a bar** on the session **and** have a valid
    200-session average under §2.2's tolerance."""

    def test_a_name_that_did_not_print_is_in_neither_count(self) -> None:
        with_blanks = reading(universe(above=41, below=59, blank=100))
        assert with_blanks.measured_count == 100
        assert with_blanks.pct_above_dma == Decimal("41.0000")

    def test_a_name_still_warming_up_is_in_neither_count(self) -> None:
        young = reading(universe(above=41, below=59, warming=100))
        assert young.universe_count == 200
        assert young.measured_count == 100
        assert young.above_count == 41
        assert young.pct_above_dma == Decimal("41.0000")

    def test_the_percentage_is_reported_to_four_places(self) -> None:
        third = reading(universe(above=1, below=2))
        assert third.pct_above_dma == Decimal("33.3333")
        assert third.pct_above_dma.as_tuple().exponent == -BREADTH.pct_decimals


class TestTheSeriesAndTheReadingAgree:
    """The guard that keeps "one arithmetic" true after the rounding step of ``04`` §4.2."""

    @pytest.mark.parametrize(
        ("above", "below"), [(0, 100), (40, 60), (41, 59), (1, 2), (100, 0), (7, 13)]
    )
    def test_every_session_reads_the_same_both_ways(self, above: int, below: int) -> None:
        frame = universe(above=above, below=below)
        one = reading(frame)
        row = breadth_series(frame).to_dicts()[0]
        assert Decimal(str(row["pct_above_dma"])) == one.pct_above_dma
        assert row["gate"] == one.gate.value
        assert row["measured_count"] == one.measured_count
        assert row["above_count"] == one.above_count

    def test_the_series_is_one_row_a_session(self) -> None:
        frame = pl.concat(
            [universe(above=41, below=59), universe(above=10, below=90, session=LATER)]
        )
        rows = breadth_series(frame).sort("date").to_dicts()
        assert [row["date"] for row in rows] == [SESSION, LATER]
        assert [row["gate"] for row in rows] == [Gate.OPEN.value, Gate.SHUT.value]


class TestTheGateAPlanReadsIsTheSignalSessions:
    """``04`` §4.5: the row of the **last completed session**, never a later one."""

    def test_a_later_session_does_not_move_the_signal_sessions_reading(self) -> None:
        frame = pl.concat(
            [universe(above=41, below=59), universe(above=0, below=100, session=LATER)]
        )
        assert breadth_above_dma(frame, SESSION).gate is Gate.OPEN
        assert breadth_above_dma(frame, LATER).gate is Gate.SHUT


class TestTheThresholdsAreTwtsOwn:
    """DECISIONS-TW **TW0.4**: share the arithmetic, never the calibration."""

    def test_the_shared_config_is_spelled_out_from_twt_s_fields(self) -> None:
        mine = BreadthConfig(dma_bars=50, min_pct_above_dma=60.0)
        shared = as_shared_config(mine).breadth
        assert shared.dma_bars == 50
        assert shared.min_pct_above_dma == 60.0

    def test_a_different_threshold_moves_this_sleeves_gate_and_only_this_sleeves(self) -> None:
        strict = TwtConfig(breadth=BreadthConfig(min_pct_above_dma=50.0))
        frame = universe(above=45, below=55)
        assert reading(frame).gate is Gate.OPEN
        assert reading(frame, strict).gate is Gate.SHUT

    def test_the_shipped_numbers_are_the_ones_the_document_states(self) -> None:
        """``04`` §4.1, pinned literally here as well as in ``test_twt_docs_parity.py`` — this is
        the gate that produced every number in ``01`` §6, and 45 % costs 7.4 CAGR points."""
        assert BREADTH.dma_bars == 200
        assert BREADTH.min_pct_above_dma == 40.0
