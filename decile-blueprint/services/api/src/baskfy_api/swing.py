"""What the `/swing` surfaces read — the queries behind `docs/swing/05` §2.

The split is the one `market_data.py` uses: this module owns the SQL and the shapes, the router
owns the HTTP. Nothing here computes a number the detector already computed — `sw_setup_daily`
and `sw_market_daily` are snapshots, and a page that recomputed them would be able to disagree
with the record of what the system saw on the morning a trade was taken.

**Read-only, structurally.** There is no INSERT, UPDATE or DELETE in this file. The one thing the
web app may write on `/swing` is the watchlist (SW5), and that lives elsewhere for exactly this
reason: `services/api/tests/test_swing_readonly.py` scans this module and its router the way
`test_desk_readonly.py` scans the desk's.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.swing_catalyst import CatalystView, latest_for
from baskfy_core.models import (
    Instrument,
    OhlcvDaily,
    SwMarketDaily,
    SwPlan,
    SwPlanLine,
    SwPlanSkip,
    SwPosition,
    SwScanRun,
    SwSetupDaily,
    SwSignal,
)
from baskfy_core.swing.config import Setup
from baskfy_core.swing.setups import CandidateStatus

#: How many bars the mini chart on a candidate row draws (`docs/swing/05` §2: "a 130-bar mini
#: chart per row"). Six months of sessions — enough to show the pole, the base and the pivot in
#: one picture, which is the whole point of putting a chart next to a number.
MINI_CHART_BARS: int = 130

#: The widest span `/swing/market` will answer in one call. The market page's longest preset is a
#: year; a request for a decade is a mistake, and answering it slowly is worse than refusing.
MAX_MARKET_SPAN: dt.timedelta = dt.timedelta(days=400)

SETUPS: frozenset[str] = frozenset(setup.value for setup in Setup)
STATUSES: frozenset[str] = frozenset(status.value for status in CandidateStatus)


@dataclass(frozen=True, slots=True)
class SetupRow:
    """One candidate, joined to the instrument a person recognises it by."""

    instrument_id: int
    symbol: str
    name: str
    setup: str
    status: str
    score: Decimal
    close: Decimal | None
    trigger: Decimal | None
    stop_ref: Decimal | None
    pivot_high: Decimal | None
    adr_pct: Decimal | None
    prior_move_pct: Decimal | None
    base_depth_pct: Decimal | None
    tightness_adr: Decimal | None
    dryup_ratio: Decimal | None
    rvol: Decimal | None
    gap_pct: Decimal | None
    turnover_avg: int | None
    base_bars: int | None
    up_streak: int | None
    locked_upper_circuit: bool
    sector_slug: str | None
    listed_within_2y: bool
    #: SW11B (A3): the newest filing's headline / stamp / link and the earnings date, from
    #: `sw_catalyst`; `None` when the feed has nothing for the name.
    catalyst_feed: CatalystView | None = None

    @property
    def stop_distance_pct(self) -> Decimal | None:
        """How far the stop reference sits below the trigger, as a percentage of the trigger.

        Computed here rather than stored, because it is a *ratio of two stored numbers* and
        storing it would give the page a third place to disagree with. Rounded to the same two
        places everything else on the row carries.
        """
        if self.trigger is None or self.stop_ref is None or self.trigger <= 0:
            return None
        return ((self.trigger - self.stop_ref) / self.trigger * 100).quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class SetupsPage:
    """A day's candidates, with the funnel that explains an empty one.

    `docs/swing/05` §2: "Empty states say why: 'No flags today — 41 names were liquid, 0 met the
    base rules'". Without the funnel an empty page is indistinguishable from a job that did not
    run, and those need opposite responses.
    """

    as_of: dt.date | None
    rows: tuple[SetupRow, ...]
    funnel: dict[str, object] | None
    gate: str | None
    exposure_level: int | None
    max_open_positions: int | None
    max_exposure_pct: Decimal | None
    new_entries_allowed: bool | None
    #: SW15: the day's rows were detected on bars built from live quotes ("Scan now" during
    #: the session), not on a published close — the header says so on every row.
    as_of_provisional: bool = False
    #: When the day was last scanned on demand (`sw_market_daily.detail.scan.scanned_at`);
    #: ``None`` for a day the nightly wrote.
    scanned_at: dt.datetime | None = None
    #: This user's newest "Scan now" run, whatever its state — so the page can say
    #: "scanning…" or "the last scan failed: …" without a second call.
    last_scan: ScanRunView | None = None


@dataclass(frozen=True, slots=True)
class ScanRunView:
    """One `sw_scan_run` row, as `GET /swing/scan/{id}` and the setups page read it."""

    run_id: int
    status: str
    requested_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    session_date: dt.date | None
    provisional: bool
    funnel: dict[str, object] | None
    detail: dict[str, object] | None
    error: str | None


def scan_run_view(row: SwScanRun) -> ScanRunView:
    detail = row.detail if isinstance(row.detail, dict) else None
    funnel = detail.get("funnel") if detail is not None else None
    return ScanRunView(
        run_id=int(row.id),
        status=str(row.status),
        requested_at=row.requested_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        session_date=row.session_date,
        provisional=bool(row.provisional),
        funnel=funnel if isinstance(funnel, dict) else None,
        detail=detail,
        error=row.error,
    )


async def scan_run(session: AsyncSession, *, user_id: int, run_id: int) -> ScanRunView | None:
    row = (
        await session.execute(
            select(SwScanRun).where(SwScanRun.user_id == user_id, SwScanRun.id == run_id)
        )
    ).scalar_one_or_none()
    return None if row is None else scan_run_view(row)


async def latest_scan_run(session: AsyncSession, *, user_id: int) -> ScanRunView | None:
    """The newest run by request time — in flight, done or failed alike."""
    row = (
        await session.execute(
            select(SwScanRun)
            .where(SwScanRun.user_id == user_id)
            .order_by(SwScanRun.requested_at.desc(), SwScanRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return None if row is None else scan_run_view(row)


def _scanned_at(detail: object) -> dt.datetime | None:
    if not isinstance(detail, dict):
        return None
    scan = detail.get("scan")
    stamp = scan.get("scanned_at") if isinstance(scan, dict) else None
    if not isinstance(stamp, str):
        return None
    try:
        return dt.datetime.fromisoformat(stamp)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class MarketRow:
    date: dt.date
    constituent_count: int
    pct_up_strong_1m: Decimal | None
    pct_new_52w_high: Decimal | None
    pct_above_ma_slow: Decimal | None
    index_slug: str | None
    index_close: Decimal | None
    index_ma_fast: Decimal | None
    index_ma_slow: Decimal | None
    gate: str
    exposure_level: int
    max_open_positions: int
    max_exposure_pct: Decimal
    new_entries_allowed: bool
    parabolic_count: int
    detail: dict[str, object] | None
    #: `04` §8.5 (SW9.5): how far the allocation sits below its peak that evening, and whether
    #: the lock-out was in force. Written by `swing-eod`; a page shows them in place of the rung.
    drawdown_pct: Decimal = Decimal(0)
    drawdown_locked: bool = False


@dataclass(frozen=True, slots=True)
class SignalRow:
    """One verdict the monitor raised (`sw_signal`), joined to its instrument.

    `docs/swing/05` §2 shows yesterday's rows under a watchlist name — "fired 09:23, 5-min range
    412.30 to 418.90" — and every state is a row, not only the triggers: a `LOCKED_UPPER_CIRCUIT`
    is the record of why no trade happened. Nothing here is an order, and the read carries the
    `plan_line_id` only so a page can say a line was made; it cannot reach the line.
    """

    id: int
    watch_id: int | None
    instrument_id: int
    symbol: str
    name: str
    setup: str
    session_date: dt.date
    raised_at: dt.datetime
    state: str
    or_window_minutes: int | None
    range_high: Decimal | None
    range_low: Decimal | None
    low_of_day: Decimal | None
    last_price: Decimal | None
    entry: Decimal | None
    stop: Decimal | None
    plan_line_id: int | None


@dataclass(frozen=True, slots=True)
class SectorRow:
    slug: str
    pct_above_ma_slow: float
    members: int
    candidates: int
    hot: bool


@dataclass(frozen=True, slots=True)
class PlanLineRow:
    """One line of a stored plan, joined to the symbol it names."""

    id: int
    kind: str
    symbol: str
    name: str
    setup: str | None
    quantity: int
    trigger: Decimal | None
    stop: Decimal | None
    risk_inr: Decimal
    position_value: Decimal
    trail: str | None
    note: str | None
    state: str


@dataclass(frozen=True, slots=True)
class PlanView:
    """A plan and everything a person needs to read it — including what it refused.

    `04` §9: "A watchlist of twelve names and a plan of two lines is only useful if the other ten
    explain themselves." The skips are not an appendix; they are half the document.
    """

    plan_id: str
    as_of: dt.date
    source: str
    built_at: dt.datetime
    expires_at: dt.datetime
    gate: str
    exposure_level: int
    total_risk_inr: Decimal
    total_new_exposure_inr: Decimal
    lines: tuple[PlanLineRow, ...]
    skips: tuple[tuple[str, str, str | None], ...]


@dataclass(frozen=True, slots=True)
class PositionRow:
    """One open or closed position, as `05` §2's Positions tab reads it."""

    id: int
    instrument_id: int
    symbol: str
    name: str
    setup: str
    entry_date: dt.date
    entry_avg: Decimal
    quantity_entered: int
    quantity_open: int
    initial_stop: Decimal
    stop: Decimal
    gtt_id: str | None
    trail: str
    partial_done: bool
    state: str
    closed_on: dt.date | None
    exit_avg: Decimal | None
    close_reason: str | None
    r_multiple: Decimal | None
    pnl_inr: Decimal | None
    simulated: bool
    last_close: Decimal | None

    @property
    def naked(self) -> bool:
        """Open, with no resting stop. The one state `04` §6 forbids outright."""
        return self.quantity_open > 0 and self.gtt_id is None


@dataclass(frozen=True, slots=True)
class BarPoint:
    date: dt.date
    close: Decimal
    ma_fast: Decimal | None
    ma_slow: Decimal | None


async def latest_setup_date(session: AsyncSession, user_id: int) -> dt.date | None:
    """The most recent day a **candidate** was written for this user.

    Kept because "when did this book last flag anything" is a real question, but it is **not**
    the page's clock — see :func:`latest_detected_date`. A session on which nothing met the bar
    writes no `sw_setup_daily` row at all, so this answers the last *interesting* session rather
    than the last session the detector ran.
    """
    return (
        await session.execute(
            select(func.max(SwSetupDaily.date)).where(SwSetupDaily.user_id == user_id)
        )
    ).scalar_one_or_none()


async def latest_market_date(session: AsyncSession, user_id: int) -> dt.date | None:
    return (
        await session.execute(
            select(func.max(SwMarketDaily.date)).where(SwMarketDaily.user_id == user_id)
        )
    ).scalar_one_or_none()


async def latest_detected_date(session: AsyncSession, user_id: int) -> dt.date | None:
    """The most recent session the **detector ran** for this user — the page's clock.

    `gates/sleeve-read-contract.md` C1. This used to be `max(sw_setup_daily.date)`, and that is a
    table `baskfy_worker.tasks.swing._detect_swing` leaves empty on any session where nothing met
    the bar, while `write_market_row` runs unconditionally — "no flags today" is a fact worth
    writing. So on a zero-candidate session the writer wrote today and the reader answered
    yesterday: **yesterday's triggers and yesterday's gate, stamped yesterday, on a page that
    looked perfectly current.** Not hypothetical — on the box the gate has been RED since
    2026-09-04 and the candidate count walked 21 → 22 → 16 → 19 → 12 → 9.

    The rule here is the one VBT already follows (`baskfy_api.vbt._latest_breadth`, keyed on
    `vb_breadth_daily`) and the one TWT's reader was built to (`baskfy_api.twt._latest`, keyed on
    `tw_breadth_daily`): **key the clock on the row the detector writes every session, whatever
    the tape did.** For swing that row is `sw_market_daily`.

    Deliberately **not** the pipeline's `as_of`: the swing step can be skipped (a failed quality
    gate, a deployment with no tenant) while the screener publishes normally, and a page that
    asked for the published date would then render an empty day rather than the last real one.

    The fall back to `max(sw_setup_daily.date)` covers a book whose setups predate its market
    rows; when both exist the market row is at or ahead of the setups, so the fallback never
    *lowers* the answer.
    """
    detected = await latest_market_date(session, user_id)
    if detected is not None:
        return detected
    return await latest_setup_date(session, user_id)


def _setup_query(user_id: int, on: dt.date) -> Select[tuple[SwSetupDaily, str, str]]:
    return (
        select(SwSetupDaily, Instrument.symbol, Instrument.name)
        .join(Instrument, Instrument.id == SwSetupDaily.instrument_id)
        .where(SwSetupDaily.user_id == user_id, SwSetupDaily.date == on)
        .order_by(SwSetupDaily.setup, SwSetupDaily.score.desc(), Instrument.symbol)
    )


async def setups(
    session: AsyncSession,
    *,
    user_id: int,
    on: dt.date | None = None,
    setup: str | None = None,
    status: str | None = None,
) -> SetupsPage:
    """The day's candidates, and the market row that says what the book may do with them.

    ``setup`` and ``status`` filter; neither widens. A filter that matched nothing still returns
    the funnel, because "no EPs today" is a fact about the market and "no rows at all" is a fact
    about the job.
    """
    as_of = on or await latest_detected_date(session, user_id)
    last_scan = await latest_scan_run(session, user_id=user_id)
    if as_of is None:
        return SetupsPage(None, (), None, None, None, None, None, None, last_scan=last_scan)

    query = _setup_query(user_id, as_of)
    if setup is not None:
        query = query.where(SwSetupDaily.setup == setup)
    if status is not None:
        query = query.where(SwSetupDaily.status == status)

    found = (await session.execute(query)).all()
    feed = await latest_for(
        session, user_id=user_id, instrument_ids=[row.instrument_id for row, _, _ in found]
    )
    rows = tuple(
        SetupRow(
            instrument_id=row.instrument_id,
            symbol=symbol,
            name=name,
            setup=row.setup,
            status=row.status,
            score=row.score,
            close=row.close,
            trigger=row.trigger,
            stop_ref=row.stop_ref,
            pivot_high=row.pivot_high,
            adr_pct=row.adr_pct,
            prior_move_pct=row.prior_move_pct,
            base_depth_pct=row.base_depth_pct,
            tightness_adr=row.tightness_adr,
            dryup_ratio=row.dryup_ratio,
            rvol=row.rvol,
            gap_pct=row.gap_pct,
            turnover_avg=row.turnover_avg,
            base_bars=row.base_bars,
            up_streak=row.up_streak,
            locked_upper_circuit=row.locked_upper_circuit,
            sector_slug=row.sector_slug,
            listed_within_2y=row.listed_within_2y,
            catalyst_feed=feed.get(row.instrument_id),
        )
        for row, symbol, name in found
    )

    market = (
        await session.execute(
            select(SwMarketDaily).where(
                SwMarketDaily.user_id == user_id, SwMarketDaily.date == as_of
            )
        )
    ).scalar_one_or_none()
    detail = market.detail if market is not None else None
    funnel = detail.get("funnel") if isinstance(detail, dict) else None
    return SetupsPage(
        as_of=as_of,
        rows=rows,
        funnel=funnel if isinstance(funnel, dict) else None,
        gate=market.gate if market is not None else None,
        exposure_level=market.exposure_level if market is not None else None,
        max_open_positions=market.max_open_positions if market is not None else None,
        max_exposure_pct=market.max_exposure_pct if market is not None else None,
        new_entries_allowed=market.new_entries_allowed if market is not None else None,
        as_of_provisional=(market is not None and bool(market.provisional))
        or any(bool(row.provisional) for row, _, _ in found),
        scanned_at=_scanned_at(detail),
        last_scan=last_scan,
    )


async def market_history(
    session: AsyncSession,
    *,
    user_id: int,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> tuple[MarketRow, ...]:
    """`sw_market_daily` over a span, oldest first — the market page's three series and the band."""
    query = select(SwMarketDaily).where(SwMarketDaily.user_id == user_id)
    if start is not None:
        query = query.where(SwMarketDaily.date >= start)
    if end is not None:
        query = query.where(SwMarketDaily.date <= end)
    rows = (await session.execute(query.order_by(SwMarketDaily.date))).scalars().all()
    return tuple(
        MarketRow(
            date=row.date,
            constituent_count=row.constituent_count,
            pct_up_strong_1m=row.pct_up_strong_1m,
            pct_new_52w_high=row.pct_new_52w_high,
            pct_above_ma_slow=row.pct_above_ma_slow,
            index_slug=row.index_slug,
            index_close=row.index_close,
            index_ma_fast=row.index_ma_fast,
            index_ma_slow=row.index_ma_slow,
            gate=row.gate,
            exposure_level=row.exposure_level,
            max_open_positions=row.max_open_positions,
            max_exposure_pct=row.max_exposure_pct,
            new_entries_allowed=row.new_entries_allowed,
            detail=row.detail if isinstance(row.detail, dict) else None,
            parabolic_count=row.parabolic_count,
            drawdown_pct=row.drawdown_pct,
            drawdown_locked=row.drawdown_locked,
        )
        for row in rows
    )


async def latest_signal_date(session: AsyncSession, user_id: int) -> dt.date | None:
    """The newest session the monitor wrote a verdict for — "yesterday", on a page."""
    return (
        await session.execute(
            select(func.max(SwSignal.session_date)).where(SwSignal.user_id == user_id)
        )
    ).scalar_one_or_none()


async def signals(
    session: AsyncSession,
    *,
    user_id: int,
    on: dt.date | None = None,
    instrument_id: int | None = None,
) -> tuple[dt.date | None, tuple[SignalRow, ...]]:
    """A session's `sw_signal` rows, newest first; the latest session when ``on`` is absent.

    Append-only rows read back as they were written: the page renders the range and the levels
    with their stored digits, and nothing is recomputed. An empty session is `(date, ())`, and a
    tenant with no signals at all is `(None, ())` — the surface exists before the monitor has run.
    """
    as_of = on or await latest_signal_date(session, user_id)
    if as_of is None:
        return None, ()
    query = (
        select(SwSignal, Instrument.symbol, Instrument.name)
        .join(Instrument, Instrument.id == SwSignal.instrument_id)
        .where(SwSignal.user_id == user_id, SwSignal.session_date == as_of)
        .order_by(SwSignal.raised_at.desc(), SwSignal.id.desc())
    )
    if instrument_id is not None:
        query = query.where(SwSignal.instrument_id == instrument_id)
    found = (await session.execute(query)).all()
    return as_of, tuple(
        SignalRow(
            id=row.id,
            watch_id=row.watch_id,
            instrument_id=row.instrument_id,
            symbol=symbol,
            name=name,
            setup=row.setup,
            session_date=row.session_date,
            raised_at=row.raised_at,
            state=row.state,
            or_window_minutes=row.or_window_minutes,
            range_high=row.range_high,
            range_low=row.range_low,
            low_of_day=row.low_of_day,
            last_price=row.last_price,
            entry=row.entry,
            stop=row.stop,
            plan_line_id=row.plan_line_id,
        )
        for row, symbol, name in found
    )


async def sectors(
    session: AsyncSession, *, user_id: int, on: dt.date | None = None
) -> tuple[dt.date | None, tuple[SectorRow, ...]]:
    """The strip: sector breadth for the day, with how many of today's candidates sit in each.

    The breadth numbers were computed by the detection job over its own liquid universe and
    stored in `sw_market_daily.detail.sectors` (DECISIONS-SW SW3.1). The candidate counts are
    counted here, because they are a property of the day's rows rather than of the market.
    """
    as_of = on or await latest_market_date(session, user_id)
    if as_of is None:
        return None, ()
    row = (
        await session.execute(
            select(SwMarketDaily).where(
                SwMarketDaily.user_id == user_id, SwMarketDaily.date == as_of
            )
        )
    ).scalar_one_or_none()
    if row is None or not isinstance(row.detail, dict):
        return as_of, ()
    listed = row.detail.get("sectors")
    if not isinstance(listed, list):
        return as_of, ()

    counted = (
        await session.execute(
            select(SwSetupDaily.sector_slug, func.count())
            .where(
                SwSetupDaily.user_id == user_id,
                SwSetupDaily.date == as_of,
                SwSetupDaily.sector_slug.is_not(None),
            )
            .group_by(SwSetupDaily.sector_slug)
        )
    ).all()
    counts: dict[str, int] = {str(slug): int(total) for slug, total in counted}
    # `04` §2.6's bonus goes to the top three, so the strip marks exactly those three as hot —
    # read from the stored order rather than re-sorted, so the page and the score agree.
    result: list[SectorRow] = []
    for index, entry in enumerate(listed):
        if not isinstance(entry, dict):
            continue
        slug = str(entry.get("slug", ""))
        result.append(
            SectorRow(
                slug=slug,
                pct_above_ma_slow=float(entry.get("pct_above_ma_slow") or 0.0),
                members=int(entry.get("members") or 0),
                candidates=int(counts.get(slug, 0)),
                hot=index < HOT_SECTORS_ON_THE_STRIP,
            )
        )
    return as_of, tuple(result)


#: `04` §2.6 gives its `+5` to "a sector in the top-3 breadth strip", so three is what the page
#: marks. `05` §2 shows five; the extra two are context, not a bonus.
HOT_SECTORS_ON_THE_STRIP: int = 3


async def latest_plan(
    session: AsyncSession, *, user_id: int, source: str | None = None
) -> PlanView | None:
    """The most recent plan, with its lines and its skips.

    Newest by `built_at`, not by `as_of`: a morning plan and an evening preview can share a date,
    and the one a person wants is the one that was built last.
    """
    query = select(SwPlan).where(SwPlan.user_id == user_id)
    if source is not None:
        query = query.where(SwPlan.source == source)
    plan = (
        await session.execute(query.order_by(SwPlan.built_at.desc(), SwPlan.id.desc()).limit(1))
    ).scalar_one_or_none()
    if plan is None:
        return None

    lines = (
        await session.execute(
            select(SwPlanLine, Instrument.symbol, Instrument.name)
            .join(Instrument, Instrument.id == SwPlanLine.instrument_id)
            .where(SwPlanLine.plan_id == plan.id)
            .order_by(SwPlanLine.id)
        )
    ).all()
    skips = (
        await session.execute(
            select(SwPlanSkip.symbol, SwPlanSkip.reason, SwPlanSkip.detail)
            .where(SwPlanSkip.plan_id == plan.id)
            .order_by(SwPlanSkip.symbol)
        )
    ).all()
    return PlanView(
        plan_id=str(plan.plan_id),
        as_of=plan.as_of,
        source=plan.source,
        built_at=plan.built_at,
        expires_at=plan.expires_at,
        gate=plan.gate,
        exposure_level=plan.exposure_level,
        total_risk_inr=plan.total_risk_inr,
        total_new_exposure_inr=plan.total_new_exposure_inr,
        lines=tuple(
            PlanLineRow(
                id=row.id,
                kind=row.kind,
                symbol=symbol,
                name=name,
                setup=row.setup,
                quantity=row.quantity,
                trigger=row.trigger,
                stop=row.stop,
                risk_inr=row.risk_inr,
                position_value=row.position_value,
                trail=row.trail,
                note=row.note,
                state=row.state,
            )
            for row, symbol, name in lines
        ),
        skips=tuple((str(symbol), str(reason), detail) for symbol, reason, detail in skips),
    )


async def positions(
    session: AsyncSession, *, user_id: int, include_closed: bool = True
) -> tuple[PositionRow, ...]:
    """The book: open first, then closed, newest first within each."""
    query = (
        select(SwPosition, Instrument.symbol, Instrument.name)
        .join(Instrument, Instrument.id == SwPosition.instrument_id)
        .where(SwPosition.user_id == user_id)
        .order_by(SwPosition.state, SwPosition.entry_date.desc(), SwPosition.id.desc())
    )
    if not include_closed:
        query = query.where(SwPosition.state != "CLOSED")
    rows = (await session.execute(query)).all()
    if not rows:
        return ()
    closes = await _latest_closes(session, [row[0].instrument_id for row in rows])
    return tuple(
        PositionRow(
            id=row.id,
            instrument_id=row.instrument_id,
            symbol=symbol,
            name=name,
            setup=row.setup,
            entry_date=row.entry_date,
            entry_avg=row.entry_avg,
            quantity_entered=row.quantity_entered,
            quantity_open=row.quantity_open,
            initial_stop=row.initial_stop,
            stop=row.stop,
            gtt_id=row.gtt_id,
            trail=row.trail,
            partial_done=row.partial_done,
            state=row.state,
            closed_on=row.closed_on,
            exit_avg=row.exit_avg,
            close_reason=row.close_reason,
            r_multiple=row.r_multiple,
            pnl_inr=row.pnl_inr,
            simulated=row.simulated,
            last_close=closes.get(row.instrument_id),
        )
        for row, symbol, name in rows
    )


async def _latest_closes(session: AsyncSession, instrument_ids: list[int]) -> dict[int, Decimal]:
    """The most recent adjusted close per instrument, in one grouped query."""
    if not instrument_ids:
        return {}
    newest = (
        select(
            OhlcvDaily.instrument_id.label("instrument_id"),
            func.max(OhlcvDaily.date).label("date"),
        )
        .where(OhlcvDaily.instrument_id.in_(instrument_ids))
        .group_by(OhlcvDaily.instrument_id)
        .subquery()
    )
    rows = await session.execute(
        select(OhlcvDaily.instrument_id, OhlcvDaily.close).join(
            newest,
            and_(
                OhlcvDaily.instrument_id == newest.c.instrument_id,
                OhlcvDaily.date == newest.c.date,
            ),
        )
    )
    return {int(instrument_id): close for instrument_id, close in rows.all()}


async def bars(
    session: AsyncSession,
    *,
    instrument_id: int,
    end: dt.date | None = None,
    count: int | None = None,
) -> tuple[BarPoint, ...]:
    """The mini chart's series: the last ``count`` closes with their 10- and 20-day averages.

    Adjusted closes, deliberately. The chart's job is to show the *shape* — the pole, the base,
    the pivot — and a raw series with a split in it shows a cliff that never happened. The
    numbers a person acts on (the trigger, the stop) are exchange prices on the row beside it.
    """
    limit = count or MINI_CHART_BARS
    query = (
        select(OhlcvDaily.date, OhlcvDaily.close)
        .where(OhlcvDaily.instrument_id == instrument_id)
        .order_by(OhlcvDaily.date.desc())
        .limit(limit)
    )
    if end is not None:
        query = query.where(OhlcvDaily.date <= end)
    rows = list(reversed((await session.execute(query)).all()))
    closes = [Decimal(row[1]) for row in rows]
    return tuple(
        BarPoint(
            date=row[0],
            close=closes[index],
            ma_fast=_mean(closes, index, 10),
            ma_slow=_mean(closes, index, 20),
        )
        for index, row in enumerate(rows)
    )


def _mean(values: list[Decimal], index: int, window: int) -> Decimal | None:
    """The trailing mean, or ``None`` before the window is full.

    ``None`` rather than a partial average: a "10-day average" computed from four days is a
    different statistic, and drawing it on the same line as the real one makes the first fortnight
    of every chart a lie.
    """
    if index + 1 < window:
        return None
    window_values = values[index + 1 - window : index + 1]
    return (sum(window_values, Decimal(0)) / window).quantize(Decimal("0.01"))
