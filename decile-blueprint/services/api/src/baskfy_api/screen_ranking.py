"""The I/O half of ranking explain and portfolio selection (docs/ranking/PLAN.md C4 step 6, C5).

``baskfy_core.ranking_engine.explain`` and ``baskfy_core.ranking_selection.select_portfolio`` are
pure; everything they need from the database is read here: the ranked frame (through
:func:`baskfy_api.screener.rank_definition`, the one engine path every screen run takes), the
recent-corporate-action flag, a portfolio's holdings, and the daily returns the correlation cap
reads from ``ohlcv_daily``.

**Informational only.** Nothing in this module plans, places or routes an order, and it imports
nothing that can (``tests/test_api_screens_explain_selection.py`` asserts it, transitively).
A selection is a proposal for a person to read; non-negotiable #1 is untouched.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.screener import RankedFrame, desk_score_version, rank_definition, uses_desk_score
from baskfy_core import ranking_engine
from baskfy_core.models import Instrument, OhlcvDaily, PortfolioHolding, TradingDay
from baskfy_core.ranking_selection import Candidate, Holding
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_core.seed_data import NSE_EXCHANGE_ID

#: C4 step 6: "recent_corporate_action (adj_factor != 1 within 252 sessions)".
CORPORATE_ACTION_SESSIONS = 252

#: The frame column C5 calls ``adv_value_inr``: the 12-month median daily traded value, in rupees.
ADV_VALUE_COLUMN = "median_vol_12m"
CLOSE_RAW_COLUMN = "close_raw"


class InstrumentNotInUniverse(LookupError):
    """The symbol has no row in the definition's universe on ``as_of`` — nothing to explain."""


# ---------------------------------------------------------------------------
# Explain
# ---------------------------------------------------------------------------


async def previous_trading_day(session: AsyncSession, as_of: dt.date) -> dt.date | None:
    """The NSE trading day strictly before ``as_of``, or ``None`` before the calendar starts."""
    return (
        await session.execute(
            select(func.max(TradingDay.date)).where(
                TradingDay.exchange_id == NSE_EXCHANGE_ID,
                TradingDay.is_trading_day.is_(True),
                TradingDay.date < as_of,
            )
        )
    ).scalar_one_or_none()


async def previous_session_rank(
    session: AsyncSession, definition: ScreenDefinition, instrument_id: int, as_of: dt.date
) -> tuple[dt.date | None, int | None]:
    """The previous session and ``instrument_id``'s rank in the same definition's run on it.

    The run is :func:`rank_definition` as of that day — the same engine path, over that day's
    point-in-time frame (membership, factor rows and the ``desk_score_daily`` row of that date;
    house rule 5), never today's frame re-read. A name the run did not rank, or a day with no
    rows, is ``None``: nothing is carried forward or guessed.
    """
    previous = await previous_trading_day(session, as_of)
    if previous is None:
        return None, None
    ranked = await rank_definition(session, definition, previous)
    if ranked.result is None:
        return previous, None
    rows = ranked.result.ranked
    matches = rows.loc[rows[ranking_engine.INSTRUMENT_ID] == instrument_id]
    if matches.empty:
        return previous, None
    return previous, int(str(matches.iloc[0][ranking_engine.RANK]))


async def recent_corporate_action(
    session: AsyncSession, instrument_id: int, as_of: dt.date
) -> bool:
    """Whether a price-adjusting action went ex inside the last 252 sessions up to ``as_of``.

    ``adj_factor`` on a bar is the product of every action with a *later* ex-date
    (``baskfy_core.adjustments``), so under today's "as of today" series a bar before a
    *future* action also has ``adj_factor != 1``. Reading C4's "adj_factor != 1" literally would
    therefore flag an action ``as_of`` could not have known about (house rule 5). The factor
    *changing* inside the window is the point-in-time reading: it changes exactly at an ex-date,
    and an action after ``as_of`` scales every bar of the window alike.
    """
    window = (
        select(OhlcvDaily.adj_factor)
        .where(OhlcvDaily.instrument_id == instrument_id, OhlcvDaily.date <= as_of)
        .order_by(OhlcvDaily.date.desc())
        .limit(CORPORATE_ACTION_SESSIONS)
        .subquery()
    )
    distinct = (
        await session.execute(select(func.count(func.distinct(window.c.adj_factor))))
    ).scalar_one()
    return int(distinct) > 1


async def explain_symbol(
    session: AsyncSession,
    definition: ScreenDefinition,
    symbol: str,
    *,
    as_of: dt.date,
    data_version: int,
) -> ranking_engine.RankExplanation:
    """C4 step 6 for ``symbol`` in the run of ``definition`` on ``as_of``.

    The row comes from ``result.scored`` — every row of the universe, ranked or not — so a name
    that failed a filter is explained too, with ``rank`` null and the failures named.
    ``rank_history`` compares with the previous session's run (:func:`previous_session_rank`).
    Raises :class:`InstrumentNotInUniverse` when the universe has no such row.
    """
    ranked = await rank_definition(session, definition, as_of)
    wanted = symbol.strip().upper()
    if ranked.result is None:
        raise InstrumentNotInUniverse(wanted)
    scored = ranked.result.scored
    matches = scored.loc[scored[ranking_engine.SYMBOL] == wanted]
    if matches.empty:
        raise InstrumentNotInUniverse(wanted)
    row = matches.iloc[0].copy()
    instrument_id = int(str(row[ranking_engine.INSTRUMENT_ID]))
    row[ranking_engine.RECENT_CORPORATE_ACTION] = await recent_corporate_action(
        session, instrument_id, as_of
    )
    previous_as_of, previous_rank = await previous_session_rank(
        session, definition, instrument_id, as_of
    )
    context = ranking_engine.ExplainContext(
        result=ranked.result,
        universe=ranked.query.universe_slug,
        as_of=as_of,
        data_version=data_version,
        desk_score_version=(
            await desk_score_version(session, as_of) if uses_desk_score(definition) else None
        ),
        clause_details=ranked.query.clause_details,
        previous_as_of=previous_as_of,
        previous_rank=previous_rank,
    )
    return ranking_engine.explain(row, context)


# ---------------------------------------------------------------------------
# Selection inputs
# ---------------------------------------------------------------------------


def _number(value: object) -> float | None:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        number = float(value)
        return None if math.isnan(number) else number
    raise TypeError(f"expected a number, got {type(value).__name__}")


def _decimal(value: object) -> Decimal | None:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    if isinstance(value, float):
        return None if math.isnan(value) else Decimal(str(value))
    raise TypeError(f"expected a number, got {type(value).__name__}")


def candidates_from(ranked: RankedFrame) -> tuple[Candidate, ...]:
    """C5 ``Candidate`` rows from the engine's whole ranked output — not a page of it.

    ``score`` is ``composite_score`` in composite mode and the first term's raw value otherwise
    (sequential and single rank on raw values; there is no composite to report). ``close_raw``
    is the exchange print (house rule 6); ``adv_value_inr`` is ``median_vol_12m``.
    """
    if ranked.result is None:
        return ()
    first = ranked.spec.terms[0].key
    score_column = ranking_engine.COMPOSITE_SCORE if ranked.spec.mode == "composite" else first
    out: list[Candidate] = []
    for record in ranked.result.ranked.to_dict("records"):
        sector = record.get(ranking_engine.SECTOR)
        close_raw = _decimal(record.get(CLOSE_RAW_COLUMN))
        out.append(
            Candidate(
                instrument_id=int(str(record[ranking_engine.INSTRUMENT_ID])),
                symbol=str(record[ranking_engine.SYMBOL]),
                quality_rank=int(str(record[ranking_engine.RANK])),
                score=_number(record.get(score_column)),
                sector=sector if isinstance(sector, str) else None,
                adv_value_inr=_decimal(record.get(ADV_VALUE_COLUMN)),
                close_raw=close_raw if close_raw is not None and close_raw > 0 else None,
            )
        )
    return tuple(out)


@dataclass(frozen=True, slots=True)
class BookHoldings:
    """Holdings C5 can count, and the symbols that could not be counted (no quantity)."""

    holdings: tuple[Holding, ...]
    without_quantity: tuple[str, ...]


def aggregate_holdings(rows: Iterable[tuple[int, str, Decimal | None]]) -> BookHoldings:
    """One ``Holding`` per instrument: the quantities of its slices summed.

    A ``portfolio_holding`` row is a slice of a position (0035), so INFY at two brokers is two
    rows and one holding. A name whose slices record no positive quantity is not a position C5
    can size, and is reported rather than silently dropped.
    """
    totals: dict[int, Decimal] = {}
    symbols: dict[int, str] = {}
    for instrument_id, symbol, quantity in rows:
        symbols[instrument_id] = symbol
        totals[instrument_id] = totals.get(instrument_id, Decimal(0)) + (quantity or Decimal(0))
    holdings = tuple(
        Holding(instrument_id=instrument_id, symbol=symbols[instrument_id], quantity=total)
        for instrument_id, total in sorted(totals.items())
        if total > 0
    )
    without = tuple(
        sorted(symbols[instrument_id] for instrument_id, total in totals.items() if total <= 0)
    )
    return BookHoldings(holdings=holdings, without_quantity=without)


async def portfolio_holdings(session: AsyncSession, portfolio_id: int) -> BookHoldings:
    """The holdings of one portfolio the caller has already been proven to own."""
    rows = (
        await session.execute(
            select(PortfolioHolding.instrument_id, Instrument.symbol, PortfolioHolding.quantity)
            .join(Instrument, Instrument.id == PortfolioHolding.instrument_id)
            .where(PortfolioHolding.portfolio_id == portfolio_id)
        )
    ).all()
    return aggregate_holdings((row[0], row[1], row[2]) for row in rows)


def with_candidate_sectors(
    holdings: Sequence[Holding], candidates: Sequence[Candidate]
) -> tuple[Holding, ...]:
    """A holding's sector is its candidate row's point-in-time sector, when it has one."""
    sectors = {candidate.instrument_id: candidate.sector for candidate in candidates}
    return tuple(
        replace(holding, sector=sectors.get(holding.instrument_id)) for holding in holdings
    )


async def daily_returns(
    session: AsyncSession, instrument_ids: Iterable[int], as_of: dt.date, window: int
) -> pd.DataFrame:
    """C5's ``returns``: daily simple returns of ``close`` over the last ``window`` sessions.

    Indexed by trading date (NSE calendar, ``<= as_of``), one float column per instrument id;
    a session with no bar is NaN, so core's pairwise-complete rule sees the gap. ``close`` is the
    adjusted series (house rule 6). Returns are point-in-time even though the series is "as of
    today" (house rule 5): an action after ``as_of`` multiplies every bar of the window by the
    same factor, which cancels in a ratio of two of them.
    """
    ids = sorted(set(instrument_ids))
    sessions = list(
        (
            await session.execute(
                select(TradingDay.date)
                .where(
                    TradingDay.exchange_id == NSE_EXCHANGE_ID,
                    TradingDay.is_trading_day.is_(True),
                    TradingDay.date <= as_of,
                )
                .order_by(TradingDay.date.desc())
                .limit(window + 1)
            )
        ).scalars()
    )
    sessions.reverse()
    if not ids or len(sessions) < 2:  # noqa: PLR2004 - one return needs two closes
        return pd.DataFrame(index=pd.Index([], name="date"), columns=ids, dtype="float64")
    bars = (
        await session.execute(
            select(OhlcvDaily.instrument_id, OhlcvDaily.date, OhlcvDaily.close).where(
                OhlcvDaily.instrument_id.in_(ids),
                OhlcvDaily.date >= sessions[0],
                OhlcvDaily.date <= as_of,
            )
        )
    ).all()
    closes = pd.DataFrame(float("nan"), index=pd.Index(sessions, name="date"), columns=ids)
    wanted: Mapping[dt.date, int] = {day: position for position, day in enumerate(sessions)}
    for instrument_id, date, close in bars:
        position = wanted.get(date)
        if position is not None:
            closes.iat[position, ids.index(int(instrument_id))] = float(close)
    return closes.pct_change(fill_method=None).iloc[1:]
