"""PM0.7: what a dead name's holder gets, and how the book marks it."""

from datetime import date, timedelta

import polars as pl

from portfolioml.labels import MultibaggerConfig, multibagger
from portfolioml.tranches import TrancheConfig, simulate

H = 100
CFG = MultibaggerConfig(up=1.0, horizon=H)
DAYS = [date(2020, 1, 1) + timedelta(days=i) for i in range(200)]


def bars(n: int, terminal: str, px: float = 10.0) -> pl.DataFrame:
    return pl.DataFrame({"instrument_id": [1] * n, "date": DAYS[:n], "open": [px] * n, "high": [px] * n, "low": [px] * n,
                         "close": [px] * n, "terminal": [terminal] * n})


def row(df: pl.DataFrame, i: int) -> dict:
    return df.sort("date").row(i, named=True)


def test_a_dead_row_whose_horizon_runs_past_the_data_stays_unlabelled():
    got = multibagger(bars(50, "delisted"), CFG, "b", last_complete=DAYS[10])
    assert row(got, 10)["label"] == 0
    assert row(got, 11)["label"] is None


def test_a_carries_the_last_close():
    got = multibagger(bars(50, "delisted", 10.0).with_columns(pl.when(pl.col("date") == DAYS[49]).then(5.0).otherwise(pl.col("close")).alias("close")), CFG, "a")
    assert row(got, 10)["fwd_ret_h"] == -0.5


def test_b_writes_off_when_death_comes_more_than_sixty_sessions_before_the_horizon():
    got = multibagger(bars(50, "delisted"), CFG, "b")
    assert row(got, 10)["fwd_ret_h"] == -1.0  # 39 sessions of bars left, 61 short of the horizon


def test_b_keeps_the_last_close_when_death_is_within_sixty_sessions_of_the_horizon():
    got = multibagger(bars(50, "delisted"), CFG, "b")
    assert row(got, 45)["fwd_ret_h"] == -1.0
    long = multibagger(bars(120, "delisted"), CFG, "b")
    assert row(long, 40)["fwd_ret_h"] == 0.0  # 79 sessions left: 21 short, inside the sixty


def test_a_merged_name_is_never_written_off():
    got = multibagger(bars(50, "merged"), CFG, "b")
    assert row(got, 10)["fwd_ret_h"] == 0.0


def test_a_name_still_alive_at_the_end_of_data_stays_unlabelled():
    got = multibagger(bars(50, "alive"), CFG, "b")
    assert row(got, 10)["label"] is None and row(got, 10)["fwd_ret_h"] is None


def test_a_complete_path_is_untouched_by_the_terminal_rule():
    alive = multibagger(bars(150, "delisted"), CFG, None)
    both = multibagger(bars(150, "delisted"), CFG, "b")
    assert row(alive, 10) == row(both, 10)


def test_a_touch_before_death_still_counts():
    b = bars(50, "suspended").with_columns(pl.when(pl.col("date") == DAYS[30]).then(25.0).otherwise(pl.col("high")).alias("high"))
    assert row(multibagger(b, CFG, "b"), 10)["label"] == 1


def book(terminal, marks_close=8.0):
    long_days = [date(2024, 1, 1) + timedelta(days=i) for i in range(120)]
    rows = [{"instrument_id": 1, "date": d, "entry_px": 10.0, "close": 10.0, "fwd_ret_h": 0.0, "fwd_min_ret": 0.0, "label": 0} for d in long_days[:5]]
    rows += [{"instrument_id": 2, "date": d, "entry_px": 10.0, "close": 10.0, "fwd_ret_h": 0.0, "fwd_min_ret": 0.0, "label": 0} for d in long_days]
    ds = pl.DataFrame(rows)
    scores = ds.select("instrument_id", "date", (-pl.col("instrument_id").cast(pl.Float64)).alias("score"))
    marks = ds.select("instrument_id", "date", pl.when(pl.col("instrument_id") == 1).then(marks_close).otherwise(pl.col("close")).alias("close"),
                      pl.when(pl.col("instrument_id") == 1).then(pl.lit("delisted")).otherwise(pl.lit("alive")).alias("terminal"))
    return simulate(ds, scores, "score", TrancheConfig(top_n=1, horizon=100, cost_per_side=0.0, terminal=terminal), marks=marks)[1]


def test_the_book_marks_from_every_bar_not_only_investable_rows():
    assert book(None)["equity"].min() < 1.0


def test_variant_b_zeroes_a_dead_line_after_sixty_sessions():
    assert book("b")["equity"].min() < book(None)["equity"].min()
