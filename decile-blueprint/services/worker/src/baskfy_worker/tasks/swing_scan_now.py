"""SW15 — "Scan now": the swing detection on demand, intraday from Kite quotes.

Maulik, 3 Sep 2026: "I wanted to have the scan anytime, and since we have the Kite API, we
should have all the data." The nightly job (`swing.py`) detects on published closes at 21:00;
this runs the same detectors whenever a person presses the button, and during the session it
detects on **today so far**.

THE ONE DECISION THIS MODULE MAKES
----------------------------------
Which session to scan (:func:`decide_session`). On a trading day **from the open onwards** — any
hour after 09:15 IST, not merely until the close (:func:`session_has_started`, widened 8 Sep 2026
so a 16:00 login stops being served yesterday) — and before tonight's publish, the session is
**today**, and today's bar does not exist yet — so one is built per liquid name from the live quote
(:func:`provisional_bars`): open / high / low from the quote's ``ohlc``, close = ``last_price``,
volume = the session's volume so far, turnover = close x volume, the day's ``upper_circuit``, the
last published bar's ``adj_factor`` (`docs/swing/04` §1 read for a day still in progress —
DECISIONS-SW SW15.1). That bar is appended to the published ones and the whole of
`run_detect_swing` runs over it with ``provisional=True`` stamped on every row. Before the open
the session is the last published one and the run is the nightly's body, plain — a quote at 08:00
carries *yesterday's* close, and stamping that as today would be a lie the detectors act on.

A provisional bar is honest about what it is: a base that "tightened" by 13:42 can widen by
15:30, the volume dry-up is measured on four hours of volume, and the breadth over such bars is
the breadth of a half-day. The page says so on every row (`provisional`, `scanned_at`), and the
nightly's upsert replaces every one of them — the same keys flip to ``False``, the rest are
deleted (`swing._upsert_setups`).

WHAT IT MUST NOT DO
-------------------
Reach an order. A scan reads quotes and writes detection rows; nothing here names the execution
package, and the API route that enqueues it is one of the money-free writes `docs/swing/02`
Track A permits (`test_swing_readonly.py`). The quote read is `KiteProvider.quotes` — at most
500 names a call, one limiter token a call (STANDING-ANSWERS B10) — and it is the only network
call. Law 1 holds: the bar is built *here*, in the worker, and handed to the pure detectors.

FAIL SOFT
---------
The run row (`sw_scan_run`) is the record. It is marked ``RUNNING`` in its own transaction, the
detection writes land in the next one, and a failure anywhere in between — a quote read that
raised, a Kite session that has expired, a decision that cannot be made because nothing is
published — rolls that transaction back and marks the row ``FAILED`` with the reason. Nothing is
half-written, and the page shows the reason rather than a stale funnel.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

import polars as pl
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import OhlcvDaily, SwScanRun, TradingDay
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_providers.records import QuoteRecord
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.swing import BAR_SCHEMA, load_swing_config, run_detect_swing
from baskfy_worker.tasks.swing_premarket import LiquidName, QuoteSource, liquid_universe

log = logging.getLogger(__name__)

#: NSE's cash session. Facts about the exchange, not thresholds (STANDING-ANSWERS B13): a scan
#: between these two instants on a trading day is a scan of a day still in progress.
MARKET_OPEN: Final = dt.time(9, 15)
MARKET_CLOSE: Final = dt.time(15, 30)

#: The run's four states — `baskfy_core.models.swing.SW_SCAN_STATUSES`, spelled here so the
#: task cannot write a word the check constraint refuses.
QUEUED: Final = "QUEUED"
RUNNING: Final = "RUNNING"
DONE: Final = "DONE"
FAILED: Final = "FAILED"

#: How the decision explains itself on the run row.
REASON_MARKET_OPEN: Final = "market open: today so far, from live quotes"
REASON_PUBLISHED: Final = "before the open: the last published session, from published bars"
#: After 15:30 and before tonight's publish, a quote carries the day's final traded price — so
#: today is still the honest session to scan, and the bar built from it is very nearly the one
#: the bhavcopy will confirm.
REASON_AFTER_CLOSE: Final = "after the close: today so far, from the day's final quotes"
REASON_ALREADY_PUBLISHED: Final = "today is already published: re-detected from published bars"


class ScanNotRunnable(Exception):
    """The scan cannot be made at all — nothing published, no quote source for a live session."""


@dataclass(frozen=True, slots=True)
class ScanDecision:
    """Which session the scan is of, and whether its bar has to be built."""

    session_date: dt.date
    provisional: bool
    #: The last published session — where the point-in-time reads look on a provisional scan.
    published_as_of: dt.date
    reason: str


@dataclass(slots=True)
class BarBuild:
    """What became of the quotes on the way to a bar each."""

    frame: pl.DataFrame
    quotes: int = 0
    #: Names skipped, by reason: no quote came back, no factor (the last bar is not the last
    #: session), a price that is not a price.
    skipped: dict[str, int] = field(
        default_factory=lambda: {"no_quote": 0, "no_factor": 0, "bad_price": 0}
    )

    def as_detail(self) -> dict[str, object]:
        return {
            "quotes": self.quotes,
            "bars_built": self.frame.height,
            "skipped": dict(self.skipped),
        }


def _live_index_level(source: object, slug: str) -> float | None:
    """The benchmark's level right now, or None if this provider cannot say.

    Duck-typed on `index_level` so the scan works with any quote source — the replay harness and
    the tests hand it fakes that have no such method, and they must keep working.
    """
    fetch = getattr(source, "index_level", None)
    if not callable(fetch):
        return None
    try:
        level = fetch(slug)
    except Exception:
        log.warning("could not read a live level for %s; the gate reads published closes", slug)
        return None
    return None if level is None else float(level)


async def last_published_session(session: AsyncSession) -> dt.date | None:
    """The newest date with a published bar — "the last session" as the page means it."""
    return (await session.execute(select(func.max(OhlcvDaily.date)))).scalar_one_or_none()


async def is_trading_day(session: AsyncSession, on: dt.date) -> bool:
    flag = (
        await session.execute(
            select(TradingDay.is_trading_day).where(
                TradingDay.exchange_id == NSE_EXCHANGE_ID, TradingDay.date == on
            )
        )
    ).scalar_one_or_none()
    return bool(flag)


def market_is_open(now: dt.datetime) -> bool:
    """Between the open and the close, inclusive, on the clock ``now`` carries (IST, naive)."""
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def session_has_started(now: dt.datetime) -> bool:
    """Has today's session begun? True from 09:15 onwards, at any hour after it.

    WHY THIS IS NOT `market_is_open` (Maulik, 8 Sep 2026).

    "Given any period of the day, whenever user connects zerodha, start fetching data across the
    instruments, so we show all the data live across the site."

    The scan used to build today's provisional bar only between 09:15 and 15:30. So a login at
    16:00 — after the close, before the nightly publishes around 18:45 — got *yesterday*, for
    two and a half hours, on a day whose prices were final and sitting in Kite ready to be read.
    That is the window this opens.

    **The asymmetry is deliberate.** After the close a quote returns the day's last traded
    price, so a provisional bar for today is accurate rather than a guess — it is very nearly
    the bar the bhavcopy will confirm. Before 09:15 a quote returns *yesterday's* close, and
    stamping that as today would be a lie the detectors would then act on. So the window opens
    at the open and stays open; it does not wrap around to the small hours.

    The caller's `published >= today` check still comes first, so once the nightly has published
    today this never fires and no provisional row is written over a settled one.
    """
    return now.time() >= MARKET_OPEN


async def decide_session(session: AsyncSession, now: dt.datetime) -> ScanDecision:
    """Today from live quotes from the open onwards; before the open, the last published session.

    ``now`` is IST and naive, as every swing task passes it. "Today is published" cannot happen
    during the session, but a redelivered task waking after the nightly could see it; that
    case is a plain re-run, never a second provisional bar on top of a published one.
    """
    published = await last_published_session(session)
    if published is None:
        raise ScanNotRunnable("no published bars: the pipeline has not run yet")
    today = now.date()
    if published >= today:
        return ScanDecision(published, False, published, REASON_ALREADY_PUBLISHED)
    if session_has_started(now) and await is_trading_day(session, today):
        reason = REASON_MARKET_OPEN if market_is_open(now) else REASON_AFTER_CLOSE
        return ScanDecision(today, True, published, reason)
    return ScanDecision(published, False, published, REASON_PUBLISHED)


def _money(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def provisional_bars(
    names: Sequence[LiquidName],
    quotes: Sequence[QuoteRecord],
    *,
    factors: dict[int, float],
    on: dt.date,
) -> BarBuild:
    """One bar per name that answered, in the adjusted space the detectors read.

    The quote is an exchange print; the published series is ``raw x adj_factor``. So every price
    on the bar is multiplied by the name's last factor — the same factor
    `swing.to_exchange_prices` will divide the levels by on the way out, so a trigger from a
    provisional bar is the exchange price a person types. ``upper_circuit`` gets the same
    treatment `load_swing_bars` gives the published band. Turnover is money and stays raw
    (close x volume), as the pipeline stores it.

    A quote with ``volume`` 0 is still a bar — a name that has not traded by 09:16 has a price
    and no volume, and the liquidity floor reads a 20-day average, not one morning. A quote
    whose ``ohlc`` is missing a side takes the last price for it. A name with no factor — its
    last bar is not the last session, so the universe would not have quoted it anyway — is
    skipped and counted, never invented.
    """
    by_symbol = {name.symbol: name for name in names}
    build = BarBuild(frame=pl.DataFrame(schema=BAR_SCHEMA), quotes=len(quotes))
    rows: list[dict[str, object]] = []
    answered: set[str] = set()
    for quote in quotes:
        name = by_symbol.get(quote.symbol)
        if name is None:
            continue
        answered.add(quote.symbol)
        factor = factors.get(name.instrument_id)
        if factor is None or factor <= 0:
            build.skipped["no_factor"] += 1
            continue
        last = _money(quote.last_price)
        if last is None or last <= 0:
            build.skipped["bad_price"] += 1
            continue
        open_ = _money(quote.open) or last
        high = max(_money(quote.high) or last, last)
        low = min(_money(quote.low) or last, last)
        volume = float(max(int(quote.volume), 0))
        circuit = _money(quote.upper_circuit)
        rows.append(
            {
                "instrument_id": name.instrument_id,
                "symbol": name.symbol,
                "date": on,
                "open": open_ * factor,
                "high": high * factor,
                "low": low * factor,
                "close": last * factor,
                "volume": volume,
                "turnover": round(last * volume, 2),
                "upper_circuit": None if circuit is None else circuit * factor,
                "adj_factor": factor,
            }
        )
    build.skipped["no_quote"] = len(by_symbol) - len(answered)
    if rows:
        build.frame = pl.DataFrame(rows, schema=BAR_SCHEMA)
    return build


async def last_factors(
    session: AsyncSession, *, on: dt.date, instrument_ids: Sequence[int]
) -> dict[int, float]:
    """``instrument_id -> adj_factor`` of the bar published for ``on`` — only names whose last
    bar *is* the last session have one, which is the skip rule above."""
    if not instrument_ids:
        return {}
    rows = await session.execute(
        select(OhlcvDaily.instrument_id, OhlcvDaily.adj_factor).where(
            OhlcvDaily.date == on, OhlcvDaily.instrument_id.in_(list(instrument_ids))
        )
    )
    return {
        int(instrument_id): (float(factor) if factor is not None else 1.0)
        for instrument_id, factor in rows
    }


async def build_provisional_frame(
    session: AsyncSession,
    *,
    user_id: int,
    decision: ScanDecision,
    quotes: QuoteSource,
) -> BarBuild:
    """The liquid universe as of the last close, quoted, one bar each for today."""
    config = await load_swing_config(session, user_id)
    names = await liquid_universe(session, as_of=decision.published_as_of, config=config)
    if not names:
        return BarBuild(frame=pl.DataFrame(schema=BAR_SCHEMA))
    factors = await last_factors(
        session, on=decision.published_as_of, instrument_ids=[n.instrument_id for n in names]
    )
    records = quotes.quotes([name.symbol for name in names])
    return provisional_bars(names, records, factors=factors, on=decision.session_date)


@dataclass(frozen=True, slots=True)
class ScanReport:
    """What the run did — returned to the Celery task and mirrored on the run row."""

    run_id: int
    status: str
    session_date: dt.date | None
    provisional: bool
    candidates: int
    detail: dict[str, object]
    error: str | None

    def as_detail(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "session_date": None if self.session_date is None else self.session_date.isoformat(),
            "provisional": self.provisional,
            "candidates": self.candidates,
            "detail": self.detail,
            "error": self.error,
        }


async def _load_run(session: AsyncSession, run_id: int, user_id: int) -> SwScanRun | None:
    return (
        await session.execute(
            select(SwScanRun).where(SwScanRun.id == run_id, SwScanRun.user_id == user_id)
        )
    ).scalar_one_or_none()


async def run_scan_now(  # noqa: PLR0913 - one keyword per input the scan depends on
    session: AsyncSession,
    *,
    run_id: int,
    user_id: int,
    index_slug: str,
    execution_enabled: bool,
    quote_source: Callable[[], QuoteSource] | None,
    now: dt.datetime,
    clock: Callable[[], dt.datetime] | None = None,
) -> ScanReport:
    """One press of the button, end to end, inside the caller's transaction.

    The detection writes run in a **savepoint**: a failure anywhere in them — the quote read,
    a Kite session that has expired, a decision that cannot be made — rolls that savepoint
    back and the run row takes ``FAILED`` and the reason in the same outer transaction, so the
    task commits either the rows and ``DONE`` or nothing and ``FAILED``, never half of either.

    ``quote_source`` is called only on the provisional path — outside market hours no Kite
    session is needed and none is opened. ``now`` is IST naive (the decision); ``clock`` gives
    the timestamps written to the row (UTC, aware) and defaults to the wall clock.
    """
    stamp = clock or (lambda: dt.datetime.now(tz=dt.UTC))
    run = await _load_run(session, run_id, user_id)
    if run is None:
        raise ScanNotRunnable(f"no sw_scan_run {run_id} for this user")
    run.status = RUNNING
    run.started_at = stamp()
    await session.flush()
    requested = run.detail if isinstance(run.detail, dict) else {}

    decision: ScanDecision | None = None
    try:
        async with session.begin_nested():
            decision = await decide_session(session, now)
            written, detail = await _scan(
                session,
                run_id=run_id,
                user_id=user_id,
                decision=decision,
                index_slug=index_slug,
                execution_enabled=execution_enabled,
                quote_source=quote_source,
                scanned_at=stamp(),
            )
    except Exception as exc:
        log.warning("scan now %s failed: %s: %s", run_id, type(exc).__name__, exc)
        error = f"{type(exc).__name__}: {exc}"
        run.status = FAILED
        run.error = error
        run.finished_at = stamp()
        if decision is not None:
            run.session_date = decision.session_date
            run.provisional = decision.provisional
        await session.flush()
        return ScanReport(
            run_id,
            FAILED,
            None if decision is None else decision.session_date,
            False if decision is None else decision.provisional,
            0,
            requested,
            error,
        )
    detail = {**requested, **detail}
    run.status = DONE
    run.session_date = decision.session_date
    run.provisional = decision.provisional
    run.detail = detail
    run.error = None
    run.finished_at = stamp()
    await session.flush()
    return ScanReport(
        run_id, DONE, decision.session_date, decision.provisional, written, detail, None
    )


async def _scan(  # noqa: PLR0913 - one keyword per input the scan depends on
    session: AsyncSession,
    *,
    run_id: int,
    user_id: int,
    decision: ScanDecision,
    index_slug: str,
    execution_enabled: bool,
    quote_source: Callable[[], QuoteSource] | None,
    scanned_at: dt.datetime,
) -> tuple[int, dict[str, object]]:
    """The detection itself: the bar build when the session is live, then the nightly's body."""
    scan: dict[str, object] = {
        "run_id": run_id,
        "scanned_at": scanned_at.isoformat(),
        "provisional": decision.provisional,
        "reason": decision.reason,
    }
    build: BarBuild | None = None
    live_index_level: float | None = None
    if decision.provisional:
        if quote_source is None:
            raise ScanNotRunnable(
                "the market is open and there is no Kite quote source (no Kite session)"
            )
        source = quote_source()
        build = await build_provisional_frame(
            session, user_id=user_id, decision=decision, quotes=source
        )
        scan["quotes"] = build.quotes
        # THE BENCHMARK'S LIVE LEVEL, SO THE GATE'S INDEX RULE CAN MOVE TODAY (9 Sep 2026).
        #
        # Maulik: "tomorrow it might get green on live market data ... once a user logs in and
        # connects the Kite broker, verify whether it's red or green."
        #
        # Breadth already recomputed from the provisional bars. The index rule did not: it read
        # `index_snapshot_daily`, which holds published sessions only, so its newest row is
        # yesterday's and the half of the gate that decides the verdict was frozen until the
        # nightly ran. One extra quote fixes that.
        #
        # Best-effort by construction. A provider that cannot quote the index answers None, the
        # gate falls back to published closes, and the scan is exactly as good as it was before —
        # late, never wrong. A live gate that raised would be worse than a late one.
        level = _live_index_level(source, index_slug)
        if level is not None:
            live_index_level = level
            scan["live_index_level"] = level
    outcome = StepOutcome()
    written = await run_detect_swing(
        session,
        outcome,
        decision.session_date,
        user_id=user_id,
        index_slug=index_slug,
        execution_enabled=execution_enabled,
        extra_bars=None if build is None else build.frame,
        provisional=decision.provisional,
        reference_date=decision.published_as_of if decision.provisional else None,
        scan=scan,
        live_index_level=live_index_level,
    )
    detail: dict[str, object] = {
        "reason": decision.reason,
        "candidates": written,
        "scanned_at": scanned_at.isoformat(),
    }
    for key in ("funnel", "skipped_reason"):
        if key in outcome.detail:
            detail[key] = outcome.detail[key]
    if build is not None:
        detail.update(build.as_detail())
    return written, detail


async def sweep_queued(
    session: AsyncSession, *, user_id: int, publish: Callable[[int], str]
) -> list[int]:
    """Publish every ``QUEUED`` row nobody has published yet (``task_id`` null) — the desk
    console's button writes the row and nothing else, and the API's does the same when it has
    no broker to hand. Returns the run ids published."""
    rows = (
        (
            await session.execute(
                select(SwScanRun)
                .where(
                    SwScanRun.user_id == user_id,
                    SwScanRun.status == QUEUED,
                    SwScanRun.task_id.is_(None),
                )
                .order_by(SwScanRun.id)
            )
        )
        .scalars()
        .all()
    )
    published: list[int] = []
    for row in rows:
        row.task_id = publish(int(row.id))
        published.append(int(row.id))
    await session.flush()
    return published


__all__ = [
    "DONE",
    "FAILED",
    "MARKET_CLOSE",
    "MARKET_OPEN",
    "QUEUED",
    "RUNNING",
    "BarBuild",
    "ScanDecision",
    "ScanNotRunnable",
    "ScanReport",
    "build_provisional_frame",
    "decide_session",
    "last_factors",
    "last_published_session",
    "market_is_open",
    "provisional_bars",
    "run_scan_now",
    "sweep_queued",
]
