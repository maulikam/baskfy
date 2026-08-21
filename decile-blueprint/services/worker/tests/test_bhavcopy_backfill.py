"""The bhavcopy bar backfill (docs/DECISIONS.md §21.1).

These assert the mapping's *contract*, not what the code currently emits. The contract is set by
three documents: `docs/09` (providers never adjust; `close` is derived later), `docs/04` (the
`ohlcv_daily` column set and its `source` CHECK) and `docs/05` §13 (turnover is the preferred input
to `vol_day_val`, not a nice-to-have).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl

from decile_worker.bhavcopy_backfill import (
    BAR_SOURCE,
    EQUITY_SERIES,
    BhavcopyBackfillReport,
    _rows_for_day,
)

DAY = dt.date(2026, 8, 18)


def bhavcopy(**overrides: object) -> pl.DataFrame:
    """One bhavcopy row, in the schema `NSEProvider.bhavcopy` really returns."""
    row: dict[str, object] = {
        "symbol": "CUPID",
        "series": "EQ",
        "date": DAY,
        "open": Decimal("273.0000"),
        "high": Decimal("280.5000"),
        "low": Decimal("271.1000"),
        "close": Decimal("278.4500"),
        "prev_close": Decimal("272.0000"),
        "volume": 1_234_567,
        "turnover": Decimal("18768602294.72"),
        "trades": 635_844,
        "upper_circuit": Decimal("299.2000"),
        "lower_circuit": Decimal("244.8000"),
    }
    row.update(overrides)
    return pl.DataFrame([row])


IDS = {("CUPID", "EQ"): 7, ("DIACABS", "BE"): 9}


class TestTheMapping:
    def test_close_is_written_to_both_close_and_close_raw(self) -> None:
        """docs/09: "providers never adjust". Step 4 derives `close`; ingest must not.

        Writing the exchange print to both is what makes `adj_factor = 1` truthful on arrival.
        """
        (row,), _ = _rows_for_day(bhavcopy(), IDS, EQUITY_SERIES)
        assert row["close"] == Decimal("278.4500")
        assert row["close_raw"] == row["close"]
        assert row["adj_factor"] == Decimal(1)

    def test_volume_is_written_to_both_volume_and_volume_raw(self) -> None:
        (row,), _ = _rows_for_day(bhavcopy(), IDS, EQUITY_SERIES)
        assert row["volume"] == row["volume_raw"] == 1_234_567

    def test_turnover_is_carried(self) -> None:
        """docs/05 §13: use exchange turnover for `vol_day_val`; only fall back to close x volume
        when it is unavailable. Dropping it here would make that fallback unconditional — which is
        precisely the Kite limitation this path exists to escape (§21.1)."""
        (row,), _ = _rows_for_day(bhavcopy(), IDS, EQUITY_SERIES)
        assert row["turnover"] == Decimal("18768602294.72")

    def test_both_circuit_bands_are_carried(self) -> None:
        """docs/05 §12's detection is INFERRED because no band is published to compare against.
        The bhavcopy publishes both, so dropping them would discard the observation."""
        (row,), _ = _rows_for_day(bhavcopy(), IDS, EQUITY_SERIES)
        assert row["upper_circuit"] == Decimal("299.2000")
        assert row["lower_circuit"] == Decimal("244.8000")

    def test_the_source_is_one_the_check_constraint_admits(self) -> None:
        """docs/04: `ohlcv_daily_source` is `source IN ('kite', 'nse')`."""
        (row,), _ = _rows_for_day(bhavcopy(), IDS, EQUITY_SERIES)
        assert row["source"] == BAR_SOURCE == "nse"

    def test_a_null_circuit_band_survives_as_null(self) -> None:
        """A band NSE did not publish must arrive as NULL, not as a zero that reads as a price."""
        frame = bhavcopy(upper_circuit=None, lower_circuit=None)
        (row,), _ = _rows_for_day(frame, IDS, EQUITY_SERIES)
        assert row["upper_circuit"] is None
        assert row["lower_circuit"] is None


class TestTheJoin:
    def test_it_matches_on_symbol_and_series_together(self) -> None:
        """`instrument` is unique on `(exchange_id, symbol, series)` and the bhavcopy publishes one
        symbol in several series. Matching on symbol alone would file a BE row's bars against the
        EQ listing — a silent cross-contamination of two different securities."""
        frame = bhavcopy(symbol="DIACABS", series="BE")
        (row,), unmatched = _rows_for_day(frame, IDS, EQUITY_SERIES)
        assert row["instrument_id"] == 9
        assert unmatched == set()

    def test_a_symbol_in_an_unlisted_series_does_not_borrow_another_series_id(self) -> None:
        frame = bhavcopy(symbol="CUPID", series="BE")
        rows, unmatched = _rows_for_day(frame, IDS, ("EQ", "BE"))
        assert rows == []
        assert unmatched == {"CUPID:BE"}

    def test_an_unknown_symbol_is_reported_rather_than_dropped(self) -> None:
        """A symbol with no `instrument` row is a listings-register gap. Counting it silently
        would let a shrinking universe look like a quiet day."""
        rows, unmatched = _rows_for_day(bhavcopy(symbol="NOSUCH"), IDS, EQUITY_SERIES)
        assert rows == []
        assert unmatched == {"NOSUCH:EQ"}


class TestTheSeriesFilter:
    def test_non_equity_series_are_excluded(self) -> None:
        """GS/GB are government securities and N0 is debt. They are in the same file and are not
        instruments this product screens."""
        frame = pl.concat([bhavcopy(), bhavcopy(symbol="GOVT", series="GS")])
        rows, unmatched = _rows_for_day(frame, IDS, EQUITY_SERIES)
        assert [r["instrument_id"] for r in rows] == [7]
        # Excluded by series, so not reported as an unmatched equity symbol either.
        assert unmatched == set()

    def test_the_default_series_are_the_ones_the_register_holds(self) -> None:
        assert EQUITY_SERIES == ("EQ", "BE", "BZ")


class TestTheReport:
    def test_a_report_with_no_failures_succeeds(self) -> None:
        assert BhavcopyBackfillReport().succeeded

    def test_a_report_with_a_failure_does_not(self) -> None:
        report = BhavcopyBackfillReport()
        report.failures["2026-08-18"] = "boom"
        assert not report.succeeded

    def test_missing_days_are_not_failures(self) -> None:
        """A day the calendar calls a session but NSE published no file for is a data gap, not an
        error — docs/09 gives the quality gate, not the parser, the job of deciding publishability.
        A 404 must not abort a 671-day run."""
        report = BhavcopyBackfillReport()
        report.missing_days.append(DAY)
        assert report.succeeded
