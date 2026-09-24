"""The FO run's operator commands (``docs/fno/06`` FO2).

    python -m baskfy_worker.fno_cli seed
    python -m baskfy_worker.fno_cli backfill --from 2022-01-03 [--to 2026-09-24] \
        [--seed-archive ~/baskfy-research/fno/archive]
    python -m baskfy_worker.fno_cli ingest [--date 2026-09-24]

``seed`` writes the sole user's ``fo_sleeve_config`` (F1 ₹10,00,000 by Maulik's M.1, F2 ₹0, risk
1.0 %) and ``fo_book_config`` rows; idempotent, never resets a number a person chose.

``backfill`` is resumable: sessions already ``INGESTED`` are skipped, each session commits as it
lands, one NSE request per session at the NSE limiter. ``--seed-archive`` copies raw zips already
on disk into the provider's archive first, so NSE is not asked for them again.

``ingest`` runs one night by hand — the bhavcopy and the next session's ban list — exactly as the
18:30 task does, but without the ``BASKFY_FNO_SCAN_ENABLED`` gate: a person at a terminal is the
gate. Every command is read-only toward the broker; none can reach an order path.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from baskfy_core.models.base import JsonObject
from baskfy_providers.factory import build_archive, build_nse_provider
from baskfy_providers.settings import get_provider_settings
from baskfy_worker.celery_app import IST
from baskfy_worker.db import checkpointed_session, session_scope
from baskfy_worker.fno.backfill import BACKFILL_START, backfill_days, trading_days
from baskfy_worker.fno.nightly import run_night
from baskfy_worker.providers import sole_user_id
from baskfy_worker.seeds.fno_config import seed_fno
from baskfy_worker.telemetry import provider_retry_hooks


async def _seed() -> JsonObject:
    user_id = sole_user_id()
    if user_id is None:
        return {"skipped": "no BASKFY_SOLE_USER_ID configured"}
    async with session_scope() as session:
        written = await seed_fno(session, user_id)
    return {"user_id": user_id, **written}


async def _backfill(start: dt.date, end: dt.date, seed_dir: Path | None) -> JsonObject:
    settings = get_provider_settings()
    archive = build_archive(settings)
    provider = build_nse_provider(settings, archive=archive, retry_hooks=provider_retry_hooks())
    async with checkpointed_session() as session:
        days = await trading_days(session, start, end)
        report = await backfill_days(
            session,
            provider,
            days,
            archive=archive,
            seed_dir=seed_dir,
            checkpoint=session.commit,
        )
    return {"from": start.isoformat(), "to": end.isoformat(), **report.as_dict()}


async def _ingest(day: dt.date) -> JsonObject:
    provider = build_nse_provider(get_provider_settings(), retry_hooks=provider_retry_hooks())
    async with session_scope() as session:
        return await run_night(session, provider, day, now_ist=dt.datetime.now(tz=IST))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fno_cli", description=__doc__)
    parser.add_argument("command", choices=("seed", "backfill", "ingest"))
    parser.add_argument(
        "--from", dest="start", help=f"backfill: first session (default {BACKFILL_START})"
    )
    parser.add_argument("--to", dest="end", help="backfill: last session (default: today)")
    parser.add_argument(
        "--seed-archive",
        dest="seed_archive",
        help="backfill: a directory of already-downloaded raw zips (nse/fo-bhavcopy/DATE.zip)",
    )
    parser.add_argument("--date", help="ingest: the session (default: today)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    today = dt.datetime.now(tz=IST).date()
    result: JsonObject
    if args.command == "seed":
        result = asyncio.run(_seed())
    elif args.command == "backfill":
        start = dt.date.fromisoformat(args.start) if args.start else BACKFILL_START
        end = dt.date.fromisoformat(args.end) if args.end else today
        if end < start:
            parser.error("--to is before --from")
        seed_dir = Path(args.seed_archive).expanduser() if args.seed_archive else None
        if seed_dir is not None and not seed_dir.is_dir():
            parser.error(f"--seed-archive {seed_dir} is not a directory")
        result = asyncio.run(_backfill(start, end, seed_dir))
    else:
        day = dt.date.fromisoformat(args.date) if args.date else today
        result = asyncio.run(_ingest(day))
    sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
