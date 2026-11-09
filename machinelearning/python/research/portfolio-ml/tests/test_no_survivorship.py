"""PM0.7 AC2: the panel holds dead names. Runs on an export that has them (PM_DATA)."""

import os
from pathlib import Path

import polars as pl
import pytest

DATA = os.environ.get("PM_DATA")
MIN_DEAD_SHARE = 0.10


@pytest.mark.skipif(DATA is None, reason="set PM_DATA to an export with dead names")
def test_the_universe_is_not_survivors_only():
    i = pl.read_csv(Path(DATA) / "instrument.csv.gz", infer_schema_length=50000).filter(
        (pl.col("instrument_type") == "EQ") & pl.col("series").is_in(["EQ", "BE", "BZ"]))
    share = i["delisted_on"].is_not_null().mean()
    assert share > MIN_DEAD_SHARE, f"{share:.1%} of instruments dead"
