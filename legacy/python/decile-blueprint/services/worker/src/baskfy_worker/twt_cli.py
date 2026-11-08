"""One-shot three-weeks-tight detection for a date (``docs/twt/06`` § TW4): ``make twt DATE=…``.

The nightly chain runs this as its fourteenth step and Beat runs it again at 21:00. This is the
same body without Celery, and it exists for the three moments a schedule cannot cover: a date the
chain published before this sleeve existed, a date whose step was skipped because the quality gate
failed, and the first time anybody wants to see what the detector says without waiting until
tomorrow evening.

It prints the **funnel**, because "0 signals" and "0 signals out of 4,186 names, 1,412 with a
200-day average and 47 holding the tight state" are different answers — and on this sleeve the
first one is the *normal* answer: the research's nine years produced 164 trades, about eighteen
entries a year, so a session with states and no entries is what a working night looks like.

    uv run python -m baskfy_worker.twt_cli --date 2026-09-10
    uv run python -m baskfy_worker.twt_cli --date 2026-09-10 --sessions 5
    uv run python -m baskfy_worker.twt_cli --date 2026-09-10 --force

Every date is idempotent, so re-running one changes nothing but the row's ``created_at`` default.
By default a date that already has a ``tw_breadth_daily`` row is left alone (the 21:00 retry's
rule); ``--force`` re-detects it anyway, which is what you want after changing a threshold.

**It writes and nothing else.** No order path, no broker, no Kite call, and no flag can change
that: it reads ``ohlcv_daily``, ``instrument``, ``index_member_daily``, ``trading_day`` and
``tw_position``, and it writes ``tw_state_daily``, ``tw_signal_daily``, ``tw_breadth_daily`` and
the four ratchet columns of ``tw_position``. It never touches ``stop_price``, ``gtt_id`` or
``gtt_trigger``: the trail it computes is a level for a plan a person confirms (``02`` Track C
§3).
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
from baskfy_worker.tasks.twt import detect_session
from baskfy_worker.tasks.twt_evening import (
    SOURCE_EVENING,
    SOURCE_MORNING,
    last_detected_session,
    run_twt_evening,
)


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


async def _run(day: dt.date, sessions: int, *, force: bool) -> JsonObject:
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
            days = [day] if sessions <= 1 else await _recent_sessions(session, day, sessions)
            return {
                "user_id": deps.twt_user_id,
                "nightly_enabled": deps.twt_nightly_enabled,
                "sessions": [
                    await detect_session(session, one, user_id=deps.twt_user_id, force=force)
                    for one in days
                ],
            }
    finally:
        await engine.dispose()


async def _plan(day: dt.date | None, source: str) -> JsonObject:
    """TW6's ``make twt-plan`` — build the plan for one session and print it. **Places nothing.**

    The routes the first live morning uses are the desk's; this is the same plan from a terminal,
    for the evenings when there is no browser to hand. It writes ``tw_plan``, ``tw_plan_line`` and
    ``tw_plan_skip``, and every line it writes is ``PROPOSED``.

    With no ``--date`` it plans the last session the detector wrote a tradeable breadth row for,
    which before the open is yesterday — ``04`` §11.1's clock, not the calendar's.
    """
    deps = build_pipeline_dependencies()
    if deps.twt_user_id is None:
        return {
            "error": "BASKFY_SOLE_USER_ID is not set, and the tw_ schema is keyed by user. "
            "Set it, or run `make seed` to create the development account."
        }
    if source not in (SOURCE_EVENING, SOURCE_MORNING):
        return {"error": f"SOURCE must be {SOURCE_EVENING} or {SOURCE_MORNING}, not {source!r}"}
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    user_id = int(deps.twt_user_id)
    try:
        async with maker() as session, session.begin():
            today = dt.datetime.now(tz=IST).date()
            one = day or await last_detected_session(session, user_id, today)
            if one is None:
                return {"error": "no detected session to plan from; run `make twt DATE=…` first"}
            outcome = StepOutcome()
            report = await run_twt_evening(
                session,
                outcome,
                one,
                user_id=user_id,
                execution_enabled=deps.twt_execution_enabled,
                source=source,
                notify=False,
            )
            if report is None:
                return {"date": one.isoformat(), "skipped": outcome.detail}
            return {"user_id": user_id, "plan": report.as_detail()}
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baskfy-twt", description=__doc__)
    parser.add_argument("--date", default=None, help="trade date (YYYY-MM-DD); default today IST")
    parser.add_argument("--sessions", type=int, default=1, help="re-detect the last N trading days")
    parser.add_argument(
        "--force", action="store_true", help="re-detect a date that already has rows"
    )
    parser.add_argument(
        "--plan",
        nargs="?",
        const=SOURCE_EVENING,
        default=None,
        help=(
            f"build the session's plan instead of detecting (TW6): {SOURCE_EVENING} or "
            f"{SOURCE_MORNING}. Places nothing."
        ),
    )
    args = parser.parse_args(argv)
    if args.plan is not None:
        day = dt.date.fromisoformat(args.date) if args.date else None
        report = asyncio.run(_plan(day, str(args.plan).upper()))
        print(json.dumps(report, indent=2, default=str))
        return 1 if "error" in report else 0
    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(tz=IST).date()
    report = asyncio.run(_run(day, args.sessions, force=args.force))
    print(json.dumps(report, indent=2, default=str))
    return 1 if "error" in report else 0


if __name__ == "__main__":
    sys.exit(main())
