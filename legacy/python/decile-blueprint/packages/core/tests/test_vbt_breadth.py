"""The regime gate (``docs/vbt/04`` §4).

The rule that turns 2018-19 from a -45% hole into a -28% one. Without it the strategy earns the
same CAGR at twice the drawdown and is invested 85% of the time instead of 63%, which is a
different product sold under the same name.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import polars as pl
import pytest
from vbt_fixtures import sessions

from baskfy_core.vbt.breadth import breadth_above_dma, breadth_series, gate_for
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, BreadthConfig, Gate

DAY = sessions(1)[0]


def universe(rows: Sequence[tuple[float | None, float | None]]) -> pl.DataFrame:
    """``(close, sma_dma)`` per name on one session. ``None`` means "no bar" / "still warming"."""
    return pl.DataFrame(
        {
            "instrument_id": list(range(len(rows))),
            "date": [DAY] * len(rows),
            "close": [close for close, _ in rows],
            "sma_dma": [dma for _, dma in rows],
        },
        schema_overrides={"close": pl.Float64, "sma_dma": pl.Float64},
    )


class TestTheReading:
    def test_the_share_is_of_the_names_that_have_an_average(self) -> None:
        reading = breadth_above_dma(universe([(110.0, 100.0), (90.0, 100.0)]), DAY)
        assert reading.measured_count == 2
        assert reading.above_count == 1
        assert reading.pct_above_dma == Decimal("50.0000")

    def test_a_name_still_warming_up_is_in_neither_half(self) -> None:
        """Counting it as "not above" would report a listing wave as a bear market."""
        reading = breadth_above_dma(universe([(110.0, 100.0), (95.0, None)]), DAY)
        assert reading.universe_count == 2
        assert reading.measured_count == 1
        assert reading.pct_above_dma == Decimal("100.0000")

    def test_a_name_with_no_bar_is_not_in_the_universe_count(self) -> None:
        reading = breadth_above_dma(universe([(110.0, 100.0), (None, 100.0)]), DAY)
        assert reading.universe_count == 1
        assert reading.measured_count == 1

    def test_an_empty_session_is_shut_and_not_a_division_by_zero(self) -> None:
        reading = breadth_above_dma(universe([]), DAY)
        assert reading.pct_above_dma == Decimal("0.0000")
        assert reading.gate is Gate.SHUT

    def test_a_close_exactly_on_its_average_is_not_above_it(self) -> None:
        reading = breadth_above_dma(universe([(100.0, 100.0)]), DAY)
        assert reading.above_count == 0


class TestTheGate:
    def test_the_threshold_is_strict(self) -> None:
        """`04` §4.2 — ``> 40``, as the research read it. Exactly 40% is shut."""
        assert gate_for(Decimal("40.0000")) is Gate.SHUT
        assert gate_for(Decimal("40.0001")) is Gate.OPEN

    def test_a_thin_tape_shuts_it(self) -> None:
        rows = [(90.0, 100.0)] * 7 + [(110.0, 100.0)] * 3
        assert breadth_above_dma(universe(rows), DAY).gate is Gate.SHUT

    def test_a_broad_tape_opens_it(self) -> None:
        rows = [(90.0, 100.0)] * 3 + [(110.0, 100.0)] * 7
        reading = breadth_above_dma(universe(rows), DAY)
        assert reading.gate is Gate.OPEN
        assert reading.is_open

    @pytest.mark.parametrize("threshold", [30.0, 35.0, 45.0, 50.0])
    def test_the_threshold_is_a_field_and_moving_it_moves_the_verdict(
        self, threshold: float
    ) -> None:
        """`04` §4.4 lists what each of these costs; the point here is only that nothing
        downstream compares against a literal 40."""
        config = BreadthConfig(min_pct_above_dma=threshold)
        expected = Gate.OPEN if threshold < 42.0 else Gate.SHUT
        assert gate_for(Decimal("42.0000"), config) is expected


class TestTheSeries:
    def test_one_row_per_session_with_the_gate(self) -> None:
        days = sessions(3)
        frame = pl.DataFrame(
            {
                "instrument_id": [1, 2, 1, 2, 1, 2],
                "date": [days[0], days[0], days[1], days[1], days[2], days[2]],
                "close": [110.0, 105.0, 90.0, 95.0, 110.0, 90.0],
                "sma_dma": [100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
            }
        )
        series = breadth_series(frame)
        assert series["gate"].to_list() == [
            Gate.OPEN.value,
            Gate.SHUT.value,
            Gate.OPEN.value,
        ]
        assert series["pct_above_dma"].to_list() == [100.0, 0.0, 50.0]

    def test_the_series_and_the_single_reading_agree(self) -> None:
        rows = [(110.0, 100.0)] * 6 + [(90.0, 100.0)] * 4
        frame = universe(rows)
        one = breadth_above_dma(frame, DAY)
        many = breadth_series(frame)
        assert float(one.pct_above_dma) == pytest.approx(many["pct_above_dma"].to_list()[0])
        assert one.gate.value == many["gate"].to_list()[0]


def test_the_gate_reads_the_two_hundred_day_average() -> None:
    assert DEFAULT_VBT_CONFIG.breadth.dma_bars == DEFAULT_VBT_CONFIG.trend.dma_bars == 200
