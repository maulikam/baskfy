"""T-bill series for backtest Sharpe — excess over the series, not over zero (T9.5)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from baskfy_core.backtest import BacktestConfig, BacktestResult
from baskfy_core.backtest_metrics import compute_metrics
from baskfy_core.risk_free import annual_rate_on, attach_tbill_curve, load_tbill_series


def test_the_bundled_series_covers_the_merge_window() -> None:
    series = load_tbill_series()
    assert len(series) == 176
    assert series[0][0] == dt.date(2011, 11, 1)
    assert series[-1][0] == dt.date(2026, 6, 1)
    april_2020 = annual_rate_on(dt.date(2020, 4, 15), series, Decimal(0))
    # COVID crash in the money-market rate — must not be a made-up 6.5%.
    assert Decimal("0.03") < april_2020 < Decimal("0.05")


def test_a_day_before_the_series_uses_the_fallback() -> None:
    series = load_tbill_series()
    assert annual_rate_on(dt.date(2011, 1, 3), series, Decimal("0.065")) == Decimal("0.065")


def test_attach_fills_an_empty_curve() -> None:
    config = BacktestConfig(start=dt.date(2020, 1, 1), end=dt.date(2021, 1, 1))
    attached = attach_tbill_curve(config)
    assert attached.risk_free_curve
    assert attached.risk_free_curve[0][0] <= dt.date(2021, 1, 1)


def test_sharpe_with_the_series_is_not_excess_over_zero() -> None:
    """A noisy growing book has a lower Sharpe once a positive rf is subtracted."""
    dates = tuple(dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(60))
    equity = tuple(
        Decimal("1000000")
        * (Decimal("1.002") ** i)
        * (Decimal("1.01") if i % 7 == 0 else Decimal("1"))
        for i in range(60)
    )
    zeros = tuple(Decimal(0) for _ in dates)
    blank = tuple(None for _ in dates)
    base = BacktestConfig(start=dates[0], end=dates[-1])
    with_rf = attach_tbill_curve(base)

    def _result(config: BacktestConfig) -> BacktestResult:
        return BacktestResult(
            config=config,
            dates=dates,
            equity=equity,
            cash=equity,
            invested=zeros,
            benchmark=blank,
            trades=(),
            holdings=(),
            delistings=(),
            total_costs=Decimal(0),
            dividends_credited=Decimal(0),
            rebalance_dates=(),
        )

    zero = compute_metrics(_result(base))
    tbill = compute_metrics(_result(with_rf))
    assert zero.sharpe is not None and tbill.sharpe is not None
    assert tbill.sharpe < zero.sharpe
