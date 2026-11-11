"""PM6: the nightly's feature path equals the research dataset's, and its pure pieces behave."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import numpy as np
import polars as pl
import pytest

import mb_task as T
from portfolioml import pipeline as P
from portfolioml.features import ALL_FEATURES

DATA = Path(os.environ.get("PM_DATA", Path(__file__).resolve().parents[1] / "data"))
DATASET = DATA / "dataset_mb_u100_h250_tb.parquet"
CA = DATA / "_corporate_actions" / "corporate-actions-year"


@pytest.mark.skipif(not (DATASET.exists() and CA.exists()), reason="needs the local export, its dataset and the corporate-actions archive")
@pytest.mark.parametrize("day", [dt.date(2019, 6, 14), dt.date(2023, 3, 1), dt.date(2025, 11, 7)])
def test_task_features_equal_the_research_dataset(day: dt.date) -> None:
    bars, ins, idx = T.prepared(T.raw_from_export(DATA, day), CA, T.paths()[2])  # bars end on `day`: no look-ahead
    mine = P.tail_features(bars, ins, idx).filter((pl.col("date") == day) & pl.col("universe")).select("instrument_id", *ALL_FEATURES)
    ref = (pl.scan_parquet(DATASET).filter((pl.col("date") == day) & pl.col("universe"))
           .select("instrument_id", *ALL_FEATURES).collect())
    assert sorted(mine["instrument_id"]) == sorted(ref["instrument_id"])
    j = ref.join(mine, on="instrument_id", suffix="_t")
    for c in ALL_FEATURES:
        a, b = j[c].cast(pl.Float64).to_numpy(), j[f"{c}_t"].cast(pl.Float64).to_numpy()
        assert np.array_equal(np.isnan(a), np.isnan(b)), c
        ok = ~np.isnan(a)
        assert np.allclose(a[ok], b[ok], rtol=1e-5, atol=1e-5), c


def _bars(closes: dict[int, list[float]]) -> pl.DataFrame:
    start = dt.date(2025, 1, 1)
    rows = [{"instrument_id": i, "date": start + dt.timedelta(days=k), "close": c} for i, cs in closes.items() for k, c in enumerate(cs)]
    return pl.DataFrame(rows)


def test_hi63_is_a_close_at_a_63_session_high_within_the_last_21() -> None:
    flat = [100.0] * 100
    recent = [100.0] * 90 + [110.0] + [105.0] * 9  # high on session 91, 10 sessions before the end
    stale = [100.0] * 60 + [120.0] + [100.0] * 39  # high 40 sessions before the end
    short = [100.0] * 40  # under 63 sessions of history: never fires
    out = T.hi63(_bars({1: flat, 2: recent, 3: stale, 4: short}))
    got = dict(zip(out["instrument_id"], out["hi63_trigger_date"]))
    assert got[2] == dt.date(2025, 1, 1) + dt.timedelta(days=90)
    assert 3 not in got and 4 not in got
    assert got[1] == dt.date(2025, 1, 1) + dt.timedelta(days=99)  # a flat close equals its own 63-session max


class _Booster:
    def predict(self, x: np.ndarray) -> np.ndarray:
        return x[:, 0] * 1.23456789


def test_ranked_rounds_to_storage_precision_and_breaks_ties_by_id() -> None:
    n = 4
    day = pl.DataFrame({"instrument_id": [7, 3, 5, 9], "close_raw": [49.0, 60.0, 500.0, 80.0],
                        "turnover20": [9e7, 9e7, 4e7, 6e7]} | {f: [1.0, 1.0, 2.0, 0.5] if f == ALL_FEATURES[0] else [0.0] * n for f in ALL_FEATURES})
    out = T.ranked(day, _Booster())
    assert out["instrument_id"].to_list() == [5, 3, 7, 9]
    assert out["rank"].to_list() == [1, 2, 3, 4] and set(out["universe_n"]) == {4}
    assert out["score"].to_list()[0] == round(2 * 1.23456789, 6)
    assert out["investable"].to_list() == [False, True, False, True]  # ₹50 close and ₹5 cr turnover floors
