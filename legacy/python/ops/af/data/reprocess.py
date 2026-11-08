"""Per-instrument ``reprocess_instrument`` with a jump check. Dry-run by default.

    uv run python ops/af/data/reprocess.py --instrument-id 123
    uv run python ops/af/data/reprocess.py --apply --backup-id <id> --instrument-id 123
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import CorporateAction, OhlcvDaily
from baskfy_worker.db import session_scope
from baskfy_worker.tasks.adjustments import reprocess_instrument


async def candidate_ids(session: AsyncSession, instrument_id: int | None) -> list[int]:
    if instrument_id is not None:
        return [instrument_id]
    rows = await session.execute(
        select(CorporateAction.instrument_id).distinct().order_by(CorporateAction.instrument_id)
    )
    return [row[0] for row in rows]


async def jump_ok(session: AsyncSession, instrument_id: int) -> bool:
    """Cheap continuity check: no adjacent close ratio outside 0.4–2.5 without a factor move."""
    rows = (
        await session.execute(
            select(OhlcvDaily.date, OhlcvDaily.close, OhlcvDaily.adj_factor)
            .where(OhlcvDaily.instrument_id == instrument_id)
            .order_by(OhlcvDaily.date)
        )
    ).all()
    if len(rows) < 2:
        return True
    for prev, cur in zip(rows, rows[1:], strict=False):
        prev_close, cur_close = prev[1], cur[1]
        if prev_close is None or cur_close is None or prev_close == 0:
            continue
        ratio = float(cur_close / prev_close)
        if 0.4 <= ratio <= 2.5:
            continue
        if prev[2] != cur[2]:
            continue
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--instrument-id", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-id", default="")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    if args.apply and not args.backup_id:
        print("--apply requires --backup-id", file=sys.stderr)
        return 2

    async def _run() -> dict[str, object]:
        async with session_scope(args.database_url) as session:
            ids = (await candidate_ids(session, args.instrument_id))[: args.limit]
            results: list[dict[str, object]] = []
            for instrument_id in ids:
                if args.apply:
                    outcome = await reprocess_instrument(session, instrument_id)
                    ok = await jump_ok(session, instrument_id)
                    results.append(
                        {
                            "instrument_id": instrument_id,
                            "bars_rewritten": outcome.bars_rewritten,
                            "deep_rescaled": outcome.deep_rescaled,
                            "jump_ok": ok,
                        }
                    )
                else:
                    results.append({"instrument_id": instrument_id, "would_reprocess": True})
            return {"dry_run": not args.apply, "results": results}

    print(asyncio.run(_run()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
