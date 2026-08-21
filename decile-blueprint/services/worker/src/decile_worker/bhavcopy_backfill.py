"""Bar backfill from the NSE bhavcopy — a **deviation from docs/09**, argued in DECISIONS §21.1.

    python -m decile_worker.bhavcopy_backfill --from 2024-01-01 --to 2026-08-18

WHY THIS EXISTS, AND WHY IT IS NOT WHAT docs/09 ASKS FOR
========================================================
docs/09 §"Provider ports" assigns ``daily_bars`` to ``KiteProvider`` and lists ``bhavcopy`` under
``ReferenceProvider`` — "incl. circuit bands, series". docs/09 §Backfill then describes the bar
backfill as "~2,300 instruments x 15 years", chunked per instrument, resumed from
``ingest_cursor``. ``decile_worker.backfill`` implements exactly that and is unchanged.

This module reads the *same* bars out of the bhavcopy instead, one file per trading day. It exists
because ``decile_worker.backfill`` cannot run without Kite credentials, and because the bhavcopy
turns out to be the better source for this engine. Both halves of that are worth stating plainly.

**What it buys.** The bhavcopy carries ``turnover`` and the two circuit bands natively. Kite
carries neither. docs/05 §13 wants exchange turnover for ``vol_day_val`` and says to "only fall
back to close x volume when turnover is unavailable"; via Kite that fallback is *always* taken.
docs/05 §12's circuit detection is marked INFERRED for the same reason — with no published band it
has to be guessed from the tick. Ingesting the bhavcopy makes both of them observed facts.

**What it costs.** The bhavcopy archive only serves the UDiFF layout, which begins in the first
days of 2024 — 2023-07-03 returns 404, 2024-01-02 returns 2,658 rows. So this cannot produce
docs/09's fifteen years, and the fifteen-year backtest stays unrunnable on it. It is the right
source for the window the product actually serves (docs/01 §2.13 puts ``DATA_START_DATE`` at
2024-11-01) and the wrong source for deep history. When Kite credentials exist, run *both*:
``decile_worker.backfill`` for depth, this for the recent window's turnover and bands.

Resumability
------------
Per-day transaction, every write an upsert — the model ``decile_worker.reference_backfill``
already uses, and for the same reason: a day of bars is seconds of work, so re-running a range is
cheaper than a cursor table. It is cheaper still than it looks, because docs/09 §"NSE specifics"
requires every file to be archived before it is parsed ("Never re-fetch to re-parse: the archive
is the reproducibility record"), so a resumed run reads the archive from disk rather than the
network.

What this module does NOT do
----------------------------
It writes raw bars only. ``close`` is left equal to ``close_raw`` and ``adj_factor`` at 1, exactly
as ``decile_worker.tasks.bars.upsert_bars`` does, because deriving the adjusted series is step 4's
job (docs/09: "providers never adjust"). Adjustment is a separate pass over
``decile_worker.tasks.adjustments`` and this module does not run it — until it has, ``close`` is
the exchange print and every path-dependent factor is measured across any split in the window.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import polars as pl
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from decile_core.models import Instrument, OhlcvDaily, TradingDay
from decile_core.seed_data import NSE_EXCHANGE_ID
from decile_worker.db import session_scope
from decile_worker.providers import build_pipeline_dependencies
from decile_worker.window import DateWindow

#: docs/04's ``ohlcv_daily.source`` CHECK admits 'kite' and 'nse'. These rows are the latter.
BAR_SOURCE = "nse"

#: The equity series the listings register actually holds (EQ, BE, BZ as at 2026-08-18). The
#: bhavcopy also carries SM/ST (SME), GS/GB (government securities) and N0 (debt); those are not
#: equity and no ``instrument`` row matches them, so they would be dropped by the join anyway.
#: Naming them here makes the intent explicit rather than incidental.
EQUITY_SERIES: tuple[str, ...] = ("EQ", "BE", "BZ")

#: Rows per INSERT. Same value ``decile_worker.tasks.bars`` uses.
UPSERT_CHUNK = 1000


@dataclass(slots=True)
class BhavcopyBackfillReport:
    trading_days: int = 0
    days_written: int = 0
    bars_written: int = 0
    unmatched_symbols: set[str] = field(default_factory=set)
    missing_days: list[dt.date] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return not self.failures


async def trading_days_in(session: AsyncSession, window: DateWindow) -> list[dt.date]:
    rows = await session.execute(
        select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date >= window.start,
            TradingDay.date <= window.end,
        )
        .order_by(TradingDay.date)
    )
    return [row[0] for row in rows]


async def instrument_ids_by_key(session: AsyncSession) -> dict[tuple[str, str], int]:
    """``(symbol, series) -> instrument.id``.

    Keyed on both because ``instrument`` is unique on ``(exchange_id, symbol, series)`` and the
    bhavcopy publishes the same symbol in more than one series. Matching on symbol alone would
    attach a BE row's bars to the EQ listing.
    """
    rows = await session.execute(
        select(Instrument.symbol, Instrument.series, Instrument.id).where(
            Instrument.exchange_id == NSE_EXCHANGE_ID
        )
    )
    return {(symbol, series): ident for symbol, series, ident in rows}


def _rows_for_day(
    frame: pl.DataFrame,
    ids: dict[tuple[str, str], int],
    series: Sequence[str],
) -> tuple[list[dict[str, object]], set[str]]:
    """Bhavcopy frame -> ``ohlcv_daily`` values, plus the symbols nothing matched."""
    equity = frame.filter(pl.col("series").is_in(list(series)))
    values: list[dict[str, object]] = []
    unmatched: set[str] = set()
    for row in equity.iter_rows(named=True):
        instrument_id = ids.get((row["symbol"], row["series"]))
        if instrument_id is None:
            unmatched.add(f"{row['symbol']}:{row['series']}")
            continue
        values.append(
            {
                "instrument_id": instrument_id,
                "date": row["date"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                # `close` starts equal to `close_raw`; apply_adjustments rewrites it (docs/09).
                "close": row["close"],
                "close_raw": row["close"],
                "volume": row["volume"],
                "volume_raw": row["volume"],
                "turnover": row["turnover"],
                "upper_circuit": row["upper_circuit"],
                "lower_circuit": row["lower_circuit"],
                "adj_factor": Decimal(1),
                "source": BAR_SOURCE,
            }
        )
    return values, unmatched


async def upsert_day(session: AsyncSession, values: list[dict[str, object]]) -> int:
    """Upsert one day's bars for every instrument at once.

    ``close``, ``volume`` and ``adj_factor`` are deliberately not overwritten on conflict, for the
    reason ``decile_worker.tasks.bars.upsert_bars`` gives: they are step 4's outputs, and
    re-ingesting a raw file must not silently un-adjust a series that has already been adjusted.
    """
    written = 0
    for offset in range(0, len(values), UPSERT_CHUNK):
        chunk = values[offset : offset + UPSERT_CHUNK]
        stmt = insert(OhlcvDaily).values(chunk)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[OhlcvDaily.instrument_id, OhlcvDaily.date],
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close_raw": stmt.excluded.close_raw,
                    "volume_raw": stmt.excluded.volume_raw,
                    "turnover": stmt.excluded.turnover,
                    "upper_circuit": stmt.excluded.upper_circuit,
                    "lower_circuit": stmt.excluded.lower_circuit,
                    "source": stmt.excluded.source,
                },
            )
        )
        written += len(chunk)
    return written


async def backfill_bars_from_bhavcopy(
    provider: object,
    window: DateWindow,
    *,
    database_url: str | None = None,
    series: Sequence[str] = EQUITY_SERIES,
    progress_every: int = 25,
) -> BhavcopyBackfillReport:
    report = BhavcopyBackfillReport()

    async with session_scope(database_url) as session:
        days = await trading_days_in(session, window)
        ids = await instrument_ids_by_key(session)
    report.trading_days = len(days)
    if not ids:
        report.failures["setup"] = (
            "no instrument rows: run `make refdata` (or reference_backfill) first so the "
            "listings register exists to join bars against"
        )
        return report

    fetch = getattr(provider, "bhavcopy", None)
    if fetch is None:
        report.failures["setup"] = "no provider offers bhavcopy"
        return report

    for index, day in enumerate(days, start=1):
        try:
            frame = fetch(day)
        except Exception as exc:
            # A 404 here is the ordinary shape of "this predates the UDiFF archive" and of a day
            # the calendar believes is a session but NSE published no file for. Recorded as
            # missing rather than failed: docs/09 gives the quality gate, not the parser, the job
            # of deciding what is publishable.
            report.missing_days.append(day)
            if "404" not in str(exc):
                report.failures[day.isoformat()] = f"{type(exc).__name__}: {exc}"
            continue

        try:
            values, unmatched = _rows_for_day(frame, ids, series)
            report.unmatched_symbols |= unmatched
            if values:
                async with session_scope(database_url) as session:
                    report.bars_written += await upsert_day(session, values)
                report.days_written += 1
        except Exception as exc:
            report.failures[day.isoformat()] = f"{type(exc).__name__}: {exc}"

        if progress_every and index % progress_every == 0:
            print(
                f"  {index}/{len(days)} days · {report.bars_written} bars · "
                f"{len(report.missing_days)} missing",
                flush=True,
            )

    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m decile_worker.bhavcopy_backfill",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--from", dest="start", required=True, help="ISO start date")
    parser.add_argument("--to", dest="end", default=None, help="ISO end date (default: today)")
    parser.add_argument(
        "--series",
        default=",".join(EQUITY_SERIES),
        help=f"comma-separated bhavcopy series to ingest (default: {','.join(EQUITY_SERIES)})",
    )
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    window = DateWindow(
        dt.date.fromisoformat(args.start),
        dt.date.fromisoformat(args.end) if args.end else dt.date.today(),
    )
    series = tuple(s.strip() for s in args.series.split(",") if s.strip())

    provider = build_pipeline_dependencies().provider
    report = asyncio.run(
        backfill_bars_from_bhavcopy(provider, window, database_url=args.database_url, series=series)
    )
    print(
        f"{report.trading_days} trading day(s) in {window}; "
        f"{report.days_written} written; {report.bars_written} bars; "
        f"{len(report.missing_days)} day(s) with no bhavcopy; "
        f"{len(report.unmatched_symbols)} unmatched symbol(s)"
    )
    if report.missing_days:
        first, last = report.missing_days[0], report.missing_days[-1]
        print(f"  no bhavcopy published between {first} and {last}")
    for day, error in list(report.failures.items())[:20]:
        print(f"  {day}: {error}")
    return 0 if report.succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
