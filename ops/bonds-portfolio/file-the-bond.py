"""Create the **Bonds** portfolio and file `SGBDE31III` into it, through the audited routes.

    AWS_PROFILE=baskfy-poc bash tools/deploy/box-python.sh api ops/bonds-portfolio/file-the-bond.py

THIS SCRIPT WRITES. Everything else in `ops/` is a read; this one is the exception, and it is the
parent's to run, once, **after the deploy that carries the tier-4 symbol bridge** (without it the
sync cannot see the bond and step 2 finds nothing to file).

WHY A SCRIPT AND NOT A NEW ENDPOINT
-----------------------------------
There is no new path here. It calls the two handlers the product already has, in the order a
person clicking the product would trigger them:

1. ``baskfy_api.routers.brokers.sync_holdings`` — the "Sync holdings" button. A live, read-only
   Kite fetch, written into the broker's own pile. Unfiled shares land in the pile as Unallocated;
   that is what the pile is for.
2. ``baskfy_api.routers.portfolio_overview.new_portfolio`` — `PORTFOLIO_REDESIGN.md` §6.7's
   confirm step, the "New portfolio" flow. It moves an allocation; it conserves shares; it refuses
   in words if the arithmetic does not work.

Both are called with a real ``AsyncSession`` and a real ``Principal``, which is how
`services/api/tests/test_portfolio_write.py` exercises them. The refusals, the tenancy checks and
the conservation invariant are therefore exactly the deployed ones — there is no second
implementation to keep honest.

WHAT IT WILL NOT DO
-------------------
* It places no order and cannot: nothing it imports can reach `OrderGateway`, and it asserts the
  bond is untradeable **before** filing it, so a build in which the guard had been weakened stops
  here rather than filing a share into a portfolio that could route it (non-negotiable #6).
* It creates nothing if a portfolio called "Bonds" already exists for this user — a second one
  would split the holding across two capital groups and make both wrong.
* It writes no quantity of its own. The body names the holding and **omits** the quantity, which
  `HoldingKeyIn` defines as "everything not already filed elsewhere", so the number comes from the
  broker's own read rather than from anything typed here.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from baskfy_api.auth import Principal, PrincipalKind
from baskfy_api.routers.brokers import sync_holdings
from baskfy_api.routers.portfolio_overview import (
    HoldingKeyIn,
    NewPortfolioIn,
    new_portfolio,
)
from baskfy_api.settings import get_settings
from baskfy_core.allocation_ledger import PortfolioKind, PortfolioSource
from baskfy_core.models import AppUser, Instrument, Portfolio, PortfolioHolding
from baskfy_execution.guards import UntouchableInstrumentError, assert_tradeable

#: Maulik's account. The box has two users; this is the one whose Zerodha session the API holds
#: and whose `broker_account` the holdings sync writes to. Asserted below rather than assumed.
USER_ID = 1
BROKER_ID = "zerodha"

#: What Kite's holdings endpoint calls it, and what `instrument` calls it. Both are current; see
#: `baskfy_api.portfolios` tier 4 for why they differ.
BOND_AS_HOLDINGS_REPORTS = "SGBDE31III"
BOND_AS_THE_MASTER_SPELLS_IT = "SGBDE31III-GB"

PORTFOLIO_NAME = "Bonds"


async def main() -> None:
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            await _run(session)
    finally:
        await engine.dispose()


async def _run(session) -> None:  # noqa: ANN001 - an AsyncSession, typed by its only caller
    # ---- 0. The guard, first. ------------------------------------------------------------
    # If this build would trade the bond, nothing below should run: filing it would be putting a
    # tradeable SGB into a portfolio, which is the one outcome this whole exercise forbids.
    for spelling in (BOND_AS_HOLDINGS_REPORTS, BOND_AS_THE_MASTER_SPELLS_IT):
        try:
            assert_tradeable(spelling)
        except UntouchableInstrumentError:
            continue
        print(f"REFUSED: {spelling} is tradeable in this build. Non-negotiable #6 is broken.")
        return
    print("guard=ok both spellings are untouchable")

    user = await session.get(AppUser, USER_ID)
    if user is None:
        print(f"REFUSED: no app_user {USER_ID}")
        return
    principal = Principal(kind=PrincipalKind.USER, user_id=USER_ID, public_id=user.public_id)
    print(f"user={USER_ID} email={user.email}")

    existing = await session.scalar(
        select(Portfolio.id).where(
            Portfolio.user_id == USER_ID, Portfolio.name == PORTFOLIO_NAME
        )
    )
    if existing is not None:
        print(f"REFUSED: portfolio {existing} is already called {PORTFOLIO_NAME!r}; nothing done")
        return

    bond_id = await session.scalar(
        select(Instrument.id).where(Instrument.symbol == BOND_AS_THE_MASTER_SPELLS_IT)
    )
    if bond_id is None:
        print(f"REFUSED: no instrument row for {BOND_AS_THE_MASTER_SPELLS_IT}")
        return
    print(f"instrument={bond_id} symbol={BOND_AS_THE_MASTER_SPELLS_IT}")
    print(f"before: {await _where_is(session, int(bond_id))}")

    # ---- 1. The "Sync holdings" button. --------------------------------------------------
    # One read-only Kite call. With tier 4 deployed the bond now resolves, so it lands in the
    # broker's pile as Unallocated instead of being reported as an unresolved symbol.
    synced = await sync_holdings(principal, session, BROKER_ID)
    print(
        f"sync: source={synced.source} persisted={synced.persisted} written={synced.written} "
        f"pile={synced.portfolio_id} unresolved={synced.unresolved}"
    )
    if not synced.persisted:
        print("REFUSED: the broker read was not persistable; nothing was filed")
        return
    if BOND_AS_HOLDINGS_REPORTS in synced.unresolved:
        print("REFUSED: the bond is STILL unresolved — tier 4 is not in the deployed image")
        return

    held = await _where_is(session, int(bond_id))
    print(f"after sync: {held}")
    if not held:
        print("REFUSED: the broker did not report the bond in this read; nothing to file")
        return

    accounts = await _accounts_holding(session, int(bond_id))
    if len(accounts) != 1:
        print(f"REFUSED: the bond sits at {len(accounts)} broker accounts {accounts}; not guessing")
        return
    broker_account_id = accounts[0]
    print(f"broker_account={broker_account_id}")

    # ---- 2. §6.7's "New portfolio". ------------------------------------------------------
    # No quantity: "everything not already filed elsewhere", resolved by the route against the
    # ledger. The number is the broker's, never this script's.
    detail = await new_portfolio(
        NewPortfolioIn(
            name=PORTFOLIO_NAME,
            kind=PortfolioKind.CAPITAL,
            source=PortfolioSource.HOLDING_GROUP,
            holdings=[
                HoldingKeyIn(
                    instrument_id=int(bond_id),
                    broker_account_id=broker_account_id,
                )
            ],
        ),
        session,
        principal,
    )
    await session.commit()

    print(
        f"created: portfolio={detail.summary.portfolio_id} name={detail.summary.name!r} "
        f"kind={detail.summary.kind.value} source={detail.summary.source.value} "
        f"counts_toward_total={detail.summary.counts_toward_total} value={detail.summary.value}"
    )
    print(f"after: {await _where_is(session, int(bond_id))}")


async def _where_is(session, instrument_id: int) -> dict[str, Decimal]:  # noqa: ANN001
    """``'<portfolio id> <name>' -> quantity`` for one instrument, straight out of the table."""
    rows = (
        await session.execute(
            select(Portfolio.id, Portfolio.name, PortfolioHolding.quantity)
            .join(PortfolioHolding, PortfolioHolding.portfolio_id == Portfolio.id)
            .where(PortfolioHolding.instrument_id == instrument_id)
        )
    ).all()
    return {f"{pid} {name}": Decimal(quantity) for pid, name, quantity in rows}


async def _accounts_holding(session, instrument_id: int) -> list[int]:  # noqa: ANN001
    """Every broker account this instrument is recorded at.

    A *holding* in the ledger is ``(instrument, broker account)``: the same bond at two brokers is
    two positions, allocated and sold apart. If it were ever at two, the right answer is a human
    saying which one goes into Bonds — so this refuses rather than picking.
    """
    rows = (
        await session.execute(
            select(PortfolioHolding.broker_account_id)
            .where(PortfolioHolding.instrument_id == instrument_id)
            .distinct()
        )
    ).scalars()
    return sorted(int(value) for value in rows if value is not None)


if __name__ == "__main__":
    print(f"as_of={dt.datetime.now(tz=dt.UTC).isoformat()}")
    asyncio.run(main())
