"""Broker holdings sync — PORTFOLIO_REDESIGN.md §4.3 and acceptance criterion 4.

    "A sell detected by sync either auto-attributes (whole-holding case) or creates a
     reconciliation item — it never silently alters a return series."

Every test here runs the whole chain — fixture provider -> ``attribute_sell`` -> the database —
against real PostgreSQL, with **no network** and no broker credentials of any kind. The provider
is :class:`baskfy_providers.fixtures.FixtureHoldingsProvider`, whose rows the test writes itself;
nothing in this file has ever spoken to Zerodha.

The stories below are deliberately told in two syncs where a single sync could seed the same
state by hand. A hand-seeded ``reconciliation_item`` would prove the sync reads a row somebody
wrote; two syncs prove the sync writes the row the next sync reads, which is the property that
actually has to hold at 3am.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

import pytest
import pytest_asyncio
from helpers import make_instrument, requires_db
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from baskfy_api.seed import seed_exchange
from baskfy_core.allocation_ledger import PortfolioKind, PortfolioSource, ReconciliationReason
from baskfy_core.models import AppUser, BrokerAccount, Instrument, Portfolio, PortfolioHolding
from baskfy_core.models.accounts import BrokerCash, ReconciliationItem
from baskfy_providers.errors import ProviderUnavailable, UpstreamUnavailable
from baskfy_providers.fixtures import FixtureHoldingsProvider
from baskfy_providers.records import BrokerAccountRef, BrokerHoldingRecord
from baskfy_worker.steps import StepOutcome, StepStatus
from baskfy_worker.tasks.holdings_sync import HoldingsSyncResult, run_holdings_sync

pytestmark = [pytest.mark.db, requires_db]

#: Not ``@example.com``: the shared ``clean_db`` fixture deletes those accounts directly, and
#: ``portfolio.user_id`` has no ``ON DELETE`` action — a leftover portfolio would turn that
#: delete into a foreign-key violation and take every other worker test down with it. This suite
#: owns its rows and removes them itself.
EMAIL = "c2-holdings-sync@baskfy.test"

#: Symbols are prefixed so they cannot collide with a real instrument another suite left behind —
#: ``instrument`` is unique on ``(exchange_id, symbol, series)`` and this suite creates its own.
PREFIX = "C2SYNC"
TODAY = dt.date(2026, 8, 18)
TOMORROW = dt.date(2026, 8, 19)


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session that deliberately does **not** use the shared ``clean_db`` fixture.

    ``clean_db`` truncates the pipeline tables and then calls ``seed_reference``, which since
    another tree's in-flight catalogue work reaches ``seed_catalogue`` ->
    ``_first_fillable_genesis`` -> ``resolve_as_of`` and raises ``NoPublishedData`` on a database
    that has just had ``pipeline_run`` truncated. That breaks **every** db-marked worker test at
    present, this suite included, and it is not this leaf's defect to fix — ``services/api`` is
    owned elsewhere and is being edited concurrently.

    Rather than seed a published pipeline run this suite has no use for, it owns its rows
    outright: :func:`_purge` runs before and after every test, and every symbol it creates
    carries :data:`PREFIX`. Nothing here reads the reference universe, so nothing here needs it.
    """
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active, active.begin():
        yield active


@dataclass(slots=True)
class Ledger:
    """The account under test, plus the instrument ids the stories refer to by symbol."""

    user_id: int
    broker_account_id: int
    instruments: dict[str, int]

    def id_of(self, symbol: str) -> int:
        return self.instruments[symbol]


async def _purge(session: AsyncSession) -> None:
    """Remove this suite's rows, in foreign-key order. Runs before and after every test."""
    users = select(AppUser.id).where(AppUser.email == EMAIL).scalar_subquery()
    accounts = select(BrokerAccount.id).where(BrokerAccount.user_id.in_(users)).scalar_subquery()
    portfolios = select(Portfolio.id).where(Portfolio.user_id.in_(users)).scalar_subquery()
    await session.execute(delete(ReconciliationItem).where(ReconciliationItem.user_id.in_(users)))
    await session.execute(delete(BrokerCash).where(BrokerCash.broker_account_id.in_(accounts)))
    await session.execute(
        delete(PortfolioHolding).where(PortfolioHolding.portfolio_id.in_(portfolios))
    )
    await session.execute(delete(Portfolio).where(Portfolio.user_id.in_(users)))
    await session.execute(delete(BrokerAccount).where(BrokerAccount.user_id.in_(users)))
    await session.execute(delete(AppUser).where(AppUser.email == EMAIL))
    await session.execute(delete(Instrument).where(Instrument.symbol.like(f"{PREFIX}%")))
    await session.flush()


@pytest_asyncio.fixture
async def ledger(session: AsyncSession) -> AsyncIterator[Ledger]:
    # The one reference row an instrument needs. Idempotent, and seeded here because this suite
    # does not use the shared seeding — see the `session` fixture above.
    await seed_exchange(session)
    await _purge(session)
    user = AppUser(public_id="c2holdings01", email=EMAIL)
    session.add(user)
    await session.flush()
    account = BrokerAccount(user_id=user.id, broker_id="zerodha", label="primary")
    session.add(account)
    await session.flush()
    instruments = {
        symbol: await make_instrument(session, f"{PREFIX}{symbol}", token=900_000 + offset)
        for offset, symbol in enumerate(("SBIN", "INFY", "TCS", "HDFCBANK", "WIPRO"))
    }
    yield Ledger(user_id=user.id, broker_account_id=account.id, instruments=instruments)
    await _purge(session)


async def make_portfolio(
    session: AsyncSession,
    ledger: Ledger,
    name: str,
    *,
    kind: PortfolioKind = PortfolioKind.CAPITAL,
) -> int:
    portfolio = Portfolio(
        user_id=ledger.user_id,
        name=name,
        kind=kind.value,
        source=PortfolioSource.HOLDING_GROUP.value,
        started_on=TODAY,
        broker_account_id=ledger.broker_account_id,
    )
    session.add(portfolio)
    await session.flush()
    return portfolio.id


async def allocate(  # noqa: PLR0913 - one parameter per column a story needs to set
    session: AsyncSession,
    ledger: Ledger,
    portfolio_id: int,
    symbol: str,
    quantity: str | None,
    *,
    kind: PortfolioKind = PortfolioKind.CAPITAL,
    avg_price: str = "100",
) -> None:
    session.add(
        PortfolioHolding(
            portfolio_id=portfolio_id,
            instrument_id=ledger.id_of(symbol),
            broker_account_id=ledger.broker_account_id,
            portfolio_kind=kind.value,
            quantity=None if quantity is None else Decimal(quantity),
            avg_price=Decimal(avg_price),
            added_on=TODAY,
        )
    )
    await session.flush()


def broker(**positions: str) -> list[BrokerHoldingRecord]:
    """``broker(SBIN="100")`` — what the broker reports, in the simple whole-quantity case."""
    return [
        BrokerHoldingRecord(symbol=f"{PREFIX}{symbol}", quantity=Decimal(quantity))
        for symbol, quantity in positions.items()
    ]


def provider_for(
    ledger: Ledger,
    rows: Sequence[BrokerHoldingRecord],
    cash: Mapping[int, Decimal] | None = None,
) -> FixtureHoldingsProvider:
    return FixtureHoldingsProvider({ledger.broker_account_id: list(rows)}, cash)


async def sync(  # noqa: PLR0913 - the run's inputs, each of which a story varies
    session: AsyncSession,
    ledger: Ledger,
    rows: Sequence[BrokerHoldingRecord],
    *,
    on: dt.date = TODAY,
    cash: Mapping[int, Decimal] | None = None,
    outcome: StepOutcome | None = None,
) -> HoldingsSyncResult:
    return await run_holdings_sync(
        session,
        provider_for(ledger, rows, cash),
        outcome or StepOutcome(),
        broker_account_id=ledger.broker_account_id,
        on=on,
    )


async def open_items(session: AsyncSession, ledger: Ledger) -> list[ReconciliationItem]:
    rows = await session.execute(
        select(ReconciliationItem)
        .where(ReconciliationItem.user_id == ledger.user_id)
        .order_by(ReconciliationItem.instrument_id, ReconciliationItem.id)
    )
    return list(rows.scalars().all())


async def quantity_of(
    session: AsyncSession, ledger: Ledger, portfolio_id: int, symbol: str
) -> Decimal | None:
    value = await session.execute(
        select(PortfolioHolding.quantity).where(
            PortfolioHolding.portfolio_id == portfolio_id,
            PortfolioHolding.instrument_id == ledger.id_of(symbol),
        )
    )
    return value.scalar_one_or_none()


class TestAnAttributedSell:
    """§4.3: "With whole-holding allocation, attribution is automatic ... Apply it silently.\""""

    async def test_a_decrease_in_an_allocated_holding_raises_no_question(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        momentum = await make_portfolio(session, ledger, "Momentum")
        await allocate(session, ledger, momentum, "SBIN", "100")

        result = await sync(session, ledger, broker(SBIN="60"))

        assert result.sells_attributed == 1
        assert result.items_raised == 0
        assert await open_items(session, ledger) == []
        assert await quantity_of(session, ledger, momentum, "SBIN") == Decimal("60.0000")

    async def test_a_full_exit_is_still_attributed(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        """The broker stops reporting the name entirely. Iterating the fetch alone would miss it."""
        momentum = await make_portfolio(session, ledger, "Momentum")
        await allocate(session, ledger, momentum, "SBIN", "100")

        result = await sync(session, ledger, broker())

        assert result.vanished == 1
        assert result.sells_attributed == 1
        assert result.items_raised == 0
        assert await quantity_of(session, ledger, momentum, "SBIN") == Decimal("0.0000")

    async def test_the_monitoring_view_over_the_same_shares_moves_too(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        """§4.5's rule for corporate actions, applied to a sell: one position, one quantity.

        A number that is true in the capital portfolio and stale in a lens over the same shares
        is two answers to one question.
        """
        momentum = await make_portfolio(session, ledger, "Momentum")
        lens = await make_portfolio(session, ledger, "Banks", kind=PortfolioKind.MONITORING)
        await allocate(session, ledger, momentum, "SBIN", "100")
        await allocate(session, ledger, lens, "SBIN", "100", kind=PortfolioKind.MONITORING)

        await sync(session, ledger, broker(SBIN="60"))

        assert await quantity_of(session, ledger, momentum, "SBIN") == Decimal("60.0000")
        assert await quantity_of(session, ledger, lens, "SBIN") == Decimal("60.0000")

    async def test_pledged_and_unsettled_shares_are_not_a_sell(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        """Desk non-negotiable #2, end to end.

        Reading ``quantity`` alone would see 100 shares become 60 and ask the user about a sale
        that never happened. The other 40 are pledged and in the settlement pipe.
        """
        momentum = await make_portfolio(session, ledger, "Momentum")
        await allocate(session, ledger, momentum, "SBIN", "100")

        result = await sync(
            session,
            ledger,
            [
                BrokerHoldingRecord(
                    symbol=f"{PREFIX}SBIN",
                    quantity=Decimal("60"),
                    t1_quantity=Decimal("10"),
                    collateral_quantity=Decimal("30"),
                )
            ],
        )

        assert result.unchanged == 1
        assert result.sells_attributed == 0
        assert await open_items(session, ledger) == []


class TestAQuestionInstead:
    """§4.3: "If sync detects a change that cannot be auto-attributed ... create a
    Reconciliation item"."""

    async def test_a_decrease_in_an_unallocated_holding_asks_which_portfolio(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        """A holding known to us but in no *capital* portfolio — §4.1's lens-only position.

        Unallocated is not a portfolio (``allocation_ledger.UNALLOCATED is None``), so a holding
        that appears only in monitoring views is unallocated by definition, and a sell out of it
        has no owner to be attributed to.
        """
        lens = await make_portfolio(session, ledger, "Defence", kind=PortfolioKind.MONITORING)
        await allocate(session, ledger, lens, "INFY", "80", kind=PortfolioKind.MONITORING)

        result = await sync(session, ledger, broker(INFY="30"))

        assert result.sells_questioned == 1
        assert result.sells_attributed == 0
        items = await open_items(session, ledger)
        assert len(items) == 1
        assert items[0].reason == ReconciliationReason.UNALLOCATED_HOLDING.value
        assert items[0].quantity == Decimal("50.0000")
        assert items[0].state == "OPEN"
        assert items[0].resolved_portfolio_id is None
        # The freeze of §4.3: an unanswered question does not move the ledger.
        assert await quantity_of(session, ledger, lens, "INFY") == Decimal("80.0000")

    async def test_selling_more_than_recorded_asks_and_does_not_alter_the_holding(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        """The two-sync story that makes ``QUANTITY_MISMATCH`` reachable at all.

        Sync one: we have 100 allocated, the broker reports 150. The extra 50 are an inflow we
        cannot account for, so they become a question rather than free shares in a portfolio.
        Sync two: the broker reports nothing. 150 left the account and only 100 were ever ours to
        attribute — attributing the excess would corrupt Momentum's return series with volume it
        never held, which is exactly what ``attribute_sell`` refuses to do.
        """
        momentum = await make_portfolio(session, ledger, "Momentum")
        await allocate(session, ledger, momentum, "SBIN", "100")

        first = await sync(session, ledger, broker(SBIN="150"))
        assert first.inflows_questioned == 1
        assert await quantity_of(session, ledger, momentum, "SBIN") == Decimal("100.0000")

        second = await sync(session, ledger, broker(), on=TOMORROW)

        assert second.sells_questioned == 1
        assert second.sells_attributed == 0
        reasons = {item.reason: item for item in await open_items(session, ledger)}
        mismatch = reasons[ReconciliationReason.QUANTITY_MISMATCH.value]
        assert mismatch.quantity == Decimal("150.0000")
        assert mismatch.suggested_portfolio_id == momentum
        assert await quantity_of(session, ledger, momentum, "SBIN") == Decimal("100.0000")

    async def test_a_never_seen_holding_asks_and_lands_unallocated(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        """§6.6: connect broker -> everything lands in Unallocated.

        Landing unallocated means landing in **no portfolio**: the sync inserts no
        ``portfolio_holding`` row, because inserting one would be an allocation nobody made. The
        open ``UNKNOWN_INFLOW`` item is the landing record — instrument, account, quantity, and
        the question §6.7's grouping flow exists to answer.
        """
        await make_portfolio(session, ledger, "Momentum")

        result = await sync(session, ledger, broker(TCS="25"))

        assert result.new == 1
        assert result.items_raised == 1
        items = await open_items(session, ledger)
        assert [item.reason for item in items] == [ReconciliationReason.UNKNOWN_INFLOW.value]
        assert items[0].quantity == Decimal("25.0000")
        assert items[0].instrument_id == ledger.id_of("TCS")
        assert items[0].broker_account_id == ledger.broker_account_id
        assert items[0].suggested_portfolio_id is None

        allocated = await session.execute(
            select(PortfolioHolding).where(PortfolioHolding.instrument_id == ledger.id_of("TCS"))
        )
        assert allocated.scalars().all() == []

    async def test_a_question_is_never_answered_by_the_sync(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        """It asks; a human answers. Nothing here writes RESOLVED or names a portfolio as fact."""
        lens = await make_portfolio(session, ledger, "Defence", kind=PortfolioKind.MONITORING)
        await allocate(session, ledger, lens, "INFY", "80", kind=PortfolioKind.MONITORING)

        await sync(session, ledger, broker(INFY="30"))

        assert {item.state for item in await open_items(session, ledger)} == {"OPEN"}


class TestIdempotence:
    """House rule 7: re-running any day's job produces identical rows."""

    async def test_syncing_twice_with_unchanged_data_creates_no_second_item(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        await make_portfolio(session, ledger, "Momentum")
        rows = broker(TCS="25")

        first = await sync(session, ledger, rows)
        second = await sync(session, ledger, rows, on=TOMORROW)

        assert first.items_raised == 1
        assert second.items_raised == 0
        assert second.unchanged == 1
        assert second.new == 0
        assert len(await open_items(session, ledger)) == 1

    async def test_an_open_question_is_restated_not_duplicated(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        """The gap changed, so the question changes — but it is still one question."""
        await make_portfolio(session, ledger, "Momentum")

        await sync(session, ledger, broker(TCS="25"))
        await sync(session, ledger, broker(TCS="40"), on=TOMORROW)

        items = await open_items(session, ledger)
        assert len(items) == 1
        assert items[0].quantity == Decimal("40.0000")
        assert items[0].detected_on == TOMORROW

    async def test_an_attributed_sell_settles_and_stays_settled(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        momentum = await make_portfolio(session, ledger, "Momentum")
        await allocate(session, ledger, momentum, "SBIN", "100")

        await sync(session, ledger, broker(SBIN="60"))
        again = await sync(session, ledger, broker(SBIN="60"), on=TOMORROW)

        assert again.unchanged == 1
        assert again.sells_attributed == 0
        assert await quantity_of(session, ledger, momentum, "SBIN") == Decimal("60.0000")


class TestEverySymbolIsAccountedFor:
    async def test_the_counters_add_up_across_every_case_at_once(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        """One run containing every verdict, and the identity that says none was lost.

        ``run_holdings_sync`` calls ``verify()`` itself, so a sync that dropped a name would have
        raised before this test could read a counter. The assertions restate the identity anyway,
        because a self-check nobody checks is a self-check that can be deleted.
        """
        momentum = await make_portfolio(session, ledger, "Momentum")
        lens = await make_portfolio(session, ledger, "Lens", kind=PortfolioKind.MONITORING)
        await allocate(session, ledger, momentum, "SBIN", "100")  # unchanged
        await allocate(session, ledger, momentum, "INFY", "50")  # attributed sell
        await allocate(session, ledger, lens, "HDFCBANK", "10", kind=PortfolioKind.MONITORING)
        await allocate(session, ledger, momentum, "WIPRO", None)  # NULL quantity -> skipped

        result = await sync(
            session,
            ledger,
            [
                *broker(SBIN="100", INFY="20", TCS="25", HDFCBANK="10", WIPRO="5"),
                BrokerHoldingRecord(symbol="NOTLISTEDANYWHERE", quantity=Decimal("9")),
            ],
        )

        assert result.fetched == 6
        assert result.resolved + result.failed == result.fetched
        assert result.failed == 1
        assert result.unmatched_symbols == ("NOTLISTEDANYWHERE",)
        assert result.positions == 5
        assert result.verdicts() == result.positions
        assert result.unchanged == 2  # SBIN and HDFCBANK
        assert result.sells_attributed == 1  # INFY 50 -> 20
        assert result.new == 1  # TCS, never seen
        assert result.skipped == 1  # WIPRO, quantity unknown
        assert result.sells_questioned == 0
        assert result.inflows_questioned == 0

    async def test_an_unresolvable_symbol_is_named_not_dropped(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        """``reconciliation_item`` needs an instrument id, so no row can be written for it.

        What can be done is refusing to be quiet: the count and the symbol both reach the
        operator's step detail.
        """
        outcome = StepOutcome()
        result = await sync(
            session,
            ledger,
            [BrokerHoldingRecord(symbol="GHOSTSYMBOL", quantity=Decimal("1"))],
            outcome=outcome,
        )

        assert result.failed == 1
        assert outcome.detail["unmatched_symbols"] == ["GHOSTSYMBOL"]

    async def test_a_holding_at_two_brokers_stays_two_positions(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        """§6.7: HDFC Bank — 320 (Zerodha 200 · Upstox 120). One sell must move one of them.

        The second account's rows are untouched because the ``UPDATE`` is scoped by broker
        account, not only by instrument.
        """
        second = BrokerAccount(user_id=ledger.user_id, broker_id="upstox", label="second")
        session.add(second)
        await session.flush()
        momentum = await make_portfolio(session, ledger, "Momentum")
        await allocate(session, ledger, momentum, "SBIN", "200")
        session.add(
            PortfolioHolding(
                portfolio_id=momentum,
                instrument_id=ledger.id_of("SBIN"),
                broker_account_id=second.id,
                portfolio_kind=PortfolioKind.CAPITAL.value,
                quantity=Decimal("120"),
                avg_price=Decimal("100"),
                added_on=TODAY,
            )
        )
        await session.flush()

        await sync(session, ledger, broker(SBIN="150"))

        rows = await session.execute(
            select(PortfolioHolding.broker_account_id, PortfolioHolding.quantity).where(
                PortfolioHolding.instrument_id == ledger.id_of("SBIN")
            )
        )
        assert {int(row[0]): row[1] for row in rows.tuples()} == {
            ledger.broker_account_id: Decimal("150.0000"),
            second.id: Decimal("120.0000"),
        }


class TestCash:
    """§4.4: one Unallocated cash bucket per broker account."""

    async def test_the_reported_balance_is_written(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        result = await sync(
            session,
            ledger,
            broker(),
            cash={ledger.broker_account_id: Decimal("12345.678")},
        )

        assert result.cash_written is True
        stored = await session.execute(
            select(BrokerCash.balance, BrokerCash.as_of).where(
                BrokerCash.broker_account_id == ledger.broker_account_id
            )
        )
        balance, as_of = stored.tuples().one()
        # House rule 8: rounded at write time, so the API, the UI and a CSV cannot disagree.
        assert balance == Decimal("12345.68")
        assert as_of == TODAY

    async def test_a_second_sync_updates_rather_than_duplicates(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        await sync(session, ledger, broker(), cash={ledger.broker_account_id: Decimal("100")})
        await sync(
            session,
            ledger,
            broker(),
            on=TOMORROW,
            cash={ledger.broker_account_id: Decimal("250")},
        )

        rows = await session.execute(
            select(BrokerCash).where(BrokerCash.broker_account_id == ledger.broker_account_id)
        )
        stored = rows.scalars().all()
        assert len(stored) == 1
        assert stored[0].balance == Decimal("250.00")
        assert stored[0].as_of == TOMORROW

    async def test_a_broker_that_reports_no_cash_leaves_the_balance_alone(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        """``None`` is not zero. Writing a zero we inferred would take real money off the screen."""
        await sync(session, ledger, broker(), cash={ledger.broker_account_id: Decimal("500")})
        outcome = StepOutcome()
        result = await sync(session, ledger, broker(), on=TOMORROW, outcome=outcome)

        assert result.cash_written is False
        assert outcome.detail["cash"] == "the broker reported no cash balance"
        balance = await session.execute(
            select(BrokerCash.balance).where(
                BrokerCash.broker_account_id == ledger.broker_account_id
            )
        )
        assert balance.scalar_one() == Decimal("500.00")


class TestFailuresAreNeverSwallowed:
    """House rule 3, and the reason an empty result must not be a way to report an outage."""

    async def test_a_provider_failure_is_noted_and_re_raised(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        class Broken(FixtureHoldingsProvider):
            def broker_holdings(self, account: BrokerAccountRef) -> list[BrokerHoldingRecord]:
                raise UpstreamUnavailable("the broker timed out", provider="fixture-holdings")

        outcome = StepOutcome()
        with pytest.raises(UpstreamUnavailable):
            await run_holdings_sync(
                session,
                Broken({ledger.broker_account_id: []}),
                outcome,
                broker_account_id=ledger.broker_account_id,
                on=TODAY,
            )

        assert outcome.detail["stage"] == "broker_holdings"
        assert "timed out" in str(outcome.detail["error"])

    async def test_an_unknown_account_for_the_fixture_still_raises(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        """The sync does not convert "I have no answer" into "they hold nothing"."""
        with pytest.raises(ProviderUnavailable):
            await run_holdings_sync(
                session,
                FixtureHoldingsProvider({ledger.broker_account_id + 999: []}),
                StepOutcome(),
                broker_account_id=ledger.broker_account_id,
                on=TODAY,
            )

    async def test_an_unknown_broker_account_is_refused(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            await run_holdings_sync(
                session,
                provider_for(ledger, []),
                StepOutcome(),
                broker_account_id=10**9,
                on=TODAY,
            )

    async def test_a_provider_without_the_capability_skips_rather_than_guessing(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        outcome = StepOutcome()
        result = await run_holdings_sync(
            session,
            object(),
            outcome,
            broker_account_id=ledger.broker_account_id,
            on=TODAY,
        )

        assert result.fetched == 0
        assert outcome.status is StepStatus.SKIPPED
        assert outcome.detail["reason"] == "no provider offers broker_holdings"
        assert await open_items(session, ledger) == []


class TestTenantIsolation:
    async def test_another_users_holdings_are_never_read(
        self, session: AsyncSession, ledger: Ledger
    ) -> None:
        """``portfolio_holding`` is scoped by user *and* account.

        The account already belongs to one user, so the extra predicate is redundant against
        correct data — and it is exactly what turns a corrupted ``broker_account_id`` from a
        cross-tenant read into an empty result.
        """
        stranger = AppUser(public_id="c2stranger01", email="c2-stranger@baskfy.test")
        session.add(stranger)
        await session.flush()
        theirs = Portfolio(
            user_id=stranger.id,
            name="Theirs",
            kind=PortfolioKind.CAPITAL.value,
            source=PortfolioSource.HOLDING_GROUP.value,
            started_on=TODAY,
        )
        session.add(theirs)
        await session.flush()
        session.add(
            PortfolioHolding(
                portfolio_id=theirs.id,
                instrument_id=ledger.id_of("SBIN"),
                broker_account_id=ledger.broker_account_id,
                portfolio_kind=PortfolioKind.CAPITAL.value,
                quantity=Decimal("100"),
                avg_price=Decimal("100"),
                added_on=TODAY,
            )
        )
        await session.flush()

        try:
            result = await sync(session, ledger, broker(SBIN="60"))

            # Our user has no record of SBIN, so this is an inflow question — not a sell out of
            # somebody else's portfolio.
            assert result.new == 1
            assert result.sells_attributed == 0
            items = await open_items(session, ledger)
            assert [item.reason for item in items] == [ReconciliationReason.UNKNOWN_INFLOW.value]
            assert await quantity_of(session, ledger, theirs.id, "SBIN") == Decimal("100.0000")
        finally:
            await session.execute(
                delete(PortfolioHolding).where(PortfolioHolding.portfolio_id == theirs.id)
            )
            await session.execute(delete(Portfolio).where(Portfolio.id == theirs.id))
            await session.execute(delete(AppUser).where(AppUser.id == stranger.id))
            await session.flush()
