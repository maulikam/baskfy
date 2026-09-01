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
from dataclasses import dataclass
from decimal import Decimal

from baskfy_execution.broker_ports import total_quantity
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.broker_holdings import HoldingsResult
from baskfy_api.portfolios import replace_holdings, resolve_symbols
from baskfy_core.allocation_ledger import PortfolioKind, PortfolioSource
from baskfy_core.models import Portfolio

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
    written = await replace_holdings(session, portfolio, wanted, added_on=as_of)
    return BrokerSyncResult(
        persisted=True,
        written=written,
        unresolved=tuple(unresolved),
        portfolio_id=portfolio.id,
        reason=f"{written} holding(s) written to {portfolio.name} from a live read",
    )
