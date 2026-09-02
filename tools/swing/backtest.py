#!/usr/bin/env python
"""Run the swing EOD backtest (`docs/swing/04` §11) and print its statistics (SW9).

    cd decile-blueprint
    uv run python ../tools/swing/backtest.py --fixture
    uv run python ../tools/swing/backtest.py --start 2017-01-01 --end 2026-08-29

Two modes.

``--fixture`` needs no database: it runs the planted year from
``packages/core/tests/swing_backtest_fixtures.py`` — one flag whose R is worked by hand in the
fixture's docstring — through ``baskfy_core.swing.run_backtest``, prints the planted trade beside
the number the fixture expects, and exits 1 if they differ. It is the engine's smoke test from
the command line, and what `06` SW9 means by "a fixture year with a known planted flag
reproduces the planted trade's R to 2 dp".

The default mode is the Celery task's body (``baskfy_worker.tasks.swing_backtest.run_and_commit``)
run in-process against ``BASKFY_DATABASE_URL``: the bars are loaded the way the detectors are
given them (adjusted, the cash series, 200 sessions of lookback before ``--start``, names
delisted inside the run kept), the calendar comes from ``trading_day``, the run is stored in
``sw_backtest_run`` — started row committed first, the result or the error after — and the
statistics are printed with how long it took. `06` SW9 wants the full history (2017 →) under
thirty minutes on the dev box; the elapsed time this prints is that measurement. The user is
``BASKFY_SOLE_USER_ID`` unless ``--user-id`` says otherwise. Nothing here can place an order.

The printed table is the same shape either way: `04` §10's statistics, then by setup, by year,
the funnel, and the equity curve's ends. ``--json`` prints the whole ``BacktestResult.to_json()``
instead, which is exactly what the row stores.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DECILE = ROOT / "decile-blueprint"
CORE_TESTS = DECILE / "packages" / "core" / "tests"

from baskfy_core.swing.backtest import (  # noqa: E402 - after the path constants, like replay.py
    CAVEATS,
    BacktestParams,
    BacktestResult,
    BacktestTrade,
)
from baskfy_core.swing.backtest import run_backtest as run_pure  # noqa: E402
from baskfy_core.swing.journal import JournalStats  # noqa: E402

#: `04` §11's first session.
DEFAULT_START = dt.date(2017, 1, 1)


# --- printing ------------------------------------------------------------------------------


def _row(label: str, value: object) -> str:
    return f"  {label:<24}{value}"


def _stats_lines(stats: JournalStats) -> list[str]:
    pf = "—" if stats.profit_factor is None else str(stats.profit_factor)
    return [
        _row("trades", stats.trades),
        _row("win_rate_pct", stats.win_rate_pct),
        _row("expectancy_r", stats.expectancy_r),
        _row("net_r", stats.net_r),
        _row("avg_win_r", stats.avg_win_r),
        _row("avg_loss_r", stats.avg_loss_r),
        _row("profit_factor", pf),
        _row("largest_win_r", stats.largest_win_r),
        _row("largest_loss_r", stats.largest_loss_r),
        _row("current_loss_streak", stats.current_loss_streak),
    ]


def _group_lines(title: str, groups: dict[str, JournalStats]) -> list[str]:
    lines = [f"{title}:"]
    if not groups:
        return [*lines, "  (none)"]
    lines.append(f"  {'':<10}{'trades':>8}{'win%':>8}{'exp R':>9}{'net R':>10}{'PF':>8}")
    for key, stats in groups.items():
        pf = "—" if stats.profit_factor is None else str(stats.profit_factor)
        lines.append(
            f"  {key:<10}{stats.trades:>8}{stats.win_rate_pct!s:>8}"
            f"{stats.expectancy_r!s:>9}{stats.net_r!s:>10}{pf:>8}"
        )
    return lines


def report(result: BacktestResult) -> str:
    params = result.params
    lines = [
        f"swing backtest {params.start} .. {params.end}: sleeve {params.sleeve_inr}, "
        f"cost {params.cost_pct_per_side}%/side",
        "stats:",
        *_stats_lines(result.stats),
        *_group_lines("by setup", result.by_setup),
        *_group_lines("by year", {str(year): stats for year, stats in result.by_year.items()}),
        "funnel:",
        *[_row(key, value) for key, value in result.funnel.items()],
    ]
    if result.equity_curve:
        first_day, first = result.equity_curve[0]
        last_day, last = result.equity_curve[-1]
        lines += [
            "equity:",
            _row(f"{first_day}", first),
            _row(f"{last_day}", last),
        ]
    lines.append("caveats:")
    lines += [f"  - {caveat}" for caveat in CAVEATS]
    return "\n".join(lines)


def trade_line(trade: BacktestTrade) -> str:
    return (
        f"{trade.symbol} {trade.setup} {trade.entry_date} -> {trade.exit_date} "
        f"qty {trade.quantity} entry {trade.entry} stop {trade.initial_stop} "
        f"exit {trade.exit_avg} {trade.close_reason} R={trade.r_multiple}"
    )


# --- the two modes -------------------------------------------------------------------------


def run_fixture(*, as_json: bool) -> int:
    """The planted year, no database. Exit 1 if the planted R is not reproduced."""
    if str(CORE_TESTS) not in sys.path:
        sys.path.insert(0, str(CORE_TESTS))
    from swing_backtest_fixtures import PLANTED, planted_frame  # noqa: PLC0415 - fixture path

    frame, calendar = planted_frame()
    params = BacktestParams(start=calendar[0], end=calendar[-1])
    started = time.perf_counter()
    result = run_pure(frame, params, calendar=calendar)
    elapsed = time.perf_counter() - started
    if as_json:
        print(json.dumps(result.to_json(), indent=2))
        return 0
    print(report(result))
    print(f"elapsed: {elapsed:.2f}s over {len(calendar)} sessions, {frame.height} bars")
    planted = [t for t in result.trades if t.symbol == PLANTED.symbol]
    if len(planted) != 1:
        print(f"planted trade: expected exactly one {PLANTED.symbol} trade, got {len(planted)}")
        return 1
    print(f"planted trade: {trade_line(planted[0])}")
    ok = planted[0].r_multiple == PLANTED.r_multiple
    verdict = "reproduced" if ok else "NOT reproduced"
    print(f"planted R={planted[0].r_multiple} expected R={PLANTED.r_multiple}: {verdict}")
    return 0 if ok else 1


def run_stored(  # noqa: PLR0913 - one keyword per option of the run
    *,
    database_url: str,
    user_id: int,
    start: dt.date,
    end: dt.date,
    sleeve_inr: Decimal | None,
    cost_pct_per_side: Decimal | None,
    as_json: bool,
) -> int:
    """The task body against the database: load, run, store; print the stats and the time."""
    from sqlalchemy import select  # noqa: PLC0415 - database mode only
    from sqlalchemy.ext.asyncio import AsyncSession  # noqa: PLC0415

    from baskfy_core.models.swing import SwBacktestRun  # noqa: PLC0415
    from baskfy_worker.db import run_checkpointed, session_scope  # noqa: PLC0415
    from baskfy_worker.tasks.swing_backtest import run_and_commit  # noqa: PLC0415

    async def body(session: AsyncSession) -> dict[str, object]:
        return await run_and_commit(
            session,
            user_id=user_id,
            start=start,
            end=end,
            sleeve_inr=sleeve_inr,
            cost_pct_per_side=cost_pct_per_side,
        )

    started = time.perf_counter()
    summary = run_checkpointed(body, database_url)
    elapsed = time.perf_counter() - started

    async def stored() -> dict[str, object]:
        async with session_scope(database_url) as session:
            row = (
                await session.execute(
                    select(SwBacktestRun).where(SwBacktestRun.id == int(str(summary["run_id"])))
                )
            ).scalar_one()
            return dict(row.stats or {})

    stats_json = asyncio.run(stored())
    if as_json:
        print(json.dumps(stats_json, indent=2))
        return 0
    print(f"stored run {summary['run_id']} for user {user_id} ({summary['sessions']} sessions)")
    print(f"elapsed: {elapsed / 60:.1f} min ({elapsed:.0f}s)")
    # JSONB hands keys back in its own order; print them in the engine's.
    print("stats:")
    stats = stats_json.get("stats")
    if isinstance(stats, dict):
        for key in JournalStats.__dataclass_fields__:
            value = stats.get(key)
            print(_row(key, "—" if value is None else value))
    for group in ("by_setup", "by_year"):
        groups = stats_json.get(group)
        if isinstance(groups, dict):
            print(f"{group.replace('_', ' ')}:")
            for key in sorted(groups):
                entry = groups[key]
                if isinstance(entry, dict):
                    print(
                        _row(
                            key,
                            f"trades {entry.get('trades')} win% {entry.get('win_rate_pct')} "
                            f"exp R {entry.get('expectancy_r')} net R {entry.get('net_r')}",
                        )
                    )
    funnel = stats_json.get("funnel")
    if isinstance(funnel, dict):
        print("funnel:")
        for key in sorted(funnel):
            print(_row(key, funnel[key]))
    print("caveats:")
    for caveat in CAVEATS:
        print(f"  - {caveat}")
    headline = {key: summary[key] for key in ("trades", "win_rate_pct", "expectancy_r", "net_r")}
    print(" ".join(f"{key}={value}" for key, value in headline.items()))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fixture", action="store_true", help="run the planted year, no database")
    parser.add_argument(
        "--start",
        type=dt.date.fromisoformat,
        default=DEFAULT_START,
        help="first session (default 2017-01-01)",
    )
    parser.add_argument(
        "--end", type=dt.date.fromisoformat, default=None, help="last session (default: today)"
    )
    parser.add_argument(
        "--sleeve-inr", type=Decimal, default=None, help="the constant sleeve (default 1000000)"
    )
    parser.add_argument(
        "--cost-pct", type=Decimal, default=None, help="cost per side in percent (default 0.13)"
    )
    parser.add_argument(
        "--user-id", type=int, default=None, help="the sw_ user (default BASKFY_SOLE_USER_ID)"
    )
    parser.add_argument(
        "--json", action="store_true", help="print the stored JSON instead of the table"
    )
    args = parser.parse_args(argv)

    if args.fixture:
        return run_fixture(as_json=args.json)

    database_url = os.environ.get("BASKFY_DATABASE_URL", "").strip()
    if not database_url:
        print("BASKFY_DATABASE_URL is not set (or pass --fixture to run without a database)")
        return 2
    raw_user = (
        args.user_id if args.user_id is not None else os.environ.get("BASKFY_SOLE_USER_ID", "")
    )
    try:
        user_id = int(str(raw_user).strip())
    except ValueError:
        print("BASKFY_SOLE_USER_ID is not set (or pass --user-id)")
        return 2
    end = args.end or dt.date.today()
    if end < args.start:
        print(f"--end {end} is before --start {args.start}")
        return 2
    return run_stored(
        database_url=database_url,
        user_id=user_id,
        start=args.start,
        end=end,
        sleeve_inr=args.sleeve_inr,
        cost_pct_per_side=args.cost_pct,
        as_json=args.json,
    )


if __name__ == "__main__":
    sys.exit(main())
