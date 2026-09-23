"""The options run's one-shot commands (``docs/options/06`` OP2).

    uv run python -m baskfy_worker.options_cli seed             # event days + config rows
    uv run python -m baskfy_worker.options_cli refresh-master   # tonight's NFO master (Kite)
    uv run python -m baskfy_worker.options_cli probe            # OP0's six live reads (OP3)
    uv run python -m baskfy_worker.options_cli backfill-index-bars --from 2015-01-01 [--to D]
    uv run python -m baskfy_worker.options_cli collect-once     # one chain minute, by hand
    uv run python -m baskfy_worker.options_cli plan [--at ISO]  # the O1 plan builder, once (OP6)
    uv run python -m baskfy_worker.options_cli plan-o2 [--at ISO] # the O2 plan builder, once (OP7)
    uv run python -m baskfy_worker.options_cli plan-o3 [--at ISO] # the O3 plan builder, once (OP8)

``seed`` writes, for ``BASKFY_SOLE_USER_ID``, the verified event days of
``baskfy_worker.seeds.options_event_days``, the ``op_book_config`` row and the four
``op_sleeve_config`` rows — every capital **0** (PACK.6). Idempotent; never resets a chosen number.

``refresh-master`` is the Beat job's body without Celery: one read-only Kite call.

``probe`` runs OP0's six deferred reads (``baskfy_worker.options.probe``) and prints one JSON
report; it writes nothing to the database and places nothing. ``backfill-index-bars`` is the
Tier-1 NIFTY 50 / INDIA VIX minute backfill (resumable, committed per 60-day window, on the bulk
lane). ``collect-once`` takes one chain snapshot now, **ignoring** the collect flag but not the
session window, the trading day or the Kite session — for a person verifying the collector by hand.
``plan`` runs the O1 builder for ``--at`` (default now) ignoring the monitor flag but nothing
else — idempotent per date, it returns a decided session unchanged and prints the alert instead of
sending it. ``plan-o2`` is the same for O2, and makes no Kite call at all (OP7.3); ``plan-o3``
is the same for O3-B then O3-A, asking only the margin calculator. None of these commands has an
order path, and no flag changes that.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from collections.abc import Sequence
from functools import partial

from sqlalchemy import select

from baskfy_core.models import OpContract
from baskfy_core.models.base import JsonObject
from baskfy_providers.factory import KiteLane, build_kite_provider
from baskfy_providers.settings import get_provider_settings
from baskfy_worker.celery_app import IST
from baskfy_worker.db import checkpointed_session, session_scope
from baskfy_worker.options import index_bars
from baskfy_worker.options.collector import collect_gate, collect_minute
from baskfy_worker.options.master import EmptyMaster, master_alert, refresh_master, to_contract
from baskfy_worker.options.plan import kite_margin_reader, plan_o1_minute
from baskfy_worker.options.plan_o2 import plan_o2_minute
from baskfy_worker.options.plan_o3 import plan_o3_minute
from baskfy_worker.options.probe import ProbeInputs, run_probe
from baskfy_worker.options.reads import build_options_kite
from baskfy_worker.providers import sole_user_id
from baskfy_worker.seeds.options_event_days import seed_options


async def _seed() -> JsonObject:
    user_id = sole_user_id()
    if user_id is None:
        return {"skipped": "no BASKFY_SOLE_USER_ID configured"}
    async with session_scope() as session:
        written = await seed_options(session, user_id)
    return {"user_id": user_id, **written}


async def _refresh(day: dt.date) -> JsonObject:
    provider = build_kite_provider(get_provider_settings(), lane=KiteLane.BULK)
    records = provider.option_contracts("NIFTY")
    async with session_scope() as session:
        try:
            report = await refresh_master(session, records, as_of=day)
        except EmptyMaster as exc:
            return {"refused": str(exc)}
    out = report.as_dict()
    alert = master_alert(report)
    if alert is not None:
        out["alert_summary"] = alert.summary
    return out


async def _probe_inputs(today: dt.date) -> ProbeInputs:
    """The two things the probe reads from the database: index tokens and an expired contract."""
    async with session_scope() as session:
        rows = await index_bars.index_rows(session)
        expired = (
            await session.execute(
                select(OpContract)
                .where(OpContract.underlying == "NIFTY", OpContract.expiry < today)
                .order_by(OpContract.expiry.desc(), OpContract.strike)
                .limit(1)
            )
        ).scalar_one_or_none()
    return ProbeInputs(
        today=today,
        index_tokens={symbol: row.kite_token for symbol, row in rows.items()},
        expired=to_contract(expired) if expired is not None else None,
    )


def _probe(today: dt.date) -> JsonObject:
    from baskfy_worker.tasks.celery_tasks import kite_session_usable  # noqa: PLC0415 - heavy

    if not kite_session_usable():
        return {"skipped": "no usable Kite session on this box; log in first", "read_only": True}
    inputs = asyncio.run(_probe_inputs(today))
    return run_probe(build_options_kite(), inputs)


async def _backfill(start: dt.date, end: dt.date) -> JsonObject:
    kite = build_options_kite(bars_lane=KiteLane.BULK)
    async with checkpointed_session() as session:
        report = await index_bars.backfill(
            session,
            kite.bars,
            start,
            end,
            now=dt.datetime.now(tz=IST),
            checkpoint=session.commit,
        )
    return {"from": start.isoformat(), "to": end.isoformat(), **report.as_dict()}


async def _collect_once() -> JsonObject:
    from baskfy_api.swing_health import is_session_day  # noqa: PLC0415 - the API's calendar read
    from baskfy_worker.tasks.celery_tasks import kite_session_usable  # noqa: PLC0415 - heavy

    now = dt.datetime.now(tz=IST)
    async with session_scope() as session:
        refused = await collect_gate(
            now,
            enabled=True,
            kite_ok=kite_session_usable,
            trading_day=partial(is_session_day, session),
        )
        if refused is not None:
            return {"skipped": refused}
        report = await collect_minute(session, build_options_kite().quotes, now)
    return report.as_dict()


async def _plan(now: dt.datetime) -> JsonObject:
    from baskfy_api.swing_health import is_session_day  # noqa: PLC0415 - the API's calendar read
    from baskfy_worker.tasks.celery_tasks import kite_session_usable  # noqa: PLC0415 - heavy

    user_id = sole_user_id()
    if user_id is None:
        return {"skipped": "no BASKFY_SOLE_USER_ID configured"}
    margin = kite_margin_reader(build_options_kite().general) if kite_session_usable() else None
    async with session_scope() as session:
        if not await is_session_day(session, now.astimezone(IST).date()):
            return {"skipped": "not an NSE trading day"}
        report, alerts = await plan_o1_minute(
            session, user_id, now, trading_day=True, margin=margin
        )
    report["alerts"] = [alert.summary for alert in alerts]
    return report


async def _plan_o2(now: dt.datetime) -> JsonObject:
    from baskfy_api.swing_health import is_session_day  # noqa: PLC0415 - the API's calendar read

    user_id = sole_user_id()
    if user_id is None:
        return {"skipped": "no BASKFY_SOLE_USER_ID configured"}
    async with session_scope() as session:
        if not await is_session_day(session, now.astimezone(IST).date()):
            return {"skipped": "not an NSE trading day"}
        report, alerts = await plan_o2_minute(session, user_id, now, trading_day=True)
    report["alerts"] = [alert.summary for alert in alerts]
    return report


async def _plan_o3(now: dt.datetime) -> JsonObject:
    from baskfy_api.swing_health import is_session_day  # noqa: PLC0415 - the API's calendar read
    from baskfy_worker.tasks.celery_tasks import kite_session_usable  # noqa: PLC0415 - heavy

    user_id = sole_user_id()
    if user_id is None:
        return {"skipped": "no BASKFY_SOLE_USER_ID configured"}
    margin = kite_margin_reader(build_options_kite().general) if kite_session_usable() else None
    async with session_scope() as session:
        if not await is_session_day(session, now.astimezone(IST).date()):
            return {"skipped": "not an NSE trading day"}
        report, alerts = await plan_o3_minute(
            session, user_id, now, trading_day=True, margin=margin
        )
    report["alerts"] = [alert.summary for alert in alerts]
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="options_cli", description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "seed",
            "refresh-master",
            "probe",
            "backfill-index-bars",
            "collect-once",
            "plan",
            "plan-o2",
            "plan-o3",
        ),
    )
    parser.add_argument("--date", help="refresh-master / probe: the as-of date (default: today)")
    parser.add_argument(
        "--at", help="plan / plan-o2 / plan-o3: the IST moment to plan for (default: now)"
    )
    parser.add_argument("--from", dest="start", help="backfill-index-bars: first date")
    parser.add_argument("--to", dest="end", help="backfill-index-bars: last date (default: today)")
    args = parser.parse_args(argv)
    today = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(tz=IST).date()
    result: JsonObject
    if args.command == "seed":
        result = asyncio.run(_seed())
    elif args.command == "refresh-master":
        result = asyncio.run(_refresh(today))
    elif args.command == "probe":
        result = _probe(today)
    elif args.command == "backfill-index-bars":
        if not args.start:
            parser.error("backfill-index-bars needs --from")
        end = dt.date.fromisoformat(args.end) if args.end else dt.datetime.now(tz=IST).date()
        result = asyncio.run(_backfill(dt.date.fromisoformat(args.start), end))
    elif args.command in ("plan", "plan-o2", "plan-o3"):
        at = dt.datetime.fromisoformat(args.at) if args.at else dt.datetime.now(tz=IST)
        moment = at if at.tzinfo is not None else at.replace(tzinfo=IST)
        planners = {"plan": _plan, "plan-o2": _plan_o2, "plan-o3": _plan_o3}
        result = asyncio.run(planners[args.command](moment))
    else:
        result = asyncio.run(_collect_once())
    sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
