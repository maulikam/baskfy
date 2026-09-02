"""The watchlist — `docs/swing/03` §4 and `04` §9.5 (SW5).

A watchlist is the method's memory between the scan and the morning. `docs/swing/01` §8: the
weekend produces "a watchlist of a few dozen forming flags, the levels that would trigger next
week", and the first hour of a session is spent watching those levels rather than looking for new
ones. So this module owns three things and nothing else:

**Who gets on it.** The detectors do most of the work: a flag that scores
``watch.auto_watch_min_score`` [60] or better and is still `SETTING_UP`, and **every** `GAP_DAY`
EP whatever it scored — an EP is enterable for three sessions and there is no second chance to
notice one. A person can add a name by hand, and a `MANUAL` row keeps the levels they typed.

**Who comes off it.** A flag after ten sessions without a trigger, an EP after three. Expiry is a
**state change**, never a delete: the record of what was watched is the record of what was passed
over, and a journal that only remembers the trades taken cannot answer "what did I miss".

**What a person may change.** The note and the catalyst — `01` §3's "news check", which Baskfy
cannot do for them because it holds no news feed (D10). Those are free text and move no money,
which is why `docs/swing/02` Track A allows them on a read-only surface.

Nothing here sizes a position, builds a plan or reaches a broker.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import (
    Instrument,
    OhlcvDaily,
    SwSetupDaily,
    SwWatch,
    TradingDay,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG, Setup, SwingConfig
from baskfy_core.swing.setups import CandidateStatus

#: The states a row can be in (`03` §4). `WATCHING` is the only one the morning reads.
WATCHING = "WATCHING"
TRIGGERED = "TRIGGERED"
EXPIRED = "EXPIRED"
DISMISSED = "DISMISSED"

SOURCE_DETECTOR = "DETECTOR"
SOURCE_MANUAL = "MANUAL"


@dataclass(frozen=True, slots=True)
class WatchRow:
    """One row, joined to the instrument a person recognises it by."""

    id: int
    instrument_id: int
    symbol: str
    name: str
    setup: str
    source: str
    added_on: dt.date
    expires_on: dt.date | None
    trigger: Decimal | None
    stop_ref: Decimal | None
    note: str | None
    catalyst: str | None
    state: str
    #: The latest close, so the page can show how far the price is from the trigger without a
    #: second request. `None` when the instrument has no bar yet.
    last_close: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AutoWatchResult:
    """What one evening's auto-watch did, for the step payload and the email."""

    added: int
    already_watching: int
    expired: int
    considered: int


async def _sessions_ahead(session: AsyncSession, start: dt.date, count: int) -> dt.date | None:
    """The date ``count`` trading days after ``start``, or ``None`` if the calendar ends first.

    From the calendar rather than by adding days, for the reason the lookback is: ten sessions is
    a fortnight in a normal month and rather more across Diwali, and an expiry computed in
    calendar days would retire a base early exactly when the market was closed for a week.
    """
    rows = await session.execute(
        select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date > start,
        )
        .order_by(TradingDay.date)
        .limit(count)
    )
    dates: list[dt.date] = [row[0] for row in rows]
    return dates[-1] if len(dates) == count else None


def _valid_bars(setup: str, config: SwingConfig) -> int:
    """How many sessions a row of this setup stays on the list (`04` §9.5)."""
    if setup == Setup.EP.value:
        return config.ep.valid_bars
    return config.watch.flag_valid_bars


async def auto_watch(
    session: AsyncSession,
    *,
    user_id: int,
    on: dt.date,
    config: SwingConfig = DEFAULT_SWING_CONFIG,
) -> AutoWatchResult:
    """Put the evening's qualifying candidates on the list, and retire the stale ones.

    Idempotent per `(user_id, instrument_id, setup, added_on)` — the table's own unique key — so
    re-running an evening adds nothing and the counters say `already_watching` rather than
    inventing duplicates.

    A `PARABOLIC_SHORT` is never watched. It is not tradeable (`TRADEABLE_SETUPS`, PACK.1), and a
    watchlist is a list of things to buy; putting one on it would be an invitation nobody can act
    on.
    """
    expired = await expire_stale(session, user_id=user_id, on=on)

    candidates = (
        (
            await session.execute(
                select(SwSetupDaily).where(
                    SwSetupDaily.user_id == user_id,
                    SwSetupDaily.date == on,
                    SwSetupDaily.setup.in_([Setup.FLAG.value, Setup.EP.value]),
                    or_(
                        # Every EP, whatever it scored: three sessions of validity and no second
                        # chance to notice it.
                        SwSetupDaily.setup == Setup.EP.value,
                        and_(
                            SwSetupDaily.status == CandidateStatus.SETTING_UP.value,
                            SwSetupDaily.score >= Decimal(str(config.watch.auto_watch_min_score)),
                        ),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )

    existing = {
        (row.instrument_id, row.setup)
        for row in (
            await session.execute(
                select(SwWatch).where(SwWatch.user_id == user_id, SwWatch.state == WATCHING)
            )
        ).scalars()
    }

    added = 0
    already = 0
    for candidate in candidates:
        if (candidate.instrument_id, candidate.setup) in existing:
            already += 1
            continue
        session.add(
            SwWatch(
                user_id=user_id,
                instrument_id=candidate.instrument_id,
                setup=candidate.setup,
                source=SOURCE_DETECTOR,
                added_on=on,
                expires_on=await _sessions_ahead(session, on, _valid_bars(candidate.setup, config)),
                trigger=candidate.trigger,
                stop_ref=candidate.stop_ref,
                setup_daily_date=candidate.date,
                state=WATCHING,
            )
        )
        existing.add((candidate.instrument_id, candidate.setup))
        added += 1
    await session.flush()
    return AutoWatchResult(
        added=added, already_watching=already, expired=expired, considered=len(candidates)
    )


async def expire_stale(session: AsyncSession, *, user_id: int, on: dt.date) -> int:
    """Move every `WATCHING` row whose `expires_on` has passed to `EXPIRED`. Returns the count.

    A `MANUAL` row has no `expires_on` and is therefore never touched: a person who typed a level
    is watching for a reason the detector does not know about, and retiring it for them would be
    the system overruling them.
    """
    rows = (
        (
            await session.execute(
                select(SwWatch).where(
                    SwWatch.user_id == user_id,
                    SwWatch.state == WATCHING,
                    SwWatch.expires_on.is_not(None),
                    SwWatch.expires_on < on,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.state = EXPIRED
    await session.flush()
    return len(rows)


def _watching_query(
    user_id: int, *, state: str | None = WATCHING
) -> Select[tuple[SwWatch, str, str]]:
    query = (
        select(SwWatch, Instrument.symbol, Instrument.name)
        .join(Instrument, Instrument.id == SwWatch.instrument_id)
        .where(SwWatch.user_id == user_id)
        .order_by(SwWatch.added_on.desc(), Instrument.symbol)
    )
    return query if state is None else query.where(SwWatch.state == state)


async def _latest_closes(session: AsyncSession, instrument_ids: list[int]) -> dict[int, Decimal]:
    """The most recent adjusted close per instrument.

    One grouped subquery rather than a query per row: a watchlist of forty names would otherwise
    be forty round trips to show one column.
    """
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
    result = await session.execute(
        select(OhlcvDaily.instrument_id, OhlcvDaily.close).join(
            newest,
            and_(
                OhlcvDaily.instrument_id == newest.c.instrument_id,
                OhlcvDaily.date == newest.c.date,
            ),
        )
    )
    return {int(instrument_id): close for instrument_id, close in result.all()}


async def list_watch(
    session: AsyncSession, *, user_id: int, state: str | None = WATCHING
) -> tuple[WatchRow, ...]:
    """The list, newest first. ``state=None`` returns every row, including what expired."""
    rows = (await session.execute(_watching_query(user_id, state=state))).all()
    if not rows:
        return ()
    latest = await _latest_closes(session, [row[0].instrument_id for row in rows])
    return tuple(
        WatchRow(
            id=row.id,
            instrument_id=row.instrument_id,
            symbol=symbol,
            name=name,
            setup=row.setup,
            source=row.source,
            added_on=row.added_on,
            expires_on=row.expires_on,
            trigger=row.trigger,
            stop_ref=row.stop_ref,
            note=row.note,
            catalyst=row.catalyst,
            state=row.state,
            last_close=latest.get(row.instrument_id),
        )
        for row, symbol, name in rows
    )


class WatchNotFound(LookupError):
    """No such row for this user. A 404, never a 403 — see `curated_tenant`."""


async def load(session: AsyncSession, *, user_id: int, watch_id: int) -> SwWatch:
    row = (
        await session.execute(
            select(SwWatch).where(SwWatch.id == watch_id, SwWatch.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise WatchNotFound(str(watch_id))
    return row


async def add_manual(  # noqa: PLR0913 - one keyword per field a person types
    session: AsyncSession,
    *,
    user_id: int,
    instrument_id: int,
    setup: str,
    on: dt.date,
    trigger: Decimal | None = None,
    stop_ref: Decimal | None = None,
    note: str | None = None,
    catalyst: str | None = None,
) -> SwWatch:
    """A name a person put on the list themselves.

    No expiry: a `MANUAL` row stays until it is dismissed. The person is watching for a reason
    the detectors do not know — a chart they read, something they heard — and a system that
    retired it after ten sessions would be overruling a judgement it cannot see.
    """
    existing = (
        await session.execute(
            select(SwWatch).where(
                SwWatch.user_id == user_id,
                SwWatch.instrument_id == instrument_id,
                SwWatch.setup == setup,
                SwWatch.state == WATCHING,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Re-adding a name already on the list updates its levels rather than making a second
        # row: two rows for one name would both become plan lines, and the sizing would spend
        # the cash twice.
        if trigger is not None:
            existing.trigger = trigger
        if stop_ref is not None:
            existing.stop_ref = stop_ref
        if note is not None:
            existing.note = note
        if catalyst is not None:
            existing.catalyst = catalyst
        existing.source = SOURCE_MANUAL
        await session.flush()
        return existing

    row = SwWatch(
        user_id=user_id,
        instrument_id=instrument_id,
        setup=setup,
        source=SOURCE_MANUAL,
        added_on=on,
        expires_on=None,
        trigger=trigger,
        stop_ref=stop_ref,
        note=note,
        catalyst=catalyst,
        state=WATCHING,
    )
    session.add(row)
    await session.flush()
    return row


async def annotate(
    session: AsyncSession,
    *,
    user_id: int,
    watch_id: int,
    note: str | None = None,
    catalyst: str | None = None,
) -> SwWatch:
    """The two free-text fields. Neither moves money; `02` Track A allows both."""
    row = await load(session, user_id=user_id, watch_id=watch_id)
    if note is not None:
        row.note = note
    if catalyst is not None:
        row.catalyst = catalyst
    await session.flush()
    return row


async def dismiss(session: AsyncSession, *, user_id: int, watch_id: int) -> SwWatch:
    """Take a name off the list — a state change, not a delete.

    `DISMISSED` and `EXPIRED` are different facts: one is "I looked and said no", the other is
    "it ran out of time". A journal that could not tell them apart could not answer the only
    question that matters about a watchlist, which is whether the names on it were the right ones.
    """
    row = await load(session, user_id=user_id, watch_id=watch_id)
    row.state = DISMISSED
    await session.flush()
    return row
