"""M28 — write the recovered corporate actions, then rebuild the adjusted series.

The database half of `baskfy_core.action_recovery`. It reads `close_raw` from `ohlcv_daily`,
fetches the same instrument's *adjusted* history from Kite, hands both to the pure recovery, and
writes what comes back into `corporate_action` — then calls `reprocess_instrument`, which is what
actually turns a stored action into a corrected price series.

    uv run python -m baskfy_worker.action_recovery              # dry run: reports, writes nothing
    uv run python -m baskfy_worker.action_recovery --write      # writes, then reprocesses

**Dry run is the default and that is deliberate.** This rewrites price history for instruments a
live strategy ranks, so the writing form has to be typed on purpose.

WHAT IT WRITES, AND WHAT IT REFUSES
-----------------------------------
Only **share-count actions** — splits and bonuses. The 38 cash-shaped actions are recovered,
counted and deliberately not written: M27 measured the reference corpus and it computes momentum
on a *price* return, so applying dividends would be adopting a different convention, not fixing
data (`DECISIONS-MERGE.md` M27, `reconciliation/RECOVERED-ACTIONS.md`).

A step whose flanks were available and not flat is **rejected** — that is a one-day glitch in
either series, not an action. A step at the edge of the observation window is written, because it
could not be tested rather than having failed the test; NESTLEIND's 10:1 split is exactly that
case and is entirely real.

REVERSIBILITY, WHICH IS THE POINT
---------------------------------
Every row carries ``raw['source'] = 'ratio_recovery'``, so the whole set is one predicate:

    DELETE FROM corporate_action WHERE raw->>'source' = 'ratio_recovery';

then re-run `reprocess_instrument` for the affected instruments and the series is back. Nothing
here touches `close_raw`, which stays the exchange print, so the reversal is total. `raw` also
carries the measured factor, the confirmation status and the split/bonus ambiguity, because a row
that cannot explain where it came from is worse than no row.
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

from baskfy_core.action_recovery import RecoveredAction, recover_actions
from baskfy_core.models import CorporateAction
from baskfy_worker.db import session_scope
from baskfy_worker.providers import build_pipeline_dependencies
from baskfy_worker.tasks.adjustments import reprocess_instrument

#: The marker that makes every row this module writes reversible in one predicate.
SOURCE: Final = "ratio_recovery"

#: A split and a bonus are the same price factor, so one type has to be chosen. `split` is the
#: one whose ratio maps directly onto what was measured (`split_factor(a, b)` is price `b/a`),
#: and `raw` records the bonus reading it is indistinguishable from.
ACTION_TYPE: Final = "split"

#: Kite's history reaches back further, but the bhavcopy series starts in 2024 and a comparison
#: needs both. Widening this is free once a deep backfill has run.
WINDOW_START: Final = dt.date(2024, 1, 1)


@dataclass
class Recovery:
    """What the run found, per instrument and in total."""

    instruments_examined: int = 0
    instruments_with_actions: int = 0
    share_count: list[tuple[str, RecoveredAction]] = field(default_factory=list)
    cash: list[tuple[str, RecoveredAction]] = field(default_factory=list)
    rejected: list[tuple[str, RecoveredAction]] = field(default_factory=list)
    written: int = 0
    already_present: int = 0
    bars_rewritten: int = 0
    errors: list[str] = field(default_factory=list)


async def _bars(session: AsyncSession, instrument_id: int) -> list[tuple[dt.date, float]]:
    rows = (
        await session.execute(
            text(
                "select date, close_raw from ohlcv_daily "
                "where instrument_id = :i and date >= :s and close_raw is not null "
                "order by date"
            ),
            {"i": instrument_id, "s": WINDOW_START},
        )
    ).all()
    return [(row[0], float(row[1])) for row in rows]


async def _candidates(
    session: AsyncSession, symbols: Sequence[str] | None
) -> list[tuple[int, str, int]]:
    query = (
        "select id, symbol, kite_token from instrument "
        "where kite_token is not null and series in ('EQ', 'BE') and delisted_on is null"
    )
    params: dict[str, object] = {}
    if symbols:
        query += " and symbol = any(:symbols)"
        params["symbols"] = list(symbols)
    query += " order by symbol"
    rows = (await session.execute(text(query), params)).all()
    return [(row[0], row[1], row[2]) for row in rows]


def _payload(symbol: str, action: RecoveredAction) -> dict[str, object]:
    """`corporate_action.raw` — everything needed to audit or reverse the row."""
    ratio_from, ratio_to = action.as_split_ratio()
    return {
        "source": SOURCE,
        "symbol": symbol,
        "measured_factor": round(action.factor, 6),
        "ratio": f"{ratio_from}:{ratio_to}",
        "shape": action.shape,
        "confirmed": action.confirmed,
        "at_edge": action.at_edge,
        "ambiguous_with": action.ambiguous_with,
        "note": (
            "Derived from the ratio between the NSE bhavcopy exchange print and Kite's adjusted "
            "history (DECISIONS-MERGE M24/M28). Not sourced from a corporate-action feed. A split "
            "and a bonus are the same price factor, so the type is a choice; the factor is not. "
            "shape='irregular_probably_demerger' means the ratio is not one a split or bonus "
            "produces -- the price step is real and applied, but 'split' is a label of "
            "convenience rather than a claim about the event."
        ),
    }


async def run(
    *,
    write: bool,
    symbols: Sequence[str] | None = None,
    database_url: str | None = None,
) -> Recovery:
    report = Recovery()
    provider = build_pipeline_dependencies().provider
    fetch = getattr(provider, "daily_bars", None)
    if not callable(fetch):
        raise RuntimeError("no provider offers daily_bars")

    async with session_scope(database_url) as session:
        candidates = await _candidates(session, symbols)

    for instrument_id, symbol, token in candidates:
        async with session_scope(database_url) as session:
            raw = await _bars(session, instrument_id)
        if not raw:
            continue
        report.instruments_examined += 1
        try:
            frame = fetch(token, raw[0][0], raw[-1][0])
        except Exception as exc:
            report.errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
            continue
        adjusted = [
            (
                row["date"].date() if hasattr(row["date"], "date") else row["date"],
                float(row["close"]),
            )
            for row in frame.to_dicts()
        ]

        actions = recover_actions(raw, adjusted)
        if not actions:
            continue
        report.instruments_with_actions += 1
        for action in actions:
            if action.rejected:
                report.rejected.append((symbol, action))
            elif action.share_count:
                report.share_count.append((symbol, action))
            else:
                report.cash.append((symbol, action))

        if not write:
            continue

        writable = [a for a in actions if a.share_count and not a.rejected]
        if not writable:
            continue
        async with session_scope(database_url) as session:
            written = await _write(session, instrument_id, symbol, writable)
            report.written += written
            report.already_present += len(writable) - written
            # Rebuild from close_raw over the whole history, which is what makes a stored action
            # into a corrected price. Idempotent, so a re-run is free.
            result = await reprocess_instrument(session, instrument_id)
            report.bars_rewritten += result.bars_rewritten
    return report


async def _write(
    session: AsyncSession, instrument_id: int, symbol: str, actions: Sequence[RecoveredAction]
) -> int:
    """Insert the actions this instrument is missing. Returns how many were new.

    **A date the database already knows anything about is skipped entirely**, and that is not the
    same as the unique constraint. The constraint is on `(instrument, action_type, ex_date)`, so
    NSE's `bonus 4:1` and a recovered `split 5:1` on the same day are different rows and both
    survive it — and both then get applied, multiplying a 5x adjustment into 25x.

    That is exactly what happened to CUPID on 2026-03-09 in M28's first write, and it was found by
    reading the resulting prices rather than by trusting the insert. The recovered factor measures
    the **whole** step at that ex-date, so anything already recorded there is a component of it,
    not a separate action. One or the other, never both — and the feed's row is the one that
    stays, because a sourced action outranks an inference.

    (Two actions on the same date from the *same* source are fine and real: CUPID carries an NSE
    split and an NSE bonus both dated 2024-04-15.)
    """
    known = set(
        (
            await session.execute(
                text("select distinct ex_date from corporate_action where instrument_id = :i"),
                {"i": instrument_id},
            )
        )
        .scalars()
        .all()
    )
    actions = [a for a in actions if a.ex_date not in known]
    if not actions:
        return 0

    values = []
    for action in actions:
        ratio_from, ratio_to = action.as_split_ratio()
        values.append(
            {
                "instrument_id": instrument_id,
                "action_type": ACTION_TYPE,
                "ex_date": action.ex_date,
                "ratio_from": ratio_from,
                "ratio_to": ratio_to,
                "amount": None,
                "raw": _payload(symbol, action),
            }
        )
    statement = (
        insert(CorporateAction)
        .values(values)
        .on_conflict_do_nothing(
            index_elements=[
                CorporateAction.instrument_id,
                CorporateAction.action_type,
                CorporateAction.ex_date,
            ]
        )
        .returning(CorporateAction.id)
    )
    return len((await session.execute(statement)).all())


def _render(report: Recovery, *, write: bool) -> str:
    lines = [
        "corporate-action recovery" + ("" if write else "  [DRY RUN — nothing was written]"),
        "=" * 60,
        f"instruments examined        : {report.instruments_examined}",
        f"instruments with an action  : {report.instruments_with_actions}",
        f"share-count ({'written' if write else 'writable'})       : {len(report.share_count)}",
        f"cash-shaped (never written) : {len(report.cash)}",
        f"rejected as glitches        : {len(report.rejected)}",
        f"  of which irregular shape  : "
        f"{sum(1 for _, a in report.share_count if not a.clean_shape)}"
        " (probably demergers — applied, and marked)",
    ]
    if write:
        lines += [
            f"rows inserted               : {report.written}",
            f"already present             : {report.already_present}",
            f"bars rewritten              : {report.bars_rewritten}",
        ]
    if report.errors:
        lines += ["", f"errors ({len(report.errors)}):", *[f"  {e}" for e in report.errors[:10]]]
    if report.rejected:
        lines += ["", "rejected (flanks available, ratio not flat — a glitch, not an action):"]
        lines += [f"  {s:14s} {a.ex_date}  factor={a.factor:.4f}" for s, a in report.rejected[:20]]
    lines += ["", "share-count actions:"]
    lines += [
        f"  {s:14s} {a.ex_date}  {a.ratio}  factor={a.factor:.4f}"
        f"{'' if a.confirmed else '  (window edge)'}"
        for s, a in sorted(report.share_count, key=lambda pair: (pair[0], pair[1].ex_date))
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m baskfy_worker.action_recovery", description=__doc__
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="actually insert the actions and rebuild the adjusted series",
    )
    parser.add_argument("--symbols", default="", help="comma-separated; default is every EQ/BE")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
    report = asyncio.run(run(write=args.write, symbols=symbols, database_url=args.database_url))
    print(_render(report, write=args.write))
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
