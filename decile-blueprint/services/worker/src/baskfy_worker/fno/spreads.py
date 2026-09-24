"""The 15:00 spread sample and the forward results calendar (``docs/fno/06`` FO3, ``01`` §2).

WHY
---
``RESEARCH.md`` charged stock options an **assumed** 3 % slippage. FO3 measures it: at 15:00 each
session, one Kite ``quote()`` over the ATM ± 3 listed strikes (CE and PE) of the near monthly of
the top 30 stock underlyings by futures turnover, plus NIFTY and BANKNIFTY. 32 × 7 × 2 = 448 keys,
inside Kite's 500 per call. The per-underlying median half-spread ÷ mid it yields replaces the
assumption in FO9's re-test once an underlying has ≥ 20 sessions of it (``06`` FO9).

WHAT IS READ, FROM WHERE
------------------------
* **The names** — ``fo_contract_daily`` only: each stock's futures turnover (every expiry summed)
  per session, the median over the last 20 ingested sessions before today (fewer when fewer are
  stored), the top 30. ``fo_underlying_daily.fut_turnover_20d`` is FO1's derivation and not yet
  filled, so it is not read (DECISIONS-FO FO3.2).
* **The near monthly** — ``op_contract`` (FO2 widened it to every underlying): a monthly is the
  last listed expiry of its calendar month (``docs/options/04`` §1.1, never a weekday rule), and
  the near one is the first **after** today — on expiry day the expiring series is thirty minutes
  from settlement and its book says nothing about a contract anyone would open (FO3.3).
* **ATM** — the last settle of that expiry's future in ``fo_contract_daily`` (the latest ingested
  session), falling back to the nearest listed future. Futures are not in ``op_contract``, and a
  second ``quote()`` to centre the strikes would break "one call"; a one-day-old centre with
  three strikes a side still brackets the live ATM on all but a gap day, and the sample is a
  spread measurement, not a trade (FO3.4).
* **The quotes** — through :class:`~baskfy_worker.options.reads.QuoteReader`, i.e. the box's
  shared read limiter and the 1 req/s quote clock (``build_options_kite``). The provider batches
  at 500 keys; with the defaults the sample is one call, and a configuration above 500 keys
  would be split into ⌈keys ÷ 500⌉ calls on the same clock — reported, never silent (FO3.5).

WHAT IS WRITTEN
---------------
``fo_spread_sample``: one row per answered contract — best bid and best ask from the depth, their
mid, OI, ``taken_at`` (the run's moment) and ``trade_date``. Rounded at write time to the
column's 2 dp, half-up (house rule 8). Append-only: ``ON CONFLICT DO NOTHING`` on
``(taken_at, symbol, expiry, strike, option_type)``. **A contract Kite answered with a one-sided or
empty book is stored with the missing side null and a null mid** — an empty book is itself the
liquidity fact FO9 must count; a key Kite did not answer at all writes nothing (FO3.1). A crossed
book (ask < bid) keeps both prices and a null mid.

THE RESULTS CALENDAR (QUESTIONS Q8)
-----------------------------------
For the 30 stock names, ``NSEProvider.results_calendar`` — the NSE limiter, the cookie prime,
archive-then-parse under ``nse/event-calendar/<SYMBOL>/<date>.json``. Forward only: meetings on or
after today. **Stored where it already lands: the provider's raw archive**, keyed by the day it was
read, which is exactly the point-in-time record Q8 asks for; a second read of the same day is
answered from the archive with no request. The run's result carries each name's next results
date. No table is added: a queryable one needs a migration past 0052 and moves the FO2 schema
tests' pinned head, so it is FO4's call when the scan's ``SKIPPED_EVENT`` needs it (FO3.6).
Per symbol, fail soft on the provider's own errors, exactly as the swing catalyst feed does.

Nothing here has an order verb (law 2). Moves no money.
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import FoContractDaily, FoSpreadSample, OpContract
from baskfy_core.models.base import JsonObject
from baskfy_core.models.fno import FUTURE_TYPE
from baskfy_providers.errors import ProviderError
from baskfy_providers.kite import QUOTE_BATCH_SIZE
from baskfy_providers.records import EarningsDateRecord, OptionQuoteRecord
from baskfy_worker.options.reads import QuoteReader

log = logging.getLogger("baskfy_worker.fno.spreads")

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST")
#: ``06`` FO3: the top 30 stock underlyings by futures turnover.
TOP_STOCKS: Final = 30
#: ``06`` FO3: "plus NIFTY and BANKNIFTY".
INDEX_UNDERLYINGS: Final[tuple[str, ...]] = ("NIFTY", "BANKNIFTY")
#: ``06`` FO3: ATM ± 3 strikes.
STRIKES_EACH_SIDE: Final = 3
#: The turnover median's window, in ingested sessions (FO3.2).
TURNOVER_SESSIONS: Final = 20
#: ``06`` FO9: measured slippage replaces the assumption where FO3 has ≥ 20 sessions of it.
MIN_SPREAD_SESSIONS: Final = 20
#: A quote is a live book only inside the session.
SESSION_OPEN: Final = dt.time(9, 15)
SESSION_CLOSE: Final = dt.time(15, 30)
NFO: Final = "NFO"
FUTSTK: Final = "FUTSTK"

_CENT: Final = Decimal("0.01")


def _cent(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(_CENT, rounding=ROUND_HALF_UP)


# --- pure: which names, which expiry, which strikes -------------------------------------------


def rank_by_turnover(
    turnover: Mapping[str, Sequence[Decimal]], top: int = TOP_STOCKS
) -> list[str]:
    """The ``top`` symbols by median per-session futures turnover; ties by symbol, ascending.

    A symbol with no turnover in the window is not ranked.
    """
    medians = {
        symbol: statistics.median(values) for symbol, values in turnover.items() if values
    }
    ranked = sorted(medians, key=lambda s: (-medians[s], s))
    return ranked[:top]


def monthly_expiries(expiries: Iterable[dt.date]) -> list[dt.date]:
    """The last listed expiry of each calendar month, ascending (``docs/options/04`` §1.1)."""
    last: dict[tuple[int, int], dt.date] = {}
    for expiry in expiries:
        key = (expiry.year, expiry.month)
        if key not in last or expiry > last[key]:
            last[key] = expiry
    return sorted(last.values())


def near_monthly(expiries: Iterable[dt.date], today: dt.date) -> dt.date | None:
    """The first monthly expiry strictly after ``today`` (FO3.3), or ``None``."""
    return next((e for e in monthly_expiries(expiries) if e > today), None)


def atm_window(
    strikes: Iterable[Decimal], reference: Decimal, each_side: int = STRIKES_EACH_SIDE
) -> tuple[Decimal, ...]:
    """The listed strike nearest ``reference`` (the lower on a tie) and ``each_side`` listed
    strikes either side of it — by listing order, so an irregular ladder needs no step rule."""
    ladder = sorted(set(strikes))
    if not ladder:
        return ()
    centre = min(range(len(ladder)), key=lambda i: (abs(ladder[i] - reference), ladder[i]))
    return tuple(ladder[max(0, centre - each_side) : centre + each_side + 1])


@dataclass(frozen=True, slots=True)
class SampleContract:
    """One contract the sample quotes — the master's facts, never parsed from the symbol."""

    symbol: str
    tradingsymbol: str
    expiry: dt.date
    strike: Decimal
    option_type: str

    @property
    def key(self) -> str:
        return f"{NFO}:{self.tradingsymbol}"


def pick_sample(
    contracts: Sequence[SampleContract],
    reference: Decimal,
    today: dt.date,
    each_side: int = STRIKES_EACH_SIDE,
) -> list[SampleContract]:
    """One underlying's sample: its near monthly, ATM ± ``each_side`` strikes, CE and PE."""
    expiry = near_monthly((c.expiry for c in contracts), today)
    if expiry is None:
        return []
    series = [c for c in contracts if c.expiry == expiry]
    wanted = set(atm_window((c.strike for c in series), reference, each_side))
    return sorted(
        (c for c in series if c.strike in wanted), key=lambda c: (c.strike, c.option_type)
    )


def reference_level(
    futures: Mapping[dt.date, Decimal], expiry: dt.date
) -> Decimal | None:
    """The settle of ``expiry``'s future, else of the listed future nearest it (FO3.4)."""
    if expiry in futures:
        return futures[expiry]
    if not futures:
        return None
    nearest = min(futures, key=lambda e: (abs((e - expiry).days), e))
    return futures[nearest]


def quote_calls(keys: int, batch: int = QUOTE_BATCH_SIZE) -> int:
    """How many ``quote()`` calls ``keys`` keys cost (FO3.5): one at ≤ 500."""
    return -(-keys // batch) if keys > 0 else 0


def sample_rows(
    contracts: Sequence[SampleContract],
    quotes: Mapping[str, OptionQuoteRecord],
    *,
    trade_date: dt.date,
    taken_at: dt.datetime,
) -> list[dict[str, object]]:
    """The rows to store: one per contract Kite answered (FO3.1), rounded at write time."""
    rows: list[dict[str, object]] = []
    for c in contracts:
        record = quotes.get(c.key)
        if record is None:
            continue
        bid = _cent(record.bids[0].price) if record.bids else None
        ask = _cent(record.asks[0].price) if record.asks else None
        mid = (
            _cent((bid + ask) / 2) if bid is not None and ask is not None and ask >= bid else None
        )
        rows.append(
            {
                "trade_date": trade_date,
                "symbol": c.symbol,
                "expiry": c.expiry,
                "strike": _cent(c.strike),
                "option_type": c.option_type,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "oi": record.oi,
                "taken_at": taken_at,
            }
        )
    return rows


# --- pure: what FO9 reads ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpreadObservation:
    """One stored sample row, as the statistic needs it."""

    trade_date: dt.date
    symbol: str
    expiry: dt.date
    strike: Decimal
    option_type: str
    bid: Decimal | None
    ask: Decimal | None
    taken_at: dt.datetime


@dataclass(frozen=True, slots=True)
class SpreadStat:
    """An underlying's measured half-spread ÷ mid over its last sessions (``06`` FO9)."""

    symbol: str
    #: Median of (ask − bid) / (ask + bid) — the half-spread over the mid — per contract sample.
    median_half_spread_pct_of_mid: Decimal
    #: Distinct sessions with at least one two-sided quote, within the window.
    sessions: int
    #: Two-sided observations the median was taken over.
    observations: int

    def measured(self, minimum: int = MIN_SPREAD_SESSIONS) -> bool:
        """Whether FO9 may use it instead of the research's assumption (≥ 20 sessions)."""
        return self.sessions >= minimum


def half_spread_ratio(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    """(ask − bid) ÷ 2 over (ask + bid) ÷ 2, from the stored bid and ask — never the rounded
    mid. ``None`` for a one-sided, empty or crossed book."""
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return None
    return (ask - bid) / (ask + bid)


def half_spread_stats(
    observations: Iterable[SpreadObservation], sessions: int = MIN_SPREAD_SESSIONS
) -> dict[str, SpreadStat]:
    """Per underlying, the median half-spread ÷ mid over its last ``sessions`` sessions that
    have a two-sided quote, and how many sessions that is.

    A contract sampled twice in one session (a manual re-run) counts once, at its latest sample.
    One-sided and empty books are not in the median — they have no spread — and a session with
    only those does not count as a session of measurement.
    """
    if sessions <= 0:
        raise ValueError("sessions must be positive")
    latest: dict[tuple[dt.date, str, dt.date, Decimal, str], SpreadObservation] = {}
    for o in observations:
        key = (o.trade_date, o.symbol, o.expiry, o.strike, o.option_type)
        held = latest.get(key)
        if held is None or o.taken_at > held.taken_at:
            latest[key] = o
    by_symbol: dict[str, dict[dt.date, list[Decimal]]] = defaultdict(lambda: defaultdict(list))
    for o in latest.values():
        ratio = half_spread_ratio(o.bid, o.ask)
        if ratio is not None:
            by_symbol[o.symbol][o.trade_date].append(ratio)
    out: dict[str, SpreadStat] = {}
    for symbol, days in by_symbol.items():
        window = sorted(days)[-sessions:]
        ratios = [r for d in window for r in days[d]]
        out[symbol] = SpreadStat(
            symbol=symbol,
            median_half_spread_pct_of_mid=statistics.median(ratios),
            sessions=len(window),
            observations=len(ratios),
        )
    return out


# --- pure: the results calendar ------------------------------------------------------------------


def upcoming_results(
    records: Iterable[EarningsDateRecord], on_or_after: dt.date
) -> dict[str, list[EarningsDateRecord]]:
    """Forward only (Q8): each symbol's result meetings on or after ``on_or_after``, soonest
    first, one per date."""
    out: dict[str, dict[dt.date, EarningsDateRecord]] = defaultdict(dict)
    for r in records:
        if r.event_date >= on_or_after:
            out[r.symbol.upper()].setdefault(r.event_date, r)
    return {s: [by_date[d] for d in sorted(by_date)] for s, by_date in out.items()}


def in_session(now: dt.datetime) -> bool:
    """09:15 ≤ now ≤ 15:30 IST: a quote outside it is not a live book."""
    clock = now.astimezone(IST).time()
    return SESSION_OPEN <= clock <= SESSION_CLOSE


# --- I/O -----------------------------------------------------------------------------------------


class ResultsSource(Protocol):
    """``NSEProvider.results_calendar``; tests pass a fake."""

    def results_calendar(
        self, symbols: Sequence[str], *, on: dt.date
    ) -> list[EarningsDateRecord]: ...


@dataclass(slots=True)
class SpreadReport:
    """One run's result; JSON-able for the task."""

    trade_date: dt.date
    taken_at: dt.datetime
    reference_session: dt.date | None = None
    underlyings: list[str] = field(default_factory=list)
    no_sample: list[str] = field(default_factory=list)
    keys: int = 0
    calls: int = 0
    rows: int = 0
    inserted: int = 0
    unanswered: int = 0
    one_sided: int = 0
    skipped: str | None = None
    results: dict[str, list[str]] = field(default_factory=dict)
    results_errors: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> JsonObject:
        return {
            "trade_date": self.trade_date.isoformat(),
            "taken_at": self.taken_at.isoformat(),
            "reference_session": (
                self.reference_session.isoformat() if self.reference_session else None
            ),
            "underlyings": list(self.underlyings),
            "no_sample": list(self.no_sample),
            "keys": self.keys,
            "calls": self.calls,
            "rows": self.rows,
            "inserted": self.inserted,
            "unanswered": self.unanswered,
            "one_sided": self.one_sided,
            "skipped": self.skipped,
            "results": {s: list(d) for s, d in self.results.items()},
            "results_errors": dict(self.results_errors),
        }


async def latest_sessions(
    session: AsyncSession, before: dt.date, count: int = TURNOVER_SESSIONS
) -> list[dt.date]:
    """The last ``count`` sessions ``fo_contract_daily`` holds before ``before``, newest first."""
    rows = await session.execute(
        select(FoContractDaily.trade_date)
        .where(FoContractDaily.trade_date < before)
        .group_by(FoContractDaily.trade_date)
        .order_by(FoContractDaily.trade_date.desc())
        .limit(count)
    )
    return list(rows.scalars())


async def top_stock_underlyings(
    session: AsyncSession, sessions: Sequence[dt.date], top: int = TOP_STOCKS
) -> list[str]:
    """The top ``top`` stocks by median per-session futures turnover over ``sessions``."""
    if not sessions:
        return []
    rows = await session.execute(
        select(
            FoContractDaily.symbol,
            FoContractDaily.trade_date,
            func.sum(FoContractDaily.turnover),
        )
        .where(
            FoContractDaily.instrument == FUTSTK,
            FoContractDaily.trade_date.in_(list(sessions)),
            FoContractDaily.turnover.is_not(None),
        )
        .group_by(FoContractDaily.symbol, FoContractDaily.trade_date)
    )
    turnover: dict[str, list[Decimal]] = defaultdict(list)
    for symbol, _day, total in rows.tuples():
        if total is not None:
            turnover[symbol].append(Decimal(total))
    return rank_by_turnover(turnover, top)


async def future_settles(
    session: AsyncSession, day: dt.date, symbols: Sequence[str]
) -> dict[str, dict[dt.date, Decimal]]:
    """Each symbol's futures settles on ``day``, by expiry."""
    rows = await session.execute(
        select(FoContractDaily.symbol, FoContractDaily.expiry, FoContractDaily.settle).where(
            FoContractDaily.trade_date == day,
            FoContractDaily.option_type == FUTURE_TYPE,
            FoContractDaily.symbol.in_(list(symbols)),
        )
    )
    out: dict[str, dict[dt.date, Decimal]] = defaultdict(dict)
    for symbol, expiry, settle in rows.tuples():
        out[symbol][expiry] = settle
    return dict(out)


async def listed_options(
    session: AsyncSession, symbols: Sequence[str], today: dt.date
) -> dict[str, list[SampleContract]]:
    """Every still-listed option in ``op_contract`` for ``symbols``, expiring on or after today."""
    rows = await session.execute(
        select(OpContract).where(
            OpContract.underlying.in_(list(symbols)),
            OpContract.expired.is_(False),
            OpContract.expiry >= today,
        )
    )
    out: dict[str, list[SampleContract]] = defaultdict(list)
    for row in rows.scalars():
        out[row.underlying].append(
            SampleContract(
                symbol=row.underlying,
                tradingsymbol=row.tradingsymbol,
                expiry=row.expiry,
                strike=row.strike,
                option_type=row.option_type,
            )
        )
    return dict(out)


async def plan_sample(
    session: AsyncSession, today: dt.date, report: SpreadReport, top: int = TOP_STOCKS
) -> tuple[list[str], list[SampleContract]]:
    """The stock names (for the calendar) and the contracts to quote, from the database only."""
    sessions = await latest_sessions(session, today)
    if not sessions:
        report.skipped = "fo_contract_daily holds no session before today (FO2 has not ingested)"
        return [], []
    report.reference_session = sessions[0]
    stocks = await top_stock_underlyings(session, sessions, top)
    names = [*stocks, *(i for i in INDEX_UNDERLYINGS if i not in stocks)]
    report.underlyings = names
    settles = await future_settles(session, sessions[0], names)
    listed = await listed_options(session, names, today)
    picked: list[SampleContract] = []
    for name in names:
        contracts = listed.get(name, [])
        expiry = near_monthly((c.expiry for c in contracts), today)
        level = reference_level(settles.get(name, {}), expiry) if expiry is not None else None
        chosen = pick_sample(contracts, level, today) if level is not None else []
        if not chosen:
            report.no_sample.append(name)
        picked.extend(chosen)
    return stocks, picked


async def insert_samples(session: AsyncSession, rows: Sequence[dict[str, object]]) -> int:
    """Append-only: a moment already stored is left exactly as it was."""
    if not rows:
        return 0
    stmt = (
        insert(FoSpreadSample)
        .values(list(rows))
        .on_conflict_do_nothing(constraint="uq_fo_spread_sample_contract_moment")
    )
    result = await session.execute(stmt)
    count = getattr(result, "rowcount", None)
    return int(count) if isinstance(count, int) and count >= 0 else len(rows)


def read_results(
    source: ResultsSource, symbols: Sequence[str], today: dt.date, report: SpreadReport
) -> None:
    """Q8: each name's forward result meetings, per symbol and fail-soft on provider errors."""
    for symbol in symbols:
        try:
            meetings = source.results_calendar([symbol], on=today)
        except ProviderError as exc:
            report.results_errors[symbol] = f"{type(exc).__name__}: {exc}"
            log.warning("results calendar for %s failed: %s", symbol, exc)
            continue
        ahead = upcoming_results(meetings, today).get(symbol.upper(), [])
        report.results[symbol] = [m.event_date.isoformat() for m in ahead]


async def run_spread_sample(  # noqa: PLR0913 - the session, the clock and the three seams
    session: AsyncSession,
    now: dt.datetime,
    *,
    kite_ok: Callable[[], bool],
    quotes: Callable[[], QuoteReader],
    results: ResultsSource | None,
    top: int = TOP_STOCKS,
) -> SpreadReport:
    """One 15:00 run. The caller has checked the flag, the window and the trading day.

    ``quotes`` builds the Kite reader and is called **only** when ``kite_ok()`` says a session
    exists: with none, no Kite adapter is built and no Kite call is made (``06`` FO3). The
    results calendar is NSE and runs either way.
    """
    today = now.astimezone(IST).date()
    report = SpreadReport(trade_date=today, taken_at=now)
    stocks, picked = await plan_sample(session, today, report, top)
    if report.skipped is None:
        if not picked:
            report.skipped = "no listed near-monthly strikes for any sampled name"
        elif not kite_ok():
            report.skipped = "no usable Kite session"
        else:
            keys = [c.key for c in picked]
            report.keys = len(keys)
            report.calls = quote_calls(len(keys))
            answered = {q.key: q for q in quotes().option_quotes(keys)}
            rows = sample_rows(picked, answered, trade_date=today, taken_at=now)
            report.rows = len(rows)
            report.unanswered = len(picked) - len(rows)
            report.one_sided = sum(1 for r in rows if r["mid"] is None)
            report.inserted = await insert_samples(session, rows)
    if results is not None and stocks:
        read_results(results, stocks, today, report)
    return report


async def load_spread_stats(
    session: AsyncSession,
    as_of: dt.date,
    sessions: int = MIN_SPREAD_SESSIONS,
    symbols: Sequence[str] | None = None,
) -> dict[str, SpreadStat]:
    """What FO9 reads: :func:`half_spread_stats` over the stored samples up to ``as_of``.

    Point-in-time: nothing sampled after ``as_of`` is read (house rule 5). Reads a bounded window
    — the last ``sessions`` sample dates — per the whole table, then the pure helper trims per
    symbol (a symbol that joined the top 30 late simply has fewer sessions).
    """
    dates = (
        (
            await session.execute(
                select(FoSpreadSample.trade_date)
                .where(FoSpreadSample.trade_date <= as_of)
                .group_by(FoSpreadSample.trade_date)
                .order_by(FoSpreadSample.trade_date.desc())
                .limit(sessions)
            )
        )
        .scalars()
        .all()
    )
    if not dates:
        return {}
    stmt = select(FoSpreadSample).where(FoSpreadSample.trade_date.in_(list(dates)))
    if symbols is not None:
        stmt = stmt.where(FoSpreadSample.symbol.in_(list(symbols)))
    rows = (await session.execute(stmt)).scalars()
    return half_spread_stats(
        (
            SpreadObservation(
                trade_date=r.trade_date,
                symbol=r.symbol,
                expiry=r.expiry,
                strike=r.strike,
                option_type=r.option_type,
                bid=r.bid,
                ask=r.ask,
                taken_at=r.taken_at,
            )
            for r in rows
        ),
        sessions,
    )
