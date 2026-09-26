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

**The writes are a person's correction of a headline tag and a person's label on a row's
attention opinion** (the section at the bottom): labels on display context, stored so the page
shows the person's word and the labels export as the fine-tuning sets. Each reaches one table
and no number the strategies wrote.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from typing import Final, Literal

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.swing import latest_detected_date
from baskfy_api.swing_catalyst import ANNOUNCEMENT, CatalystView, latest_for
from baskfy_core.candidate_review import (
    REVIEW_CONFIDENCE_FLOOR,
    SOURCE_LABELLED,
    Number,
    ReviewLabel,
    ReviewOpinion,
    RowContext,
    RowFacts,
    opinion_from_laya,
    review_key,
    review_state,
    rules_opinion,
)
from baskfy_core.catalyst_tags import (
    PRIORITY_OF,
    CatalystTag,
    EventType,
    ReviewPriority,
    cache_key,
    resolve_tag,
    tag_from_laya,
    tag_headline,
)
from baskfy_core.market_hours_cb import IST
from baskfy_core.models import (
    CandidateReviewLabel,
    CatalystTagCorrection,
    Instrument,
    Screen,
    ScreenRun,
    SwCatalyst,
    SwMarketDaily,
    SwSetupDaily,
    TwBreadthDaily,
    TwSignalDaily,
    TwStateDaily,
    VbBreadthDaily,
    VbSignalDaily,
)
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_core.swing.config import TRADEABLE_SETUPS

log = logging.getLogger(__name__)

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
    #: The scan's own numbers about the row — the ones `baskfy_core.candidate_review` says in
    #: words for Laya. Stored facts restated, never computed here.
    numbers: dict[str, Number] = field(default_factory=dict)

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
    #: The word on that headline (`baskfy_core.catalyst_tags`): a person's correction when there
    #: is one, else Laya's answer or the rules' — display context, never an input. ``None``
    #: exactly when there is no headline to read.
    catalyst_tag: CatalystTag | None = None
    #: The opinion on the row: a person's label when one is stored for this row's state
    #: (``source == "labelled"``, always shown), else Laya's — the technicals in words plus the
    #: filing — when the sidecar has answered; `candidate_review.shown` says whether the page
    #: shows it as a word.
    opinion: ReviewOpinion | None = None
    #: What Laya answered on this row's state, whatever tier ``opinion`` came from — ``None``
    #: until the sidecar has answered. Served beside the opinion so the page can show the
    #: model's word and percentage even when the rules or a label stand (Maulik, 25 Sep 2026:
    #: "implement such that I can see % which Laya suggests").
    laya: ReviewOpinion | None = None

    def facts(self) -> tuple[RowFacts, ...]:
        return tuple(
            RowFacts(hit.strategy.value, hit.detail, hit.actionable, dict(hit.numbers))
            for hit in self.strategies
        )

    #: The day around the row: the strategies' gates and breadth, the sector, the screens.
    context: RowContext | None = None

    def state(self) -> dict[str, str]:
        """What Laya is shown for this row — and what a label is keyed on."""
        return review_state(
            self.facts(), self.catalyst.headline if self.catalyst else None, self.context
        )

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
                    # Every column the detector wrote (Maulik, 25 Sep 2026: "all the parameters
                    # which swing has noticed"); `candidate_review` says each one in words.
                    numbers={
                        "score": row.score,
                        "close": row.close,
                        "trigger": row.trigger,
                        "stop_ref": row.stop_ref,
                        "pivot_high": row.pivot_high,
                        "gap_pct": row.gap_pct,
                        "rvol": row.rvol,
                        "base_depth_pct": row.base_depth_pct,
                        "base_bars": row.base_bars,
                        "tightness_adr": row.tightness_adr,
                        "dryup_ratio": row.dryup_ratio,
                        "prior_move_pct": row.prior_move_pct,
                        "adr_pct": row.adr_pct,
                        "dist_ma_fast_pct": row.dist_ma_fast_pct,
                        "dist_ma_slow_pct": row.dist_ma_slow_pct,
                        "up_streak": row.up_streak,
                        "turnover_avg": row.turnover_avg,
                        "locked_upper_circuit": row.locked_upper_circuit,
                        "listed_within_2y": row.listed_within_2y,
                        "sector_slug": row.sector_slug,
                    },
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
                numbers={
                    "close": row.close_raw,
                    "rvol": row.rvol,
                    "change_pct": row.change_pct,
                    "close_position": row.close_position,
                    "ret_20_pct": row.ret_20_pct,
                    "volume": row.volume,
                    "vol_sma_50": row.vol_sma_50,
                    "turnover_avg_20": row.turnover_avg_20,
                    "sma_200": row.sma_200,
                    "ema_21": row.ema_21,
                    "high_20_prior": row.high_20_prior,
                    "limit_price": row.limit_price,
                    "stop_price": row.stop_price,
                    "locked_upper_circuit": row.locked_upper_circuit,
                    "failed_filters": ", ".join(row.failed_filters) if row.failed_filters else None,
                },
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
            select(TwStateDaily, Instrument, TwSignalDaily)
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
    for row, instrument, event in found:
        signal = event.state if event is not None else None
        sessions_out = event.sessions_out_before if event is not None else None
        stop_preview = event.stop_preview if event is not None else None
        failed = (
            ", ".join(event.failed_filters) if event is not None and event.failed_filters else None
        )
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
                    numbers={
                        "close": row.close_raw,
                        "week_range_pct": row.week_range_pct,
                        "week_close_0": row.week_close_0,
                        "week_close_1": row.week_close_1,
                        "week_close_2": row.week_close_2,
                        "sessions_in_state": row.sessions_in_state,
                        "month_low_ratio": row.month_low_ratio,
                        "sma_dma": row.sma_dma,
                        "volume": row.volume,
                        "vol_sma_50": row.vol_sma_50,
                        "turnover_avg_20": row.turnover_avg_20,
                        "locked_upper_circuit": row.locked_upper_circuit,
                        "sessions_out_before": sessions_out,
                        "stop_preview": stop_preview,
                        "failed_filters": failed,
                    },
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


def _tag(
    view: CatalystView | None, laya_answer: object = None, corrected: EventType | None = None
) -> CatalystTag | None:
    """The tag for the feed's newest headline; nothing when there is no headline.

    The rules read the headline; Laya's cached answer for that same headline, when the sidecar
    has written one (`infra/laya/laya_loop.py`), is resolved against them by
    `baskfy_core.catalyst_tags.resolve_tag` — the model where it is sure, the rules where it is
    not, the other's word kept as a disagreement — and a person's correction of that headline,
    when one is stored, wins over both. None of the three reads any of the row's numbers, and
    none reads the filing, so the same string tags the same on every row it appears on.
    Whether the filing explains the move stays the reader's call, on the exchange's page.
    """
    if view is None or view.headline is None or not view.headline.strip():
        return None
    return resolve_tag(tag_headline(view.headline), tag_from_laya(laya_answer), corrected)


#: How far back the page looks for a filing worth opening. A material filing older than this is
#: history, not a catalyst; beyond it the newest filing of any age stands, so a name is never
#: blank while the feed holds something.
MATERIAL_WINDOW_DAYS: Final = 45
#: Filings read per name when choosing: the feed can hold hundreds after a manual scan, and the
#: newest forty cover the window on any name that files daily.
FILINGS_PER_NAME: Final = 40

_PRIORITY_RANK: Final[dict[ReviewPriority, int]] = {
    ReviewPriority.HIGH: 2,
    ReviewPriority.MEDIUM: 1,
    ReviewPriority.LOW: 0,
}


async def best_catalyst_for(
    session: AsyncSession,
    *,
    user_id: int,
    instrument_ids: Sequence[int],
    today: dt.date | None = None,
) -> dict[int, CatalystView]:
    """The filing worth opening per name, not merely the newest one (25 Sep 2026, the box).

    After a manual scan the feed holds every filing of the last fortnight, and the newest is
    almost always paperwork — an investor-meet schedule, a newspaper cutting, a SAST
    disclosure — so a page showing "the newest filing" showed a routine notice on every row and
    Laya, which is sure only on material headlines, had nothing worth reading. Here each
    name's filings inside `MATERIAL_WINDOW_DAYS` are tagged by the rules and the **highest
    priority wins, newest first within it**; with nothing in the window, the newest filing of
    any age stands. The earnings date is the calendar's, unchanged (`latest_for`).

    Deterministic and rules-only: the choice is never the model's, so the model's answer on the
    chosen headline is an opinion about a filing the rules picked, not a filing the model chose.
    """
    wanted = sorted({int(instrument_id) for instrument_id in instrument_ids})
    if not wanted:
        return {}
    calendar = await latest_for(session, user_id=user_id, instrument_ids=wanted)
    rows = (
        await session.execute(
            select(SwCatalyst)
            .where(
                SwCatalyst.user_id == user_id,
                SwCatalyst.instrument_id.in_(wanted),
                SwCatalyst.source == ANNOUNCEMENT,
            )
            .order_by(
                SwCatalyst.instrument_id,
                SwCatalyst.published_at.desc().nulls_last(),
                SwCatalyst.id.desc(),
            )
        )
    ).scalars()
    per_name: dict[int, list[SwCatalyst]] = defaultdict(list)
    for row in rows:
        if len(per_name[row.instrument_id]) < FILINGS_PER_NAME:
            per_name[row.instrument_id].append(row)
    horizon = (today or dt.datetime.now(dt.UTC).date()) - dt.timedelta(days=MATERIAL_WINDOW_DAYS)
    chosen: dict[int, CatalystView] = {}
    for instrument_id in wanted:
        filings = per_name.get(instrument_id, [])
        earnings_date = calendar[instrument_id].earnings_date if instrument_id in calendar else None
        if not filings:
            if earnings_date is not None:
                chosen[instrument_id] = CatalystView(None, None, None, earnings_date)
            continue
        recent = [
            f for f in filings if f.published_at is not None and f.published_at.date() >= horizon
        ]
        best = (
            max(
                recent,
                key=lambda f: (
                    _PRIORITY_RANK[PRIORITY_OF[tag_headline(f.headline).event_type]],
                    f.published_at or dt.datetime.min.replace(tzinfo=dt.UTC),
                    f.id,
                ),
            )
            if recent
            else filings[0]
        )
        chosen[instrument_id] = CatalystView(
            headline=best.headline,
            published_at=best.published_at,
            url=best.url,
            earnings_date=earnings_date,
        )
    return chosen


#: Where the API tells the sidecar which headlines the page is showing, so Laya reads the
#: filing the rules chose and not only the newest one per name (`infra/laya/laya_loop.py`).
WANTED_KEY: Final = "catalyst_tag:wanted"
WANTED_TTL_S: Final = 3 * 24 * 3600


#: The sidecar's last pass (`infra/laya/laya_loop.py` `HEARTBEAT_KEY`).
HEARTBEAT_KEY: Final = "catalyst_tag:heartbeat"


@dataclass(frozen=True, slots=True)
class LayaStatus:
    """Whether Laya has looked, and at how much of what the page shows — so "no percentages"
    reads as "Laya was unsure" or "Laya has not run", never as a page that quietly broke."""

    last_pass_at: dt.datetime | None
    #: Rows whose filing Laya answered on, whichever reader the page shows.
    answered: int
    #: Rows whose shown tag is Laya's.
    shown: int
    #: Rows with a filing at all.
    of: int


async def laya_last_pass(cache: Redis | None) -> dt.datetime | None:
    if cache is None:
        return None
    try:
        raw = await cache.get(HEARTBEAT_KEY)
    except RedisError:
        return None
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        at = payload.get("at") if isinstance(payload, dict) else None
        return dt.datetime.strptime(at, "%Y-%m-%dT%H:%M:%S%z") if isinstance(at, str) else None
    except (ValueError, TypeError):
        return None


def laya_status(rows: Sequence[CandidateRow], last_pass_at: dt.datetime | None) -> LayaStatus:
    with_filing = [row for row in rows if row.catalyst is not None and row.catalyst.headline]
    answered = sum(
        1
        for row in with_filing
        if row.catalyst_tag is not None
        and (
            row.catalyst_tag.source == "laya"
            or (row.catalyst_tag.disagrees_with or "").startswith("laya:")
        )
    )
    shown = sum(
        1
        for row in with_filing
        if row.catalyst_tag is not None and row.catalyst_tag.source == "laya"
    )
    return LayaStatus(
        last_pass_at=last_pass_at, answered=answered, shown=shown, of=len(with_filing)
    )


#: The row states the page is showing and has no opinion for yet: a hash of key -> state JSON,
#: read and cleared by the sidecar (`infra/laya/laya_loop.py`). A hash, not a set, because the
#: sidecar needs the words, and only the API can say them (it holds the rows).
REVIEW_WANTED_KEY: Final = "candidate_review:wanted"


async def review_opinions(
    cache: Redis | None, states: dict[str, dict[str, str]]
) -> dict[str, ReviewOpinion]:
    """The sidecar's cached opinion per row key; the misses are queued for it. Empty without a
    cache or when Redis is unreachable — which means "not sure" on the page, never an error."""
    if cache is None or not states:
        return {}
    keys = list(states)
    try:
        raw = await cache.mget(keys)
    except RedisError:
        return {}
    found: dict[str, ReviewOpinion] = {}
    missing: dict[str, str] = {}
    for key, value in zip(keys, raw, strict=True):
        if value is None:
            missing[key] = json.dumps(states[key], sort_keys=True, ensure_ascii=False)
            continue
        try:
            opinion = opinion_from_laya(json.loads(value))
        except ValueError:
            opinion = None
        if opinion is not None:
            found[key] = opinion
    if missing:
        # The state is written with sorted keys for a stable payload; the sidecar puts the
        # fields back in `candidate_review.STATE_FIELDS` order before the model sees them.
        try:
            pipe = cache.pipeline()
            pipe.hset(REVIEW_WANTED_KEY, mapping=missing)
            pipe.expire(REVIEW_WANTED_KEY, WANTED_TTL_S)
            await pipe.execute()
        except RedisError as exc:
            log.warning("could not queue row states for the sidecar: %s", exc)
    return found


async def laya_answers(cache: Redis | None, headlines: list[str]) -> dict[str, object]:
    """The sidecar's cached answer per headline, keyed by the headline; empty without a cache,
    on a miss, or when Redis is unreachable — every one of which means "the rules tag"."""
    if cache is None or not headlines:
        return {}
    keys = [cache_key(headline) for headline in headlines]
    try:
        raw = await cache.mget(keys)
    except RedisError:
        return {}
    found: dict[str, object] = {}
    missing: list[str] = []
    for headline, value in zip(headlines, raw, strict=True):
        if value is None:
            missing.append(headline)
            continue
        try:
            found[headline] = json.loads(value)
        except ValueError:
            continue
    if missing:
        # Ask the sidecar for what the page is showing and does not have yet. A set write on a
        # read path, bounded by a TTL; never a model call, never a wait.
        try:
            pipe = cache.pipeline()
            pipe.sadd(WANTED_KEY, *missing)
            pipe.expire(WANTED_KEY, WANTED_TTL_S)
            await pipe.execute()
        except RedisError as exc:
            # The page is not the sidecar's keeper: a cache that will not take the note costs
            # the note, not the read. Logged, so a silent Redis is still visible somewhere.
            log.warning("could not note wanted headlines for the sidecar: %s", exc)
    return found


async def corrections_for(
    session: AsyncSession, *, user_id: int, headlines: list[str]
) -> dict[str, EventType]:
    """The person's stored corrections for these headlines, keyed by the headline — one query
    over the content-addressed key, so the page pays for its corrections once, not per row."""
    if not headlines:
        return {}
    # Two spellings of one headline share a key (the key is case- and space-blind), and both
    # take the correction.
    by_key: dict[str, list[str]] = defaultdict(list)
    for headline in headlines:
        by_key[cache_key(headline)].append(headline)
    found = (
        await session.execute(
            select(CatalystTagCorrection.headline_key, CatalystTagCorrection.event_type).where(
                CatalystTagCorrection.user_id == user_id,
                CatalystTagCorrection.headline_key.in_(list(by_key)),
            )
        )
    ).all()
    return {
        headline: EventType(event_type)
        for key, event_type in found
        for headline in by_key.get(key, ())
    }


async def labels_for(
    session: AsyncSession, *, user_id: int, keys: Sequence[str]
) -> dict[str, ReviewLabel]:
    """The person's stored labels for these row states, keyed by the review key — one query
    over the content-addressed key, so the page pays for its labels once, not per row."""
    if not keys:
        return {}
    found = (
        await session.execute(
            select(CandidateReviewLabel.review_key, CandidateReviewLabel.label).where(
                CandidateReviewLabel.user_id == user_id,
                CandidateReviewLabel.review_key.in_(sorted(set(keys))),
            )
        )
    ).all()
    return {key: ReviewLabel(label) for key, label in found}


def _opinion(
    laya: ReviewOpinion | None, label: ReviewLabel | None, rules: ReviewOpinion
) -> ReviewOpinion:
    """A person's label wins; then the model when it is sure; then the rules, which always
    have a word (25 Sep 2026 — a page of "not sure" said nothing)."""
    if label is not None:
        return ReviewOpinion(label, 0.0, source=SOURCE_LABELLED)
    if laya is not None and laya.confidence >= REVIEW_CONFIDENCE_FLOOR:
        return laya
    return rules


def _rules_opinion(row: CandidateRow) -> ReviewOpinion:
    """The baseline reads the headline itself for the adverse check (OV11): the resolved tag may
    be Laya's or a person's, and neither carries the rules' phrases, so passing the tag alone
    let a confident model re-tag "SEBI order" as corporate_action and the skip disappeared."""
    tag = row.catalyst_tag
    return rules_opinion(
        row.facts(),
        row.catalyst.headline if row.catalyst is not None else None,
        None if tag is None else tag.event_type.value,
        None if tag is None else tag.review_priority.value,
        row.context,
    )


async def _gates(
    session: AsyncSession, *, user_id: int, sessions: dict[Strategy, dt.date | None]
) -> tuple[dict[str, str], dict[str, Decimal]]:
    """Each strategy's own gate word and breadth on its latest session — the day's context every
    row on the page shares. Read once, not per row."""
    gates: dict[str, str] = {}
    breadth: dict[str, Decimal] = {}
    if (day := sessions.get(Strategy.SWING)) is not None:
        market = (
            await session.execute(
                select(SwMarketDaily.gate).where(
                    SwMarketDaily.user_id == user_id, SwMarketDaily.date == day
                )
            )
        ).scalar_one_or_none()
        if market:
            gates[STRATEGY_NAMES[Strategy.SWING]] = str(market)
    if (day := sessions.get(Strategy.VOLUME_BREAKOUT)) is not None:
        vb = (
            await session.execute(
                select(VbBreadthDaily.gate, VbBreadthDaily.pct_above_dma).where(
                    VbBreadthDaily.user_id == user_id, VbBreadthDaily.date == day
                )
            )
        ).one_or_none()
        if vb is not None:
            gates[STRATEGY_NAMES[Strategy.VOLUME_BREAKOUT]] = str(vb[0])
            if vb[1] is not None:
                breadth[STRATEGY_NAMES[Strategy.VOLUME_BREAKOUT]] = vb[1]
    if (day := sessions.get(Strategy.THREE_WEEKS_TIGHT)) is not None:
        tw = (
            await session.execute(
                select(TwBreadthDaily.gate, TwBreadthDaily.pct_above_dma).where(
                    TwBreadthDaily.user_id == user_id, TwBreadthDaily.date == day
                )
            )
        ).one_or_none()
        if tw is not None:
            gates[STRATEGY_NAMES[Strategy.THREE_WEEKS_TIGHT]] = str(tw[0])
            if tw[1] is not None:
                breadth[STRATEGY_NAMES[Strategy.THREE_WEEKS_TIGHT]] = tw[1]
    return gates, breadth


def _context(row: CandidateRow, gates: dict[str, str], breadth: dict[str, Decimal]) -> RowContext:
    """The day's context for one row: only the gates of the strategies that raised it, the
    sector the swing detector recorded, the screens the name is on — and the dates each piece
    is from (OV11): the strategies' own sessions, the filing's exchange date (the feed stores
    the stamp in UTC; NSE publishes in IST, so the date is taken there), the results date, and
    each screen run's ``as_of``. Stored dates only, so the state holds still within a session."""
    names = {hit.name for hit in row.strategies}
    sector = next(
        (
            str(hit.numbers["sector_slug"])
            for hit in row.strategies
            if isinstance(hit.numbers.get("sector_slug"), str)
        ),
        None,
    )
    published = row.catalyst.published_at if row.catalyst is not None else None
    return RowContext(
        gates={name: gate for name, gate in gates.items() if name in names},
        breadth_pct={name: pct for name, pct in breadth.items() if name in names},
        sector=sector,
        screens=tuple((screen.name, screen.rank, screen.of) for screen in row.screens),
        sessions={hit.name: hit.as_of for hit in row.strategies},
        filing_published=None if published is None else published.astimezone(IST).date(),
        earnings_date=row.catalyst.earnings_date if row.catalyst is not None else None,
        screen_runs={screen.name: screen.as_of for screen in row.screens},
        screens_changed=tuple(screen.name for screen in row.screens if screen.definition_changed),
    )


async def overlap(
    session: AsyncSession,
    *,
    user_id: int,
    strategies_user_id: int | None,
    scope: Scope = "actionable",
    cache: Redis | None = None,
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
        catalysts = await best_catalyst_for(
            session, user_id=strategies_user_id, instrument_ids=list(kept)
        )
    headlines = sorted(
        {v.headline for v in catalysts.values() if v.headline and v.headline.strip()}
    )
    answers = await laya_answers(cache, headlines)
    corrections = await corrections_for(session, user_id=user_id, headlines=headlines)

    rows = [
        CandidateRow(
            instrument_id=instrument_id,
            symbol=instruments[instrument_id].symbol,
            name=instruments[instrument_id].name,
            close=next((hit.close for hit in found if hit.close is not None), None),
            strategies=tuple(found),
            screens=tuple(screens.get(instrument_id, ())),
            catalyst=catalysts.get(instrument_id),
            catalyst_tag=_tag(
                catalysts.get(instrument_id),
                answers.get(getattr(catalysts.get(instrument_id), "headline", None) or ""),
                corrections.get(getattr(catalysts.get(instrument_id), "headline", None) or ""),
            ),
        )
        for instrument_id, found in kept.items()
    ]
    rows.sort(key=lambda row: (-row.strategy_count, not row.actionable, row.symbol))
    gates, breadth = (
        await _gates(session, user_id=strategies_user_id, sessions=sessions)
        if strategies_user_id is not None
        else ({}, {})
    )
    rows = [replace(row, context=_context(row, gates, breadth)) for row in rows]
    states: dict[str, dict[str, str]] = {}
    keys: list[str] = []
    for row in rows:
        state = row.state()
        key = review_key(state)
        states[key] = state
        keys.append(key)
    opinions = await review_opinions(cache, states)
    labels = await labels_for(session, user_id=user_id, keys=keys)
    rows = [
        replace(
            row,
            opinion=_opinion(opinions.get(key), labels.get(key), _rules_opinion(row)),
            laya=opinions.get(key),
        )
        for row, key in zip(rows, keys, strict=True)
    ]
    return OverlapView(
        sessions=sessions,
        rows=tuple(rows),
        strategies_read=strategies_user_id is not None,
        screens_checked=screens_checked,
    )


# --- Corrections and labels: the two things this page writes ----------------------------------
#
# Everything above is a read over stored rows, and `test_overlap_readonly.py` scans those
# functions for a write verb. This section is the exception it names: a person's correction of
# a headline tag — written to `catalyst_tag_correction` — and a person's label on a row's
# attention opinion — written to `candidate_review_label` — the two sets the fine-tunes will
# train on, and nowhere else. Either changes what the page says and what its export contains; it
# changes no rank, no filter, no size and no order, because neither the tag nor the opinion ever
# reached one. Both tables are content-addressed (`cache_key` on the headline, `review_key` on
# the row state), so a word given on one row applies to every row that carries the same text.


@dataclass(frozen=True, slots=True)
class CorrectionRecord:
    """One export line: the input text, the person's label, and what the two readers said."""

    headline: str
    event_type: EventType
    rules_event_type: EventType | None
    laya_event_type: EventType | None
    laya_confidence: Decimal | None
    note: str | None
    corrected_at: dt.datetime


def _confidence(tag: CatalystTag | None) -> Decimal | None:
    """Laya's probability as the column stores it — four decimals, never a float on the wire."""
    if tag is None or tag.confidence is None:
        return None
    return Decimal(f"{tag.confidence:.4f}")


async def correct_tag(  # noqa: PLR0913 - the headline, the label, the note and the model answer are all inputs
    session: AsyncSession,
    *,
    user_id: int,
    headline: str,
    event_type: EventType,
    note: str | None,
    laya_answer: object = None,
) -> CatalystTag:
    """Store a person's word on a headline — one row per ``(user, headline_key)``, updated in
    place on a second correction — and return the tag the page now shows for it.

    What the rules and Laya said is recorded at this moment, from the same reads the page uses,
    because that disagreement is the training signal and neither reader stands still.
    """
    rules = tag_headline(headline)
    laya = tag_from_laya(laya_answer)
    key = cache_key(headline)
    row = (
        await session.scalars(
            select(CatalystTagCorrection).where(
                CatalystTagCorrection.user_id == user_id,
                CatalystTagCorrection.headline_key == key,
            )
        )
    ).one_or_none()
    if row is None:
        row = CatalystTagCorrection(user_id=user_id, headline_key=key, headline=headline)
        session.add(row)
    row.headline = headline
    row.event_type = event_type.value
    row.note = note
    row.rules_event_type = rules.event_type.value
    row.laya_event_type = None if laya is None else laya.event_type.value
    row.laya_confidence = _confidence(laya)
    await session.flush()
    return resolve_tag(rules, laya, event_type)


async def clear_correction(session: AsyncSession, *, user_id: int, headline: str) -> bool:
    """Remove the person's word on a headline, so the readers' resolution shows again.
    ``False`` when there was none to remove."""
    row = (
        await session.scalars(
            select(CatalystTagCorrection).where(
                CatalystTagCorrection.user_id == user_id,
                CatalystTagCorrection.headline_key == cache_key(headline),
            )
        )
    ).one_or_none()
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def corrections_export(session: AsyncSession, *, user_id: int) -> list[CorrectionRecord]:
    """Every correction the person has made, oldest first — the fine-tuning set."""
    rows = (
        await session.scalars(
            select(CatalystTagCorrection)
            .where(CatalystTagCorrection.user_id == user_id)
            .order_by(CatalystTagCorrection.created_at, CatalystTagCorrection.id)
        )
    ).all()
    return [
        CorrectionRecord(
            headline=row.headline,
            event_type=EventType(row.event_type),
            rules_event_type=(
                None if row.rules_event_type is None else EventType(row.rules_event_type)
            ),
            laya_event_type=None if row.laya_event_type is None else EventType(row.laya_event_type),
            laya_confidence=row.laya_confidence,
            note=row.note,
            corrected_at=row.updated_at,
        )
        for row in rows
    ]


# --- Labels: a person's word on a row's attention opinion -------------------------------------


@dataclass(frozen=True, slots=True)
class LabelRecord:
    """One export line: the words the model saw, the person's label, and what the model said."""

    symbol: str
    state: dict[str, object]
    label: ReviewLabel
    laya_label: ReviewLabel | None
    laya_confidence: Decimal | None
    note: str | None
    labelled_at: dt.datetime


def _opinion_confidence(opinion: ReviewOpinion | None) -> Decimal | None:
    """Laya's probability as the column stores it — four decimals, never a float on the wire."""
    if opinion is None:
        return None
    return Decimal(f"{opinion.confidence:.4f}")


async def label_row(  # noqa: PLR0913 - the row, the label, the note and the model answer are all inputs
    session: AsyncSession,
    *,
    user_id: int,
    row: CandidateRow,
    label: ReviewLabel,
    note: str | None,
    laya_opinion: ReviewOpinion | None = None,
) -> ReviewOpinion:
    """Store a person's word on a row's state — one row per ``(user, review_key)``, updated in
    place on a second label — and return the opinion the page now shows for it.

    The state stored is the row's own, as `overlap` computed it, so it is exactly what the page
    showed and what the model was asked about. What Laya had cached is recorded at this moment
    because that agreement or disagreement is the training signal and the checkpoint moves.
    """
    state = row.state()
    key = review_key(state)
    stored = (
        await session.scalars(
            select(CandidateReviewLabel).where(
                CandidateReviewLabel.user_id == user_id,
                CandidateReviewLabel.review_key == key,
            )
        )
    ).one_or_none()
    if stored is None:
        stored = CandidateReviewLabel(
            user_id=user_id,
            review_key=key,
            state=dict(state),
            instrument_id=row.instrument_id,
            symbol=row.symbol,
        )
        session.add(stored)
    stored.state = dict(state)
    stored.instrument_id = row.instrument_id
    stored.symbol = row.symbol
    stored.label = label.value
    stored.note = note
    stored.laya_label = None if laya_opinion is None else laya_opinion.label.value
    stored.laya_confidence = _opinion_confidence(laya_opinion)
    await session.flush()
    return ReviewOpinion(label, 0.0, source=SOURCE_LABELLED)


async def clear_label(session: AsyncSession, *, user_id: int, row: CandidateRow) -> bool:
    """Remove the person's word on a row's state, so the model's opinion shows again.
    ``False`` when there was none to remove."""
    stored = (
        await session.scalars(
            select(CandidateReviewLabel).where(
                CandidateReviewLabel.user_id == user_id,
                CandidateReviewLabel.review_key == review_key(row.state()),
            )
        )
    ).one_or_none()
    if stored is None:
        return False
    await session.delete(stored)
    await session.flush()
    return True


async def labels_export(session: AsyncSession, *, user_id: int) -> list[LabelRecord]:
    """Every label the person has given, oldest first — the fine-tuning set for the row question."""
    rows = (
        await session.scalars(
            select(CandidateReviewLabel)
            .where(CandidateReviewLabel.user_id == user_id)
            .order_by(CandidateReviewLabel.created_at, CandidateReviewLabel.id)
        )
    ).all()
    return [
        LabelRecord(
            symbol=stored.symbol,
            state=dict(stored.state),
            label=ReviewLabel(stored.label),
            laya_label=None if stored.laya_label is None else ReviewLabel(stored.laya_label),
            laya_confidence=stored.laya_confidence,
            note=stored.note,
            labelled_at=stored.updated_at,
        )
        for stored in rows
    ]
