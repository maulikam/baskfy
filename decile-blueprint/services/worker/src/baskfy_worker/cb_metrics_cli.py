"""One-shot: upsert ``cb_metrics`` for every non-archived basket (Tree 7 / SC2).

Beat runs this nightly as ``baskfy.cb.compute_metrics``. Until Beat has run against a live
database, the catalog shows ``metrics: null``. This CLI is the same body without Celery.

    uv run python -m baskfy_worker.cb_metrics_cli
    uv run python -m baskfy_worker.cb_metrics_cli --date 2026-08-21
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from baskfy_api.curated_metrics_service import compute_all_metrics
from baskfy_api.settings import get_settings
from baskfy_worker.celery_app import IST


async def _run(day: dt.date) -> dict[str, object]:
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            return await compute_all_metrics(session, day)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upsert cb_metrics for every curated basket")
    parser.add_argument(
        "--date",
        help="As-of trading day (YYYY-MM-DD). Default: today IST.",
    )
    args = parser.parse_args(argv)
    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(tz=IST).date()
    result = asyncio.run(_run(day))
    print(json.dumps(result, indent=2, default=str))
    return 0 if int(result.get("baskets", 0)) >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
