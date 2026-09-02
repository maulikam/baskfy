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

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import (
    Instrument,
    OhlcvDaily,
    SwMarketDaily,
    SwSetupDaily,
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


@dataclass(frozen=True, slots=True)
class SectorRow:
    slug: str
    pct_above_ma_slow: float
    members: int
    candidates: int
    hot: bool


@dataclass(frozen=True, slots=True)
class BarPoint:
    date: dt.date
    close: Decimal
    ma_fast: Decimal | None
    ma_slow: Decimal | None


async def latest_setup_date(session: AsyncSession, user_id: int) -> dt.date | None:
    """The most recent day the detectors wrote anything for this user.

    Deliberately **not** the pipeline's `as_of`: the swing step can be skipped (a failed quality
    gate, a deployment with no tenant) while the screener publishes normally, and a page that
    asked for the published date would then render an empty day rather than the last real one.
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
    as_of = on or await latest_setup_date(session, user_id)
    if as_of is None:
        return SetupsPage(None, (), None, None, None, None, None, None)

    query = _setup_query(user_id, as_of)
    if setup is not None:
        query = query.where(SwSetupDaily.setup == setup)
    if status is not None:
        query = query.where(SwSetupDaily.status == status)

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
        )
        for row, symbol, name in (await session.execute(query)).all()
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
        )
        for row in rows
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
