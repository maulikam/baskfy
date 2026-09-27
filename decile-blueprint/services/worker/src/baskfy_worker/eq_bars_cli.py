"""One-minute equity bars, by hand (LV5).

    uv run python -m baskfy_worker.eq_bars_cli session [--date YYYY-MM-DD]
    uv run python -m baskfy_worker.eq_bars_cli backfill --from 2025-01-01 [--to YYYY-MM-DD]

``session`` is the Beat job's body without Celery: the day's minutes for every liquid name, one
Kite historical call each, committed every 25 names. ``backfill`` walks ``[from, to]`` per name in
Kite's 60-day windows on the bulk lane, committing after every window; interrupted, it resumes
from the newest bar each name already has. Both are read-only against Kite and idempotent
against the database. Neither has an order path.

Cost: one call per name per window. ~570 names x one year ≈ 3,500 calls ≈ 20 minutes at the bulk
lane's clock; the swing universe as of the last published session decides the names.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_providers.factory import KiteLane
from baskfy_worker import eq_bars
from baskfy_worker.celery_app import IST
from baskfy_worker.db import checkpointed_session
from baskfy_worker.options.reads import build_options_kite
from baskfy_worker.providers import sole_user_id
from baskfy_worker.tasks.published_session import last_published_session
from baskfy_worker.telemetry import provider_retry_hooks


async def _names(
    session: AsyncSession, *, on_or_before: dt.date
) -> tuple[dt.date, list[eq_bars.EqName]]:
    user_id = sole_user_id()
    if user_id is None:
        raise SystemExit("BASKFY_SOLE_USER_ID is not set")
    as_of = await last_published_session(session, on_or_before=on_or_before)
    if as_of is None:
        raise SystemExit("nothing is published yet; the universe has no session to stand on")
    return as_of, await eq_bars.liquid_names(session, as_of=as_of, user_id=user_id)


async def _session(day: dt.date) -> dict[str, object]:
    now = dt.datetime.now(tz=IST)
    kite = build_options_kite(retry_hooks=provider_retry_hooks(), bars_lane=KiteLane.BULK)
    async with checkpointed_session() as session:
        as_of, names = await _names(session, on_or_before=day - dt.timedelta(days=1))
        report = await eq_bars.reconcile_session(
            session, kite.bars, day, now=now, names=names, checkpoint=session.commit
        )
        await session.commit()
    return {"date": day.isoformat(), "as_of": as_of.isoformat(), **report.as_dict()}


async def _backfill(start: dt.date, end: dt.date) -> dict[str, object]:
    now = dt.datetime.now(tz=IST)
    kite = build_options_kite(retry_hooks=provider_retry_hooks(), bars_lane=KiteLane.BULK)
    async with checkpointed_session() as session:
        as_of, names = await _names(session, on_or_before=end)
        report = await eq_bars.backfill(
            session, kite.bars, start, end, now=now, names=names, checkpoint=session.commit
        )
        await session.commit()
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "as_of": as_of.isoformat(),
        **report.as_dict(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eq_bars_cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("session", help="the day's minutes for every liquid name")
    one.add_argument("--date", type=dt.date.fromisoformat, default=None)
    many = sub.add_parser("backfill", help="a date range, per name, resumable")
    many.add_argument("--from", dest="start", type=dt.date.fromisoformat, required=True)
    many.add_argument("--to", dest="end", type=dt.date.fromisoformat, default=None)
    args = parser.parse_args(argv)
    today = dt.datetime.now(tz=IST).date()
    if args.command == "session":
        result = asyncio.run(_session(args.date or today))
    else:
        result = asyncio.run(_backfill(args.start, args.end or today))
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
