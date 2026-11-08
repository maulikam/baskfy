"""PM3 AC: the sector cap holds, and never leaves a tranche short."""

from datetime import date, timedelta

import polars as pl

from portfolioml.tranches import TrancheConfig, simulate

DAYS = [date(2024, 1, 1) + timedelta(days=i) for i in range(45)]


def frames(sectors: list[str | None]) -> tuple[pl.DataFrame, pl.DataFrame]:
    rows = [
        {"instrument_id": i, "date": d, "entry_px": 10.0, "close": 10.0, "fwd_ret_h": 0.1, "fwd_min_ret": -0.1,
         "label": 0, "sector": s}
        for i, s in enumerate(sectors) for d in DAYS
    ]
    ds = pl.DataFrame(rows, schema_overrides={"sector": pl.String})
    scores = ds.select("instrument_id", "date", (-pl.col("instrument_id").cast(pl.Float64)).alias("score"))
    return ds, scores


def tranches_of(ds: pl.DataFrame, scores: pl.DataFrame, cfg: TrancheConfig) -> pl.DataFrame:
    return simulate(ds, scores, "score", cfg)[0]


def test_no_tranche_holds_more_than_three_of_one_sector():
    ds, scores = frames(["A"] * 10 + ["B"] * 10 + ["C"] * 10)
    picks = tranches_of(ds, scores, TrancheConfig(top_n=9, horizon=5))
    assert picks.group_by("date", "sector").len()["len"].max() == 3
    assert picks.group_by("date").len()["len"].min() == 9


def test_too_few_sectors_still_fills_top_n():
    ds, scores = frames(["A"] * 12)
    picks = tranches_of(ds, scores, TrancheConfig(top_n=6, horizon=5))
    assert picks.group_by("date").len()["len"].min() == 6


def test_the_cap_prefers_the_best_scores_and_fills_from_the_next_best():
    ds, scores = frames(["A"] * 8 + ["B"] * 2)
    picks = tranches_of(ds, scores, TrancheConfig(top_n=6, horizon=5))
    first = picks.filter(pl.col("date") == picks["date"].min())
    assert sorted(first["instrument_id"].to_list()) == [0, 1, 2, 3, 8, 9]


def test_names_without_a_sector_are_not_one_sector():
    ds, scores = frames([None] * 12)
    picks = tranches_of(ds, scores, TrancheConfig(top_n=6, horizon=5))
    first = picks.filter(pl.col("date") == picks["date"].min())
    assert sorted(first["instrument_id"].to_list()) == [0, 1, 2, 3, 4, 5]


def test_no_cap_without_a_sector_column_or_when_switched_off():
    ds, scores = frames(["A"] * 12)
    assert tranches_of(ds.drop("sector"), scores, TrancheConfig(top_n=6, horizon=5)).height > 0
    off = tranches_of(ds, scores, TrancheConfig(top_n=6, horizon=5, max_per_sector=None))
    assert off.filter(pl.col("date") == off["date"].min())["instrument_id"].to_list() == [0, 1, 2, 3, 4, 5]


def test_skip_held_never_rebuys_a_name_held_by_the_last_twelve_tranches():
    long_days = [date(2024, 1, 1) + timedelta(days=i) for i in range(500)]
    rows = [{"instrument_id": i, "date": d, "entry_px": 10.0, "close": 10.0, "fwd_ret_h": 0.1, "fwd_min_ret": -0.1, "label": 0}
            for i in range(60) for d in long_days]
    ds = pl.DataFrame(rows)
    scores = ds.select("instrument_id", "date", (-pl.col("instrument_id").cast(pl.Float64)).alias("score"))
    p = simulate(ds, scores, "score", TrancheConfig(top_n=3, horizon=10, skip_held=True))[0]
    dates = p["date"].unique().sort().to_list()
    for j, d in enumerate(dates):
        recent = p.filter(pl.col("date").is_in(dates[max(0, j - 12):j]))["instrument_id"].to_list()
        assert not set(p.filter(pl.col("date") == d)["instrument_id"].to_list()) & set(recent)
    assert p.group_by("date").len()["len"].min() == 3


def test_cadence_uses_one_slice_per_entry_and_loses_no_capital():
    ds, scores = frames([f"S{i}" for i in range(30)])
    picks, eq = simulate(ds.drop("sector"), scores, "score", TrancheConfig(top_n=1, horizon=4, cadence_sessions=2, skip_held=True, cost_per_side=0.0))
    assert picks["date"].n_unique() == 23
    assert eq["equity"].min() > 0.99
