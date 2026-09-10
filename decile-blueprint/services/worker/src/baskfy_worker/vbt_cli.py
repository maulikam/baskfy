"""One-shot volume-breakout detection for a date (``docs/vbt/06`` VB4): ``make vbt DATE=…``.

The nightly chain runs this as its thirteenth step and Beat runs it again at 21:10. This is the
same body without Celery, and it exists for the three moments a schedule cannot cover: a date the
chain published before this sleeve existed, a date whose step was skipped because the quality gate
failed, and the first time anybody wants to see what the detector says without waiting until
tomorrow evening.

It prints the **funnel**, because "0 signals" and "0 signals out of 1,412 names with a 200-day
average, 37 of which cleared the volume scan" are different answers, and only the second one tells
you whether to look at the data or at the thresholds.

    uv run python -m baskfy_worker.vbt_cli --date 2026-09-09
    uv run python -m baskfy_worker.vbt_cli --date 2026-09-09 --sessions 5
    uv run python -m baskfy_worker.vbt_cli --date 2026-09-09 --force

Every date is idempotent, so re-running one changes nothing but the row's ``created_at`` default.
By default a date that already has rows is left alone (the 21:10 retry's rule); ``--force``
re-detects it anyway, which is what you want after changing a threshold.

**It writes and nothing else.** No order path, no broker, no Kite call: it reads ``ohlcv_daily``,
``instrument``, ``index_member_daily`` and ``trading_day``, and writes ``vb_signal_daily`` and
``vb_breadth_daily``.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_api.settings import get_settings
from baskfy_core.models import TradingDay
from baskfy_core.models.base import JsonObject
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_worker.celery_app import IST
from baskfy_worker.providers import build_pipeline_dependencies
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.vbt import published_signal_count, run_detect_vbt


async def _recent_sessions(session: AsyncSession, as_of: dt.date, count: int) -> list[dt.date]:
    """The last ``count`` trading days up to and including ``as_of``, oldest first.

    Only days the calendar knows about, so ``--sessions 5`` over a long weekend re-detects five
    sessions that happened rather than five calendar days of which two were holidays.
    """
    rows = await session.execute(
        select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date <= as_of,
        )
        .order_by(TradingDay.date.desc())
        .limit(count)
    )
    return sorted(row[0] for row in rows)


async def _detect_one(
    session: AsyncSession, day: dt.date, *, user_id: int, force: bool
) -> JsonObject:
    if not force:
        already = await published_signal_count(session, user_id, day)
        if already:
            return {"date": day.isoformat(), "skipped": "already detected", "rows": already}
    outcome = StepOutcome()
    signals = await run_detect_vbt(session, outcome, day, user_id=user_id)
    return {
        "date": day.isoformat(),
        "signals": signals,
        "status": outcome.status.value,
        "detail": outcome.detail,
    }


async def _run(day: dt.date, sessions: int, *, force: bool) -> JsonObject:
    deps = build_pipeline_dependencies()
    if deps.vbt_user_id is None:
        return {
            "error": "BASKFY_SOLE_USER_ID is not set, and the vb_ schema is keyed by user. "
            "Set it, or run `make seed` to create the development account."
        }
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session, session.begin():
            days = [day] if sessions <= 1 else await _recent_sessions(session, day, sessions)
            return {
                "user_id": deps.vbt_user_id,
                "nightly_enabled": deps.vbt_nightly_enabled,
                "sessions": [
                    await _detect_one(session, one, user_id=deps.vbt_user_id, force=force)
                    for one in days
                ],
            }
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baskfy-vbt", description=__doc__)
    parser.add_argument("--date", default=None, help="trade date (YYYY-MM-DD); default today IST")
    parser.add_argument("--sessions", type=int, default=1, help="re-detect the last N trading days")
    parser.add_argument(
        "--force", action="store_true", help="re-detect a date that already has rows"
    )
    args = parser.parse_args(argv)
    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(tz=IST).date()
    report = asyncio.run(_run(day, args.sessions, force=args.force))
    print(json.dumps(report, indent=2, default=str))
    return 1 if "error" in report else 0


if __name__ == "__main__":
    sys.exit(main())
