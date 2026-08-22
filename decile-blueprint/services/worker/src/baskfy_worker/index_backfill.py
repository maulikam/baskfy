"""Nine years of index and sector levels, from Kite (M31).

    uv run python -m baskfy_worker.index_backfill                  # DRY RUN
    uv run python -m baskfy_worker.index_backfill --write

`index_snapshot_daily` held **2026-07-08 onward** — six weeks — so every history chart on
`/market-health` and `/dashboard` said *"a line needs two"*. The bars behind them went back to 2017
after M29; the index levels did not.

Kite carries the indices themselves as instruments in its `INDICES` segment — **136 of them on
NSE**, NIFTY 50 and NIFTY BANK through every sector index — and serves daily candles for each with
the same 2,000-day cap as an equity. So the levels are a fetch, not a reconstruction.

WHAT THIS FILLS AND WHAT IT DOES NOT
------------------------------------
It fills `level`, `change_abs` and `change_pct`. It leaves `pe`, `pb` and `div_yield` NULL,
because Kite does not serve them and inventing a P/E is worse than an empty column.

**It does not fill breadth.** `market_health_daily` answers "what percentage of NIFTY 50 is above
its 200-DMA", which needs the *constituents* on that date — `index_member_daily`, which holds seven
dates and which NSE publishes only for today. That is a separate and much harder problem, and
guessing at it would put survivorship bias into a table the backtester reads. See
`NEEDS-MAULIK.md` item 12.

MATCHING KITE'S NAMES TO `index_def`
------------------------------------
The two disagree in punctuation and abbreviation rather than in substance: `NIFTY SMALLCAP 250`
against Kite's `NIFTY SMLCAP 250`, `Nifty Bank` against `NIFTY BANK`. Names are normalised — upper
case, alphanumerics only, and a small table of NSE's own abbreviations — and anything that still
does not match is **reported, not guessed**. A wrong match here would put the wrong index's history
under a name somebody trusts.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import IndexSnapshotDaily
from baskfy_worker.db import session_scope
from baskfy_worker.deep_backfill import KITE_EPOCH, chunk_windows
from baskfy_worker.providers import build_pipeline_dependencies

DEFAULT_START: Final = dt.date(2017, 1, 1)

#: How many unmatched names the report prints before summarising the rest.
REPORTED_UNMATCHED: Final = 25
UPSERT_CHUNK: Final = 1500

#: NSE's own abbreviations, short form on the left as Kite writes them. Expanded in one direction
#: only: applying them to both sides turns a name already in long form into nonsense
#: (`NIFTYINDIACONSUMPTION` -> `NIFTYINDIAINDIACONSUMPTION`), which is how the first run matched
#: 55 of 136 instead of 90.
ABBREVIATIONS: Final[tuple[tuple[str, str], ...]] = (
    # NOT ("PSEBANK", "PSUBANK"): NIFTY PSE is public-sector *enterprises* and NIFTY PSU BANK is
    # public-sector *banks*. They are different indices, and that rule would have filed one's
    # history under the other's name. Caught by the test asserting every abbreviation shortens.
    ("SMLCAP", "SMALLCAP"),
    ("MIDSML", "MIDSMALL"),
    ("INFRA", "INFRASTRUCTURE"),
    ("FINSERVICE", "FINANCIALSERVICES"),
    ("SERVSECTOR", "CONSUMERSERVICES"),
    ("CONSUMPTION", "INDIACONSUMPTION"),
    ("DIVOPPS", "DIVIDENDOPPORTUNITIES"),
    ("PR2XLEV", "PR2XLEVERAGE"),
    ("PR1XINV", "PR1XINVERSE"),
    ("TR2XLEV", "TR2XLEVERAGE"),
    ("TR1XINV", "TR1XINVERSE"),
    ("CONSRDURBL", "CONSUMERDURABLES"),
    ("OILGAS", "OILANDGAS"),
    ("HEALTHCARE", "HEALTHCAREINDEX"),
)


def normalise(name: str) -> str:
    """Upper case, alphanumerics only. No interpretation — only punctuation and case."""
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def keys(name: str) -> set[str]:
    """Every form this name might be written in, so a match can be looked up rather than guessed.

    Both the literal normalisation and the abbreviation-expanded one, because either side may be
    the short form: `Nifty Bank` and `NIFTY BANK` normalise identically, while `NIFTY SMLCAP 250`
    only meets `NIFTY SMALLCAP 250` after expansion. Returning a *set* and intersecting is what
    lets the expansion run in one direction without deciding in advance which side is short.
    """
    flat = normalise(name)
    out = {flat}
    expanded = flat
    for short, long in ABBREVIATIONS:
        expanded = expanded.replace(short, long)
    out.add(expanded)
    return out


@dataclass
class IndexBackfill:
    kite_indices: int = 0
    matched: int = 0
    unmatched_ours: list[str] = field(default_factory=list)
    unmatched_kite: list[str] = field(default_factory=list)
    written: int = 0
    errors: list[str] = field(default_factory=list)


def _as_float(value: object) -> float:
    """A candle field as a number. `object` rather than `Any`, per house rule 3.

    A suppression here would be hiding the one thing worth checking: that the vendor sent a
    number at all.
    """
    if isinstance(value, (int, float, Decimal, str)):
        return float(value)
    raise TypeError(f"expected a number from the candle, got {type(value).__name__}")


def _rows(index_id: int, candles: list[dict[str, object]]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    previous: float | None = None
    for candle in candles:
        day = candle["date"]
        day = day.date() if hasattr(day, "date") else day
        close = _as_float(candle["close"])
        if close <= 0:
            previous = None
            continue
        change_abs = None if previous is None else close - previous
        change_pct = None if previous in (None, 0) else (close / previous - 1) * 100
        out.append(
            {
                "index_id": index_id,
                "date": day,
                "level": Decimal(str(round(close, 4))),
                "change_abs": None if change_abs is None else Decimal(str(round(change_abs, 4))),
                "change_pct": None if change_pct is None else Decimal(str(round(change_pct, 4))),
            }
        )
        previous = close
    return out


async def _write(session: AsyncSession, rows: list[dict[str, object]]) -> int:
    written = 0
    for offset in range(0, len(rows), UPSERT_CHUNK):
        chunk = rows[offset : offset + UPSERT_CHUNK]
        statement = insert(IndexSnapshotDaily).values(chunk)
        await session.execute(
            # A date the nightly chain already wrote keeps its row: that one may carry `pe`, `pb`
            # and `div_yield` from NSE, which this module cannot supply and must not erase.
            statement.on_conflict_do_nothing(
                index_elements=[IndexSnapshotDaily.index_id, IndexSnapshotDaily.date]
            )
        )
        written += len(chunk)
    return written


async def run(
    *,
    write: bool,
    start: dt.date = DEFAULT_START,
    end: dt.date | None = None,
    database_url: str | None = None,
) -> IndexBackfill:
    report = IndexBackfill()
    finish = end or dt.date.today()
    provider = build_pipeline_dependencies().provider
    fetch = getattr(provider, "daily_bars", None)
    if not callable(fetch):
        raise RuntimeError("no provider offers daily_bars")

    kite_indices = _kite_indices()
    report.kite_indices = len(kite_indices)
    by_name: dict[str, int] = {}
    for name, token in kite_indices:
        for key in keys(name):
            by_name.setdefault(key, token)

    async with session_scope(database_url) as session:
        ours = (
            await session.execute(text("select id, slug, name from index_def order by id"))
        ).all()

    matched_names: set[str] = set()
    for index_id, slug, name in ours:
        found: int | None = next(
            (by_name[k] for k in (*keys(name), *keys(slug)) if k in by_name), None
        )
        if found is None:
            report.unmatched_ours.append(f"{slug} ({name})")
            continue
        token = found
        report.matched += 1
        matched_names.update(keys(name))
        try:
            candles: list[dict[str, object]] = []
            for chunk_start, chunk_end in chunk_windows(start, finish):
                frame = fetch(token, chunk_start, chunk_end)
                candles.extend(frame.to_dicts())
        except Exception as exc:  # one bad index must not end the run
            report.errors.append(f"{slug}: {type(exc).__name__}: {exc}")
            continue
        rows = _rows(index_id, candles)
        if not rows:
            continue
        if not write:
            report.written += len(rows)
            continue
        async with session_scope(database_url) as session:
            report.written += await _write(session, rows)

    report.unmatched_kite = sorted(
        name for name, _ in kite_indices if not (keys(name) & matched_names)
    )
    return report


def _kite_indices() -> list[tuple[str, int]]:
    """`(tradingsymbol, instrument_token)` for every NSE index Kite carries.

    Read straight from `kiteconnect` rather than through the provider port: the port's
    `list_instruments` is the *equity* master the pipeline ingests, and an index is not an
    instrument the screener holds. This is the one place that needs the raw dump.
    """
    from kiteconnect import KiteConnect  # noqa: PLC0415

    from baskfy_providers.settings import get_provider_settings  # noqa: PLC0415
    from baskfy_providers.tokens import AccessTokenStore  # noqa: PLC0415

    settings = get_provider_settings()
    client = KiteConnect(api_key=settings.kite_api_key)
    client.set_access_token(
        AccessTokenStore(settings.kite_token_path, settings.kite_token_encryption_key).load().value
    )
    return [
        (row["tradingsymbol"], int(row["instrument_token"]))
        for row in client.instruments("NSE")
        if row.get("segment") == "INDICES"
    ]


def _render(report: IndexBackfill, *, write: bool) -> str:
    lines = [
        "index and sector history from Kite" + ("" if write else "  [DRY RUN — nothing written]"),
        "=" * 62,
        f"indices Kite carries        : {report.kite_indices}",
        f"matched to index_def        : {report.matched}",
        f"rows {'written' if write else 'writable':<10}            : {report.written:,}",
        f"ours with no Kite match     : {len(report.unmatched_ours)}",
        f"Kite's with no index_def    : {len(report.unmatched_kite)}",
    ]
    if report.errors:
        lines += ["", f"errors ({len(report.errors)}):", *[f"  {e}" for e in report.errors[:10]]]
    if report.unmatched_ours:
        lines += ["", "ours with no Kite match (no history available for these):"]
        lines += [f"  {n}" for n in report.unmatched_ours[:REPORTED_UNMATCHED]]
        if len(report.unmatched_ours) > REPORTED_UNMATCHED:
            lines.append(f"  ... and {len(report.unmatched_ours) - REPORTED_UNMATCHED} more")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m baskfy_worker.index_backfill", description=__doc__
    )
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--from", dest="start", default=DEFAULT_START.isoformat())
    parser.add_argument("--to", dest="end", default=None)
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    start = dt.date.fromisoformat(args.start)
    report = asyncio.run(
        run(
            write=args.write,
            start=max(start, KITE_EPOCH),
            end=dt.date.fromisoformat(args.end) if args.end else None,
            database_url=args.database_url,
        )
    )
    print(_render(report, write=args.write))
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
