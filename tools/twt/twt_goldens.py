#!/usr/bin/env python
"""TW2's runner: the study, re-run by the sleeve's own functions, and graded (``docs/twt/06`` TW2).

    cd decile-blueprint
    uv run python ../tools/twt/twt_goldens.py            # run it and print the report
    uv run python ../tools/twt/twt_goldens.py --seam     # say whether the core is ready, and stop

**The core this calls is being written by a sibling agent.** Everything around it — the panel
loader, the committed goldens, the comparison, the recall scorer — is finished and tested;
:func:`execute` is the one seam, and until ``baskfy_core.twt.backtest`` exists it raises
:class:`SeamNotReady` naming exactly what it needs. It never returns an empty result, and TW2's
golden test skips with that reason rather than passing on nothing.

The seam, written out
---------------------
``baskfy_core.twt.backtest`` must provide four names, with the signatures
``baskfy_core.vbt.backtest`` already uses — the shape TW1 is being built against::

    panel_from_frame(indicated: pl.DataFrame, signal_column: str = "state") -> Panel
    gate_vector(breadth: pl.DataFrame, sessions: tuple[dt.date, ...]) -> np.ndarray
    run_backtest(panel: Panel, gate: np.ndarray, params: BacktestParams) -> BacktestResult
    summarise(result: BacktestResult) -> BacktestStats | None

and one dataclass, ``BacktestParams``, accepting at least ``start``, ``sleeve_inr``,
``cost_pct_per_side``, ``tick`` and ``config``. A produced trade must carry ``symbol``,
``entry_date``, ``exit_date``, ``entry_price``, ``exit_price``, ``quantity``, ``pnl_inr``,
``return_pct``, ``hold_sessions`` and ``reason`` (:func:`compare.trades_from` checks each by name,
and says which one is missing).

The golden parameter set
------------------------
The config is ``DEFAULT_TWT_CONFIG``, **unchanged**. The one number the study used differently is
passed where ``baskfy_core.twt.signals`` provides for it: :data:`GOLDEN_FLOOR_INR` is
``EntryConfig.research_min_turnover_inr`` [₹2 crore], handed to ``signal_mask``'s ``floor_inr``
keyword, which that module's docstring reserves for "exactly one caller: TW2's golden parameter
set". ``04`` §3.5 and DECISIONS-TW TW0.3 name both floors. Everything else is the sleeve's own
default, which is the point: a golden run that had to be specially configured would be grading a
configuration rather than the sleeve.
"""

from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Final

import polars as pl
from twt_compare import (
    DEFAULT_METRIC_TOLERANCE,
    DEFAULT_TRADE_TOLERANCE,
    MetricComparison,
    Trade,
    TradeComparison,
    compare_metrics,
    compare_trades,
    read_golden_metrics,
    read_golden_trades,
    trades_from,
)
from twt_panel import PANEL, LoadedPanel, load
from twt_scan import FIXTURES

from baskfy_core.twt.breadth import breadth_series
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, TwtConfig
from baskfy_core.twt.signals import signal_mask, with_twt_columns

#: The module TW2 proper calls, and the four names it must export.
SEAM_MODULE: Final = "baskfy_core.twt.backtest"
SEAM_NAMES: Final[tuple[str, ...]] = (
    "panel_from_frame",
    "gate_vector",
    "run_backtest",
    "summarise",
    "BacktestParams",
)

#: The committed goldens — copies of ``research/tight-close/out/``, byte for byte.
GOLDEN_TRADES: Final = FIXTURES / "golden_trades.csv"
GOLDEN_METRICS: Final = FIXTURES / "golden_metrics.json"

#: The study's own window and money (``final_tc.py``): ₹10 lakh from 2017-10-16, 25 bps a side,
#: fills on the exchange tick (the paisa, not the desk's ₹0.05).
GOLDEN_START: Final = dt.date(2017, 10, 16)
GOLDEN_SLEEVE_INR: Final = Decimal("1000000")
GOLDEN_COST_PCT_PER_SIDE: Final = Decimal("0.25")
GOLDEN_TICK: Final = Decimal("0.01")

#: The sleeve's own configuration, unchanged. TW2 grades the sleeve, not a variant of it.
GOLDEN_CONFIG: Final[TwtConfig] = DEFAULT_TWT_CONFIG
#: The research's liquidity floor [₹2 crore], passed to ``signal_mask``'s ``floor_inr`` keyword —
#: the one knob ``04`` §3.5 provides for this one caller. The shipped floor is ₹5 crore.
GOLDEN_FLOOR_INR: Final[Decimal] = DEFAULT_TWT_CONFIG.entry.research_min_turnover_inr
#: The column the backtest reads as "this session is an entry signal".
GOLDEN_SIGNAL_COLUMN: Final[str] = "signal"

#: ``summarise``'s field names, against the keys ``final_metrics.json`` uses for the same numbers.
METRIC_NAMES: Final[dict[str, str]] = {
    "cagr_pct": "cagr_pct",
    "max_drawdown_pct": "max_dd_pct",
    "trades": "trades",
    "win_rate_pct": "win_rate_pct",
    "profit_factor": "profit_factor",
    "avg_hold_sessions": "avg_hold",
    "exposure_pct": "exposure_pct",
}


class SeamNotReady(RuntimeError):
    """``baskfy_core.twt.backtest`` is absent or incomplete. Raised, never swallowed."""


def seam_status() -> tuple[bool, str]:
    """``(ready, why)``. The one place that decides whether TW2 proper can run at all."""
    try:
        module = import_module(SEAM_MODULE)
    except ModuleNotFoundError:
        return False, (
            f"{SEAM_MODULE} does not exist yet. TW2's harness is built and tested; the core is "
            f"TW1's sibling's, and when it lands this module needs {', '.join(SEAM_NAMES)} from it."
        )
    missing = [name for name in SEAM_NAMES if not hasattr(module, name)]
    if missing:
        return False, f"{SEAM_MODULE} exists but is missing {missing}"
    return True, f"{SEAM_MODULE} provides {', '.join(SEAM_NAMES)}"


def seam() -> ModuleType:
    ready, why = seam_status()
    if not ready:
        raise SeamNotReady(why)
    return import_module(SEAM_MODULE)


def tagged_frame(loaded: LoadedPanel, config: TwtConfig = GOLDEN_CONFIG) -> pl.DataFrame:
    """The panel's bars with every indicator and signal column the sleeve computes.

    This half needs no ``backtest`` module and runs today: it is how TW2 can check the scan's own
    counts against ``01`` §2 before the engine exists.
    """
    detected = with_twt_columns(loaded.bars, loaded.calendar, config)
    return detected.with_columns(
        signal_mask(detected, config, GOLDEN_FLOOR_INR).alias(GOLDEN_SIGNAL_COLUMN)
    )


@dataclass(frozen=True, slots=True)
class GoldenRun:
    """What the runner produced and how it compares to the study."""

    trades: tuple[Trade, ...]
    metrics: dict[str, float]
    trade_comparison: TradeComparison
    metric_comparison: MetricComparison

    @property
    def clean(self) -> bool:
        return self.trade_comparison.clean and self.metric_comparison.passes

    def render(self) -> str:
        return "\n".join(
            [
                "TRADES",
                self.trade_comparison.render(),
                "",
                "METRICS",
                self.metric_comparison.render(),
            ]
        )


def execute(loaded: LoadedPanel, config: TwtConfig = GOLDEN_CONFIG) -> object:
    """**The seam.** The study's panel through the sleeve's own functions, into a backtest result.

    This is the call TW2 proper makes and the only thing in this harness that needs TW1's core.
    Four lines, in the order ``04`` §11 sequences a session. The return type is ``object`` because
    the engine's ``BacktestResult`` does not exist yet to be named; :func:`produce` and
    :func:`produce_metrics` are the typed readings of it.
    """
    engine = seam()
    frame = tagged_frame(loaded, config)
    panel = engine.panel_from_frame(frame, GOLDEN_SIGNAL_COLUMN)
    gate = engine.gate_vector(breadth_series(frame, config), panel.sessions)
    return engine.run_backtest(
        panel,
        gate,
        engine.BacktestParams(
            start=GOLDEN_START,
            sleeve_inr=GOLDEN_SLEEVE_INR,
            cost_pct_per_side=GOLDEN_COST_PCT_PER_SIDE,
            tick=GOLDEN_TICK,
            config=config,
        ),
    )


def produce(result: object) -> tuple[Trade, ...]:
    """A run's trades, adapted into the comparison's own shape."""
    trades = getattr(result, "trades", None)
    if trades is None:
        raise SeamNotReady(f"{SEAM_MODULE}.run_backtest returned {result!r}, which has no trades")
    return trades_from(trades)


def produce_metrics(result: object) -> dict[str, float]:
    """A run's headline numbers, keyed as ``final_metrics.json`` keys them."""
    stats = seam().summarise(result)
    if stats is None:
        raise SeamNotReady("summarise() returned None: the run produced no equity curve")
    return {study: float(getattr(stats, ours)) for ours, study in METRIC_NAMES.items()}


def grade(
    produced: tuple[Trade, ...],
    metrics: dict[str, float],
    trades_path: Path = GOLDEN_TRADES,
    metrics_path: Path = GOLDEN_METRICS,
) -> GoldenRun:
    """Grade a produced run against the committed goldens. Needs no panel and no core."""
    expected = read_golden_trades(trades_path)
    return GoldenRun(
        trades=produced,
        metrics=metrics,
        trade_comparison=compare_trades(produced, expected, DEFAULT_TRADE_TOLERANCE),
        metric_comparison=compare_metrics(
            metrics, read_golden_metrics(metrics_path), DEFAULT_METRIC_TOLERANCE
        ),
    )


def run(path: Path = PANEL, config: TwtConfig = GOLDEN_CONFIG) -> GoldenRun:
    """Load, execute **once**, grade both readings of the same run."""
    result = execute(load(config, path), config)
    return grade(produce(result), produce_metrics(result))


def main() -> int:  # pragma: no cover - operator entry point
    parser = argparse.ArgumentParser(description="TW2: reproduce the tight-close study")
    parser.add_argument("--seam", action="store_true", help="report whether the core is ready")
    parsed = parser.parse_args()
    ready, why = seam_status()
    print(f"seam: {'READY' if ready else 'NOT READY'} — {why}")
    if parsed.seam:
        return 0
    if not ready:
        return 1
    outcome = run()
    print(outcome.render())
    return 0 if outcome.clean else 1


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
