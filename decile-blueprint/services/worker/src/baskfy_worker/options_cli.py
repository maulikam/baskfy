"""The options run's one-shot commands (``docs/options/06`` OP2).

    uv run python -m baskfy_worker.options_cli seed             # event days + config rows
    uv run python -m baskfy_worker.options_cli refresh-master   # tonight's NFO master (Kite)

``seed`` writes, for ``BASKFY_SOLE_USER_ID``, the verified event days of
``baskfy_worker.seeds.options_event_days``, the ``op_book_config`` row and the four
``op_sleeve_config`` rows — every capital **0** (PACK.6). Idempotent; never resets a chosen number.

``refresh-master`` is the Beat job's body without Celery: one read-only Kite call. Neither command
has an order path, and no flag changes that.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from collections.abc import Sequence

from baskfy_core.models.base import JsonObject
from baskfy_providers.factory import KiteLane, build_kite_provider
from baskfy_providers.settings import get_provider_settings
from baskfy_worker.celery_app import IST
from baskfy_worker.db import session_scope
from baskfy_worker.options.master import EmptyMaster, master_alert, refresh_master
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="options_cli", description=__doc__)
    parser.add_argument("command", choices=("seed", "refresh-master"))
    parser.add_argument("--date", help="refresh-master: the as-of date (default: today, IST)")
    args = parser.parse_args(argv)
    if args.command == "seed":
        result = asyncio.run(_seed())
    else:
        day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(tz=IST).date()
        result = asyncio.run(_refresh(day))
    sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
