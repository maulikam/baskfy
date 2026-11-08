"""Portfolios, symbol resolution and rebalance execution — docs/07 §"Portfolios & rebalance".

The database half of Prompt 14. ``baskfy_core.rank_buffer`` owns the *rule* and
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
3. a ``symbol_alias.old_symbol`` pointing at one,
4. the same symbol carrying a **G-sec series suffix** — see :data:`GSEC_SERIES_SUFFIXES`.

A tier that returns exactly one row wins. A tier that returns several is ambiguous and resolution
stops there rather than falling through to a vaguer tier.

Tier 4 and the bond that belonged to nobody (12 Sep 2026)
---------------------------------------------------------
Zerodha spells one instrument two ways, and the difference cost a real holding its place in the
ledger. Kite's **instruments** dump gives the Sovereign Gold Bond as ``SGBDE31III-GB`` — symbol
plus the ``GB`` series — and that is the row in ``instrument`` (id 12753 on the box, token
5753857). Kite's **holdings** endpoint reports the very same position as ``SGBDE31III``, bare.
Resolution was exact-match, so every sync since the account was connected filed that symbol under
``unresolved`` and moved on: the bond was held by the broker, worth real money, and present in no
portfolio at all — not even the broker's own pile, which is where everything unfiled is supposed
to land. It was found by `gates/pktea-phantom.md` P8, as the ``broker_has_unfiled=1`` line of an
audit looking for something else.

The bridge is a tier rather than a ``symbol_alias`` row because the bare name is not an *old*
name: nothing was renamed, the two spellings are simultaneous and both current, and filing one
instrument's row by hand would leave the next bond Maulik buys just as invisible. It is restricted
to ``GB``/``GS`` — the series `baskfy_execution.guards.UNTOUCHABLE_SERIES` names, the government
paper this quirk is observed on — and **never** to the ``BE``/``SM``/``ST`` suffixes that share
the shape: 7,238 of the box's 11,215 symbols carry a dash, and a general rule over those would be
a machine for silently filing one company's shares under another's name. Over the 178 ``-GB`` and
``-GS`` instruments the box actually holds, **no** bare counterpart exists, so the tier can never
shadow a real symbol; and because it runs last, a bare symbol that resolves on its own always wins
anyway.

Resolving the name is what makes the holding *reconcilable*, which is the point. A row filed by
hand, against a symbol the sync cannot see, is write-once — the exact defect
`baskfy_api.broker_holdings_sync` was taught to sweep for on the same day.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from sqlalchemy import Select, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.broker_accounts import ensure_default_broker_account
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
from baskfy_core.rank_buffer import HeldName, RebalancePlan, ScreenRank, plan_rebalance
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_core.seed_data import NSE_EXCHANGE_ID

__all__ = [
    "GSEC_SERIES_SUFFIXES",
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
    "resolve_broker_account",
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


async def resolve_broker_account(session: AsyncSession, portfolio: Portfolio) -> int:
    """Which broker account a holding written into ``portfolio`` belongs to.

    Migration 0019 put ``broker_account_id`` in ``portfolio_holding``'s primary key and made it
    ``NOT NULL``, because a share is always held *somewhere* and "held, broker unknown" is a
    missing fact rather than a position. The migration also installed a ``BEFORE INSERT`` trigger
    that fills the column in when a writer leaves it NULL, so that no writer — present or future,
    including one nobody has written yet — can produce an unattributed row.

    This function is the same rule, spelled in Python, so that the *live* writer names the
    account itself and the trigger is left as a backstop rather than as the mechanism. Two
    reasons it is worth doing here as well as there:

    * a value the application chose is a value the application can report, test and reason
      about; a value a trigger chose arrives only after a round trip, and only if somebody
      remembers to read it back;
    * the resolution order below is the one the rest of the API already uses for the same
      question (``baskfy_api.curated_investments`` calls
      :func:`~baskfy_api.broker_accounts.ensure_default_broker_account` for ``cb_investment``),
      so a holding and an investment made on the same day land on the same account.

    Order, most specific first:

    1. the portfolio's own ``broker_account_id`` — a portfolio that declares itself attributable
       to one account is the strongest statement anyone has made about where its money is;
    2. otherwise the owner's default broker account, created if they have none.

    Step 2 differs from the trigger in one narrow case, and deliberately: the trigger, unable to
    call Python, falls back to *any* account the owner has before creating one, while this
    prefers :data:`~baskfy_core.tenancy.DEFAULT_BROKER_ID` and creates it. A user whose only
    account is at another broker therefore gets a default account here and their existing one
    from the trigger. The divergence is unreachable for this writer — it names the account, so
    the trigger never fires for it — and the honest fix for the general case belongs with P4.2,
    where a holding will carry the account it was actually synced from.
    """
    if portfolio.broker_account_id is not None:
        return portfolio.broker_account_id
    return await ensure_default_broker_account(session, portfolio.user_id)


async def replace_holdings(
    session: AsyncSession,
    portfolio: Portfolio,
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

    **Broker attribution (0019).** Every row written here names its ``broker_account_id``, chosen
    by :func:`resolve_broker_account`. The caller's input — a CSV or a JSON array of symbols —
    carries no broker column, so it describes *one* row per instrument, and this function makes
    the stored rows say exactly that: a name previously held at two accounts collapses to one row
    at the resolved account, and the count returned is the number of rows the caller asked for.
    A replacement that left another account's row standing would report an import of five names
    into a portfolio that then held six, which is the kind of quiet disagreement house rule 8
    exists to prevent.

    ``added_on`` for a name that was held at *several* accounts is the earliest of them — the
    same rule migration 0019's ``downgrade()`` uses when it merges those rows, so a portfolio
    that goes down and up again through the migration and a portfolio replaced through this
    function agree on the date.
    """
    broker_account_id = await resolve_broker_account(session, portfolio)
    existing = (
        await session.execute(
            select(
                PortfolioHolding.instrument_id,
                PortfolioHolding.broker_account_id,
                PortfolioHolding.added_on,
            ).where(PortfolioHolding.portfolio_id == portfolio.id)
        )
    ).all()
    #: instrument -> the earliest date it appeared under, across every account it sits at.
    first_seen: dict[int, dt.date] = {}
    for row in existing:
        seen = first_seen.get(row.instrument_id)
        first_seen[row.instrument_id] = row.added_on if seen is None else min(seen, row.added_on)
    already_here = {
        row.instrument_id for row in existing if row.broker_account_id == broker_account_id
    }

    wanted = list(holdings)
    keep = {instrument_id for instrument_id, _, _ in wanted}

    #: One statement rather than one per name: everything this portfolio holds that the caller
    #: did not name, plus everything it named that is sitting at a different account.
    await session.execute(
        delete(PortfolioHolding).where(
            PortfolioHolding.portfolio_id == portfolio.id,
            or_(
                PortfolioHolding.instrument_id.notin_(keep),
                PortfolioHolding.broker_account_id != broker_account_id,
            ),
        )
    )
    for instrument_id, quantity, avg_price in wanted:
        if instrument_id in already_here:
            await session.execute(
                update(PortfolioHolding)
                .where(
                    PortfolioHolding.portfolio_id == portfolio.id,
                    PortfolioHolding.instrument_id == instrument_id,
                    PortfolioHolding.broker_account_id == broker_account_id,
                )
                #: ``added_on`` is re-stated rather than left alone, because the surviving row
                #: may be inheriting the date of a row at another account that this replacement
                #: is deleting. For a name held in one place it is the value already there.
                .values(
                    quantity=quantity,
                    avg_price=avg_price,
                    added_on=first_seen.get(instrument_id, added_on),
                )
            )
        else:
            session.add(
                PortfolioHolding(
                    portfolio_id=portfolio.id,
                    instrument_id=instrument_id,
                    broker_account_id=broker_account_id,
                    quantity=quantity,
                    avg_price=avg_price,
                    added_on=first_seen.get(instrument_id, added_on),
                )
            )
    await session.flush()
    return len(wanted)


# ---------------------------------------------------------------------------
# Symbol resolution
# ---------------------------------------------------------------------------


#: The series suffixes the instrument master carries and Kite's holdings endpoint drops.
#:
#: ``GB`` is the Sovereign Gold Bond series and ``GS`` the dated government securities — the two
#: `baskfy_execution.guards.UNTOUCHABLE_SERIES` refuses to trade, and the only place this product
#: has observed Zerodha using two spellings for one instrument at the same time. See the module
#: docstring for why this is deliberately not widened to ``BE``/``SM``/``ST``: those suffixes mark
#: a *different listing of a real company*, and guessing across them would file one firm's shares
#: under another's name. Written with the dash so the constant is the thing appended, not a rule
#: about how to append it.
GSEC_SERIES_SUFFIXES: Final[tuple[str, ...]] = ("-GB", "-GS")


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
    """Resolve every symbol in one pass. At most four queries, whatever the file's length.

    Two always (the listing tiers), plus one for the alias tier and one for the G-sec suffix tier
    — and each of those two runs only when something is still unanswered, so the ordinary file of
    ordinary symbols still costs exactly two.
    """
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

    # Tier 4, last and narrowest: the same name with a G-sec series suffix. Only for symbols that
    # neither a listing nor an alias could answer, so a bare symbol that means something on its
    # own can never be shadowed by a suffixed one.
    still_unknown = [symbol for symbol in unresolved if symbol not in aliased]
    suffixed: dict[str, list[Candidate]] = {}
    if still_unknown:
        forms = {
            f"{symbol}{suffix}": symbol
            for symbol in still_unknown
            for suffix in GSEC_SERIES_SUFFIXES
        }
        for row in (
            await session.execute(_instrument_query().where(Instrument.symbol.in_(forms)))
        ).all():
            suffixed.setdefault(forms[row.symbol], []).append(
                Candidate(
                    instrument_id=row.id,
                    symbol=row.symbol,
                    name=row.name,
                    series=row.series,
                    delisted_on=row.delisted_on,
                )
            )

    return {
        symbol: _resolve_one(
            symbol,
            active.get(symbol, []),
            direct.get(symbol, []),
            aliased.get(symbol, []),
            suffixed.get(symbol, []),
        )
        for symbol in wanted
    }


def _resolve_one(
    symbol: str,
    active: Sequence[Candidate],
    any_listing: Sequence[Candidate],
    aliases: Sequence[Candidate],
    series_suffixed: Sequence[Candidate] = (),
) -> SymbolResolution:
    # ``via_alias`` stays False for the suffixed tier, and that is a statement rather than an
    # omission: `matched_via_alias` means "this instrument used to be called that", and the bond
    # was never called anything else. What the caller needs is in `candidates[0].symbol`, which
    # carries the instrument's real spelling however the tier was reached.
    tiers = ((active, False), (any_listing, False), (aliases, True), (series_suffixed, False))
    for tier, via_alias in tiers:
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
    string-matching the rule refuses to do (``baskfy_core.rank_buffer.plan_rebalance``).
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
