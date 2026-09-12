"""Read-only counts for the AF data-plant repair (wave 2 runs this on the box).

    uv run python ops/af/data/measure.py

Never writes. Never prints secrets. Safe against the throwaway test DB and the box alike.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models.market import BHAVCOPY_SEAM
from baskfy_worker.db import session_scope

QUERIES: tuple[tuple[str, str], ...] = (
    (
        "source_by_era",
        """
        select source,
               count(*) filter (where date < :seam) as before_seam,
               count(*) filter (where date >= :seam) as on_or_after_seam,
               count(*) as total
          from ohlcv_daily
      group by source
      order by source
        """,
    ),
    (
        "nightly_kite_instruments",
        """
        select count(distinct instrument_id)
          from ohlcv_daily
         where source = 'kite'
           and date >= :seam
        """,
    ),
    (
        "multi_leg_raw_purposes",
        """
        select count(*)
          from corporate_action
         where raw::text ~* 'SPLIT'
           and raw::text ~* 'BONUS'
        """,
    ),
)


async def measure(session: AsyncSession) -> dict[str, object]:
    out: dict[str, object] = {"seam": BHAVCOPY_SEAM.isoformat()}
    for name, sql in QUERIES:
        result = await session.execute(text(sql), {"seam": BHAVCOPY_SEAM})
        if name == "source_by_era":
            out[name] = [
                {
                    "source": row[0],
                    "before_seam": row[1],
                    "on_or_after_seam": row[2],
                    "total": row[3],
                }
                for row in result.all()
            ]
        else:
            out[name] = result.scalar_one()
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    async def _run() -> dict[str, object]:
        async with session_scope(args.database_url) as session:
            return await measure(session)

    report = asyncio.run(_run())
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
