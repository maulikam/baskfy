"""The day's candidates across the sleeves — one row per stock, from what was stored. Never re-run.

The inverse of :mod:`baskfy_api.instrument_appearances`: that module answers "where does *this*
stock appear", this one answers "which stocks appear *today*, and on how many scans". Both read
the same tables at the same sessions, so a name the instrument page says is on three scans is a
name this page lists with a count of three.

**What a row carries is what the scans wrote, restated — never a new number.** Each strategy
contributes the short fact its own page shows (``EP · GAP_DAY``, ``signal``, ``tight 4 sessions``),
whether that fact is one its plan builder could act on, and the exchange print it was computed
from. The row's ``strategy_count`` is arithmetic over those facts; there is no combined score, no
cross-strategy rank and no filter a strategy did not apply itself. `/build/overlap` used to
compute this membership in the web app from three separate page reads and showed symbols only —
"check each name on Volume breakout, Swing, Three weeks tight, or the screen itself before
acting". This read serves the same membership with the facts a person was being sent away for.

**Sessions are the strategies' own.** Swing at the last session its detector wrote (the market
row, C1), Volume breakout and Three weeks tight at their newest breadth row. They can differ — a
sleeve whose nightly step was skipped is a day behind — so the view names each one rather than
pretending to a single "as of".

**The catalyst feed is the swing sleeve's, on loan.** ``sw_catalyst`` holds a headline, a stamp
and a link for the watched and EP names only (SW11B, A3), and the earnings date the calendar
lists. A row outside that feed has no catalyst here, and that is the honest state — the feed was
never widened for this page. Nothing here reproduces a filing: it is a link out and a date.

**Actionable means the strategy's own plan builder could take the row**, nothing more: a FLAG or
EP setup for swing (`TRADEABLE_SETUPS`; PARABOLIC_SHORT is detect-only), a ``SIGNAL`` for the
other two (``SCAN_ONLY`` failed a filter, an in-state tight name has no entry event). It is a
display flag. No order path reads this module, and `test_overlap_readonly.py` keeps it so.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.swing import latest_detected_date
from baskfy_api.swing_catalyst import CatalystView, latest_for
from baskfy_core.catalyst_tags import CatalystTag, tag_headline
from baskfy_core.models import (
    Instrument,
    Screen,
    ScreenRun,
    SwSetupDaily,
    TwBreadthDaily,
    TwSignalDaily,
    TwStateDaily,
    VbBreadthDaily,
    VbSignalDaily,
)
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_core.swing.config import TRADEABLE_SETUPS

Scope = Literal["actionable", "all"]

#: ``vb_signal_daily.state`` in the page's words — the same two phrases the instrument page uses.
_VBT_STATE = {"SIGNAL": "signal", "SCAN_ONLY": "scanned, did not pass the filters"}


class Strategy(StrEnum):
    SWING = "swing"
    VOLUME_BREAKOUT = "volume_breakout"
    THREE_WEEKS_TIGHT = "three_weeks_tight"


#: The reader's name and the page each strategy links to — the instrument page's vocabulary.
STRATEGY_NAMES: dict[Strategy, str] = {
    Strategy.SWING: "Swing",
    Strategy.VOLUME_BREAKOUT: "Volume breakout",
    Strategy.THREE_WEEKS_TIGHT: "Three weeks tight",
}
STRATEGY_REFS: dict[Strategy, str] = {
    Strategy.SWING: "/swing",
    Strategy.VOLUME_BREAKOUT: "/vbt",
    Strategy.THREE_WEEKS_TIGHT: "/twt",
}


@dataclass(frozen=True, slots=True)
class StrategyHit:
    """One strategy's fact about one stock, on that strategy's latest session."""

    strategy: Strategy
    as_of: dt.date
    #: In the strategy's own words: "EP · GAP_DAY", "signal", "tight 4 sessions · signal".
    detail: str
    #: The strategy's plan builder could take this row as it stands. A display flag only.
    actionable: bool
    #: The exchange print the row was computed from (``close_raw``; swing serves its own).
    close: Decimal | None

    @property
    def name(self) -> str:
        return STRATEGY_NAMES[self.strategy]

    @property
    def ref(self) -> str:
        return STRATEGY_REFS[self.strategy]


@dataclass(frozen=True, slots=True)
class ScreenHit:
    """The stock's place on a screen's newest stored run — the instrument page's row, inverted."""

    name: str
    public_id: str
    is_template: bool
    as_of: dt.date
    rank: int | None
    of: int | None
    #: The screen was edited after this run was stored; the rank is the earlier version's.
    definition_changed: bool = False


@dataclass(frozen=True, slots=True)
class CandidateRow:
    instrument_id: int
    symbol: str
    name: str
    #: The first exchange print the strategies offer, in the order swing, volume, tight. A
    #: display figure: each strategy's page still shows its own.
    close: Decimal | None
    strategies: tuple[StrategyHit, ...]
    screens: tuple[ScreenHit, ...]
    #: The swing feed's newest announcement and earnings date, when the feed has the name.
    catalyst: CatalystView | None
    #: The rules baseline's word on that headline (`baskfy_core.catalyst_tags`): display context,
    #: never an input. ``None`` exactly when there is no headline to read.
    catalyst_tag: CatalystTag | None = None

    @property
    def strategy_count(self) -> int:
        return len(self.strategies)

    @property
    def actionable(self) -> bool:
        return any(hit.actionable for hit in self.strategies)


@dataclass(frozen=True, slots=True)
class OverlapView:
    #: Each strategy's own latest session; ``None`` when its detector has never written one.
    sessions: dict[Strategy, dt.date | None]
    rows: tuple[CandidateRow, ...]
    #: The strategies were read at all — false for a caller who is not the sole tenant.
    strategies_read: bool
    screens_checked: int


def _detail_swing(row: SwSetupDaily) -> str:
    return f"{row.setup} · {row.status}"


async def _swing(
    session: AsyncSession, *, user_id: int
) -> tuple[dt.date | None, list[tuple[Instrument, StrategyHit]]]:
    day = await latest_detected_date(session, user_id)
    if day is None:
        return None, []
    found = (
        await session.execute(
            select(SwSetupDaily, Instrument)
            .join(Instrument, Instrument.id == SwSetupDaily.instrument_id)
            .where(SwSetupDaily.user_id == user_id, SwSetupDaily.date == day)
            .order_by(SwSetupDaily.instrument_id, SwSetupDaily.setup)
        )
    ).all()
    hits: list[tuple[Instrument, StrategyHit]] = []
    for row, instrument in found:
        hits.append(
            (
                instrument,
                StrategyHit(
                    strategy=Strategy.SWING,
                    as_of=day,
                    detail=_detail_swing(row),
                    actionable=row.setup in {setup.value for setup in TRADEABLE_SETUPS},
                    close=row.close,
                ),
            )
        )
    return day, hits


async def _volume_breakout(
    session: AsyncSession, *, user_id: int
) -> tuple[dt.date | None, list[tuple[Instrument, StrategyHit]]]:
    day = (
        await session.execute(
            select(VbBreadthDaily.date)
            .where(VbBreadthDaily.user_id == user_id)
            .order_by(VbBreadthDaily.date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if day is None:
        return None, []
    found = (
        await session.execute(
            select(VbSignalDaily, Instrument)
            .join(Instrument, Instrument.id == VbSignalDaily.instrument_id)
            .where(VbSignalDaily.user_id == user_id, VbSignalDaily.date == day)
            .order_by(VbSignalDaily.instrument_id)
        )
    ).all()
    return day, [
        (
            instrument,
            StrategyHit(
                strategy=Strategy.VOLUME_BREAKOUT,
                as_of=day,
                detail=_VBT_STATE.get(row.state, row.state.lower().replace("_", " ")),
                actionable=row.state == "SIGNAL",
                close=row.close_raw,
            ),
        )
        for row, instrument in found
    ]


async def _three_weeks_tight(
    session: AsyncSession, *, user_id: int
) -> tuple[dt.date | None, list[tuple[Instrument, StrategyHit]]]:
    day = (
        await session.execute(
            select(TwBreadthDaily.date)
            .where(TwBreadthDaily.user_id == user_id)
            .order_by(TwBreadthDaily.date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if day is None:
        return None, []
    found = (
        await session.execute(
            select(TwStateDaily, Instrument, TwSignalDaily.state)
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
    ).all()
    hits: list[tuple[Instrument, StrategyHit]] = []
    for row, instrument, signal in found:
        sessions = row.sessions_in_state
        detail = f"tight {sessions} session{'' if sessions == 1 else 's'}"
        if signal:
            detail += f" · {str(signal).lower().replace('_', ' ')}"
        hits.append(
            (
                instrument,
                StrategyHit(
                    strategy=Strategy.THREE_WEEKS_TIGHT,
                    as_of=day,
                    detail=detail,
                    actionable=signal == "SIGNAL",
                    close=row.close_raw,
                ),
            )
        )
    return day, hits


def _definition_hash(screen: Screen) -> str | None:
    try:
        return ScreenDefinition.model_validate(screen.definition).definition_hash()
    except ValueError:
        return None


async def _screens(
    session: AsyncSession, *, user_id: int, instrument_ids: set[int]
) -> tuple[dict[int, list[ScreenHit]], int]:
    """The newest stored run of each screen the caller can see, inverted to instrument → hits.

    Only for the instruments already on the page: a screen's results are not a source of
    candidates here (that is what `/build` is for), they are context on a name a strategy raised.
    """
    screens = (
        await session.scalars(
            select(Screen)
            .where((Screen.user_id == user_id) | Screen.is_example.is_(True))
            .order_by(Screen.is_example, Screen.name)
        )
    ).all()
    if not screens or not instrument_ids:
        return {}, len(screens)
    newest = (
        await session.scalars(
            select(ScreenRun)
            .where(ScreenRun.screen_id.in_([screen.id for screen in screens]))
            .distinct(ScreenRun.screen_id)
            .order_by(ScreenRun.screen_id, ScreenRun.as_of.desc(), ScreenRun.created_at.desc())
        )
    ).all()
    runs = {run.screen_id: run for run in newest}
    hits: dict[int, list[ScreenHit]] = defaultdict(list)
    for screen in screens:
        run = runs.get(screen.id)
        if run is None:
            continue
        changed = _definition_hash(screen) != run.definition_hash
        for result in run.results:
            instrument_id = result.get("instrument_id")
            if not isinstance(instrument_id, int) or instrument_id not in instrument_ids:
                continue
            rank = result.get("rank")
            hits[instrument_id].append(
                ScreenHit(
                    name=screen.name,
                    public_id=screen.public_id,
                    is_template=screen.is_example,
                    as_of=run.as_of,
                    rank=rank if isinstance(rank, int) else None,
                    of=run.result_count,
                    definition_changed=changed,
                )
            )
    return dict(hits), len(screens)


def _tag(view: CatalystView | None) -> CatalystTag | None:
    """The baseline tag for the feed's newest headline; nothing when there is no headline.

    The tag reads the headline and nothing else — none of the row's numbers, and never the
    filing — so the same string tags the same on every row it appears on. Whether the filing
    explains the move stays the reader's call, on the exchange's page.
    """
    if view is None or view.headline is None or not view.headline.strip():
        return None
    return tag_headline(view.headline)


async def overlap(
    session: AsyncSession,
    *,
    user_id: int,
    strategies_user_id: int | None,
    scope: Scope = "actionable",
) -> OverlapView:
    """Every stock on any strategy's latest session, with what each strategy said about it.

    ``scope="actionable"`` keeps the rows at least one strategy could act on — the default, and
    the page a person opens in the morning. ``"all"`` keeps every row the scans wrote, which on
    a normal day is some fifty tight names and the volume rejects; a row kept for one strategy
    keeps every strategy's fact about it, so a FLAG that is also merely in the tight state shows
    both. Rows are ordered by how many strategies raised them, actionable first, then by symbol —
    the order is a sort key over stored facts, never a score.
    """
    sessions: dict[Strategy, dt.date | None] = {strategy: None for strategy in Strategy}
    instruments: dict[int, Instrument] = {}
    hits: dict[int, list[StrategyHit]] = defaultdict(list)
    if strategies_user_id is not None:
        for strategy, reader in (
            (Strategy.SWING, _swing),
            (Strategy.VOLUME_BREAKOUT, _volume_breakout),
            (Strategy.THREE_WEEKS_TIGHT, _three_weeks_tight),
        ):
            day, found = await reader(session, user_id=strategies_user_id)
            sessions[strategy] = day
            for instrument, hit in found:
                instruments[instrument.id] = instrument
                hits[instrument.id].append(hit)

    kept = {
        instrument_id: found
        for instrument_id, found in hits.items()
        if scope == "all" or any(hit.actionable for hit in found)
    }
    screens, screens_checked = await _screens(session, user_id=user_id, instrument_ids=set(kept))
    catalysts: dict[int, CatalystView] = {}
    if strategies_user_id is not None and kept:
        catalysts = await latest_for(session, user_id=strategies_user_id, instrument_ids=list(kept))

    rows = [
        CandidateRow(
            instrument_id=instrument_id,
            symbol=instruments[instrument_id].symbol,
            name=instruments[instrument_id].name,
            close=next((hit.close for hit in found if hit.close is not None), None),
            strategies=tuple(found),
            screens=tuple(screens.get(instrument_id, ())),
            catalyst=catalysts.get(instrument_id),
            catalyst_tag=_tag(catalysts.get(instrument_id)),
        )
        for instrument_id, found in kept.items()
    ]
    rows.sort(key=lambda row: (-row.strategy_count, not row.actionable, row.symbol))
    return OverlapView(
        sessions=sessions,
        rows=tuple(rows),
        strategies_read=strategies_user_id is not None,
        screens_checked=screens_checked,
    )
