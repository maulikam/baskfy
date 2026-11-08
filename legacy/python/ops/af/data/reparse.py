"""Re-parse multi-leg corporate-action purposes (AF 3.2 / 3.3). Dry-run by default.

    uv run python ops/af/data/reparse.py
    uv run python ops/af/data/reparse.py --apply --backup-id <id>

``--apply`` refuses without ``--backup-id`` so a box run is never silent.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import CorporateAction
from baskfy_providers.nse import parse_corporate_action_purposes
from baskfy_worker.db import session_scope


async def plan(session: AsyncSession) -> list[dict[str, object]]:
    rows = (await session.execute(select(CorporateAction))).scalars().all()
    changes: list[dict[str, object]] = []
    for row in rows:
        purpose = ""
        if isinstance(row.raw, dict):
            purpose = str(row.raw.get("subject") or row.raw.get("purpose") or "")
        legs = parse_corporate_action_purposes(purpose)
        if len(legs) <= 1:
            continue
        changes.append(
            {
                "instrument_id": row.instrument_id,
                "ex_date": row.ex_date.isoformat(),
                "existing_type": row.action_type,
                "legs": [
                    {
                        "action_type": leg[0],
                        "ratio_from": str(leg[1]) if leg[1] is not None else None,
                        "ratio_to": str(leg[2]) if leg[2] is not None else None,
                        "amount": str(leg[3]) if leg[3] is not None else None,
                    }
                    for leg in legs
                ],
            }
        )
    return changes


async def apply(session: AsyncSession, changes: list[dict[str, object]]) -> int:
    written = 0
    for change in changes:
        instrument_id = int(change["instrument_id"])
        ex_text = change["ex_date"]
        assert isinstance(ex_text, str)
        ex_date = dt.date.fromisoformat(ex_text)
        existing = (
            await session.execute(
                select(CorporateAction).where(
                    CorporateAction.instrument_id == instrument_id,
                    CorporateAction.ex_date == ex_date,
                )
            )
        ).scalars().all()
        if not existing:
            continue
        raw = existing[0].raw
        for row in existing:
            await session.delete(row)
        for leg in change["legs"]:
            assert isinstance(leg, dict)
            session.add(
                CorporateAction(
                    instrument_id=instrument_id,
                    action_type=str(leg["action_type"]),
                    ex_date=ex_date,
                    ratio_from=Decimal(str(leg["ratio_from"])) if leg["ratio_from"] else None,
                    ratio_to=Decimal(str(leg["ratio_to"])) if leg["ratio_to"] else None,
                    amount=Decimal(str(leg["amount"])) if leg["amount"] else None,
                    raw=raw if isinstance(raw, dict) else {},
                )
            )
            written += 1
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-id", default="")
    args = parser.parse_args(argv)
    if args.apply and not args.backup_id:
        print("--apply requires --backup-id", file=sys.stderr)
        return 2

    async def _run() -> tuple[list[dict[str, object]], int]:
        async with session_scope(args.database_url) as session:
            changes = await plan(session)
            written = 0
            if args.apply:
                written = await apply(session, changes)
            return changes, written

    changes, written = asyncio.run(_run())
    print({"dry_run": not args.apply, "candidates": len(changes), "written": written, "sample": changes[:5]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
