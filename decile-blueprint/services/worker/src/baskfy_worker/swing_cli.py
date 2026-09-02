"""One-shot swing detection for a date (`docs/swing/06` SW3): ``make swing DATE=2026-09-01``.

The nightly chain runs this as its twelfth step and Beat runs it again at 21:00. This is the same
body without Celery, and it exists for the three moments a schedule cannot cover: a date the
chain published before the swing step existed, a date whose step was skipped because the quality
gate failed, and the first time anybody wants to look at what the detectors say without waiting
until tomorrow evening.

It prints the funnel, because "0 candidates" and "0 candidates out of 41 liquid names" are
different answers and only the second one tells you whether to look at the data or at the
thresholds.

    uv run python -m baskfy_worker.swing_cli --date 2026-09-01
    uv run python -m baskfy_worker.swing_cli --date 2026-09-01 --sessions 5

``--sessions`` re-runs the last N trading days, which is what the Saturday scan does. Every date
is idempotent, so re-running one changes nothing but the row's ``created_at`` default.

**It writes and nothing else.** No order path, no broker, no Kite call: it reads `ohlcv_daily`,
`instrument`, `index_member_daily` and `index_snapshot_daily`, and writes `sw_setup_daily` and
`sw_market_daily`.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_api.settings import get_settings
from baskfy_core.models.base import JsonObject
from baskfy_worker.celery_app import IST
from baskfy_worker.providers import build_pipeline_dependencies
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.swing import recent_trading_days, run_detect_swing


async def _detect_one(
    session: AsyncSession, day: dt.date, *, user_id: int, index_slug: str, execution: bool
) -> JsonObject:
    outcome = StepOutcome()
    written = await run_detect_swing(
        session,
        outcome,
        day,
        user_id=user_id,
        index_slug=index_slug,
        execution_enabled=execution,
    )
    return {
        "date": day.isoformat(),
        "candidates": written,
        "status": outcome.status.value,
        "detail": outcome.detail,
    }


async def _run(day: dt.date, sessions: int) -> JsonObject:
    deps = build_pipeline_dependencies()
    if deps.swing_user_id is None:
        return {
            "error": "BASKFY_SOLE_USER_ID is not set, and the sw_ schema is keyed by user. "
            "Set it, or run `make seed` to create the development account."
        }
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session, session.begin():
            days = [day] if sessions <= 1 else await recent_trading_days(session, day, sessions)
            return {
                "user_id": deps.swing_user_id,
                "index_slug": deps.swing_index_slug,
                "execution_enabled": deps.swing_execution_enabled,
                "sessions": [
                    await _detect_one(
                        session,
                        one,
                        user_id=deps.swing_user_id,
                        index_slug=deps.swing_index_slug,
                        execution=deps.swing_execution_enabled,
                    )
                    for one in days
                ],
            }
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect swing setups for one date")
    parser.add_argument("--date", help="Trading day (YYYY-MM-DD). Default: today IST.")
    parser.add_argument(
        "--sessions",
        type=int,
        default=1,
        help="Re-detect the last N trading days ending at --date. Default: 1.",
    )
    args = parser.parse_args(argv)
    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(tz=IST).date()
    result = asyncio.run(_run(day, args.sessions))
    print(json.dumps(result, indent=2, default=str))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
