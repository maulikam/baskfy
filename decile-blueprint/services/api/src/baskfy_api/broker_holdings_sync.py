"""Persist a broker's holdings into a portfolio the broker owns — M75.

**The gap this closes.** `POST /brokers/{id}/sync-holdings` read the account and returned the
rows in its response. Nothing wrote them down. `portfolio_holding` was reachable only from a CSV
import, a manual replace and reconciliation, so connecting Zerodha left the Portfolio page saying
"Holdings not synced yet" forever, which is exactly what it said on 1 Sep 2026 to an account that
had just connected successfully.

**One portfolio per broker account, owned by the broker.** The shape the redesign already
describes: `source=HOLDING_GROUP` (§3 — "shares you already own, grouped"), `kind=CAPITAL` (§4.1
— it is real money and sums into net worth), and `broker_account_id` set, which the column
comments define as "attributable to one broker account". A sync replaces the rows in *that*
portfolio and no other, so nothing a person typed or imported is ever overwritten by a broker
poll. That separation is the whole reason this writes to its own group rather than merging.

**`started_on` is the day we first saw the shares, and it is never moved.** It is what
`MetricKind.SINCE_GROUPED` measures from, and re-stamping it on each sync would silently restart
the return clock every night. A CAS import is what upgrades that to `XIRR_SINCE_PURCHASE`; until
then "since we first saw them" is the only honest start date, and it has to stay put.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from baskfy_execution.broker_ports import total_quantity
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.broker_holdings import HoldingsResult
from baskfy_api.portfolios import replace_holdings, resolve_symbols
from baskfy_core.allocation_ledger import PortfolioKind, PortfolioSource
from baskfy_core.models import Portfolio, PortfolioHolding
from baskfy_core.models.swing import SwPosition

log = logging.getLogger(__name__)

#: Only these carry numbers that belong to the person. `fixture` is sample data that DRY_RUN and
#: `BASKFY_BROKER_HOLDINGS_FIXTURE` produce, and `empty`/`unwired` carry no rows at all. Writing a
#: fixture into somebody's portfolio would put invented positions behind a real rupee total — the
#: single worst thing this module could do, so it is a constant and not an `if` in a branch.
_PERSISTABLE_SOURCES: frozenset[str] = frozenset({"live"})


@dataclass(frozen=True)
class BrokerSyncResult:
    """What one sync did, in numbers a caller can print without recomputing."""

    persisted: bool
    written: int
    unresolved: tuple[str, ...]
    portfolio_id: int | None
    reason: str


def is_persistable(result: HoldingsResult) -> bool:
    """Would this read be written down? Asked BEFORE any database work is done.

    Split out of :func:`sync_holdings_into_portfolio` so the router can skip the whole path for a
    read that will never be stored. Without it, a DRY_RUN fixture or an unwired broker still
    created a `broker_account` row on the way to being refused — a write to answer "no".
    """
    return result.source in _PERSISTABLE_SOURCES and not result.degraded


def not_persisted(result: HoldingsResult) -> BrokerSyncResult:
    """The refusal, with the reason a caller can print verbatim."""
    return BrokerSyncResult(
        persisted=False,
        written=0,
        unresolved=(),
        portfolio_id=None,
        reason=(
            f"not persisted: holdings source is {result.source!r}"
            f"{' and degraded' if result.degraded else ''}. Only a live read is written to a "
            "portfolio — a fixture behind a real total would be indistinguishable from your "
            "own positions."
        ),
    )


async def portfolio_for_broker_account(
    session: AsyncSession, *, user_id: int, broker_account_id: int, broker_name: str, as_of: dt.date
) -> Portfolio:
    """The broker's own holding group, created once and then reused.

    Looked up by `(user_id, broker_account_id, source)` rather than by name, so renaming the
    portfolio in the UI does not orphan it and cause the next sync to create a second one.
    """
    existing = (
        await session.execute(
            select(Portfolio).where(
                Portfolio.user_id == user_id,
                Portfolio.broker_account_id == broker_account_id,
                Portfolio.source == PortfolioSource.HOLDING_GROUP.value,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    portfolio = Portfolio(
        user_id=user_id,
        broker_account_id=broker_account_id,
        name=f"{broker_name} holdings",
        kind=PortfolioKind.CAPITAL.value,
        source=PortfolioSource.HOLDING_GROUP.value,
        # First sight, set once. See the module docstring on why this must not move.
        started_on=as_of,
    )
    session.add(portfolio)
    await session.flush()
    return portfolio


# Keyword-only after the two positionals, so PLR0913's actual hazard cannot occur; each name here
# is a distinct fact about the sync and folding them into an object would only rename them.
async def sync_holdings_into_portfolio(  # noqa: PLR0913
    session: AsyncSession,
    result: HoldingsResult,
    *,
    user_id: int,
    broker_account_id: int,
    broker_name: str,
    as_of: dt.date,
) -> BrokerSyncResult:
    """Write `result` into the broker's holding group, or explain why it did not.

    Refuses rather than guesses in three cases, each of which would otherwise put a number in
    front of Maulik that is not his: a source that is not `live`, a degraded read (the broker was
    unreachable and fixtures were substituted), and a symbol this build cannot resolve to an
    instrument. The first two abandon the whole sync; the third skips that row and names it, so a
    newly-listed SME symbol missing from the instrument table costs one line rather than the sync.
    """
    # Kept as well as the router's pre-check: this function must be safe to call directly, and
    # one wording for the refusal means the two cannot drift apart.
    if not is_persistable(result):
        return not_persisted(result)

    resolutions = await resolve_symbols(session, [row.symbol for row in result.rows])
    wanted: list[tuple[int, Decimal | None, Decimal | None]] = []
    unresolved: list[str] = []
    for row in result.rows:
        resolution = resolutions.get(row.symbol)
        # `SymbolResolution.instrument_id` is None unless the match was unambiguous, so an
        # AMBIGUOUS symbol lands in `unresolved` rather than being guessed at. Typed attribute
        # access rather than `getattr(..., None)`: if that property is ever renamed this should
        # fail loudly, not quietly decide that nothing resolved.
        instrument_id = resolution.instrument_id if resolution is not None else None
        if instrument_id is None:
            unresolved.append(row.symbol)
            continue
        # Non-negotiable #2: what you hold is quantity + T1 + collateral, never `quantity` alone.
        wanted.append((instrument_id, total_quantity(row), row.average_price))

    portfolio = await portfolio_for_broker_account(
        session,
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker_name=broker_name,
        as_of=as_of,
    )
    # The desk's own positions file themselves first, so the pile below is trimmed by them too.
    # A failure here must not lose the sync: the holdings are the point, and an unfiled swing
    # position is a sorting job, not a wrong number.
    try:
        await file_swing_positions(
            session,
            user_id=user_id,
            broker_account_id=broker_account_id,
            held={instrument_id: q for instrument_id, q, _ in wanted if q is not None},
            as_of=as_of,
        )
    except Exception:
        log.exception("could not file swing positions for broker account %s", broker_account_id)

    wanted = await _minus_what_is_filed_elsewhere(
        session,
        wanted,
        user_id=user_id,
        broker_account_id=broker_account_id,
        pile_portfolio_id=int(portfolio.id),
    )
    written = await replace_holdings(session, portfolio, wanted, added_on=as_of)
    return BrokerSyncResult(
        persisted=True,
        written=written,
        unresolved=tuple(unresolved),
        portfolio_id=portfolio.id,
        reason=f"{written} holding(s) written to {portfolio.name} from a live read",
    )


async def _minus_what_is_filed_elsewhere(
    session: AsyncSession,
    wanted: list[tuple[int, Decimal | None, Decimal | None]],
    *,
    user_id: int,
    broker_account_id: int,
    pile_portfolio_id: int,
) -> list[tuple[int, Decimal | None, Decimal | None]]:
    """The broker's quantities, less whatever the user has already filed into real portfolios.

    **This is the bug 0035 would otherwise have introduced, and it is a double-count.**
    `replace_holdings` makes the broker's own group hold *exactly* what it is given, which was
    right when a holding lived in one place: the sync said "you hold 100 ITC" and 100 was the
    only row. Since 0035 a user can file 20 of that ITC into Long term, which takes 20 out of
    this group — and the next sync would put all 100 back, leaving 120 shares of a 100-share
    position spread over two rows that each look reasonable.

    So the broker's group holds the **remainder**, and it is what §6.6 means by Unallocated: the
    part of the pile nobody has sorted yet. A holding filed away entirely drops out of this list
    and out of the group, which is exactly the behaviour that makes the first-run pile shrink as
    the user works through it.

    A position filed to *more* than the broker reports clamps at zero rather than going negative.
    That state means shares left the account after they were filed — a sell — and inventing a
    negative row here would corrupt the total while the reconciliation inbox is the thing that
    is supposed to ask about it (§4.3).
    """
    filed = (
        await session.execute(
            select(
                PortfolioHolding.instrument_id,
                func.sum(PortfolioHolding.quantity),
            )
            .join(Portfolio, Portfolio.id == PortfolioHolding.portfolio_id)
            .where(
                Portfolio.user_id == user_id,
                PortfolioHolding.broker_account_id == broker_account_id,
                PortfolioHolding.portfolio_kind == PortfolioKind.CAPITAL.value,
                PortfolioHolding.portfolio_id != pile_portfolio_id,
            )
            .group_by(PortfolioHolding.instrument_id)
        )
    ).all()
    elsewhere = {int(instrument_id): total for instrument_id, total in filed if total is not None}
    if not elsewhere:
        return wanted

    remainders: list[tuple[int, Decimal | None, Decimal | None]] = []
    for instrument_id, quantity, avg_price in wanted:
        allocated = elsewhere.get(instrument_id)
        if allocated is None or quantity is None:
            remainders.append((instrument_id, quantity, avg_price))
            continue
        remaining = quantity - allocated
        if remaining > 0:
            remainders.append((instrument_id, remaining, avg_price))
    return remainders


#: The portfolio swing-desk positions file themselves into. Looked up by name and source rather
#: than by a stored id, because it is created lazily and a user may rename it — and if they do,
#: a renamed group keeps its shares rather than being orphaned by the next sync.
SWING_PORTFOLIO_NAME = "Swing"


async def swing_portfolio(session: AsyncSession, *, user_id: int, as_of: dt.date) -> Portfolio:
    """The user's Swing group, created once. ``source=MY_STRATEGY`` — the desk trades it.

    Not ``HOLDING_GROUP``: that source means "shares you already own, grouped after the fact" and
    it is what `portfolio_for_broker_account` looks its pile up by. Giving Swing the same source
    would make the pile lookup ambiguous and, worse, would give it "since grouped" as its headline
    metric when the desk knows the real entry date of every position it opened (§5.2).
    """
    existing = (
        await session.execute(
            select(Portfolio).where(
                Portfolio.user_id == user_id,
                Portfolio.source == PortfolioSource.MY_STRATEGY.value,
                Portfolio.name == SWING_PORTFOLIO_NAME,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    portfolio = Portfolio(
        user_id=user_id,
        name=SWING_PORTFOLIO_NAME,
        kind=PortfolioKind.CAPITAL.value,
        source=PortfolioSource.MY_STRATEGY.value,
        started_on=as_of,
    )
    session.add(portfolio)
    await session.flush()
    return portfolio


async def file_swing_positions(
    session: AsyncSession,
    *,
    user_id: int,
    broker_account_id: int,
    held: Mapping[int, Decimal],
    as_of: dt.date,
) -> int:
    """File every open swing position into the Swing group. Returns the number of rows written.

    Maulik asked for this on 10 Sep 2026, alongside the split itself: the desk already records
    every swing entry in `sw_position`, so making him hand-sort the one strategy the product
    trades for him would be asking him to re-enter data the product wrote.

    **Idempotent, and it SETS rather than adds.** `sw_position.quantity_open` is the truth about
    how many shares the strategy still holds — it shrinks on a partial exit and goes to zero on a
    close — so each sync makes the slice equal it. Adding would double the position on the second
    sync of the day; setting means a partial exit shrinks the slice and a closed position releases
    its shares back to the pile without anything having to remember what it filed last time.

    **It never takes shares another portfolio has claimed.** The slice is capped at what is left
    after the user's own filings, so a name they moved into "Long term" by hand is not quietly
    taken back by the desk. If the cap bites, the swing slice is smaller than the desk's position
    — which is visible and correctable, where over-allocating would be neither.
    """
    swing = await swing_portfolio(session, user_id=user_id, as_of=as_of)
    swing_id = int(swing.id)

    open_positions = (
        await session.execute(
            select(SwPosition.instrument_id, SwPosition.quantity_open).where(
                SwPosition.user_id == user_id,
                SwPosition.broker_account_id == broker_account_id,
                SwPosition.state.in_(("OPEN", "PARTIAL")),
                SwPosition.quantity_open > 0,
            )
        )
    ).all()
    wanted = {int(instrument_id): Decimal(quantity) for instrument_id, quantity in open_positions}

    # Everything already filed for this broker, per instrument, EXCLUDING the swing group itself
    # and the broker's own pile — those are the two the desk is allowed to take from.
    pile = await portfolio_for_broker_account(
        session,
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker_name="",
        as_of=as_of,
    )
    claimed_rows = (
        await session.execute(
            select(PortfolioHolding.instrument_id, func.sum(PortfolioHolding.quantity))
            .join(Portfolio, Portfolio.id == PortfolioHolding.portfolio_id)
            .where(
                Portfolio.user_id == user_id,
                PortfolioHolding.broker_account_id == broker_account_id,
                PortfolioHolding.portfolio_kind == PortfolioKind.CAPITAL.value,
                PortfolioHolding.portfolio_id.not_in((swing_id, int(pile.id))),
            )
            .group_by(PortfolioHolding.instrument_id)
        )
    ).all()
    claimed = {int(i): total for i, total in claimed_rows if total is not None}

    existing_rows = (
        await session.execute(
            select(PortfolioHolding.instrument_id, PortfolioHolding.quantity).where(
                PortfolioHolding.portfolio_id == swing_id,
                PortfolioHolding.broker_account_id == broker_account_id,
            )
        )
    ).all()
    already = {int(i): quantity for i, quantity in existing_rows}

    written = 0
    for instrument_id in sorted(set(wanted) | set(already)):
        free = held.get(instrument_id, Decimal(0)) - claimed.get(instrument_id, Decimal(0))
        target = min(wanted.get(instrument_id, Decimal(0)), max(free, Decimal(0)))
        current = already.get(instrument_id)
        if target <= 0:
            if current is not None:
                await session.execute(
                    delete(PortfolioHolding).where(
                        PortfolioHolding.portfolio_id == swing_id,
                        PortfolioHolding.instrument_id == instrument_id,
                        PortfolioHolding.broker_account_id == broker_account_id,
                    )
                )
            continue
        if current is None:
            session.add(
                PortfolioHolding(
                    portfolio_id=swing_id,
                    instrument_id=instrument_id,
                    broker_account_id=broker_account_id,
                    portfolio_kind=PortfolioKind.CAPITAL.value,
                    quantity=target,
                    added_on=as_of,
                )
            )
            written += 1
        elif current != target:
            await session.execute(
                update(PortfolioHolding)
                .where(
                    PortfolioHolding.portfolio_id == swing_id,
                    PortfolioHolding.instrument_id == instrument_id,
                    PortfolioHolding.broker_account_id == broker_account_id,
                )
                .values(quantity=target)
            )
            written += 1
    await session.flush()
    return written
