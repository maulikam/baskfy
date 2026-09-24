"""``docs/fno/04`` §4 — the continuous series, the CA flag, ATR14, RV20 and basis."""

from __future__ import annotations

import datetime as dt
import math

import polars as pl
import pytest

from baskfy_core.fno.config import SeriesConfig
from baskfy_core.fno.series import continuous_futures

CFG = SeriesConfig()
NEAR = dt.date(2024, 7, 25)
NEXT = dt.date(2024, 8, 29)


def _sessions(start: dt.date, n: int) -> list[dt.date]:
    out: list[dt.date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def _frame(
    settles: dict[tuple[dt.date, dt.date], float], spot: float | None = None
) -> pl.DataFrame:
    rows = [
        {
            "trade_date": d,
            "symbol": "ABC",
            "instrument": "FUTSTK",
            "expiry": e,
            "open": s,
            "high": s * 1.01,
            "low": s * 0.99,
            "settle": s,
            "underlying": spot,
            "open_interest": 1000,
            "turnover": 1e7,
            "lot_size": 100,
        }
        for (d, e), s in settles.items()
    ]
    return pl.DataFrame(rows)


def _two_contracts(
    prices_near: list[float], prices_next: list[float], days: list[dt.date]
) -> pl.DataFrame:
    settles: dict[tuple[dt.date, dt.date], float] = {}
    for d, a, b in zip(days, prices_near, prices_next, strict=True):
        if d <= NEAR:
            settles[(d, NEAR)] = a
        settles[(d, NEXT)] = b
    return _frame(settles)


def test_held_expiry_is_strictly_after_the_previous_session_and_a_roll_is_no_jump() -> None:
    days = [dt.date(2024, 7, 23), dt.date(2024, 7, 24), NEAR, dt.date(2024, 7, 26)]
    # The next month trades at a 2 % premium; a naive front-month splice would jump on the roll.
    out = continuous_futures(
        _two_contracts([100.0, 101.0, 102.0, 0.0], [102.0, 103.02, 104.04, 105.0], days), CFG
    )
    held = dict(zip(out["trade_date"].to_list(), out["held_expiry"].to_list(), strict=True))
    assert held[dt.date(2024, 7, 24)] == NEAR
    assert held[NEAR] == NEAR  # held into expiry day from the session before
    assert held[dt.date(2024, 7, 26)] == NEXT  # the previous session was the expiry
    rets = dict(zip(out["trade_date"].to_list(), out["ret"].to_list(), strict=True))
    assert rets[dt.date(2024, 7, 24)] == pytest.approx(math.log(101 / 100))
    assert rets[dt.date(2024, 7, 26)] == pytest.approx(math.log(105.0 / 104.04))
    levels = out["level_c"].to_list()
    assert levels[-1] / levels[-2] == pytest.approx(105.0 / 104.04)


def test_a_split_is_flagged_excluded_and_kept_out_of_signals_for_five_sessions() -> None:
    days = _sessions(dt.date(2024, 7, 1), 12)
    prices = [100.0, 101.0, 102.0, 51.0, 51.5, 52.0, 52.5, 53.0, 53.5, 54.0, 54.5, 55.0]
    out = continuous_futures(_frame({(d, NEXT): p for d, p in zip(days, prices, strict=True)}), CFG)
    by_day = {r["trade_date"]: r for r in out.iter_rows(named=True)}
    split = by_day[days[3]]
    assert split["ca_flag"] is True
    assert split["ret"] is None
    assert split["level_c"] is None
    # The level does not move across the split: the next ret is 51.5/51.
    assert by_day[days[4]]["ret"] == pytest.approx(math.log(51.5 / 51.0))
    assert by_day[days[4]]["level_c"] / by_day[days[2]]["level_c"] == pytest.approx(51.5 / 51.0)
    recent = [by_day[d]["ca_recent"] for d in days[3:]]
    assert recent == [True] * 5 + [False] * 4


def test_the_ca_band_is_the_research_band() -> None:
    days = _sessions(dt.date(2024, 7, 1), 3)
    up = continuous_futures(
        _frame({(days[0], NEXT): 100.0, (days[1], NEXT): 139.0, (days[2], NEXT): 139.0}), CFG
    )
    assert up["ca_flag"].to_list() == [False, False]
    up = continuous_futures(
        _frame({(days[0], NEXT): 100.0, (days[1], NEXT): 141.0, (days[2], NEXT): 141.0}), CFG
    )
    assert up["ca_flag"].to_list() == [True, False]
    down = continuous_futures(
        _frame({(days[0], NEXT): 100.0, (days[1], NEXT): 69.0, (days[2], NEXT): 69.0}), CFG
    )
    assert down["ca_flag"].to_list() == [True, False]


def test_rv20_is_stdev_of_ret_times_root_252_and_atr14_is_the_mean_true_range() -> None:
    days = _sessions(dt.date(2024, 6, 3), 25)
    prices = [100.0]
    for i in range(1, 25):
        prices.append(prices[-1] * (1.01 if i % 2 else 0.995))
    out = continuous_futures(_frame({(d, NEXT): p for d, p in zip(days, prices, strict=True)}), CFG)
    rets = out["ret"].to_list()
    last20 = pl.Series(rets[-20:])
    std = last20.std()
    assert isinstance(std, float)
    assert out["rv20"].to_list()[-1] == pytest.approx(std * math.sqrt(252))
    assert out["rv20"].to_list()[18] is None  # 19 returns: not yet 20
    assert out["atr14"].to_list()[12] is None
    assert out["atr14"].to_list()[13] is not None


def test_basis_is_null_before_udiff_and_annualised_after() -> None:
    before = dt.date(2024, 7, 4)
    days = [
        dt.date(2024, 7, 3),
        before,
        dt.date(2024, 7, 5),
        dt.date(2024, 7, 8),
        dt.date(2024, 7, 9),
    ]
    out = continuous_futures(_frame({(d, NEXT): 101.0 for d in days}, spot=100.0), CFG)
    basis = dict(zip(out["trade_date"].to_list(), out["basis_ann"].to_list(), strict=True))
    assert basis[before] is None
    expected = (101.0 / 100.0 - 1) * 365 / (NEXT - dt.date(2024, 7, 9)).days
    assert basis[dt.date(2024, 7, 9)] == pytest.approx(expected)


def test_oi_total_sums_every_expiry() -> None:
    days = [dt.date(2024, 7, 23), dt.date(2024, 7, 24)]
    out = continuous_futures(_two_contracts([100.0, 101.0], [102.0, 103.0], days), CFG)
    assert out["oi_total"].to_list() == [2000]


def test_rerunning_is_identical() -> None:
    days = _sessions(dt.date(2024, 7, 1), 30)
    frame = _frame({(d, NEXT): 100.0 + i for i, d in enumerate(days)})
    assert continuous_futures(frame, CFG).equals(continuous_futures(frame, CFG))
