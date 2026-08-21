"""Portfolios, symbol resolution and rebalance execution — docs/07 §"Portfolios & rebalance".

The database half of Prompt 14. ``baskfy_core.rebalance`` owns the *rule* and
``baskfy_core.portfolio_csv`` owns the *parser*; both are pure. This module is the I/O between
them: resolving a symbol to an ``instrument`` row, loading a portfolio's holdings, running the
screen the rebalance is measured against, and writing the history row.

Symbol resolution, and why "ambiguous" is a real answer
------------------------------------------------------
``instrument`` is unique on ``(exchange_id, symbol, series)``, so one symbol can legitimately name
more than one row — ``EQ`` and ``BE`` listings of the same company are the common case, and a
``symbol_alias`` from a rename can collide with a live symbol belonging to somebody else. Picking
one silently would put a holding in a user's portfolio that they did not choose, and Prompt 14 §1
asks for the opposite: a report that says "ambiguous" and shows the candidates.

Resolution order, most specific first:

1. an **active** NSE instrument with that symbol,
2. any NSE instrument with that symbol (a delisted holding is still a holding — docs/01 §10 keeps
   delisted rows precisely so a past position can be named),
3. a ``symbol_alias.old_symbol`` pointing at one.

A tier that returns exactly one row wins. A tier that returns several is ambiguous and resolution
stops there rather than falling through to a vaguer tier.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import Select, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.screener import (
    AsOfResolution,
    current_data_version,
    execute_screen,
    resolve_as_of,
)
from baskfy_core.models import (
    Instrument,
    Portfolio,
    PortfolioHolding,
    PortfolioRebalance,
    SymbolAlias,
)
from baskfy_core.models.base import JsonObject
from baskfy_core.portfolio_csv import (
    MatchStatus,
    ParsedHolding,
    SymbolShape,
    UnmatchedReason,
)
from baskfy_core.rebalance import HeldName, RebalancePlan, ScreenRank, plan_rebalance
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_core.seed_data import NSE_EXCHANGE_ID

__all__ = [
    "Candidate",
    "PortfolioNotFound",
    "RebalanceOutcome",
    "SymbolResolution",
    "held_names",
    "load_portfolio",
    "lookup_symbols",
    "record_rebalance",
    "replace_holdings",
    "resolution_for",
    "resolve_symbols",
    "run_rebalance",
]


class PortfolioNotFound(LookupError):
    """No portfolio with that id belongs to this user.

    Not-yours and does-not-exist are the same answer, for the reason
    ``baskfy_api.routers.screens`` gives: a 404 that distinguishes them confirms the id exists.
    """


@dataclass(frozen=True, slots=True)
class Candidate:
    """One instrument a symbol could mean."""

    instrument_id: int
    symbol: str
    name: str
    series: str | None
    delisted_on: dt.date | None


@dataclass(frozen=True, slots=True)
class SymbolResolution:
    """What one symbol from the file turned into."""

    symbol: str
    status: MatchStatus
    candidates: tuple[Candidate, ...] = ()
    reason: UnmatchedReason | None = None
    #: Set when the symbol was reached through ``symbol_alias`` rather than directly.
    matched_via_alias: bool = False

    @property
    def instrument_id(self) -> int | None:
        return self.candidates[0].instrument_id if self.status is MatchStatus.MATCHED else None


@dataclass(frozen=True, slots=True)
class RebalanceOutcome:
    """A computed rebalance and the context it was computed in."""

    plan: RebalancePlan
    resolution: AsOfResolution
    data_version: int
    screen_result_count: int
    holdings_count: int
    #: Held instruments the screen could not rank *and* that are delisted, for the UI's warning.
    delisted_count: int = field(default=0)


# ---------------------------------------------------------------------------
# Portfolios
# ---------------------------------------------------------------------------


async def load_portfolio(session: AsyncSession, portfolio_id: int, user_id: int) -> Portfolio:
    """The user's portfolio, or :class:`PortfolioNotFound`."""
    row = (
        await session.execute(
            select(Portfolio).where(Portfolio.id == portfolio_id, Portfolio.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise PortfolioNotFound(str(portfolio_id))
    return row


async def held_names(session: AsyncSession, portfolio_id: int) -> tuple[HeldName, ...]:
    """The portfolio's holdings, joined to their instruments, for the rule to consume."""
    rows = (
        await session.execute(
            select(
                PortfolioHolding.instrument_id,
                PortfolioHolding.quantity,
                Instrument.symbol,
                Instrument.name,
                Instrument.delisted_on,
            )
            .join(Instrument, Instrument.id == PortfolioHolding.instrument_id)
            .where(PortfolioHolding.portfolio_id == portfolio_id)
            .order_by(Instrument.symbol)
        )
    ).all()
    return tuple(
        HeldName(
            instrument_id=row.instrument_id,
            symbol=row.symbol,
            name=row.name,
            quantity=row.quantity,
            delisted=row.delisted_on is not None,
        )
        for row in rows
    )


async def replace_holdings(
    session: AsyncSession,
    portfolio_id: int,
    holdings: Iterable[tuple[int, Decimal | None, Decimal | None]],
    *,
    added_on: dt.date,
) -> int:
    """Make the portfolio's holdings exactly ``holdings``. Returns the row count.

    ``PUT /portfolios/{id}/holdings`` is a replacement, not a merge — that is what PUT means, and
    a merge would give a user no way to remove a position. ``added_on`` is preserved for a name
    that was already held, so a re-upload of the same file does not reset every purchase date;
    docs/04 gives the column no other meaning, and "the day this row appeared" is the only one it
    can carry.
    """
    existing = {
        row.instrument_id: row.added_on
        for row in (
            await session.execute(
                select(PortfolioHolding.instrument_id, PortfolioHolding.added_on).where(
                    PortfolioHolding.portfolio_id == portfolio_id
                )
            )
        ).all()
    }
    wanted = list(holdings)
    keep = {instrument_id for instrument_id, _, _ in wanted}

    for instrument_id in existing.keys() - keep:
        await session.execute(
            delete(PortfolioHolding).where(
                PortfolioHolding.portfolio_id == portfolio_id,
                PortfolioHolding.instrument_id == instrument_id,
            )
        )
    for instrument_id, quantity, avg_price in wanted:
        if instrument_id in existing:
            await session.execute(
                update(PortfolioHolding)
                .where(
                    PortfolioHolding.portfolio_id == portfolio_id,
                    PortfolioHolding.instrument_id == instrument_id,
                )
                .values(quantity=quantity, avg_price=avg_price)
            )
        else:
            session.add(
                PortfolioHolding(
                    portfolio_id=portfolio_id,
                    instrument_id=instrument_id,
                    quantity=quantity,
                    avg_price=avg_price,
                    added_on=added_on,
                )
            )
    await session.flush()
    return len(wanted)


# ---------------------------------------------------------------------------
# Symbol resolution
# ---------------------------------------------------------------------------


def _instrument_query() -> Select[tuple[int, str, str, str | None, dt.date | None, bool]]:
    return select(
        Instrument.id,
        Instrument.symbol,
        Instrument.name,
        Instrument.series,
        Instrument.delisted_on,
        Instrument.is_active,
    ).where(Instrument.exchange_id == NSE_EXCHANGE_ID)


async def resolve_symbols(
    session: AsyncSession, symbols: Sequence[str]
) -> dict[str, SymbolResolution]:
    """Resolve every symbol in one pass. Two queries total, whatever the file's length."""
    wanted = list(dict.fromkeys(symbols))
    if not wanted:
        return {}

    direct: dict[str, list[Candidate]] = {}
    for row in (
        await session.execute(_instrument_query().where(Instrument.symbol.in_(wanted)))
    ).all():
        direct.setdefault(row.symbol, []).append(
            Candidate(
                instrument_id=row.id,
                symbol=row.symbol,
                name=row.name,
                series=row.series,
                delisted_on=row.delisted_on,
            )
        )
    active: dict[str, list[Candidate]] = {}
    for row in (
        await session.execute(
            _instrument_query().where(Instrument.symbol.in_(wanted), Instrument.is_active.is_(True))
        )
    ).all():
        active.setdefault(row.symbol, []).append(
            Candidate(
                instrument_id=row.id,
                symbol=row.symbol,
                name=row.name,
                series=row.series,
                delisted_on=row.delisted_on,
            )
        )

    unresolved = [symbol for symbol in wanted if symbol not in direct]
    aliased: dict[str, list[Candidate]] = {}
    if unresolved:
        for alias_row in (
            await session.execute(
                select(
                    SymbolAlias.old_symbol,
                    Instrument.id,
                    Instrument.symbol,
                    Instrument.name,
                    Instrument.series,
                    Instrument.delisted_on,
                )
                .join(Instrument, Instrument.id == SymbolAlias.instrument_id)
                .where(SymbolAlias.old_symbol.in_(unresolved))
            )
        ).all():
            aliased.setdefault(alias_row.old_symbol, []).append(
                Candidate(
                    instrument_id=alias_row.id,
                    symbol=alias_row.symbol,
                    name=alias_row.name,
                    series=alias_row.series,
                    delisted_on=alias_row.delisted_on,
                )
            )

    return {
        symbol: _resolve_one(
            symbol, active.get(symbol, []), direct.get(symbol, []), aliased.get(symbol, [])
        )
        for symbol in wanted
    }


def _resolve_one(
    symbol: str,
    active: Sequence[Candidate],
    any_listing: Sequence[Candidate],
    aliases: Sequence[Candidate],
) -> SymbolResolution:
    for tier, via_alias in ((active, False), (any_listing, False), (aliases, True)):
        if len(tier) == 1:
            return SymbolResolution(
                symbol=symbol,
                status=MatchStatus.MATCHED,
                candidates=(tier[0],),
                matched_via_alias=via_alias,
            )
        if len(tier) > 1:
            return SymbolResolution(
                symbol=symbol,
                status=MatchStatus.AMBIGUOUS,
                candidates=tuple(
                    sorted(tier, key=lambda item: (item.series or "", item.instrument_id))
                ),
                matched_via_alias=via_alias,
            )
    return SymbolResolution(
        symbol=symbol, status=MatchStatus.UNMATCHED, reason=UnmatchedReason.UNKNOWN_SYMBOL
    )


def resolution_for(
    row: ParsedHolding, resolved: Mapping[str, SymbolResolution]
) -> SymbolResolution:
    """The resolution for a parsed row, short-circuiting the shapes no lookup can help with.

    A BSE scrip code and a token that is not a symbol at all never reach the database: there is
    nothing to look them up *by*, and reporting "unknown symbol" for ``532540`` would hide the
    actual problem, which is that the user exported from the wrong exchange.
    """
    if row.shape is SymbolShape.BSE_CODE:
        return SymbolResolution(
            symbol=row.symbol, status=MatchStatus.UNMATCHED, reason=UnmatchedReason.BSE_CODE
        )
    if row.shape is SymbolShape.INVALID:
        return SymbolResolution(
            symbol=row.symbol, status=MatchStatus.UNMATCHED, reason=UnmatchedReason.INVALID
        )
    return resolved.get(
        row.symbol,
        SymbolResolution(
            symbol=row.symbol,
            status=MatchStatus.UNMATCHED,
            reason=UnmatchedReason.UNKNOWN_SYMBOL,
        ),
    )


def lookup_symbols(rows: Sequence[ParsedHolding]) -> tuple[str, ...]:
    """The subset of parsed symbols worth a database round trip."""
    return tuple(row.symbol for row in rows if row.shape is SymbolShape.SYMBOL)


# ---------------------------------------------------------------------------
# The rebalance
# ---------------------------------------------------------------------------


async def run_rebalance(  # noqa: PLR0913 - the screen, the date and the two rule inputs are separate
    session: AsyncSession,
    *,
    portfolio_id: int,
    definition: ScreenDefinition,
    top_n: int,
    hold_buffer: int,
    requested_as_of: dt.date | None = None,
) -> RebalanceOutcome:
    """Run the screen, load the holdings, apply the rule.

    The screen is executed **uncached**: :func:`baskfy_api.screener.run_screen` returns the cached
    JSON bytes on a hit and no object graph, and the rule needs ``instrument_id`` per row, which
    the payload does not carry. The cost is one query on a request that is already doing several,
    and the alternative — re-parsing the cached payload and matching on symbol — is exactly the
    string-matching the rule refuses to do (``baskfy_core.rebalance.plan_rebalance``).
    """
    requested = requested_as_of if requested_as_of is not None else definition.historical_date
    resolution = await resolve_as_of(session, requested)
    data_version = await current_data_version(session)
    result = await execute_screen(
        session,
        definition,
        as_of=resolution.as_of,
        data_version=data_version,
        requested_as_of=requested,
    )
    ranked = tuple(
        ScreenRank(
            instrument_id=row.instrument_id,
            symbol=row.symbol,
            name=str(row.values.get("name", row.symbol)),
            rank=row.rank,
        )
        for row in result.rows
    )
    held = await held_names(session, portfolio_id)
    plan = plan_rebalance(ranked, held, top_n=top_n, hold_buffer=hold_buffer)
    return RebalanceOutcome(
        plan=plan,
        resolution=resolution,
        data_version=data_version,
        screen_result_count=result.result_count,
        holdings_count=len(held),
        delisted_count=sum(1 for holding in held if holding.delisted),
    )


async def record_rebalance(
    session: AsyncSession,
    *,
    portfolio_id: int,
    screen_id: int | None,
    outcome: RebalanceOutcome,
    payload: JsonObject,
) -> PortfolioRebalance:
    """Prompt 14 §4: persist what the user was told, and when.

    ``payload`` is the response body, stored verbatim. Nothing here re-derives it later.
    """
    row = PortfolioRebalance(
        portfolio_id=portfolio_id,
        screen_id=screen_id,
        as_of=outcome.resolution.as_of,
        data_version=outcome.data_version,
        top_n=outcome.plan.top_n,
        hold_buffer=outcome.plan.hold_buffer,
        payload=payload,
    )
    session.add(row)
    await session.flush()
    return row
