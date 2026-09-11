"""Deep history from Kite — the years the bhavcopy backfill never covered.

    uv run python -m baskfy_worker.deep_backfill                 # DRY RUN
    uv run python -m baskfy_worker.deep_backfill --write         # writes
    uv run python -m baskfy_worker.deep_backfill --write --from 2011-01-01

`ohlcv_daily` starts at 2024-01-01 because that is as far back as the bhavcopy run went. Kite
serves daily candles from **2000-01-03** (measured; nothing earlier exists) with a hard **2,000
day** cap per request, so 2011 to today is three requests per instrument.

WHY THIS IS NOT `baskfy_worker.backfill`
----------------------------------------
That module exists, is resumable, and would be the obvious thing to run. It writes the provider's
`close` into **`close_raw`** — "raw in, raw out" — on the documented assumption that Kite returns
unadjusted OHLC.

**Kite returns adjusted OHLC** (M24, measured). Running it would put adjusted prices into the
column house rule 6 defines as the exchange print, and — now that `corporate_action` holds 289
rows — the next `reprocess_instrument` would adjust them a second time. So this module exists
instead, and says what it is storing.

WHAT IT STORES
--------------
Kite's series is adjusted at source, so it is written as an **adjusted** series and marked
`source = 'kite'`. `close_raw` is NOT NULL in the schema, so it carries the same value: for these
years there is no exchange print on this side, and pretending otherwise would be the lie this
module was written to avoid. `adj_factor` is 1 — the adjustment is already in the price, not
applied on top of it.

`reprocess_instrument` skips `source = 'kite'` rows for exactly that reason (see
`tasks/adjustments.py`): a bar with no exchange print cannot be re-derived from one.

THE SEAM, AND WHY IT IS SPLICED
-------------------------------
The two segments meet at the first date the database already holds. They do not agree:

* ours (2024 onward) is **price-return** adjusted — splits and bonuses applied, dividends not,
  which is the convention M27 measured off the reference corpus;
* Kite's is adjusted for splits, bonuses **and dividends**.

Left alone, the join would show a step of the cumulative dividend yield since 2024 — a few per
cent, in the middle of every backtest window that crosses it. So the Kite segment is multiplied by
the ratio of the two series **on their first shared date**, which makes the join continuous and
keeps the level of the recent, verified data.

That is a splice, and it is what it looks like: within the deep segment the *shape* is Kite's
(total-return), while its *level* is anchored to ours. Returns computed wholly inside the deep
segment therefore still carry Kite's dividend adjustment. Stated here rather than discovered
later; `DECISIONS-MERGE.md` M29 carries the reasoning.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import OhlcvDaily
from baskfy_worker.db import session_scope
from baskfy_worker.providers import build_pipeline_dependencies

#: Measured against Kite on 22 Aug 2026: RELIANCE returns bars from 2000-01-03 and nothing for
#: 1999 or earlier. Requesting before this is not an error, it is simply empty.
KITE_EPOCH: Final = dt.date(2000, 1, 3)

#: Kite's hard per-request cap for the `day` interval. 2,001 days is rejected outright with
#: `InputException: interval exceeds max limit: 2000 days`.
MAX_SPAN_DAYS: Final = 2000

DEFAULT_START: Final = dt.date(2011, 1, 1)

#: Rows are written in batches well under PostgreSQL's 32,767 bind-parameter ceiling (M23.2).
UPSERT_CHUNK: Final = 1500

#: Below this the splice is the identity and there is nothing to report — the two conventions
#: already agree at the join, which is the common case for a name that paid no dividend.
SPLICE_EPSILON: Final = 1e-9


@dataclass
class DeepBackfill:
    instruments: int = 0
    fetched: int = 0
    written: int = 0
    skipped_no_overlap: int = 0
    skipped_no_deep_history: int = 0
    #: Instruments refused because their bars fall in a COMPRESSED chunk. See `compressed_ranges`.
    skipped_compressed: int = 0
    spliced: int = 0
    #: The compressed chunk ranges that caused a refusal, so the operator knows what to decompress.
    compressed_ranges: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


def chunk_windows(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """Spans no longer than Kite's cap, contiguous and gapless."""
    out: list[tuple[dt.date, dt.date]] = []
    cursor = max(start, KITE_EPOCH)
    while cursor <= end:
        stop = min(cursor + dt.timedelta(days=MAX_SPAN_DAYS - 1), end)
        out.append((cursor, stop))
        cursor = stop + dt.timedelta(days=1)
    return out


def splice_factor(
    kite: dict[dt.date, float], ours: dict[dt.date, float]
) -> tuple[float | None, dt.date | None]:
    """`ours / kite` on their first shared date, which is where the two segments join.

    None when they share no date at all — then there is nothing to anchor to, and writing the
    deep segment would put an unaligned series next to a verified one.
    """
    for day in sorted(set(kite) & set(ours)):
        if kite[day] and ours[day]:
            return ours[day] / kite[day], day
    return None, None


async def _existing(
    session: AsyncSession, instrument_id: int
) -> tuple[dict[dt.date, float], dt.date | None]:
    rows = (
        await session.execute(
            text(
                "select date, close from ohlcv_daily "
                "where instrument_id = :i and source <> 'kite' order by date"
            ),
            {"i": instrument_id},
        )
    ).all()
    series = {r[0]: float(r[1]) for r in rows if r[1] is not None}
    return series, (min(series) if series else None)


async def compressed_ranges_covering(session: AsyncSession, days: Sequence[dt.date]) -> list[str]:
    """The compressed chunk ranges that contain any of ``days``. Empty means it is safe to write.

    **THIS IS THE GUARD THE INCIDENT OF 11 Sep 2026 BOUGHT.** `_write` already does the right
    thing — `ON CONFLICT (instrument_id, date) DO NOTHING` — and on an uncompressed chunk that
    makes the whole job idempotent, which is house rule 7.

    It does not work on a compressed chunk. TimescaleDB serves the insert into the chunk's
    uncompressed portion and **the conflict check never sees the compressed row**, so the row is
    written a second time; a later recompression bakes the duplicate in, where no `ON CONFLICT`
    can ever reach it. Three such rows — from an earlier run of this very module, at the seam
    where its data met ours — crashed the nightly pipeline for 2026-09-11 with a Polars
    `aggregation 'item' expected no or a single value, got 2 values`, and had to be removed by
    dropping a chunk constraint, decompressing, deleting and recompressing.

    A guard that is correct and inoperative is worse than no guard, because it is trusted. So this
    module now asks the question `ON CONFLICT` cannot, and **refuses** rather than writing.
    """
    if not days:
        return []
    rows = (
        await session.execute(
            text(
                "select distinct range_start::date::text || '..' || range_end::date::text "
                "from timescaledb_information.chunks "
                "where hypertable_name = 'ohlcv_daily' and is_compressed "
                "and range_start::date <= :hi and range_end::date > :lo"
            ),
            {"lo": min(days), "hi": max(days)},
        )
    ).all()
    return sorted(r[0] for r in rows)


async def duplicate_pairs(session: AsyncSession) -> int:
    """How many ``(instrument_id, date)`` pairs appear more than once. Must always be zero.

    Checked after every write rather than trusted, because the failure this module can cause is
    invisible until a Polars aggregation trips over it hours later, in a different process, on a
    different day.
    """
    return int(
        (
            await session.execute(
                text(
                    "select count(*) from (select instrument_id, date from ohlcv_daily "
                    "group by instrument_id, date having count(*) > 1) t"
                )
            )
        ).scalar_one()
    )


async def _write(
    session: AsyncSession, instrument_id: int, bars: list[tuple[dt.date, dict[str, float]]]
) -> int:
    values = [
        {
            "instrument_id": instrument_id,
            "date": day,
            "open": Decimal(str(round(row["open"], 4))),
            "high": Decimal(str(round(row["high"], 4))),
            "low": Decimal(str(round(row["low"], 4))),
            "close": Decimal(str(round(row["close"], 4))),
            "volume": int(row["volume"]),
            # Same value, and the module docstring says why: there is no exchange print for these
            # years on this side, and inventing one would be the lie this module avoids.
            "close_raw": Decimal(str(round(row["close"], 4))),
            "volume_raw": int(row["volume"]),
            # The adjustment is already inside the price, not applied on top of it.
            "adj_factor": Decimal(1),
            "source": "kite",
        }
        for day, row in bars
    ]
    written = 0
    for offset in range(0, len(values), UPSERT_CHUNK):
        chunk = values[offset : offset + UPSERT_CHUNK]
        statement = insert(OhlcvDaily).values(chunk)
        await session.execute(
            # A date the database already holds from the bhavcopy is left alone: that row has a
            # real exchange print and a verified adjustment, and this segment must not overwrite
            # it. Deep history only fills what is genuinely absent.
            statement.on_conflict_do_nothing(
                index_elements=[OhlcvDaily.instrument_id, OhlcvDaily.date]
            )
        )
        written += len(chunk)
    return written


async def run(  # noqa: PLR0912, PLR0913, PLR0915
    # PLR0913: one parameter per CLI flag, which is the module's contract.
    # PLR0912/PLR0915: this is a pipeline — fetch, drop placeholders, find the seam, refuse a
    # compressed chunk, splice, write — and it reads as one because it IS one. The same
    # suppression and the same reason sit on `baskfy_core.backtest.run_backtest`: splitting a
    # sequence into helpers hides the order, and the order is the thing a reader needs.
    *,
    write: bool,
    start: dt.date = DEFAULT_START,
    end: dt.date | None = None,
    symbols: Sequence[str] | None = None,
    limit: int | None = None,
    database_url: str | None = None,
) -> DeepBackfill:
    report = DeepBackfill()
    finish = end or dt.date.today()
    provider = build_pipeline_dependencies().provider
    fetch = getattr(provider, "daily_bars", None)
    if not callable(fetch):
        raise RuntimeError("no provider offers daily_bars")

    query = (
        "select id, symbol, kite_token from instrument "
        "where kite_token is not null and series in ('EQ','BE') and delisted_on is null"
    )
    params: dict[str, object] = {}
    if symbols:
        query += " and symbol = any(:symbols)"
        params["symbols"] = list(symbols)
    query += " order by symbol"
    if limit:
        query += f" limit {int(limit)}"

    async with session_scope(database_url) as session:
        candidates = [
            (r[0], r[1], r[2]) for r in (await session.execute(text(query), params)).all()
        ]

    for instrument_id, symbol, token in candidates:
        report.instruments += 1
        frames: dict[dt.date, dict[str, float]] = {}
        try:
            for chunk_start, chunk_end in chunk_windows(start, finish):
                frame = fetch(token, chunk_start, chunk_end)
                for row in frame.to_dicts():
                    day = row["date"]
                    day = day.date() if hasattr(day, "date") else day
                    frames[day] = {
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row["volume"] or 0),
                    }
        except Exception as exc:  # one bad symbol must not end a 25-minute run
            report.errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
            continue
        if not frames:
            continue
        report.fetched += len(frames)

        async with session_scope(database_url) as session:
            ours, earliest = await _existing(session, instrument_id)

        # Kite serves placeholder candles at zero for dates before an instrument actually
        # listed -- 107 of them across MAZDOCK and PRIVISCL on the first full run. A zero close
        # is not a cheap price, it is the absence of one, and it poisons every return that
        # touches it: the bar after it is an infinite gain and the bar before it a total loss.
        frames = {d: v for d, v in frames.items() if v["close"] > 0 and v["open"] > 0}

        deep = {d: v for d, v in frames.items() if earliest is None or d < earliest}
        if not deep:
            report.skipped_no_deep_history += 1
            continue

        factor, _joined = splice_factor({d: v["close"] for d, v in frames.items()}, ours)
        if ours and factor is None:
            # Shares no date with the verified segment, so there is nothing to anchor the level
            # to. Refused rather than written unaligned.
            report.skipped_no_overlap += 1
            continue
        scale = factor if factor is not None else 1.0
        if factor is not None and abs(scale - 1.0) > SPLICE_EPSILON:
            report.spliced += 1

        bars = [
            (
                day,
                {
                    "open": row["open"] * scale,
                    "high": row["high"] * scale,
                    "low": row["low"] * scale,
                    "close": row["close"] * scale,
                    "volume": row["volume"],
                },
            )
            for day, row in sorted(deep.items())
        ]
        # THE REFUSAL. Asked once per instrument, against the days actually about to be written.
        # A compressed chunk cannot enforce the primary key, so writing into one is how this
        # module planted the three duplicates that crashed the nightly on 11 Sep 2026.
        async with session_scope(database_url) as session:
            blocked = await compressed_ranges_covering(session, [day for day, _ in bars])
        if blocked:
            report.skipped_compressed += 1
            report.compressed_ranges.update(blocked)
            continue

        if not write:
            # A dry run that reports zero writable bars is not a preview of anything. Count what
            # the writing form would insert, and only skip the insert itself.
            report.written += len(bars)
            continue
        async with session_scope(database_url) as session:
            report.written += await _write(session, instrument_id, bars)

    if write:
        await _record_duplicate_check(report, database_url)
    return report


async def _record_duplicate_check(report: DeepBackfill, database_url: str | None) -> None:
    """THE PROOF, not the promise.

    House rule 7 says re-running a job produces identical rows. The failure this module can
    actually cause is invisible for hours and surfaces in a different process on a different day —
    a Polars aggregation tripping over a second row — so it is measured here rather than assumed.
    A run that created one must say so in the one place somebody is still reading.
    """
    async with session_scope(database_url) as session:
        duplicates = await duplicate_pairs(session)
    if duplicates:
        report.errors.append(
            f"{duplicates} duplicate (instrument_id, date) pair(s) exist after this run. "
            "The primary key did not stop them, which means a write reached a compressed "
            "chunk. Do not run again until they are removed."
        )


def _render(report: DeepBackfill, *, write: bool) -> str:
    lines = [
        "deep history from Kite" + ("" if write else "  [DRY RUN — nothing was written]"),
        "=" * 60,
        f"instruments                 : {report.instruments}",
        f"bars fetched                : {report.fetched:,}",
        f"bars {'written' if write else 'writable':<10}             : {report.written:,}",
        f"level-spliced at the seam   : {report.spliced}",
        f"skipped, nothing deeper     : {report.skipped_no_deep_history}",
        f"skipped, no shared date     : {report.skipped_no_overlap}",
        f"REFUSED, compressed chunk   : {report.skipped_compressed}",
    ]
    if report.compressed_ranges:
        lines += [
            "",
            "  A compressed chunk cannot enforce the primary key, so writing into one creates",
            "  duplicates that no ON CONFLICT can catch and no later delete can reach. Decompress",
            "  these ranges, re-run, then recompress:",
        ]
        lines += [f"    {r}" for r in sorted(report.compressed_ranges)]
    if report.errors:
        lines += ["", f"errors ({len(report.errors)}):"]
        lines += [f"  {e}" for e in report.errors[:15]]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m baskfy_worker.deep_backfill", description=__doc__
    )
    parser.add_argument("--write", action="store_true", help="actually write the bars")
    parser.add_argument("--from", dest="start", default=DEFAULT_START.isoformat())
    parser.add_argument("--to", dest="end", default=None)
    parser.add_argument("--symbols", default="")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    report = asyncio.run(
        run(
            write=args.write,
            start=dt.date.fromisoformat(args.start),
            end=dt.date.fromisoformat(args.end) if args.end else None,
            symbols=[s.strip() for s in args.symbols.split(",") if s.strip()] or None,
            limit=args.limit,
            database_url=args.database_url,
        )
    )
    print(_render(report, write=args.write))
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
