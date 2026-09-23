#!/usr/bin/env python
"""Run one options sleeve at one tier and print its row (OP12).

    cd decile-blueprint
    uv run python ../tools/options/backtest.py --fixture
    uv run python ../tools/options/backtest.py --sleeve O2 --tier 1 --from 2015-01-01

`docs/options/06` OP12: "`tools/options/backtest.py --sleeve --tier --from --to` and the worker task
on the compute queue". Two modes.

``--fixture`` needs no database: it runs the OP4 fixture days (``packages/core/tests/
options_scan_fixtures.py``) through ``baskfy_core.options.backtest_suite.run_tier`` for every
sleeve, Tier 1 then Tier 2 on the model chain, and prints one row per (sleeve, tier) — never a
pooled line (`04` §13.4). It is the suite's smoke test from the command line.

The default mode is the Celery task's body (``baskfy_worker.options.backtest_run.run_backtest``)
run in-process against ``BASKFY_DATABASE_URL`` for ``BASKFY_SOLE_USER_ID`` (or ``--user-id``): the
days are the stored NIFTY 50 minute days in the range, the row is appended to ``op_backtest_run``
and committed, and the report is printed as JSON with how long it took. A day before the options
master's first expiry is counted as ``uncalendared`` (DECISIONS-OP OP12.3). Nothing here reaches a
broker.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE_TESTS = ROOT / "decile-blueprint" / "packages" / "core" / "tests"

from baskfy_core.options.backtest import Tier  # noqa: E402
from baskfy_core.options.backtest_suite import run_tier  # noqa: E402
from baskfy_core.options.config import OptionsCeilings, OptionsConfig, Sleeve  # noqa: E402
from baskfy_core.options.replay_day import ModelChain  # noqa: E402
from baskfy_core.options.scan import DayContext  # noqa: E402

TIERS = {"1": Tier.SIGNALS, "2": Tier.MODELLED, "3": Tier.OBSERVED}


def _fixture() -> int:
    if str(CORE_TESTS) not in sys.path:
        sys.path.insert(0, str(CORE_TESTS))
    import options_scan_fixtures as fx  # noqa: PLC0415 - the test tree, on demand

    days = [
        fx.market(fx.QUIET_MONTHLY, fx.quiet_monthly_bars()),
        fx.market(fx.TREND_WEEKLY, fx.trend_weekly_bars()),
        fx.market(fx.GAP_HOLD, fx.gap_hold_bars()),
        fx.market(fx.O2_UP_BREAK, fx.o2_up_break_bars()),
    ]
    options, ceilings = OptionsConfig(), OptionsCeilings()
    for sleeve in Sleeve:
        for tier in (Tier.SIGNALS, Tier.MODELLED):
            result = run_tier(
                sleeve, tier, days, context=DayContext(), options=options, ceilings=ceilings,
                source_for=None if tier is Tier.SIGNALS else (lambda m: ModelChain(m, options)),
            )  # fmt: skip
            skipped = ", ".join(f"{k} {v}" for k, v in result.skipped_by_reason) or "-"
            pnl = "" if result.net_pnl_inr is None else f"  net ₹{result.net_pnl_inr}"
            exp = "" if result.expectancy_r is None else f"  E[R] {result.expectancy_r:.2f}"
            print(
                f"{sleeve.value:<4} tier {tier.value}  sessions {result.sessions}  "
                f"{'signals' if tier is Tier.SIGNALS else 'traded'} {result.traded}{exp}{pnl}  "
                f"skipped: {skipped}"
            )
            for caveat in result.caveats:
                print(f"      {caveat}")
    return 0


async def _run(args: argparse.Namespace) -> int:
    from baskfy_worker.db import session_scope  # noqa: PLC0415 - only the database mode needs it
    from baskfy_worker.options import options_ceilings  # noqa: PLC0415
    from baskfy_worker.options.backtest_run import git_sha, run_backtest  # noqa: PLC0415
    from baskfy_worker.providers import sole_user_id  # noqa: PLC0415

    user_id = args.user_id if args.user_id is not None else sole_user_id()
    if user_id is None:
        print("no BASKFY_SOLE_USER_ID configured and no --user-id", file=sys.stderr)
        return 2
    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today()
    began = time.monotonic()
    async with session_scope() as session:
        report = await run_backtest(
            session, user_id, Sleeve(args.sleeve), TIERS[args.tier], start, end,
            options=OptionsConfig(), ceilings=options_ceilings(), sha=git_sha(),
        )  # fmt: skip
    out = {**report.as_dict(), "seconds": round(time.monotonic() - began, 1)}
    print(json.dumps(out, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fixture", action="store_true", help="the OP4 fixture days, no database")
    parser.add_argument("--sleeve", choices=[s.value for s in Sleeve])
    parser.add_argument("--tier", choices=sorted(TIERS))
    parser.add_argument("--from", dest="start")
    parser.add_argument("--to", dest="end")
    parser.add_argument("--user-id", type=int)
    args = parser.parse_args(argv)
    if args.fixture:
        return _fixture()
    if not (args.sleeve and args.tier and args.start):
        parser.error("--sleeve, --tier and --from are required without --fixture")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
