"""``fundamentals fill`` — the one-off `equity_fundamentals` fetch NEEDS-MAULIK §15 asks for.

Why a command and not just a pipeline night
-------------------------------------------
T9.1 folded the fetch into step 6 (``refresh_index_snapshots``), which is right for a night that
is already running. It is the wrong shape for the situation §15 actually describes: a table that
has **never** been filled, ~2,500 symbols at NSE's 1 req/s, on a date the pipeline has already
published. Reaching that through a full ten-step run would recompute everything to change one
join, and an interruption forty minutes in would leave nothing behind.

So this command does one thing, for one date, and can be stopped and restarted:

* **Scope is the date's own universe** — instruments with an ``ohlcv_daily`` bar on the target
  date, not every listed instrument. On 2026-08-21 that is 2,545 names rather than 10,481, and
  the 7,936 difference is names the exchange did not trade that day, for which a quote would be
  meaningless anyway.
* **The series comes from the database.** NSE quotes a symbol under a series and answers the
  wrong one with ``200`` and an empty body. ``instrument.series`` already holds it, so the fill
  costs one request per symbol instead of two.
* **Resumable at two levels.** ``--resume`` skips symbols already stored for the date, and the
  archive itself never re-fetches an already-archived key (docs/09, "never re-fetch to
  re-parse"). Re-running after an interruption costs only what is genuinely missing.
* **Committed in batches**, so an interruption keeps the work already done.
* **Every symbol is accounted for.** stored / skipped / no-quote / failed, with the failures
  named, because house rule 3 forbids a swallowed error and a run that silently covers 60% of
  the universe while reporting success is exactly the failure this repository fears most.

Usage:
    python -m baskfy_worker.fundamentals_cli fill                    # latest published date
    python -m baskfy_worker.fundamentals_cli fill --date 2026-08-21 --resume
    python -m baskfy_worker.fundamentals_cli fill --limit 25 --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import FundamentalDaily, Instrument, OhlcvDaily
from baskfy_providers.errors import ProviderError
from baskfy_providers.records import EquityFundamental
from baskfy_worker.db import run_checkpointed
from baskfy_worker.providers import build_pipeline_dependencies
from baskfy_worker.tasks.fundamentals import fundamentals_scope, store_fundamentals

#: Symbols per commit. Small enough that an interruption loses under a minute of fetching at
#: NSE's 1 req/s, large enough that the commit itself is not the cost.
BATCH_SIZE: Final = 25

#: How often to print a progress line, in symbols.
PROGRESS_EVERY: Final = 100


@dataclass(frozen=True, slots=True)
class FillOptions:
    """How one fill run behaves. Grouped so the knobs stay together as they grow."""

    #: Skip symbols already stored for the date, so an interrupted run can be restarted.
    resume: bool = False
    #: Stop after N symbols. For a smoke run, never for a real fill.
    limit: int | None = None
    #: Report the scope and stop, touching neither NSE nor the database.
    dry_run: bool = False
    #: Print progress as the run goes. Off in tests.
    progress: bool = True


@dataclass(slots=True)
class FillReport:
    """Every symbol in scope ends in exactly one of these buckets."""

    date: dt.date
    scoped: int = 0
    already_present: int = 0
    stored: int = 0
    no_quote: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    #: Quoted by NSE under a symbol this database does not carry. Counted, never discarded.
    unmatched: int = 0
    dry_run: bool = False

    @property
    def attempted(self) -> int:
        return self.scoped - self.already_present

    def accounted_for(self) -> bool:
        """The arithmetic that makes a partial run impossible to mistake for a full one.

        A dry run attempts nothing, so it accounts for nothing; saying otherwise would let
        ``--dry-run`` print the same ``ACCOUNTED OK`` line a real fill prints.
        """
        if self.dry_run:
            return False
        return (
            self.already_present
            + self.stored
            + self.unmatched
            + len(self.no_quote)
            + len(self.failed)
            == self.scoped
        )

    def render(self) -> str:
        if self.dry_run:
            return (
                f"DRY RUN date={self.date.isoformat()} scoped={self.scoped} "
                f"would_fetch={self.attempted} already_present={self.already_present}"
            )
        lines = [
            f"FILL date={self.date.isoformat()} scoped={self.scoped} "
            f"attempted={self.attempted} stored={self.stored} "
            f"already_present={self.already_present} no_quote={len(self.no_quote)} "
            f"unmatched={self.unmatched} failed={len(self.failed)}",
            f"ACCOUNTED {'OK' if self.accounted_for() else 'MISMATCH'}",
        ]
        if self.no_quote:
            lines.append("no quote from NSE: " + ", ".join(sorted(self.no_quote)))
        if self.failed:
            lines.append("failed:")
            lines += [f"  {symbol}: {reason}" for symbol, reason in sorted(self.failed)]
        return "\n".join(lines)


async def latest_published_date(session: AsyncSession) -> dt.date | None:
    """The most recent date the plant actually has bars for."""
    return (await session.execute(select(func.max(OhlcvDaily.date)))).scalar_one_or_none()


async def already_stored(session: AsyncSession, on: dt.date) -> set[str]:
    """Symbols that already have a ``fundamental_daily`` row for ``on``."""
    rows = await session.execute(
        select(Instrument.symbol)
        .join(FundamentalDaily, FundamentalDaily.instrument_id == Instrument.id)
        .where(FundamentalDaily.date == on)
    )
    return {str(row[0]).upper() for row in rows.tuples()}


async def fill(
    session: AsyncSession,
    provider: object,
    on: dt.date,
    options: FillOptions | None = None,
    checkpoint: Callable[[], Awaitable[None]] | None = None,
) -> FillReport:
    """Fetch and store one date's fundamentals, committing as it goes.

    ``checkpoint`` is what makes a batch durable — ``session.commit`` in production, so an
    interrupted run keeps the batches it finished. It is injectable because the test suite runs
    each case inside a transaction it owns and rolls back; a hard-coded commit would close that
    transaction out from under the fixture, and the resumability this function exists to provide
    would then be the one thing no test could cover.
    """
    opts = options or FillOptions()
    commit = checkpoint or session.commit
    report = FillReport(date=on, dry_run=opts.dry_run)
    scope = await fundamentals_scope(session, on)
    if opts.limit is not None:
        scope = scope[: opts.limit]
    report.scoped = len(scope)

    done = await already_stored(session, on) if opts.resume else set()
    pending = [(symbol, series) for symbol, series in scope if symbol.upper() not in done]
    report.already_present = report.scoped - len(pending)

    if opts.progress:
        print(
            f"scope={report.scoped} pending={len(pending)} "
            f"already_present={report.already_present} date={on.isoformat()}",
            flush=True,
        )
    if opts.dry_run:
        return report

    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start : start + BATCH_SIZE]
        await _fill_batch(session, provider, on, batch, report)
        await commit()
        if opts.progress and (start + len(batch)) % PROGRESS_EVERY < BATCH_SIZE:
            print(
                f"  {start + len(batch)}/{len(pending)} stored={report.stored} "
                f"no_quote={len(report.no_quote)} failed={len(report.failed)}",
                flush=True,
            )
    return report


async def _fill_batch(
    session: AsyncSession,
    provider: object,
    on: dt.date,
    batch: Sequence[tuple[str, str | None]],
    report: FillReport,
) -> None:
    """One batch, one symbol at a time.

    Per symbol rather than per batch on purpose: a batch-wide call would let one unparseable
    quote take its twenty-four neighbours down with it, and the whole point of the accounting
    is that a failure is attributable to a name.
    """
    fetch = getattr(provider, "equity_fundamentals", None)
    if not callable(fetch):
        raise RuntimeError("the configured provider offers no equity_fundamentals")

    records: list[EquityFundamental] = []
    for symbol, series in batch:
        hint = {symbol: series} if series else None
        try:
            got = list(fetch(on, [symbol], series_by_symbol=hint))
        except ProviderError as exc:
            report.failed.append((symbol, f"{type(exc).__name__}: {exc}"))
            continue
        if not got:
            report.no_quote.append(symbol)
            continue
        records.extend(got)

    if records:
        result = await store_fundamentals(session, on, records)
        report.stored += result.rows_written
        # A quote NSE returned under a symbol this database does not carry. It cannot be stored
        # (there is no instrument_id to hang it on) but it is counted, so the arithmetic in
        # `accounted_for` still balances and the drop is visible rather than silent.
        report.unmatched += result.unmatched


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m baskfy_worker.fundamentals_cli",
        description="One-off equity-fundamentals fill for a single date (NEEDS-MAULIK §15).",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    fill_cmd = subcommands.add_parser("fill", help="fetch NSE fundamentals for one date")
    fill_cmd.add_argument("--date", help="ISO date; defaults to the latest date with bars")
    fill_cmd.add_argument(
        "--resume",
        action="store_true",
        help="skip symbols already stored for the date, so an interrupted run can be restarted",
    )
    fill_cmd.add_argument("--limit", type=int, help="stop after N symbols (for a smoke run)")
    fill_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="print the scope and exit without touching NSE or the database",
    )

    args = parser.parse_args(argv)
    provider = build_pipeline_dependencies().provider

    async def operation(session: AsyncSession) -> FillReport:
        on = dt.date.fromisoformat(args.date) if args.date else await latest_published_date(session)
        if on is None:
            raise LookupError("no bars in ohlcv_daily, so there is no date to fill")
        return await fill(
            session,
            provider,
            on,
            FillOptions(resume=args.resume, limit=args.limit, dry_run=args.dry_run),
        )

    try:
        report = run_checkpointed(operation)
    except LookupError as exc:
        print(str(exc).strip("'"), file=sys.stderr)
        return 1

    print(report.render())
    if report.dry_run:
        return 0
    return 0 if report.accounted_for() and not report.failed else 1


__all__ = ["BATCH_SIZE", "FillOptions", "FillReport", "fill", "fundamentals_scope", "main"]


if __name__ == "__main__":
    sys.exit(main())
