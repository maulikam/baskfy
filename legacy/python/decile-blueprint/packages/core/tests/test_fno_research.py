"""``baskfy_core.fno.research`` — the ``RESEARCH.md`` families as functions of frames.

Two layers. **Synthetic** tests pin the mechanics the scripts define (entry at the next open, a
stop touched intraday fills at the stop or the gapped open, the chandelier never loosens, a roll
is charged one more round trip, a condor's P&L identity, the research Black-76 round trip).
**The golden** runs only with ``BASKFY_FNO_RESEARCH_DIR`` pointing at the stored research data
(``~/baskfy-research/fno``: ``futures.parquet``, ``options.parquet``, ``cont.parquet``) and
asserts ``RESEARCH.md``'s numbers — B4 N=15 n=100 +0.033R, with the 1.5x loss close +0.022R, F2
n=2,334 +0.017R — and that the ported ``continuous`` rebuilds ``cont.parquet`` exactly. Without
the data it is skipped **loudly** (FO9 makes it a gate).
"""

from __future__ import annotations

import datetime as dt
import math
import os
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from baskfy_core.fno import research as r

RESEARCH_DIR = os.environ.get("BASKFY_FNO_RESEARCH_DIR")


def _days(n: int, start: dt.date = dt.date(2024, 1, 1)) -> list[dt.date]:
    out: list[dt.date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def _panel(  # noqa: PLR0913, PLR0917 - the sequence under test, whole
    o: list[float], h: list[float], lo: list[float], c: list[float], atr: float,
    expiries: list[dt.date] | None = None,
) -> pl.DataFrame:  # fmt: skip
    n = len(c)
    days = _days(n)
    return pl.DataFrame(
        {
            "symbol": ["ABC"] * n,
            "di": list(range(n)),
            "date": days,
            "expiry": expiries or [dt.date(2030, 1, 1)] * n,
            "o": o, "h": h, "l": lo, "c": c,
            "atr": [atr] * n,
        },
        schema_overrides={"o": pl.Float64, "h": pl.Float64, "l": pl.Float64, "c": pl.Float64},
    )  # fmt: skip


def _signal(di: int, side: int = 1) -> pl.DataFrame:
    return pl.DataFrame({"symbol": ["ABC"], "di": [di], "side": [side]})


class TestSimulateFutures:
    def test_stop_touched_intraday_fills_at_the_stop(self) -> None:
        panel = _panel(
            o=[100, 100, 101, 99], h=[101, 102, 102, 100], lo=[99, 99, 100, 90],
            c=[100, 101, 101, 95], atr=2.0,
        )  # fmt: skip
        t = r.simulate_futures(panel, _signal(0), r.FuturesParams(horizon=10, k_atr=3.0))
        row = t.row(0, named=True)
        # Entry at the next open (100), stop 100 - 6 = 94, touched on day 3 (low 90, open 99).
        expected = (94 - 100) - r.FUTURES_ROUND_TRIP_COST * 100
        assert row["R"] == pytest.approx(expected / 6.0)
        assert row["days"] == 3

    def test_a_gap_through_fills_at_the_open(self) -> None:
        panel = _panel(
            o=[100, 100, 101, 90], h=[101, 102, 102, 91], lo=[99, 99, 100, 88],
            c=[100, 101, 101, 89], atr=2.0,
        )  # fmt: skip
        t = r.simulate_futures(panel, _signal(0), r.FuturesParams(horizon=10, k_atr=3.0))
        assert t["R"][0] == pytest.approx(((90 - 100) - 0.12) / 6.0)

    def test_the_chandelier_rises_with_the_best_close_and_never_loosens(self) -> None:
        panel = _panel(
            o=[100, 100, 104, 110, 108, 104], h=[101, 105, 111, 112, 109, 105],
            lo=[99, 99, 103, 108, 107, 103.5], c=[100, 104, 110, 109, 107, 104], atr=2.0,
        )  # fmt: skip
        t = r.simulate_futures(
            panel, _signal(0), r.FuturesParams(horizon=10, k_atr=3.0, trail=True)
        )
        # Best close 110 on day 2 → stop 104 from day 3; day 5's low 103.5 touches it.
        assert t["days"][0] == 5
        assert t["R"][0] == pytest.approx(((104 - 100) - 0.12) / 6.0)

    def test_the_time_exit_is_at_the_close_of_signal_plus_horizon(self) -> None:
        n = 8
        panel = _panel(
            o=[100.0] * n, h=[101.0] * n, lo=[99.0] * n, c=[100.0 + i for i in range(n)], atr=2.0
        )
        t = r.simulate_futures(panel, _signal(0), r.FuturesParams(horizon=5, k_atr=3.0))
        assert t["days"][0] == 5
        assert t["R"][0] == pytest.approx(((105 - 100) - 0.12) / 6.0)

    def test_each_roll_inside_the_hold_costs_another_round_trip(self) -> None:
        n = 8
        exps = [dt.date(2024, 1, 25)] * 4 + [dt.date(2024, 2, 29)] * 4
        panel = _panel(
            o=[100.0] * n, h=[101.0] * n, lo=[99.0] * n, c=[100.0] * n, atr=2.0, expiries=exps
        )
        charged = r.simulate_futures(
            panel, _signal(0), r.FuturesParams(horizon=6, k_atr=3.0, charge_rolls=True)
        )
        plain = r.simulate_futures(panel, _signal(0), r.FuturesParams(horizon=6, k_atr=3.0))
        assert charged["rolls"][0] == 1
        assert charged["R"][0] == pytest.approx(plain["R"][0] - 0.12 / 6.0)

    def test_one_trade_at_a_time_per_symbol(self) -> None:
        n = 8
        panel = _panel(o=[100.0] * n, h=[101.0] * n, lo=[99.0] * n, c=[100.0] * n, atr=2.0)
        signals = pl.DataFrame({"symbol": ["ABC"] * 3, "di": [0, 2, 7], "side": [1, 1, 1]})
        t = r.simulate_futures(panel, signals, r.FuturesParams(horizon=5, k_atr=3.0))
        assert t.height == 1  # di 2 is inside the first hold; di 7 has no next session to enter


class TestResearchBlack76:
    def test_implied_inverts_b76_under_the_research_rate(self) -> None:
        kinds = np.array(["CE", "PE"])
        f = np.array([100.0, 100.0])
        k = np.array([105.0, 95.0])
        t = np.array([0.1, 0.1])
        prices = r.b76(f, k, t, np.array([0.22, 0.31]), kinds)
        ivs = r.implied(f, k, t, prices, kinds)
        assert ivs == pytest.approx([0.22, 0.31], abs=1e-9)

    def test_the_research_discounts_at_six_and_a_half_percent(self) -> None:
        kinds = np.array(["CE"])
        at_rate = r.b76(
            np.array([100.0]), np.array([100.0]), np.array([1.0]), np.array([0.2]), kinds
        )
        undiscounted = r.b76(
            np.array([100.0]), np.array([100.0]), np.array([1.0]), np.array([0.2]), kinds, rate=0.0
        )
        assert at_rate[0] == pytest.approx(undiscounted[0] * math.exp(-0.065))

    def test_leg_costs_are_the_scripts(self) -> None:
        params = r.CondorParams(slip_pct=0.005)
        # Sell at 100, lot 75: slip 0.5 + STT 0.15 + 1.18 x (20/75 + 0.0355).
        assert r.leg_costs(100.0, True, 75.0, params) == pytest.approx(
            0.5 + 0.15 + 1.18 * (20 / 75 + 0.0355)
        )
        assert r.leg_costs(1.0, False, 75.0, params) == pytest.approx(
            0.05 + 1.18 * (20 / 75 + 0.000355)
        )


def _synthetic_condor_frames() -> tuple[pl.DataFrame, pl.DataFrame]:
    """One NIFTY monthly, a flat future at 1000, a chain priced at 20 % vol that decays."""
    days = _days(25, dt.date(2024, 3, 1))
    expiry = days[-1]
    fut = pl.DataFrame(
        {
            "date": days, "instrument": ["FUTIDX"] * 25, "symbol": ["NIFTY"] * 25,
            "expiry": [expiry] * 25, "open": [1000.0] * 25, "high": [1001.0] * 25,
            "low": [999.0] * 25, "close": [1000.0] * 25, "settle": [1000.0] * 25,
            "underlying": [None] * 25, "open_interest": [1] * 25, "oi_change": [0] * 25,
            "volume": [1] * 25, "turnover": [1.0] * 25, "lot_size": [50] * 25,
        },
        schema_overrides={"underlying": pl.Float64},
    )  # fmt: skip
    rows = []
    for d in days:
        years = max((expiry - d).days, 0.5) / 365.0
        for strike in range(850, 1151, 10):
            for kind in ("CE", "PE"):
                price = float(
                    r.b76(np.array([1000.0]), np.array([float(strike)]), np.array([years]),
                          np.array([0.2]), np.array([kind]))[0]
                )  # fmt: skip
                rows.append(
                    {"date": d, "instrument": "OPTIDX", "symbol": "NIFTY", "expiry": expiry,
                     "strike": float(strike), "option_type": kind, "close": round(price, 2),
                     "settle": round(price, 2), "open_interest": 10, "volume": 5}
                )  # fmt: skip
    return pl.DataFrame(rows), fut


def test_a_synthetic_condor_obeys_the_scripts_pnl_identity() -> None:
    options, fut = _synthetic_condor_frames()
    cont = r.continuous(fut)
    panel = r.build_option_panel(options, fut, cont, r.INDEX_UNDERLYINGS)
    run = r.condor_trades(
        panel, r.CondorParams(n_before=15, slip_pct=0.005, symbols=r.INDEX_UNDERLYINGS)
    )
    assert run.trades.height == 1
    t = run.trades.row(0, named=True)
    assert t["entry"] == panel.trading_days_before(t["expiry"], 15)
    assert 0.19 < t["iv"] < 0.21
    assert t["R"] == pytest.approx(t["pnl"] / t["maxloss"])
    # A flat future and a decaying chain: the 50 % profit take fires before E-1.
    assert t["exit"] < panel.trading_days_before(t["expiry"], 1)
    exit_value = t["credit"] - t["pnl"] - t["cost_R"] * t["maxloss"]
    assert exit_value <= 0.5 * t["credit"] + 1e-9


def test_summary_headline_numbers() -> None:
    trades = pl.DataFrame(
        {"entry": [dt.date(2022, 1, 3), dt.date(2022, 2, 1), dt.date(2023, 1, 2)],
         "R": [0.5, -1.0, 0.2], "cost_R": [0.01, 0.02, 0.03]}
    )  # fmt: skip
    s = r.summarise(trades)
    assert s.n == 3
    assert s.exp_r == pytest.approx(-0.1)
    assert s.win == pytest.approx(2 / 3)
    assert s.worst == -1.0
    assert s.gross_r == pytest.approx(-0.08)
    assert s.per_year[2022][0] == 2


# --------------------------------------------------------------------------------------------
# The golden (optional here; FO9 makes it a gate)
# --------------------------------------------------------------------------------------------

needs_data = pytest.mark.skipif(
    not RESEARCH_DIR,
    reason=(
        "BASKFY_FNO_RESEARCH_DIR is not set: the RESEARCH.md golden (B4 +0.033R, loss close "
        "+0.022R, F2 +0.017R) was NOT checked. Point it at ~/baskfy-research/fno to run it."
    ),
)


@pytest.fixture(scope="module")
def research_frames() -> tuple[pl.DataFrame, pl.DataFrame]:
    assert RESEARCH_DIR is not None
    root = Path(RESEARCH_DIR)
    futures = pl.read_parquet(root / "futures.parquet")
    return futures, r.continuous(futures)


@needs_data
def test_continuous_rebuilds_cont_parquet_exactly(
    research_frames: tuple[pl.DataFrame, pl.DataFrame],
) -> None:
    assert RESEARCH_DIR is not None
    _, cont = research_frames
    assert cont.equals(pl.read_parquet(Path(RESEARCH_DIR) / "cont.parquet"))


@needs_data
def test_b4_reproduces(research_frames: tuple[pl.DataFrame, pl.DataFrame]) -> None:
    assert RESEARCH_DIR is not None
    futures, cont = research_frames
    options = (
        pl.scan_parquet(Path(RESEARCH_DIR) / "options.parquet")
        .filter(pl.col("symbol").is_in(sorted(r.INDEX_UNDERLYINGS)))
        .collect()
    )
    panel = r.build_option_panel(options, futures, cont, r.INDEX_UNDERLYINGS)
    plain = r.summarise(r.condor_trades(panel, r.B4).trades)
    assert plain.n == 100
    assert round(plain.exp_r, 3) == 0.033
    assert round(plain.t_stat, 2) == 1.36
    assert round(plain.win, 2) == 0.86
    stopped = r.summarise(r.condor_trades(panel, r.B4_LOSS_CLOSE).trades)
    assert stopped.n == 100
    assert round(stopped.exp_r, 3) == 0.022
    assert round(stopped.worst, 2) == -0.73


@needs_data
def test_f2_reproduces(research_frames: tuple[pl.DataFrame, pl.DataFrame]) -> None:
    _, cont = research_frames
    trades = r.f2_trades(cont)
    s = r.summarise(trades)
    assert s.n == 2334
    assert round(s.exp_r, 3) == 0.017
    assert round(s.t_stat, 2) == 0.71
    rolls = trades["rolls"].mean()
    assert isinstance(rolls, float)
    assert round(rolls, 2) == 0.96
