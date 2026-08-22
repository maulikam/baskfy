"""The `MomentumScan` contract — M13 §1.

This is the CSV cord's replacement, so the tests are about the properties the cord had: the desk
gets every column it validates, under the names it validates them by, at the precision the export
used, and the same inputs produce the same bytes.
"""

from __future__ import annotations

import datetime as dt
import sys
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import _scan_fixture as fixture

from baskfy_core import momentum_scan as ms
from baskfy_core.score import required

FIXTURE = Path(__file__).parent / "fixtures" / "momentum_scan.csv"


@dataclass  # not frozen: `ScoringConfig` declares settable variables, which a frozen class is not
class _Cfg:
    """A scoring configuration, standing in for the desk's `app.config`.

    An instance rather than a bare class, because `ScoringConfig` is a Protocol and a class object
    satisfies it only by accident of attribute lookup. Every field the Protocol names is present
    even though this test exercises two of them: a stub that satisfies a Protocol partially is a
    stub that stops satisfying it the moment the Protocol grows, in a file far from here.
    """

    EXCLUDED_SYMBOLS: Collection[str] = ()
    REJECT_SERIES: Collection[str] = ("BE", "BZ")
    MIN_MEDIAN_DAILY_VALUE: float = 50_000_000.0
    MAX_AWAY_FROM_HIGH: float = -30.0
    MAX_CIRCUITS_3M: float = 5.0
    PENALTY_CIRCUITS_1Y: float = 0.5
    MOMENTUM_BLEND: dict[str, float] = field(
        default_factory=lambda: {
            "one_month": 0.15,
            "three_months": 0.25,
            "six_months": 0.30,
            "nine_months": 0.15,
            "one_year": 0.15,
        }
    )
    SHARPE_BLEND: dict[str, float] = field(
        default_factory=lambda: {
            "one_month": 0.15,
            "three_months": 0.25,
            "six_months": 0.30,
            "nine_months": 0.15,
            "one_year": 0.15,
        }
    )
    STOP_VOL_MULT: float = 2.0
    STOP_MIN: float = 0.08
    STOP_MAX: float = 0.25


CFG = _Cfg()


@pytest.fixture(scope="module")
def scan() -> ms.MomentumScan:
    days = fixture.trading_days()
    return ms.build(fixture.bars(), days[-1], days, cfg=CFG, carried=fixture.carried())


def test_it_carries_every_column_the_desk_validates(scan: ms.MomentumScan) -> None:
    """`load_scan` rejects a CSV missing any of these, which is a runtime failure on a Sunday."""
    assert list(scan.frame.columns) == required(CFG)


def test_the_bytes_are_stable(scan: ms.MomentumScan) -> None:
    """Byte-identity against a committed fixture the desk would have accepted.

    If this fails, either the engine changed a number or a column moved. Both are worth a human
    reading the diff, which is why the fixture is committed rather than regenerated in-test.
    """
    assert scan.to_csv() == FIXTURE.read_bytes()


def test_the_csv_opens_with_a_bom(scan: ms.MomentumScan) -> None:
    """`load_scan` reads `utf-8-sig` because the real export carries one.

    Without the BOM the first column name arrives as `﻿symbol` on the readers that expect it,
    and the failure surfaces three layers away as a missing `symbol` column.
    """
    assert scan.to_csv().startswith(b"\xef\xbb\xbf")


def test_carried_columns_win_over_the_engines_own(scan: ms.MomentumScan) -> None:
    """The engine computes a `beta` from thirty days of index history. It is not the desk's beta.

    This regressed once: a plain join kept the engine's column and suffixed the caller's, which
    left `series` null for every row and had the desk reject sixteen tradeable names.
    """
    betas = dict(zip(scan.frame["symbol"], scan.frame["beta"], strict=True))
    expected = dict(zip(fixture.carried()["symbol"], fixture.carried()["beta"], strict=True))
    assert betas == expected
    assert scan.frame["series"].to_list() == ["EQ"] * scan.frame.height


def test_carried_is_required_not_defaulted() -> None:
    """Forgetting it must be an error, not a scan that quietly scores on nulls."""
    days = fixture.trading_days()
    with pytest.raises(ValueError, match="CARRIED_COLUMNS"):
        ms.build(
            fixture.bars(),
            days[-1],
            days,
            cfg=CFG,
            carried=fixture.carried().drop("beta"),
        )


def test_screen_run_id_is_the_inputs_not_the_output() -> None:
    """Same inputs, same id, on any machine — and a different date is a different run."""
    as_of = dt.date(2026, 3, 24)
    assert ms.screen_run_id("d", as_of, 1) == ms.screen_run_id("d", as_of, 1)
    assert ms.screen_run_id("d", as_of, 1) != ms.screen_run_id("d", as_of, 2)
    assert ms.screen_run_id("d", as_of, 1) != ms.screen_run_id("e", as_of, 1)
    assert ms.screen_run_id("d", as_of, 1) != ms.screen_run_id("d", dt.date(2026, 3, 23), 1)


def test_provenance_names_what_was_borrowed(scan: ms.MomentumScan) -> None:
    p = scan.provenance()
    assert p["screen_run_id"] == scan.screen_run_id
    assert p["carried_columns"] == list(ms.CARRIED_COLUMNS)
    assert p["rows"] == 5


class TestUnadjustedDetection:
    """A missing corporate action leaves a cliff in the series. Finding it is not optional."""

    def test_clean_bars_are_clean(self) -> None:
        assert ms.unadjusted_symbols(fixture.bars()) == ()

    def test_a_split_is_caught(self) -> None:
        bars = fixture.bars()
        halved = bars.with_columns(
            pl.when((pl.col("symbol") == "BRAVO") & (pl.col("date") >= dt.date(2025, 6, 1)))
            .then(pl.col("close") / 2)
            .otherwise(pl.col("close"))
            .alias("close")
        )
        assert ms.unadjusted_symbols(halved) == ("BRAVO",)

    def test_a_twenty_percent_move_is_not_a_split(self) -> None:
        """NSE's circuit band is 20%. A day at the band is a price move, not an adjustment."""
        bars = fixture.bars()
        jumped = bars.with_columns(
            pl.when((pl.col("symbol") == "ECHO") & (pl.col("date") == dt.date(2025, 6, 2)))
            .then(pl.col("close") * 1.2)
            .otherwise(pl.col("close"))
            .alias("close")
        )
        assert "ECHO" not in ms.unadjusted_symbols(jumped)

    def test_empty_bars_do_not_raise(self) -> None:
        assert ms.unadjusted_symbols(fixture.bars().head(0)) == ()


def test_suspect_symbols_are_limited_to_the_scans_own_rows() -> None:
    """The desk's generate path fetches every symbol it has, then scans a subset of them.

    Counting suspects across the whole fetch produced "452 of 271 symbols carry an unadjusted
    corporate action" — a warning visibly wrong on its face, which is the kind people learn to
    ignore. It must count what the scan actually contains.
    """
    days = fixture.trading_days()
    bars = fixture.bars()
    outsider = bars.filter(pl.col("symbol") == "ALPHA").with_columns(
        pl.lit("ZULU").alias("symbol"),
        # its own id, in the column's own dtype: the engine keys on the id, not the symbol
        pl.lit(99).cast(bars.schema["instrument_id"]).alias("instrument_id"),
        (pl.col("close") * pl.when(pl.col("date") > dt.date(2025, 6, 1)).then(3.0).otherwise(1.0)),
    )
    with_outsider = pl.concat([bars, outsider])

    assert "ZULU" in ms.unadjusted_symbols(with_outsider)  # the raw detector sees it

    scan = ms.build(with_outsider, days[-1], days, cfg=CFG, carried=fixture.carried())
    assert "ZULU" not in scan.frame["symbol"].to_list()  # ...but it is not in the scan
    assert "ZULU" not in scan.suspect_symbols  # ...so it is not warned about
    assert len(scan.suspect_symbols) <= scan.frame.height
