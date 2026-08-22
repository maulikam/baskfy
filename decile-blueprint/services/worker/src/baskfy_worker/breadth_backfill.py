"""Breadth history, so the Market Health charts have a line to draw (M32).

    uv run python -m baskfy_worker.breadth_backfill --from 2025-08-01          # DRY RUN
    uv run python -m baskfy_worker.breadth_backfill --from 2025-08-01 --write
    uv run python -m baskfy_worker.breadth_backfill --from 2017-01-01 --every 5 --write

`/market-health` said *"Only 1 day of history so far — a line needs two"* on every chart. Three
tables have to line up for a breadth point to exist, and only one of them did:

| | held | needs |
|---|---|---|
| `ohlcv_daily` | 2017 → today (M29) | ✅ |
| `factor_daily` | **one date** | one row per instrument per date |
| `index_member_daily` | **seven dates** | constituents per date |

WHY THIS IS SAMPLED AND NOT DAILY
---------------------------------
`compute_factors` costs about **45 seconds per as-of date** — and it is the *computation*, not the
database: loading 1.2M bars takes 6 seconds and computing 64 factors over them takes 45. So a daily
backfill of nine years is roughly **30 hours**, which is an overnight job and not a page fix.

A chart needs points, not every day. `--every 5` samples one trading day a week, which is 52 points
a year and draws the same line for a fifth of the cost. `--every 1` is the full daily job when
somebody has the machine time.

THE MEMBERSHIP PROBLEM, AND WHY THESE ROWS SAY `derived`
--------------------------------------------------------
Breadth asks "what percentage of NIFTY 50 was above its 200-DMA on this date", which needs the
constituents **on that date**. NSE publishes today's. Kite has no constituents endpoint at all —
its API surface is `instruments`, `quote`, `ohlc`, `ltp`, `historical_data`, `holdings`, `orders`,
`gtt`, `mf*`, and nothing else.

So the most recent known membership is carried backwards, and every such row is written with
**`source = 'derived'`** — the value `docs/09` §Backfill reserved for exactly this ("pre-2018 index
membership is reconstructed, and marked as such ... so backtests can exclude uncertain periods").

**This is survivorship bias and it is not hidden.** A company dropped from NIFTY 50 in 2019 was
usually dropped after falling, and computing 2019's breadth over today's fifty leaves it out —
which makes the past look healthier than it was. The rows say so, `market_health_daily` inherits
it, and `NEEDS-MAULIK.md` item 12 carries the decision. Reconstructing real membership needs NSE's
index-change announcements, which nothing here has.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import IndexMemberDaily
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_worker.db import session_scope
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.factors import run_compute_factors
from baskfy_worker.tasks.market_health import run_compute_market_health

#: The marker `docs/09` reserves for membership that was carried rather than published.
DERIVED: Final = "derived"

#: One trading day in five — a weekly sample. See the module docstring for why not daily.
DEFAULT_EVERY: Final = 5

UPSERT_CHUNK: Final = 2000


@dataclass
class BreadthBackfill:
    dates: int = 0
    factors_computed: int = 0
    membership_rows: int = 0
    breadth_rows: int = 0
    skipped_have_factors: int = 0
    errors: list[str] = field(default_factory=list)


async def target_dates(
    session: AsyncSession, start: dt.date, end: dt.date, every: int
) -> list[dt.date]:
    """Every ``every``-th trading day in the window, oldest first.

    Trading days rather than calendar days, so a weekly sample never lands on a Sunday and
    produces a point with no bars behind it.
    """
    rows = (
        (
            await session.execute(
                text(
                    "select date from trading_day where exchange_id = :ex and is_trading_day "
                    "and date between :a and :b order by date"
                ),
                {"ex": NSE_EXCHANGE_ID, "a": start, "b": end},
            )
        )
        .scalars()
        .all()
    )
    return [day for index, day in enumerate(rows) if index % every == 0]


async def carry_membership(session: AsyncSession, on: dt.date) -> int:
    """Write the most recent known membership onto ``on``, marked ``derived``.

    The source date is the latest *published* membership at or before ``on``, falling back to the
    earliest published one when ``on`` predates every publication — which is the usual case here,
    since the published rows are all from last week.
    """
    source = (
        await session.execute(
            text(
                "select date from index_member_daily where source <> :derived "
                "order by abs(date - :on) limit 1"
            ),
            {"derived": DERIVED, "on": on},
        )
    ).scalar_one_or_none()
    if source is None or source == on:
        return 0

    rows = (
        await session.execute(
            text(
                "select index_id, instrument_id from index_member_daily "
                "where date = :src and source <> :derived"
            ),
            {"src": source, "derived": DERIVED},
        )
    ).all()
    if not rows:
        return 0

    values = [
        {"index_id": r[0], "date": on, "instrument_id": r[1], "source": DERIVED} for r in rows
    ]
    written = 0
    for offset in range(0, len(values), UPSERT_CHUNK):
        chunk = values[offset : offset + UPSERT_CHUNK]
        statement = insert(IndexMemberDaily).values(chunk)
        await session.execute(
            # A published row for this date wins. Only genuinely absent dates are filled.
            statement.on_conflict_do_nothing(
                index_elements=[
                    IndexMemberDaily.index_id,
                    IndexMemberDaily.date,
                    IndexMemberDaily.instrument_id,
                ]
            )
        )
        written += len(chunk)
    return written


async def _has_factors(session: AsyncSession, on: dt.date) -> bool:
    found = (
        await session.execute(
            text("select 1 from factor_daily where date = :on limit 1"), {"on": on}
        )
    ).first()
    return found is not None


async def run(
    *,
    write: bool,
    start: dt.date,
    end: dt.date | None = None,
    every: int = DEFAULT_EVERY,
    database_url: str | None = None,
) -> BreadthBackfill:
    report = BreadthBackfill()
    finish = end or dt.date.today()

    async with session_scope(database_url) as session:
        days = await target_dates(session, start, finish, every)
    report.dates = len(days)
    if not write:
        return report

    for day in days:
        try:
            async with session_scope(database_url) as session:
                if await _has_factors(session, day):
                    report.skipped_have_factors += 1
                else:
                    report.factors_computed += await run_compute_factors(
                        session, StepOutcome(), day
                    )
                report.membership_rows += await carry_membership(session, day)
                report.breadth_rows += await run_compute_market_health(session, StepOutcome(), day)
        except Exception as exc:  # one bad date must not end a long run
            report.errors.append(f"{day}: {type(exc).__name__}: {exc}")
    return report


def _render(report: BreadthBackfill, *, write: bool) -> str:
    lines = [
        "breadth history" + ("" if write else "  [DRY RUN — nothing written]"),
        "=" * 58,
        f"target dates                : {report.dates}",
    ]
    if write:
        lines += [
            f"factor rows computed        : {report.factors_computed:,}",
            f"dates that already had them : {report.skipped_have_factors}",
            f"membership rows carried     : {report.membership_rows:,}  (marked 'derived')",
            f"breadth rows written        : {report.breadth_rows:,}",
        ]
    else:
        lines.append(
            f"estimated cost              : ~{report.dates * 45 / 60:.0f} minutes"
            " (about 45s of factor computation per date)"
        )
    if report.errors:
        lines += ["", f"errors ({len(report.errors)}):", *[f"  {e}" for e in report.errors[:10]]]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m baskfy_worker.breadth_backfill", description=__doc__
    )
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", default=None)
    parser.add_argument(
        "--every",
        type=int,
        default=DEFAULT_EVERY,
        help="sample one trading day in N; 1 is daily and costs ~45s per day",
    )
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    report = asyncio.run(
        run(
            write=args.write,
            start=dt.date.fromisoformat(args.start),
            end=dt.date.fromisoformat(args.end) if args.end else None,
            every=max(1, args.every),
            database_url=args.database_url,
        )
    )
    print(_render(report, write=args.write))
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
