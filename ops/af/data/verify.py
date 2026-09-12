"""Post-repair assertions for the AF data-plant wave. Read-only.

    uv run python ops/af/data/verify.py
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models.market import BHAVCOPY_SEAM
from baskfy_worker.db import session_scope


async def verify(session: AsyncSession) -> dict[str, object]:
    deep_kite = (
        await session.execute(
            text(
                """
                select count(*) from ohlcv_daily
                 where source = 'kite' and date < :seam
                """
            ),
            {"seam": BHAVCOPY_SEAM},
        )
    ).scalar_one()
    adjusted = (
        await session.execute(
            text("select count(*) from ohlcv_daily where source = 'kite_adjusted'")
        )
    ).scalar_one()
    return {
        "seam": BHAVCOPY_SEAM.isoformat(),
        "deep_rows_still_labelled_kite": deep_kite,
        "kite_adjusted_rows": adjusted,
        "ok": deep_kite == 0,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    async def _run() -> dict[str, object]:
        async with session_scope(args.database_url) as session:
            return await verify(session)

    report = asyncio.run(_run())
    print(report)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
