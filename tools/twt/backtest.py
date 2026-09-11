"""TW9's run over the plant's own bars: ``make twt-backtest``.

    make twt-backtest                                  # 2017-10-16 -> the last published session
    make twt-backtest START=2021-01-01                  # a slice
    make twt-backtest DATE=2026-09-09                   # end somewhere other than the last bar
    make twt-backtest SLEEVE=2500000                    # the book at a different size
    make twt-backtest JSON=out/twt.json                 # the numbers, for a diff

    uv run python ../tools/twt/backtest.py --help       # the same thing by hand

It runs ``baskfy_core.twt.backtest`` over ``ohlcv_daily`` at the **shipped** ₹5 crore liquidity
floor and the exchange's ₹0.05 tick — every default of ``BacktestParams`` is already the shipped
sleeve's, so this passes none of them (DECISIONS-TW **TW2.13**) — and appends one
``tw_backtest_run`` row with ``source = PLANT``. ``/twt/backtest`` then shows it beside TW2's
reproduction of the study, and the drift between this run's CAGR and ``01`` §6's published 20.92 %
is on the row whether or not anybody likes it.

**It is sized against the run's own parameter and never against ``tw_config.sleeve_capital_inr``.**
``--sleeve`` defaults to ``04`` §12's ₹10 lakh, which is the amount ``01`` §6's numbers were
produced at. Reading the live sleeve's capital into a backtest is how a research number quietly
becomes a claim about the user's own money, and the job asserts the absence with a spy.

**It places nothing and changes no setting.** It reads four tables, writes one row, and appends —
two runs of the same date make two rows and edit nothing, because the number that was on the page
when the execution flag was considered has to survive a recalibration that produces another.

Exit codes: ``0`` the run finished; ``1`` it raised (and the row says so, with ``finished_at`` and
``error`` set); ``2`` there is nothing to run against — no sole user, or an empty plant.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "decile-blueprint" / "packages" / "core" / "src"))
sys.path.insert(0, str(REPO / "decile-blueprint" / "services" / "api" / "src"))
sys.path.insert(0, str(REPO / "decile-blueprint" / "services" / "worker" / "src"))

from sqlalchemy.ext.asyncio import (  # noqa: E402 - after the sys.path inserts above
    async_sessionmaker,
    create_async_engine,
)

from baskfy_api.settings import get_settings  # noqa: E402
from baskfy_core.models.base import JsonObject  # noqa: E402
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG  # noqa: E402
from baskfy_core.twt.published import PUBLISHED  # noqa: E402
from baskfy_worker.providers import build_pipeline_dependencies  # noqa: E402
from baskfy_worker.tasks.twt_backtest import run_twt_backtest  # noqa: E402


async def _run(
    *,
    start: dt.date,
    end: dt.date | None,
    sleeve_inr: Decimal,
    measure_etf_denominator: bool,
) -> JsonObject:
    deps = build_pipeline_dependencies()
    if deps.twt_user_id is None:
        return {
            "error": "BASKFY_SOLE_USER_ID is not set, and the tw_ schema is keyed by user. "
            "Set it, or run `make seed` to create the development account."
        }
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session, session.begin():
            row = await run_twt_backtest(
                session,
                user_id=deps.twt_user_id,
                start=start,
                end=end,
                sleeve_inr=sleeve_inr,
                measure_etf_denominator=measure_etf_denominator,
            )
            return {
                "run_id": row.id,
                "user_id": deps.twt_user_id,
                "source": row.source,
                "started_at": row.started_at.isoformat(),
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                "params": row.params,
                "stats": row.stats,
                "drift": row.drift,
            }
    finally:
        await engine.dispose()


def _print(report: JsonObject, took: float) -> None:
    stats = report.get("stats")
    drift = report.get("drift")
    if not isinstance(stats, dict):
        print("the run finished without statistics, which should not happen", file=sys.stderr)
        return
    etf = stats.get("etf_denominator")
    print(f"{'':<26}{'run':>12} {'01 §6':>12}")
    for label, key, published in (
        ("CAGR %", "cagr_pct", PUBLISHED.cagr_pct),
        ("max drawdown %", "max_drawdown_pct", PUBLISHED.max_drawdown_pct),
        ("trades", "trades", PUBLISHED.trades),
        ("win rate %", "win_rate_pct", PUBLISHED.win_rate_pct),
        ("profit factor", "profit_factor", PUBLISHED.profit_factor),
        ("avg hold (sessions)", "avg_hold_sessions", PUBLISHED.avg_hold_sessions),
        ("time invested %", "exposure_pct", PUBLISHED.exposure_pct),
        ("in sample CAGR %", "in_sample_cagr_pct", PUBLISHED.in_sample_cagr_pct),
        ("out of sample CAGR %", "out_of_sample_cagr_pct", PUBLISHED.out_of_sample_cagr_pct),
        ("gate off CAGR %", "gate_off_cagr_pct", PUBLISHED.gate_off_cagr_pct),
        ("gate off max DD %", "gate_off_max_drawdown_pct", PUBLISHED.gate_off_max_drawdown_pct),
    ):
        value = stats.get(key)
        print(f"{label:<26}{'—' if value is None else value:>12} {published:>12}")
    print(
        f"\nuniverse {stats.get('universe')} names over {stats.get('sessions')} sessions, "
        f"{stats.get('start')} -> {stats.get('end')}"
    )
    print(f"clamped below stop: {stats.get('clamped_below_stop')} (TW2.12 measured 0 on the panel)")
    if isinstance(etf, dict):
        print(
            f"ETFs in the breadth denominator (TW2.2): {etf.get('etf_instruments')} instruments, "
            f"{etf.get('etf_bars')} bars, largest reading difference "
            f"{etf.get('max_pct_above_dma_delta')} pt, "
            f"{etf.get('gate_verdicts_changed')} gate verdicts changed"
        )
    if isinstance(drift, dict):
        print(
            f"\ndrift: {drift.get('cagr_pct_delta')} CAGR points "
            f"({drift.get('run_cagr_pct')} against {drift.get('published_cagr_pct')}), "
            f"{drift.get('trades_delta')} trades — "
            + ("FLAGGED" if drift.get("flagged") else "inside the threshold")
        )
        if drift.get("flagged"):
            print(
                "  A flag is not a verdict. It says the two numbers stopped agreeing, not which "
                "one is wrong. DECISIONS-TW TW2.13 names four differences that are known in "
                "advance: the ₹0.05 tick, the ₹5 crore floor, real corporate actions, and the "
                "plant's own history."
            )
        print(
            "  The largest of those is the floor. `01` §6's headline is the research's ₹2 crore "
            f"book; this sleeve ships at ₹5 crore, and `01` §7's own row for it reads "
            f"{PUBLISHED.shipped_floor_cagr_pct} % at "
            f"{PUBLISHED.shipped_floor_max_drawdown_pct} % on "
            f"{PUBLISHED.shipped_floor_trades} trades. Compare against that before calling a "
            "difference a defect."
        )
    print(f"\n[{took:.0f}s]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="twt-backtest", description=__doc__)
    parser.add_argument(
        "--start",
        default=DEFAULT_TWT_CONFIG.backtest.start.isoformat(),
        help="first tradeable session (YYYY-MM-DD); default `04` §12's 2017-10-16",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="last session (YYYY-MM-DD); default the last session the plant has published",
    )
    parser.add_argument(
        "--sleeve",
        default=str(DEFAULT_TWT_CONFIG.backtest.initial_capital_inr),
        help="the run's own capital in rupees; NEVER tw_config.sleeve_capital_inr",
    )
    parser.add_argument(
        "--skip-etf-denominator",
        action="store_true",
        help="skip TW2.2's counterfactual breadth pass (it costs a second indicator pass)",
    )
    parser.add_argument("--json", default=None, help="also write the whole row here")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    try:
        report = asyncio.run(
            _run(
                start=dt.date.fromisoformat(args.start),
                end=dt.date.fromisoformat(args.end) if args.end else None,
                sleeve_inr=Decimal(args.sleeve),
                measure_etf_denominator=not args.skip_etf_denominator,
            )
        )
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        print(
            "the tw_backtest_run row carries finished_at and error, and the exception was "
            "re-raised rather than swallowed — a failed run that looked finished would be worse",
            file=sys.stderr,
        )
        return 1
    if "error" in report:
        print(report["error"], file=sys.stderr)
        return 2
    _print(report, time.perf_counter() - started)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
