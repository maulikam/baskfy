"""The three-weeks-tight sleeve's read surface — what ``/twt`` has been asking for since TW8.

    today(...)      the gate's reading, the names in the state, the open book and the counter
    backtest(...)   the settled run per source, and `01` §8's conditions on reading it

WHY THIS MODULE WAS WRITTEN ON 12 Sep 2026, AND WHAT IT FIXES
-------------------------------------------------------------
``apps/web/src/lib/twt/fetch.ts`` has asked for ``GET /api/v1/twt/today`` since TW8 built the hub
ahead of its data. **Nothing ever served it.** ``readOrNull`` turns the 404 into ``null`` on
purpose — a page that 500s because a job has not run yet is a worse page — so the hub rendered
"Nothing has been read for this strategy yet" whatever the database held.

That was honest while the detection tables were empty. It stopped being honest the moment TW12's
"Scan now" landed: on 12 Sep 2026 Maulik pressed the button twice, both runs finished ``DONE`` in
~19s, and the detector wrote a breadth row, two ``SIGNAL`` rows (IOLCP, OPTIEMUS) and 58 state
rows for the published session of 2026-09-11 — and the page still said nothing had been read.
The scan was never the bug. **The reader did not exist.**

It is worth naming the two things this was *not*, because both were plausible and both were
checked before a line was changed:

* **not a date mismatch.** The writer detects "the latest published session"; :func:`today`
  defaults to the latest session the detector actually wrote a breadth row for, which is the same
  date by construction and cannot drift ahead of it. Neither side asks for "today".
* **not a user mismatch.** ``POST /twt/scan`` and this route resolve the tenant through the same
  :func:`baskfy_api.curated_tenant.scoped_sole_user_id`, so a writer and a reader disagreeing
  about whose book it is would need the sole-tenant id to change between two requests.

TWO CLOCKS, AND THIS SURFACE IS ON THE SLOW ONE
------------------------------------------------
Root ``CLAUDE.md``, "Which date the product shows, and why it is not today": the baskets, the
screener and this sleeve's stored rows are the **last completed trading session**, because a
daily bar is a closed day. So on a Saturday — or at 13:15 on a Wednesday — ``as_of`` reads
Friday, and that is the correct answer rather than a staleness bug. ``04`` §11.1 says the same
thing in this sleeve's own words: there is no "today" bar until today ends.

**Open positions are marked at the last published close, not at a live quote**, and this module
says so in :class:`PositionRow` rather than implying otherwise. ``services/api`` has no quote
path at all — ``get_ltp`` and ``get_quotes`` live only in the desk's ``app/`` — and the same
``CLAUDE.md`` section records what it cost the last time a document claimed a money figure was
live when it was not. The marks come from :func:`baskfy_api.twt_sleeve.load_sleeve`, which is the
arithmetic the evening job and the desk already use, so the hub cannot report an equity the
sleeve does not have.

**No function here writes.** ``02`` Track C §4 — "``apps/web`` gets no route under ``/twt`` that
can reach the gateway" — is asserted at the router by ``test_twt_readonly.py``, and this module is
why that assertion stays cheap: there is no write in it to expose by accident.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import twt_scan, twt_sleeve
from baskfy_api.twt_settings import TwtConfigNotSeeded, read_config
from baskfy_core.models import (
    Instrument,
    TwBacktestRun,
    TwBreadthDaily,
    TwSignalDaily,
    TwStateDaily,
)
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG

#: ``04`` §4.2's gate, as the exact decimal the page prints beside the reading. The config keeps
#: it as a float because that is what the breadth expression compares against; a page renders
#: "the gate opens above 40%" and must never render "40.00000000000001".
THRESHOLD_PCT: Final = Decimal(str(DEFAULT_TWT_CONFIG.breadth.min_pct_above_dma))

#: ``04`` §6.4's first-live discipline: how many entries run at half size in total.
FIRST_LIVE_ENTRIES: Final = DEFAULT_TWT_CONFIG.sizing.first_live_entries

#: Keys of ``tw_breadth_daily.detail`` the funnel is served from. The column ``universe_count`` is
#: *already* "names in the scan universe **with a bar** on the date", so the page's first step —
#: "names in the screened market" — is the wider number the funnel records and nothing else has.
FUNNEL_INSTRUMENTS: Final = "instruments"
FUNNEL_WITH_A_BAR: Final = "with_a_bar"


@dataclasses.dataclass(frozen=True, slots=True)
class GateReading:
    """``03`` §4's row for one session, and the funnel that produced it."""

    date: dt.date | None
    gate: str | None
    pct_above_dma: Decimal | None
    threshold_pct: Decimal
    universe_count: int | None
    with_bar_count: int | None
    measured_count: int | None
    above_count: int | None
    thin_session: bool


@dataclasses.dataclass(frozen=True, slots=True)
class TightRow:
    """One row of ``03`` §2 joined to its ``03`` §3 event, if it had one.

    ``signal_state`` is ``None`` when the name is merely still in the state — the ordinary case,
    and 56 of the 58 rows on 2026-09-11. ``SCAN_ONLY`` rejects are served rather than filtered
    away: a screen that hides what it passed over cannot be audited by the person whose money it
    is (``05`` §1.2).
    """

    instrument_id: int
    symbol: str
    name: str
    close_raw: Decimal
    week_close_0: Decimal
    week_close_1: Decimal
    week_close_2: Decimal
    week_range_pct: Decimal
    month_low_ratio: Decimal
    sessions_in_state: int
    signal_state: str | None
    failed_filters: tuple[str, ...]
    turnover_avg_20: int | None
    locked_upper_circuit: bool


@dataclasses.dataclass(frozen=True, slots=True)
class PositionRow:
    """One ``OPEN`` row of ``03`` §5, marked at the **last published close**.

    ``last_price`` is that mark and not a quote: this service has no quote path, and a page told
    a close was live is a page that misstates a money figure. ``None`` when nothing has printed
    since the fill, which is a reason rather than a zero.
    """

    id: int
    instrument_id: int
    symbol: str
    name: str
    entry_date: dt.date
    entry_avg: Decimal
    quantity_open: int
    high_since: Decimal
    high_since_date: dt.date | None
    gtt_trigger: Decimal | None
    gtt_id: str | None
    next_trigger: Decimal | None
    next_trigger_for: dt.date | None
    last_price: Decimal | None
    unrealised_inr: Decimal | None
    #: A FRACTION, ``unrealised / cost``. ``0.1870`` is 18.70%, converted once in the web app's
    #: ``@/lib/twt/view`` and never in a component.
    unrealised_fraction: Decimal | None
    hold_sessions: int | None
    half_size: bool
    simulated: bool


@dataclasses.dataclass(frozen=True, slots=True)
class HalfSizeCounter:
    """``05`` §1.4 — the first-live discipline as a counter, not a settings page."""

    entries_left: int
    entries_total: int
    execution_enabled: bool


@dataclasses.dataclass(frozen=True, slots=True)
class TodayView:
    """Everything ``/twt`` renders for one session.

    ``as_of`` ``None`` means the detector has never written a session — **not** that the session
    was quiet. The two look identical on a page without the funnel, which is why the funnel is on
    it even at zero.
    """

    as_of: dt.date | None
    gate: GateReading
    tight: tuple[TightRow, ...]
    positions: tuple[PositionRow, ...]
    half_size: HalfSizeCounter
    last_scan: twt_scan.ScanRunView | None


__all__ = [
    "FIRST_LIVE_ENTRIES",
    "THRESHOLD_PCT",
    "GateReading",
    "HalfSizeCounter",
    "PositionRow",
    "TightRow",
    "TodayView",
    "today",
]


async def _latest_breadth(session: AsyncSession, user_id: int) -> TwBreadthDaily | None:
    return (
        await session.execute(
            select(TwBreadthDaily)
            .where(TwBreadthDaily.user_id == user_id)
            .order_by(TwBreadthDaily.date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _funnel_count(reading: TwBreadthDaily, key: str) -> int | None:
    """One step of the stored funnel, or ``None`` when the row predates it.

    Never a fabricated number: the web app's ``funnelSteps`` drops a null step rather than
    printing a zero, because "no reading" and "none of the market" render identically and only
    one of them is a statement about the market.
    """
    detail = reading.detail
    if detail is None:
        return None
    value = detail.get(key)
    return int(value) if isinstance(value, int) else None


def _gate_reading(reading: TwBreadthDaily | None, *, asked_for: dt.date | None) -> GateReading:
    if reading is None:
        return GateReading(
            date=asked_for,
            gate=None,
            pct_above_dma=None,
            threshold_pct=THRESHOLD_PCT,
            universe_count=None,
            with_bar_count=None,
            measured_count=None,
            above_count=None,
            thin_session=False,
        )
    return GateReading(
        date=reading.date,
        gate=reading.gate,
        pct_above_dma=reading.pct_above_dma,
        threshold_pct=THRESHOLD_PCT,
        # The column is the count *with a bar*; the wider universe lives in the funnel alone.
        universe_count=_funnel_count(reading, FUNNEL_INSTRUMENTS),
        with_bar_count=reading.universe_count,
        measured_count=reading.measured_count,
        above_count=reading.above_count,
        thin_session=reading.thin_session,
    )


async def _tight(session: AsyncSession, user_id: int, day: dt.date) -> tuple[TightRow, ...]:
    """Every name in the state on the session, with its event where it had one.

    A LEFT JOIN and not an inner one: ``03`` §3 writes a row only for an **entry event**, and the
    56 names that were merely still tight on 2026-09-11 are exactly what ``05`` §1.2's table is
    for. An inner join would have served two rows out of fifty-eight and looked like it worked.
    """
    result = await session.execute(
        select(TwStateDaily, Instrument.symbol, Instrument.name, TwSignalDaily)
        .join(Instrument, Instrument.id == TwStateDaily.instrument_id)
        .outerjoin(
            TwSignalDaily,
            (TwSignalDaily.user_id == TwStateDaily.user_id)
            & (TwSignalDaily.date == TwStateDaily.date)
            & (TwSignalDaily.instrument_id == TwStateDaily.instrument_id),
        )
        .where(TwStateDaily.user_id == user_id, TwStateDaily.date == day)
        .order_by(TwStateDaily.instrument_id)
    )
    return tuple(
        TightRow(
            instrument_id=state.instrument_id,
            symbol=symbol,
            name=name,
            close_raw=state.close_raw,
            week_close_0=state.week_close_0,
            week_close_1=state.week_close_1,
            week_close_2=state.week_close_2,
            week_range_pct=state.week_range_pct,
            month_low_ratio=state.month_low_ratio,
            sessions_in_state=state.sessions_in_state,
            signal_state=None if signal is None else signal.state,
            failed_filters=() if signal is None else tuple(signal.failed_filters),
            turnover_avg_20=state.turnover_avg_20,
            locked_upper_circuit=state.locked_upper_circuit,
        )
        for state, symbol, name, signal in result.all()
    )


async def _positions(session: AsyncSession, user_id: int, day: dt.date) -> tuple[PositionRow, ...]:
    """The sleeve's own book, marked through ``twt_sleeve`` and nothing else.

    ``04`` §9.1's marks, so the hub, the evening job and the desk cannot each derive a slightly
    different unrealised figure. **Never the account's holdings**: a share this sleeve did not
    buy is invisible here, which is non-negotiable 7's sibling.
    """
    rows = await twt_sleeve.open_positions(session, user_id)
    if not rows:
        return ()
    loaded = await twt_sleeve.load_sleeve(session, user_id, day)
    marked = {value.instrument_id: value for value in loaded.positions}
    marked_on = loaded.marked_on
    found = (
        await session.execute(
            select(Instrument.id, Instrument.symbol, Instrument.name).where(
                Instrument.id.in_([row.instrument_id for row in rows])
            )
        )
    ).all()
    names = {int(ident): (symbol, name) for ident, symbol, name in found}
    out: list[PositionRow] = []
    for row in rows:
        symbol, name = names.get(row.instrument_id, ("", ""))
        value = marked.get(row.instrument_id)
        # A mark taken from the entry is not a price the market has printed since the fill, and
        # serving it as "last price" would invent a quote. `04` §9.1 keeps that fallback for the
        # *equity*; the page is told there is no mark instead, and says so in words.
        printed = value is not None and marked_on.get(row.instrument_id) is not None
        last_price: Decimal | None = None
        unrealised: Decimal | None = None
        fraction: Decimal | None = None
        if value is not None and printed:
            last_price = value.mark
            unrealised = (value.mark - value.entry_avg) * value.quantity_open
            if value.cost != 0:
                fraction = (unrealised / value.cost).quantize(Decimal("0.0001"))
        out.append(
            PositionRow(
                id=int(row.id),
                instrument_id=row.instrument_id,
                symbol=symbol,
                name=name,
                entry_date=row.entry_date,
                entry_avg=row.entry_avg,
                quantity_open=row.quantity_open,
                high_since=row.high_since,
                high_since_date=row.high_since_date,
                gtt_trigger=row.gtt_trigger,
                gtt_id=row.gtt_id,
                next_trigger=row.next_trigger,
                next_trigger_for=row.next_trigger_for,
                last_price=last_price,
                unrealised_inr=unrealised,
                unrealised_fraction=fraction,
                hold_sessions=row.hold_sessions,
                half_size=row.half_size,
                simulated=row.simulated,
            )
        )
    return tuple(out)


async def _half_size(
    session: AsyncSession, user_id: int, *, execution_enabled: bool
) -> HalfSizeCounter:
    try:
        row = await read_config(session, user_id)
    except TwtConfigNotSeeded:
        #: An unseeded row is the full countdown, not an error: no entry has been taken, so none
        #: has been spent. `twt_sleeve.sleeve_capital` answers ₹0 on the same reasoning (TW5.1).
        return HalfSizeCounter(
            entries_left=FIRST_LIVE_ENTRIES,
            entries_total=FIRST_LIVE_ENTRIES,
            execution_enabled=execution_enabled,
        )
    return HalfSizeCounter(
        entries_left=int(row.first_live_entries_left),
        entries_total=FIRST_LIVE_ENTRIES,
        execution_enabled=execution_enabled,
    )


async def today(
    session: AsyncSession,
    *,
    user_id: int,
    day: dt.date | None = None,
    execution_enabled: bool = False,
) -> TodayView:
    """``05`` §1's hub, in one call.

    ``day`` defaults to **the latest session the detector wrote a breadth row for** — not to
    today's date, and not to the latest ``pipeline_run``. Both of those would have the reader
    asking for a session the writer has not detected: on a Saturday "today" has no bar at all,
    and the published pipeline date is only the session the *next* scan will detect. Defaulting
    to the writer's own newest row is the one resolution that cannot disagree with it.

    A named ``day`` with no reading is not an error either — it answers an empty view stamped
    with the date that was asked for, so the page can say which session it found nothing for.
    """
    reading = (
        (
            await session.execute(
                select(TwBreadthDaily).where(
                    TwBreadthDaily.user_id == user_id, TwBreadthDaily.date == day
                )
            )
        ).scalar_one_or_none()
        if day is not None
        else await _latest_breadth(session, user_id)
    )
    as_of = reading.date if reading is not None else day
    half_size = await _half_size(session, user_id, execution_enabled=execution_enabled)
    last_scan = await twt_scan.newest_run(session, user_id=user_id)
    if as_of is None:
        return TodayView(
            as_of=None,
            gate=_gate_reading(None, asked_for=None),
            tight=(),
            positions=(),
            half_size=half_size,
            last_scan=None if last_scan is None else twt_scan.scan_run_view(last_scan),
        )
    return TodayView(
        as_of=as_of,
        gate=_gate_reading(reading, asked_for=as_of),
        tight=await _tight(session, user_id, as_of),
        positions=await _positions(session, user_id, as_of),
        half_size=half_size,
        last_scan=None if last_scan is None else twt_scan.scan_run_view(last_scan),
    )


# --------------------------------------------------------------------------------------------
# TW14: the backtest page's read, added 12 Sep 2026 for the same reason `today` was.
# --------------------------------------------------------------------------------------------
#
# ``apps/web/src/lib/twt/fetch.ts`` has asked for ``GET /api/v1/twt/backtest`` since TW8, exactly
# as it asked for ``/twt/today``, and ``readOrNull`` turned its 404 into ``null`` in exactly the
# same way. The hub's version of that bug was loud — a database holding 58 names rendered as "this
# strategy has never been read". This one is quiet, because ``tw_backtest_run`` really does hold
# **0 rows on the box**, so the page's empty state has been telling the truth by accident. It
# would go on telling it after TW9's job ran, and nobody would be able to tell the two apart.
#
# WHAT IT SERVES, AND WHY IT IS NOT "EVERY ROW"
# ---------------------------------------------
# ``03`` §9 makes the table **append-only**, and every finished row carries a full equity curve —
# one point per session over nine years. A route that served the history would grow without bound
# and would serve megabytes to answer a question about two numbers. ``ix_tw_backtest_run_latest``
# exists for the query the page actually makes, and :func:`backtest` makes exactly that one: the
# latest **finished** run per source. Finished *and* carrying stats, which is the same pair of
# facts ``baskfy_worker.tasks.twt_backtest.latest_finished`` and the page's own ``latestFinished``
# filter on — a run in flight and a failed re-run must not displace the last good number.
#
# WHY AN EMPTY ANSWER CARRIES A REASON
# ------------------------------------
# "No runs" is three different facts and the page cannot tell them apart from an empty list: no
# backtest has ever been asked for, one is running, or the last one failed. On the box today it is
# the first, and a reader owed a number deserves to be told which of the three is keeping it from
# them rather than reading one sentence that is only sometimes true.
#
# WHY THE CAVEATS ARE IN THE PAYLOAD
# ----------------------------------
# House rule 9: "Disclaimers are components, not footers", and ``05`` §3 restates it for this page
# by name — the conditions are read *before* the numbers or they have already failed. The page
# renders them from ``components/twt/caveats.tsx`` and will go on doing so; what this field adds is
# that **the numbers cannot leave this service without them**, for any reader, including one that
# is not that page. ``routers/backtests.py`` already ships a ``DISCLAIMER`` constant beside its
# results on the same reasoning.


#: ``03`` §9's two kinds of run, never mixed and never averaged (``05`` §3). They answer different
#: questions — "does our data produce the study's result" and "does the study reproduce at all" —
#: and an average of the two answers neither.
BACKTEST_SOURCE_PLANT: Final = "PLANT"
BACKTEST_SOURCE_RESEARCH: Final = "RESEARCH_EXPORT"
BACKTEST_SOURCES: Final[tuple[str, ...]] = (BACKTEST_SOURCE_PLANT, BACKTEST_SOURCE_RESEARCH)

#: ``docs/twt/01`` §8, **verbatim**, one string per paragraph with the file's line wrapping
#: collapsed to single spaces and nothing else changed — its numbers, its em-dashes, its markdown
#: emphasis and its cross-references are the document's own.
#:
#: Verbatim is the point and it is checkable: ``gates/twt-backtest-route.md`` B6 normalises the
#: document's whitespace the same way and asserts every string below is a substring of §8, so a
#: caveat that got softened on its way to a reader fails a gate rather than passing a review.
#:
#: **This is the record, not the rendered copy.** The page renders ``BacktestCaveats``, which is
#: §8 in a reader's words under DECISIONS-TW **TW8.2** — the one phrase §8 has that a reader must
#: not be shown is "no backtest **in this repository**", because nothing from the inside of the
#: system reaches a reader's eyes. A renderer that chose to print this field instead of that
#: component owes the same cleaning first; that is written here so the next person does not have
#: to rediscover it.
CAVEATS: Final[tuple[tuple[str, str], ...]] = (
    (
        "trades",
        "**164 trades.** Ten of them are 53 % of gross profit; the best single trade is 11 % of "
        "it. Three years carry the CAGR on 12–15 trades each. Sharpe 1.2 on that trade count is "  # noqa: RUF001 - §8's own en dash, and verbatim is the whole point of this constant
        "a result a different draw of the same market could easily make 0.6, and a one-year "
        "average hold means the 2017–2026 window contains perhaps **fifteen independent "  # noqa: RUF001 - §8's own en dash
        'observations of the book**. Read 20.9 % as "an edge with the right sign and a wide '
        "confidence interval\". The 50-SMA variant's 543 trades and 15.6 % are the more "
        "believable statement of the same thing.",
    ),
    (
        "giveback",
        "**The trail is the strategy, and it is slow.** Average hold 105 sessions, longest 601 "
        "(BOSCHLTD, Aug 2022 → Jan 2025, +78 %). The GTT has to be re-set every session it "
        "ratchets, which is a process, not a signal — and a 20 % give-back on a ₹2.5 lakh line "
        "is a ₹50,000 open loss the book will sit through **as a matter of routine**. Anyone "
        "watching the page needs to have agreed to that in advance; `05` §2 puts the "
        "distance-to-trigger on every open line for exactly this reason.",
    ),
    (
        "history",
        "**Regime and history.** The out-of-sample 36 % is 2023–24; the in-sample 11 % is the "  # noqa: RUF001 - §8's own en dash
        "guide. Modelled fills, 25 bps, no interest on idle cash, sparse corporate actions "
        "before 2024, one history, research code. And §2's reproduction question: the scan "
        "traded here is the one visible at the close, not the one in Chartink's backtest export.",
    ),
    (
        "capital",
        "**One thing no backtest in this repository has measured:** the book at ₹25 lakh. Every "
        "number above was produced at ₹10 lakh, where the 1 %-of-turnover cap binds on nothing. "
        "TW9's run from the plant's own bars is sized against its own parameter, not against the "
        "sleeve's capital, so the two can never be confused — but the first live line is the "
        "first line the cap has ever bound.",
    ),
)

#: What an empty answer says, and it is three different sentences because it is three different
#: facts. ``05`` §3's card says only the middle one, which is right two times in three.
NO_RUN_AT_ALL: Final = (
    "No backtest has been run for this strategy yet. The rule has never been replayed over this "
    "product's own price history, so there is no result to show — which is not the same as a "
    "result of zero."
)
RUN_IN_FLIGHT: Final = (
    "A backtest is running and has not produced a result yet. Only a settled run is shown here, "
    "and there is no earlier settled run to fall back to."
)
RUN_FAILED: Final = (
    "The most recent backtest did not finish successfully, so nothing from it is shown. A failed "
    "run must never displace a settled number, and there is no earlier settled number to show."
)


@dataclasses.dataclass(frozen=True, slots=True)
class BacktestCaveat:
    """One paragraph of ``01`` §8, with the id the page's own panel gives it."""

    id: str
    text: str


@dataclasses.dataclass(frozen=True, slots=True)
class BacktestRunRow:
    """One finished ``tw_backtest_run`` row, as ``05`` §3's card reads it.

    ``params``, ``stats`` and ``drift`` are handed on as the JSONB the run stored, not re-derived:
    every figure in them is already a **decimal string** written by the job (house rule 9, all the
    way to the database), and a route that parsed and re-rendered them would round a second time.
    An absent key inside ``stats`` is the job's honest encoding of "this run could not produce
    that number" (TW9.5) and stays absent here.

    ``drift`` is ``03`` §9's comparison against ``01`` §6 and carries ``flagged`` — TW9's own run
    is flagged, at 22.17 % CAGR and -26.47 % drawdown on 169 trades against the study's 20.92 /
    -24.7 / 164 — so the warning travels with the number rather than being a thing somebody
    remembers to mention.
    """

    id: int
    source: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    params: dict[str, object]
    stats: dict[str, object] | None
    drift: dict[str, object] | None
    error: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class BacktestView:
    """``05`` §3's page: at most one settled run per source, and the conditions on reading them.

    ``reason`` is null whenever ``runs`` is non-empty. When ``runs`` is empty it names which of
    the three absences this is, so a page is never reduced to guessing.
    """

    runs: tuple[BacktestRunRow, ...]
    caveats: tuple[BacktestCaveat, ...]
    reason: str | None


def _backtest_row(row: TwBacktestRun) -> BacktestRunRow:
    return BacktestRunRow(
        id=int(row.id),
        source=row.source,
        started_at=row.started_at,
        finished_at=row.finished_at,
        params=dict(row.params),
        stats=None if row.stats is None else dict(row.stats),
        drift=None if row.drift is None else dict(row.drift),
        error=row.error,
    )


async def _latest_finished_run(
    session: AsyncSession, user_id: int, source: str
) -> TwBacktestRun | None:
    """This book's newest **settled** run of one kind — the query ``ix_tw_backtest_run_latest``
    exists for.

    Finished *and* carrying stats, which is two facts and not one: a run in flight has no
    ``finished_at``; a failed run has one and no ``stats`` (``03`` §9 sets it on failure too, so
    "still running" and "failed" are different states rather than the same silence). Neither may
    displace the last good number. ``baskfy_worker.tasks.twt_backtest.latest_finished`` filters on
    the same pair and so does the page's ``latestFinished``, so no two of the three can disagree.
    """
    return (
        await session.execute(
            select(TwBacktestRun)
            .where(
                TwBacktestRun.user_id == user_id,
                TwBacktestRun.source == source,
                TwBacktestRun.finished_at.is_not(None),
                TwBacktestRun.stats.is_not(None),
            )
            .order_by(TwBacktestRun.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _why_there_is_nothing(session: AsyncSession, user_id: int) -> str:
    """Which of the three absences this is — asked only when there is nothing settled to serve.

    One aggregate over the book's own rows, so the answer is a measurement rather than a guess:
    no row at all, a row still running, or a row that finished without a result.
    """
    counts = (
        await session.execute(
            select(
                func.count(),
                func.count(TwBacktestRun.finished_at),
            ).where(TwBacktestRun.user_id == user_id)
        )
    ).one()
    total, finished = int(counts[0]), int(counts[1])
    if total == 0:
        return NO_RUN_AT_ALL
    if finished < total:
        return RUN_IN_FLIGHT
    return RUN_FAILED


async def backtest(session: AsyncSession, *, user_id: int) -> BacktestView:
    """``05`` §3's page, in one call: the settled run per source, under ``01`` §8's conditions.

    Read-only, like everything else in this module. It selects from one table and writes nothing —
    a backtest is asked for by ``make twt-backtest`` and by the worker's own task, never by a page
    (``02`` Track C §4 keeps this surface to one money-free write and that write is the scan).
    """
    runs = tuple(
        _backtest_row(row)
        for row in (
            found
            for found in [
                await _latest_finished_run(session, user_id, source) for source in BACKTEST_SOURCES
            ]
            if found is not None
        )
    )
    caveats = tuple(BacktestCaveat(id=key, text=text) for key, text in CAVEATS)
    return BacktestView(
        runs=runs,
        caveats=caveats,
        reason=None if runs else await _why_there_is_nothing(session, user_id),
    )
