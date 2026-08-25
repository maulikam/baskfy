"""India short-rate series for backtest Sharpe (docs/10 §Outputs).

docs/10 asks for "Sharpe (rf from a configurable T-bill series)". docs/04 has no T-bill table
and no pipeline step fetches one. The committed CSV is the series: OECD MEI India IR3TIB
(3-month short-term interest rates, percent per annum), monthly from 2011-11 through 2026-06,
forward-filled onto trading days. It is a published proxy for the 91-day T-bill, not the RBI
auction cutoff itself — see DECISIONS-MERGE T9.5.

Core stays I/O-free of network and disk-at-runtime in the usual sense: this reads the same kind
of bundled seed file ``trading_calendar`` already reads.
"""

from __future__ import annotations

import csv
import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from functools import lru_cache
from importlib import resources
from typing import Final

from baskfy_core.backtest import BacktestConfig

_SERIES_FILE: Final = "india_tbill.csv"
_HUNDRED: Final = Decimal("100")


@lru_cache(maxsize=1)
def load_tbill_series() -> tuple[tuple[dt.date, Decimal], ...]:
    """``(month-start, annual rate as a decimal fraction)``, sorted ascending."""
    text = resources.files("baskfy_core.data").joinpath(_SERIES_FILE).read_text(encoding="utf-8")
    points: list[tuple[dt.date, Decimal]] = []
    for row in csv.DictReader(text.splitlines()):
        points.append(
            (dt.date.fromisoformat(row["date"]), Decimal(row["annual_pct"]) / _HUNDRED)
        )
    points.sort(key=lambda item: item[0])
    return tuple(points)


def annual_rate_on(
    day: dt.date,
    curve: Sequence[tuple[dt.date, Decimal]],
    fallback: Decimal,
) -> Decimal:
    """Last observation on or before ``day``; ``fallback`` if the series has not started."""
    chosen = fallback
    for observed, rate in curve:
        if observed > day:
            break
        chosen = rate
    return chosen


def attach_tbill_curve(config: BacktestConfig) -> BacktestConfig:
    """Attach the bundled series unless the caller already supplied one."""
    if config.risk_free_curve:
        return config
    series = load_tbill_series()
    overlapping = tuple((day, rate) for day, rate in series if day <= config.end)
    if not overlapping:
        return config
    return config.model_copy(update={"risk_free_curve": overlapping})
