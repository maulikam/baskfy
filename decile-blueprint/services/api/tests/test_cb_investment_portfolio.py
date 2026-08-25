"""Tree 5 / leaf B3 — a curated-basket investment can live inside a portfolio.

Migration 0019 gave ``cb_investment`` a nullable ``portfolio_id``. This suite is the acceptance
for the API that makes it reachable, and it runs against a **real** PostgreSQL rather than a
mock, because three of the things it has to prove are database facts that a mock would happily
lie about:

* ``ON DELETE SET NULL`` — deleting a portfolio must not delete the book underneath it;
* the nullability of ``portfolio_id`` — an unfiled investment must stay a complete row;
* tenant scoping — the ``user_id`` predicate has to actually filter rows, not merely be present
  in a compiled statement.

The handlers are called directly with a live ``AsyncSession`` rather than over HTTP. That keeps
the suite off Redis (the ``api`` fixture needs it) while still exercising the real query path,
the real problem responses, and the real foreign keys.
"""

from __future__ import annotations

import datetime as dt
import inspect
from dataclasses import dataclass
from decimal import Decimal

import pytest
from api_helpers import make_user
from screener_helpers import requires_db
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.app import create_app
from baskfy_api.auth import Principal, PrincipalKind
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.routers import curated_investments
from baskfy_api.routers.curated_investments import (
    PortfolioLinkBody,
    get_investment,
    link_investment_to_portfolio,
    list_investments,
    unlink_investment_from_portfolio,
)
from baskfy_core.curated_baskets import SOLE_USER_ENV
from baskfy_core.models import (
    BrokerAccount,
    CbBasket,
    CbBasketVersion,
    CbInvestment,
    CbInvestmentHolding,
    CbManager,
    CbOrderBatch,
    Instrument,
    Portfolio,
)

#: Applied per test rather than to the module: the three checks at the foot of this file — the
#: order-path grep, the OpenAPI shape and the pure conflict rule — need no database, and marking
#: them ``db`` would mean the safety grep stops running in the default ``make test``.
db_test = pytest.mark.db

#: A unique-slug counter. ``cb_manager.slug`` and ``cb_basket.slug`` are UNIQUE, and the seeded
#: database this suite runs against already carries the two seed managers.
_SEQ = iter(range(1, 10_000))


@dataclass(frozen=True, slots=True)
class Book:
    """One user with one investment, and the rows around it that must survive an unlink."""

    user_id: int
    principal: Principal
    #: The broker account the *investment* is held at.
    broker_account_id: int
    #: A second broker account of the same user, so a mismatch is representable.
    other_broker_account_id: int
    investment_id: int
    instrument_id: int


async def _instrument_id(session: AsyncSession) -> int:
    """Any seeded instrument. The suite never asserts on which one."""
    found = await session.scalar(select(Instrument.id).order_by(Instrument.id).limit(1))
    assert found is not None, "the seeded reference export should carry instruments"
    return int(found)


async def _broker_account(session: AsyncSession, user_id: int, broker_id: str) -> int:
    row = BrokerAccount(user_id=user_id, broker_id=broker_id, label="primary")
    session.add(row)
    await session.flush()
    return int(row.id)


async def _basket(session: AsyncSession) -> tuple[int, int]:
    """A published basket with one GENESIS version. Returns ``(basket_id, version_id)``."""
    n = next(_SEQ)
    manager = CbManager(slug=f"b3-manager-{n}", name=f"B3 Manager {n}", kind="ENGINE")
    session.add(manager)
    await session.flush()
    basket = CbBasket(
        slug=f"b3-basket-{n}",
        name=f"B3 Basket {n}",
        manager_id=manager.id,
        type="STOCK",
        access="FREE",
        visibility="PUBLISHED",
        rebalance_frequency="QUARTERLY",
        source="SCAN",
    )
    session.add(basket)
    await session.flush()
    version = CbBasketVersion(
        basket_id=basket.id,
        version_no=1,
        effective_date=dt.date(2026, 1, 1),
        label="GENESIS",
    )
    session.add(version)
    await session.flush()
    return int(basket.id), int(version.id)


async def _book(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    *,
    email: str = "b3.owner@example.com",
    sole: bool = True,
) -> Book:
    """A user, two broker accounts, a basket, an investment with a holding and a buy batch."""
    user_id, public_id = await make_user(session, email)
    if sole:
        monkeypatch.setenv(SOLE_USER_ENV, str(user_id))
    account = await _broker_account(session, user_id, "zerodha")
    other = await _broker_account(session, user_id, "upstox")
    basket_id, version_id = await _basket(session)
    investment = CbInvestment(
        user_id=user_id,
        broker_account_id=account,
        basket_id=basket_id,
        status="ACTIVE",
        version_applied_id=version_id,
    )
    session.add(investment)
    await session.flush()
    instrument_id = await _instrument_id(session)
    session.add(
        CbInvestmentHolding(
            investment_id=investment.id,
            instrument_id=instrument_id,
            qty=Decimal("10"),
            avg_price=Decimal("100.0000"),
        )
    )
    session.add(
        CbOrderBatch(
            user_id=user_id,
            broker_account_id=account,
            investment_id=investment.id,
            kind="BUY",
            requested_amount=Decimal("1000.00"),
            status="PLANNED",
        )
    )
    await session.flush()
    return Book(
        user_id=user_id,
        principal=Principal(kind=PrincipalKind.USER, user_id=user_id, public_id=public_id),
        broker_account_id=account,
        other_broker_account_id=other,
        investment_id=int(investment.id),
        instrument_id=instrument_id,
    )


async def _portfolio(
    session: AsyncSession, user_id: int, *, name: str, broker_account_id: int | None
) -> int:
    row = Portfolio(user_id=user_id, name=name, broker_account_id=broker_account_id)
    session.add(row)
    await session.flush()
    return int(row.id)


# --- G1: the link ------------------------------------------------------------


@db_test
@requires_db
async def test_link_files_an_investment_under_a_portfolio(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = await _book(screener_session, monkeypatch)
    portfolio_id = await _portfolio(
        screener_session, book.user_id, name="Core", broker_account_id=book.broker_account_id
    )

    out = await link_investment_to_portfolio(
        book.investment_id,
        PortfolioLinkBody(portfolio_id=portfolio_id),
        screener_session,
        book.principal,
    )

    assert out.investment_id == book.investment_id
    assert out.portfolio_id == portfolio_id
    assert out.portfolio_name == "Core"
    # The column, not just the response body: the join has to be in the database.
    stored = await screener_session.scalar(
        select(CbInvestment.portfolio_id).where(CbInvestment.id == book.investment_id)
    )
    assert stored == portfolio_id
    # And the read path reports it, so the portfolio view can count this investment.
    detail = await get_investment(book.investment_id, screener_session, book.principal)
    assert detail.portfolio_id == portfolio_id
    assert detail.portfolio_name == "Core"


@db_test
@requires_db
async def test_link_is_idempotent_and_moves_between_portfolios(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-filing where it already sits is a no-op; re-filing elsewhere is a move, not a copy."""
    book = await _book(screener_session, monkeypatch)
    first = await _portfolio(
        screener_session, book.user_id, name="Core", broker_account_id=book.broker_account_id
    )
    second = await _portfolio(
        screener_session, book.user_id, name="Satellite", broker_account_id=None
    )

    once = await link_investment_to_portfolio(
        book.investment_id, PortfolioLinkBody(portfolio_id=first), screener_session, book.principal
    )
    twice = await link_investment_to_portfolio(
        book.investment_id, PortfolioLinkBody(portfolio_id=first), screener_session, book.principal
    )
    assert once.model_dump() == twice.model_dump()

    moved = await link_investment_to_portfolio(
        book.investment_id,
        PortfolioLinkBody(portfolio_id=second),
        screener_session,
        book.principal,
    )
    assert moved.portfolio_id == second
    # One investment, one filing — a move must not have left a second row behind anywhere.
    filed_under_first = await screener_session.scalars(
        select(CbInvestment.id).where(CbInvestment.portfolio_id == first)
    )
    assert list(filed_under_first) == []


@db_test
@requires_db
async def test_link_never_rewrites_either_side_broker_attribution(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filing is bookkeeping. It may not restate where the shares are, or what the folder claims."""
    book = await _book(screener_session, monkeypatch)
    portfolio_id = await _portfolio(
        screener_session,
        book.user_id,
        name="Declared elsewhere",
        broker_account_id=book.other_broker_account_id,
    )

    await link_investment_to_portfolio(
        book.investment_id,
        PortfolioLinkBody(portfolio_id=portfolio_id),
        screener_session,
        book.principal,
    )

    assert (
        await screener_session.scalar(
            select(CbInvestment.broker_account_id).where(CbInvestment.id == book.investment_id)
        )
        == book.broker_account_id
    )
    assert (
        await screener_session.scalar(
            select(Portfolio.broker_account_id).where(Portfolio.id == portfolio_id)
        )
        == book.other_broker_account_id
    )


# --- G2: tenant isolation ----------------------------------------------------


@db_test
@requires_db
async def test_link_to_another_users_portfolio_is_not_found_never_forbidden(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stranger's portfolio answers exactly like one that does not exist."""
    book = await _book(screener_session, monkeypatch)
    stranger_id, _ = await make_user(screener_session, "b3.stranger@example.com")
    stranger_account = await _broker_account(screener_session, stranger_id, "zerodha")
    stranger_portfolio = await _portfolio(
        screener_session, stranger_id, name="Not yours", broker_account_id=stranger_account
    )

    with pytest.raises(Problem) as refused:
        await link_investment_to_portfolio(
            book.investment_id,
            PortfolioLinkBody(portfolio_id=stranger_portfolio),
            screener_session,
            book.principal,
        )
    assert refused.value.type is ProblemType.NOT_FOUND
    assert refused.value.status == 404, "403 would confirm the id names a real row"

    # The refusal is indistinguishable from the missing-row case: same type, same status, and a
    # detail that names only the id the caller already sent.
    with pytest.raises(Problem) as absent:
        await link_investment_to_portfolio(
            book.investment_id,
            PortfolioLinkBody(portfolio_id=stranger_portfolio + 10_000),
            screener_session,
            book.principal,
        )
    assert absent.value.type is refused.value.type
    assert absent.value.status == refused.value.status
    assert "Not yours" not in refused.value.detail

    # And nothing was written on the way to the refusal.
    assert (
        await screener_session.scalar(
            select(CbInvestment.portfolio_id).where(CbInvestment.id == book.investment_id)
        )
        is None
    )


@db_test
@requires_db
async def test_listing_filtered_by_another_users_portfolio_is_not_found(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty list would say "yours, and empty". That is a different claim, and untrue."""
    book = await _book(screener_session, monkeypatch)
    stranger_id, _ = await make_user(screener_session, "b3.stranger2@example.com")
    stranger_portfolio = await _portfolio(
        screener_session, stranger_id, name="Not yours", broker_account_id=None
    )

    with pytest.raises(Problem) as refused:
        await list_investments(
            screener_session, book.principal, portfolio_id=stranger_portfolio, unassigned=False
        )
    assert refused.value.type is ProblemType.NOT_FOUND


@db_test
@requires_db
async def test_a_foreign_principal_cannot_reach_the_link_at_all(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Track A is sole-tenant: a principal who is not the sole user is refused, not promoted."""
    book = await _book(screener_session, monkeypatch)
    portfolio_id = await _portfolio(
        screener_session, book.user_id, name="Core", broker_account_id=book.broker_account_id
    )
    intruder_id, intruder_public = await make_user(screener_session, "b3.intruder@example.com")
    intruder = Principal(kind=PrincipalKind.USER, user_id=intruder_id, public_id=intruder_public)

    with pytest.raises(Problem) as refused:
        await link_investment_to_portfolio(
            book.investment_id,
            PortfolioLinkBody(portfolio_id=portfolio_id),
            screener_session,
            intruder,
        )
    assert refused.value.status == 404


# --- G3: the unlink ----------------------------------------------------------


@db_test
@requires_db
async def test_unlink_leaves_the_investment_and_everything_under_it_intact(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The book is evidence. Un-filing removes the filing and nothing else."""
    book = await _book(screener_session, monkeypatch)
    portfolio_id = await _portfolio(
        screener_session, book.user_id, name="Core", broker_account_id=book.broker_account_id
    )
    await link_investment_to_portfolio(
        book.investment_id,
        PortfolioLinkBody(portfolio_id=portfolio_id),
        screener_session,
        book.principal,
    )

    out = await unlink_investment_from_portfolio(
        book.investment_id, screener_session, book.principal
    )

    assert out.portfolio_id is None
    assert out.portfolio_name is None
    row = await screener_session.get(CbInvestment, book.investment_id)
    assert row is not None, "unlinking must never delete the investment"
    assert row.portfolio_id is None
    assert row.status == "ACTIVE"
    assert int(row.broker_account_id) == book.broker_account_id
    holdings = list(
        (
            await screener_session.scalars(
                select(CbInvestmentHolding).where(
                    CbInvestmentHolding.investment_id == book.investment_id
                )
            )
        ).all()
    )
    assert len(holdings) == 1
    assert holdings[0].qty == Decimal("10")
    batches = list(
        (
            await screener_session.scalars(
                select(CbOrderBatch).where(CbOrderBatch.investment_id == book.investment_id)
            )
        ).all()
    )
    assert len(batches) == 1
    # The portfolio itself is not collateral damage either — unlinking is not a portfolio delete.
    assert await screener_session.get(Portfolio, portfolio_id) is not None


@db_test
@requires_db
async def test_unlink_is_idempotent_when_the_investment_is_already_unfiled(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking for a state the row is already in succeeds; inventing a 404 for it would not."""
    book = await _book(screener_session, monkeypatch)

    first = await unlink_investment_from_portfolio(
        book.investment_id, screener_session, book.principal
    )
    second = await unlink_investment_from_portfolio(
        book.investment_id, screener_session, book.principal
    )

    assert first.portfolio_id is None
    assert first.model_dump() == second.model_dump()
    assert await screener_session.get(CbInvestment, book.investment_id) is not None


@db_test
@requires_db
async def test_unlink_of_another_users_investment_is_not_found(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = await _book(screener_session, monkeypatch)
    stranger = await _book(
        screener_session, monkeypatch, email="b3.stranger3@example.com", sole=False
    )
    # The sole user is `book`; `stranger`'s investment is somebody else's row.
    with pytest.raises(Problem) as refused:
        await unlink_investment_from_portfolio(
            stranger.investment_id, screener_session, book.principal
        )
    assert refused.value.type is ProblemType.NOT_FOUND


# --- G4: the unlinked investment is a first-class citizen --------------------


@db_test
@requires_db
async def test_portfolio_id_is_optional_and_an_unfiled_investment_still_reads(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nullable on purpose (0019). An investment filed under nothing is complete, not broken."""
    book = await _book(screener_session, monkeypatch)

    detail = await get_investment(book.investment_id, screener_session, book.principal)

    assert detail.portfolio_id is None
    assert detail.portfolio_name is None
    assert detail.broker_conflict is False
    assert detail.snapshot is not None, "an unfiled investment still values"
    assert len(detail.holdings) == 1
    assert len(detail.orders) == 1

    listing = await list_investments(screener_session, book.principal)
    assert [row.id for row in listing.items] == [book.investment_id]
    assert listing.total == 1
    assert listing.unassigned_count == 1
    assert listing.broker_conflicts == 0
    assert listing.net_worth is not None


@db_test
@requires_db
async def test_list_counts_unassigned_and_can_narrow_to_them(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two investments, one filed. The default list shows both; each filter shows one."""
    book = await _book(screener_session, monkeypatch)
    basket_id, version_id = await _basket(screener_session)
    second = CbInvestment(
        user_id=book.user_id,
        broker_account_id=book.broker_account_id,
        basket_id=basket_id,
        status="ACTIVE",
        version_applied_id=version_id,
    )
    screener_session.add(second)
    await screener_session.flush()
    second_id = int(second.id)
    portfolio_id = await _portfolio(
        screener_session, book.user_id, name="Core", broker_account_id=book.broker_account_id
    )
    await link_investment_to_portfolio(
        book.investment_id,
        PortfolioLinkBody(portfolio_id=portfolio_id),
        screener_session,
        book.principal,
    )

    everything = await list_investments(screener_session, book.principal)
    assert everything.total == 2, "the default list never hides an unfiled investment"
    assert everything.unassigned_count == 1

    filed = await list_investments(
        screener_session, book.principal, portfolio_id=portfolio_id, unassigned=False
    )
    assert [row.id for row in filed.items] == [book.investment_id]
    assert filed.unassigned_count == 0

    unfiled = await list_investments(screener_session, book.principal, unassigned=True)
    assert [row.id for row in unfiled.items] == [second_id]
    assert unfiled.unassigned_count == 1


@db_test
@requires_db
async def test_asking_for_both_filters_is_refused_rather_than_guessed(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = await _book(screener_session, monkeypatch)
    portfolio_id = await _portfolio(
        screener_session, book.user_id, name="Core", broker_account_id=None
    )
    with pytest.raises(Problem) as refused:
        await list_investments(
            screener_session, book.principal, portfolio_id=portfolio_id, unassigned=True
        )
    assert refused.value.status == 400


# --- G5: broker reconciliation ----------------------------------------------


@db_test
@requires_db
async def test_broker_mismatch_is_reported_and_never_merged(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Portfolio declares one account, investment is held at another: a conflict, surfaced.

    The link is *allowed*. The portfolio's ``broker_account_id`` is a claim about a container,
    and the honest answer to a claim that has stopped being true is to say so — not to forbid
    the user from recording where their money actually is, and emphatically not to rewrite
    either row so the two agree.
    """
    book = await _book(screener_session, monkeypatch)
    portfolio_id = await _portfolio(
        screener_session,
        book.user_id,
        name="Zerodha only",
        broker_account_id=book.other_broker_account_id,
    )

    out = await link_investment_to_portfolio(
        book.investment_id,
        PortfolioLinkBody(portfolio_id=portfolio_id),
        screener_session,
        book.principal,
    )

    assert out.portfolio_id == portfolio_id, "the filing is recorded, not refused"
    assert out.broker is not None
    assert out.broker.conflict is True
    assert out.broker.investment_broker_account_id == book.broker_account_id
    assert out.broker.portfolio_broker_account_id == book.other_broker_account_id
    assert out.broker.investment_broker_id == "zerodha"
    assert out.broker.portfolio_broker_id == "upstox"
    assert out.broker.portfolio_spans_brokers is False
    assert out.broker.detail is not None
    assert "zerodha" in out.broker.detail
    assert "upstox" in out.broker.detail

    # Reported on every read afterwards, not only in the response to the write.
    detail = await get_investment(book.investment_id, screener_session, book.principal)
    assert detail.broker_conflict is True
    listing = await list_investments(screener_session, book.principal)
    assert listing.broker_conflicts == 1


@db_test
@requires_db
async def test_a_portfolio_that_spans_brokers_is_never_a_broker_conflict(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``portfolio.broker_account_id IS NULL`` declares a roll-up. Holding one broker's money
    today does not contradict it — the same asymmetry ``BrokerRollup.declaration_conflicts``
    already encodes, so the API and the roll-up cannot disagree about the same two rows."""
    book = await _book(screener_session, monkeypatch)
    portfolio_id = await _portfolio(
        screener_session, book.user_id, name="Everything", broker_account_id=None
    )

    out = await link_investment_to_portfolio(
        book.investment_id,
        PortfolioLinkBody(portfolio_id=portfolio_id),
        screener_session,
        book.principal,
    )

    assert out.broker is not None
    assert out.broker.conflict is False
    assert out.broker.portfolio_spans_brokers is True
    assert out.broker.portfolio_broker_account_id is None
    assert out.broker.detail is None
    detail = await get_investment(book.investment_id, screener_session, book.principal)
    assert detail.broker_conflict is False


@db_test
@requires_db
async def test_matching_broker_accounts_reconcile_without_a_conflict(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = await _book(screener_session, monkeypatch)
    portfolio_id = await _portfolio(
        screener_session, book.user_id, name="Core", broker_account_id=book.broker_account_id
    )

    out = await link_investment_to_portfolio(
        book.investment_id,
        PortfolioLinkBody(portfolio_id=portfolio_id),
        screener_session,
        book.principal,
    )

    assert out.broker is not None
    assert out.broker.conflict is False
    assert out.broker.detail is None
    assert out.broker.investment_broker_id == out.broker.portfolio_broker_id == "zerodha"


@db_test
@requires_db
async def test_no_broker_reconciliation_is_reported_when_nothing_is_filed(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There are not two attributions to reconcile, so reporting one would be inventing it."""
    book = await _book(screener_session, monkeypatch)

    out = await unlink_investment_from_portfolio(
        book.investment_id, screener_session, book.principal
    )

    assert out.portfolio_id is None
    assert out.broker is None


@db_test
@requires_db
async def test_a_conflict_survives_the_unlink_and_relink_round_trip(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un-filing does not "resolve" a conflict by forgetting it: re-file and it is still there."""
    book = await _book(screener_session, monkeypatch)
    portfolio_id = await _portfolio(
        screener_session,
        book.user_id,
        name="Declared elsewhere",
        broker_account_id=book.other_broker_account_id,
    )
    body = PortfolioLinkBody(portfolio_id=portfolio_id)

    before = await link_investment_to_portfolio(
        book.investment_id, body, screener_session, book.principal
    )
    await unlink_investment_from_portfolio(book.investment_id, screener_session, book.principal)
    after = await link_investment_to_portfolio(
        book.investment_id, body, screener_session, book.principal
    )

    assert before.broker is not None
    assert after.broker is not None
    assert before.broker.model_dump() == after.broker.model_dump()
    assert after.broker.conflict is True


# --- G6: deleting a portfolio is not deleting a book -------------------------


@db_test
@requires_db
async def test_delete_of_a_portfolio_does_not_delete_its_investments(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ON DELETE SET NULL``, proved against the database rather than read off the model.

    This is the worst outcome available in this leaf: a user deletes a grouping and loses the
    record of what they own. The investment, its holdings and its order batches all have to be
    exactly where they were, with only the filing gone.
    """
    book = await _book(screener_session, monkeypatch)
    portfolio_id = await _portfolio(
        screener_session, book.user_id, name="Doomed", broker_account_id=book.broker_account_id
    )
    await link_investment_to_portfolio(
        book.investment_id,
        PortfolioLinkBody(portfolio_id=portfolio_id),
        screener_session,
        book.principal,
    )

    await screener_session.execute(delete(Portfolio).where(Portfolio.id == portfolio_id))
    await screener_session.flush()
    screener_session.expire_all()

    survivor = await screener_session.get(CbInvestment, book.investment_id)
    assert survivor is not None, "deleting a portfolio must never delete the book"
    assert survivor.portfolio_id is None, "the filing is cleared, not left dangling"
    # Again straight off the database, so no identity map can answer on its behalf.
    raw = [
        tuple(row)
        for row in (
            await screener_session.execute(
                text("SELECT id, portfolio_id FROM cb_investment WHERE id = :id"),
                {"id": book.investment_id},
            )
        ).all()
    ]
    assert raw == [(book.investment_id, None)]
    assert survivor.status == "ACTIVE"
    holdings = list(
        (
            await screener_session.scalars(
                select(CbInvestmentHolding).where(
                    CbInvestmentHolding.investment_id == book.investment_id
                )
            )
        ).all()
    )
    assert [h.qty for h in holdings] == [Decimal("10")]
    batches = list(
        (
            await screener_session.scalars(
                select(CbOrderBatch).where(CbOrderBatch.investment_id == book.investment_id)
            )
        ).all()
    )
    assert [b.requested_amount for b in batches] == [Decimal("1000.00")]

    # And the API still serves it, now as an unfiled investment.
    detail = await get_investment(book.investment_id, screener_session, book.principal)
    assert detail.portfolio_id is None
    assert detail.broker_conflict is False
    listing = await list_investments(screener_session, book.principal)
    assert listing.unassigned_count == 1


@db_test
@requires_db
async def test_delete_of_one_portfolio_leaves_another_portfolios_filing_alone(
    screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SET NULL`` must clear exactly the rows that pointed at the deleted portfolio."""
    book = await _book(screener_session, monkeypatch)
    basket_id, version_id = await _basket(screener_session)
    second = CbInvestment(
        user_id=book.user_id,
        broker_account_id=book.broker_account_id,
        basket_id=basket_id,
        status="ACTIVE",
        version_applied_id=version_id,
    )
    screener_session.add(second)
    await screener_session.flush()
    # Read the id out now: ``expire_all()`` below would otherwise make ``second.id`` a lazy
    # refresh, and a lazy load off an expired attribute cannot await inside a sync property.
    second_id = int(second.id)
    doomed = await _portfolio(screener_session, book.user_id, name="Doomed", broker_account_id=None)
    kept = await _portfolio(screener_session, book.user_id, name="Kept", broker_account_id=None)
    await link_investment_to_portfolio(
        book.investment_id, PortfolioLinkBody(portfolio_id=doomed), screener_session, book.principal
    )
    await link_investment_to_portfolio(
        second_id, PortfolioLinkBody(portfolio_id=kept), screener_session, book.principal
    )

    await screener_session.execute(delete(Portfolio).where(Portfolio.id == doomed))
    await screener_session.flush()
    screener_session.expire_all()

    assert (
        await screener_session.scalar(
            select(CbInvestment.portfolio_id).where(CbInvestment.id == book.investment_id)
        )
        is None
    )
    assert (
        await screener_session.scalar(
            select(CbInvestment.portfolio_id).where(CbInvestment.id == second_id)
        )
        == kept
    )


@db_test
@requires_db
async def test_the_delete_rule_on_the_filing_is_set_null_not_cascade(
    screener_session: AsyncSession,
) -> None:
    """The spec behind the two tests above, asserted against the catalog rather than inferred.

    ``cb_investment.portfolio_id`` is ``ON DELETE SET NULL`` (migration 0019). If somebody ever
    "tidies" that to ``CASCADE``, the two behavioural tests above would still be the ones that
    fail — but this one names the cause in a single line, which is what a 3am reader needs.
    """
    rule = await screener_session.scalar(
        text(
            """
            SELECT rc.delete_rule
            FROM information_schema.referential_constraints AS rc
            JOIN information_schema.key_column_usage AS kcu
              ON kcu.constraint_name = rc.constraint_name
             AND kcu.constraint_schema = rc.constraint_schema
            WHERE kcu.table_name = 'cb_investment'
              AND kcu.column_name = 'portfolio_id'
            """
        )
    )
    assert rule == "SET NULL", f"cb_investment.portfolio_id is ON DELETE {rule}"


# --- G7: no order path -------------------------------------------------------


def test_router_records_a_book_entry_and_has_no_order_path() -> None:
    """Non-negotiable #1 and Law 2. Linking is bookkeeping; nothing here can reach a broker."""
    source = inspect.getsource(curated_investments)
    for forbidden in (
        "OrderGateway",
        "place_order",
        "kc.place_order",
        "/execute",
        "kiteconnect",
        "baskfy_execution",
    ):
        assert forbidden not in source, forbidden


def test_openapi_exposes_link_and_unlink_and_keeps_the_investment_optional() -> None:
    schema = create_app().openapi()
    path = schema["paths"]["/api/v1/cb/investments/{investment_id}/portfolio"]
    assert "put" in path
    assert "delete" in path
    assert "post" not in path, "a link is an idempotent filing, not a new resource"

    row = schema["components"]["schemas"]["InvestmentRowOut"]
    assert "portfolio_id" in row["properties"]
    assert "broker_conflict" in row["properties"]
    assert "portfolio_id" not in row.get("required", []), "0019 keeps the column nullable"

    link = schema["components"]["schemas"]["InvestmentPortfolioLinkOut"]
    assert "broker" in link["properties"]
    assert "portfolio_id" not in link.get("required", [])


def test_the_conflict_rule_is_the_same_asymmetry_the_rollup_uses() -> None:
    """Pure check of the rule, with no database in the way — both directions, and the null case."""
    declares_one = Portfolio(user_id=1, name="one", broker_account_id=7)
    spans = Portfolio(user_id=1, name="spans", broker_account_id=None)

    conflict = curated_investments._broker_conflict
    assert conflict(investment_broker_account_id=9, portfolio=declares_one) is True
    assert conflict(investment_broker_account_id=7, portfolio=declares_one) is False
    assert conflict(investment_broker_account_id=9, portfolio=spans) is False
    assert conflict(investment_broker_account_id=9, portfolio=None) is False
