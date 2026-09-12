"""``/portfolio/*`` — ``PORTFOLIO_REDESIGN.md`` §6 and §7 asserted as a specification.

Every expectation below is transcribed from the spec's own numbered criteria and read against a
**real** PostgreSQL, because the six things this suite has to prove are database facts that a
mock would happily lie about:

* criterion 2 — a monitoring view never enters a total. The exclusion is 0021's partial unique
  index plus the ledger's refusal to let a lens hold an allocation, and both are enforced by
  Postgres and by ``allocation_ledger``, not by a filter in a handler;
* criterion 3 — every displayed return carries what it is and the date it runs from;
* criterion 5 — a publisher's model figure and the user's own are two fields, never one;
* tenancy — the ``user_id`` predicate has to actually filter rows, not merely be present in a
  compiled statement;
* §6.6 — a holding that is in three monitoring views and no capital portfolio is *unallocated*,
  which only a database that permits exactly that shape can demonstrate;
* §4.3 — resolving an item writes a terminal state **and** the allocation it implies, in one
  transaction, against a check constraint that refuses a RESOLVED row naming no portfolio.

The handlers are called directly with a live ``AsyncSession`` rather than over HTTP, the same
arrangement ``test_cb_investment_portfolio.py`` uses: it keeps the suite off Redis while still
exercising the real query path, the real problem responses and the real foreign keys.

WHY THIS MODULE MIGRATES ITS OWN DATABASE
-----------------------------------------
It does not use ``screener_helpers.seeded_database``. That fixture loads the 271-row docs/13
reference export, which this suite needs none of — it names its own four instruments and asserts
against prices it wrote itself, so a shared corpus would only make the numbers below harder to
check. Migrating a clean schema is also the honest baseline for a leaf whose whole subject is a
schema three migrations old: nothing here can pass because some other fixture happened to leave a
row behind.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import screener_helpers
from api_helpers import make_user
from screener_helpers import requires_db
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from baskfy_api.auth import Principal, PrincipalKind
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.routers.portfolio_overview import (
    NavRange,
    ResolveBody,
    portfolio_activity,
    portfolio_detail,
    portfolio_holdings,
    portfolio_nav,
    portfolio_overview,
    portfolio_reconciliation,
    resolve_reconciliation_item,
)
from baskfy_core.allocation_ledger import PortfolioKind, PortfolioSource
from baskfy_core.models import (
    BrokerAccount,
    CbBasket,
    CbBasketVersion,
    CbManager,
    CbMetrics,
    CorporateAction,
    Exchange,
    IndexDef,
    IndexSnapshotDaily,
    Instrument,
    OhlcvDaily,
    Portfolio,
    PortfolioHolding,
    PortfolioSleeve,
)
from baskfy_core.models.accounts import (
    BrokerCash,
    PortfolioCashFlow,
    PortfolioNavDaily,
    ReconciliationItem,
)
from baskfy_core.reconciliation import ReconciliationState

#: The day the fixture's prices are the close of. §6.1's "Prices: close of {date}".
TODAY: Final = dt.date(2026, 8, 18)
YESTERDAY: Final = dt.date(2026, 8, 17)
EARLIER: Final = dt.date(2026, 8, 14)

#: §9's forbidden vocabulary for third-party content. Baskfy is not SEBI-registered, so these
#: words describe a service it does not provide — using one would be a regulatory claim.
FORBIDDEN_WORDS: Final = ("managed", "advisory", "pms", "portfolio management service")

#: ``index_def.id`` and ``exchange.id`` are ``SmallIntPk`` — assigned by hand, never by a
#: sequence, because ``factor_daily.universe_mask`` sets one bit per index id (docs/04). Universe
#: ids live in 1..31 and dashboard-only ids from 100 up; this one is well clear of both, so it
#: cannot collide with a seeded row and cannot be mistaken for a universe.
BENCHMARK_INDEX_ID: Final = 900
NSE_EXCHANGE_ID: Final = 1

MODULE: Final = (
    Path(__file__).resolve().parents[1] / "src" / "baskfy_api" / "routers" / "portfolio_overview.py"
)

#: The order vocabulary, matched on word boundaries. Non-negotiable #1 and §9: this router
#: cannot cause an order, and the cheapest durable proof of that is that the words never appear
#: in it.
#:
#: Boundaries rather than substrings, and the reason is worth recording: ``NFO`` sits inside
#: ``ENFORCE``, so a naive ``in`` check fails on the sentence that says this router enforces
#: §9. A guard that cries wolf on its own docstring is a guard somebody deletes.
ORDER_WORDS: Final = (
    r"place_order",
    r"kc\.place",
    r"kiteconnect",
    r"transaction_type",
    r"order_type",
    r"\bMARKET\b",
    r"\bLIMIT\b",
    r"product=",
    r"\bCNC\b",
    r"\bMIS\b",
    r"\bNFO\b",
    r"\bBFO\b",
    r"place_gtt",
)


# ---------------------------------------------------------------------------
# A migrated, empty database of this suite's own
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ledger_url() -> str:
    """A schema migrated to head. Nothing is dropped, and that is deliberate.

    The neighbouring suites reset the schema because they assert *about* migrations and seeding.
    This one asserts about rows it writes itself, so dropping would buy it nothing and cost
    something real: ``DROP SCHEMA ... CASCADE`` takes an exclusive lock on every table, which
    deadlocks against any other suite reading the same database at the same time.
    ``alembic upgrade head`` on a database already at head is a no-op, so this is idempotent and
    leaves no mark.

    Isolation comes from the transaction in :func:`session`, which is always rolled back — so
    nothing this module writes outlives the test that wrote it, whatever else is in the database.
    """
    url = screener_helpers.database_url()
    screener_helpers.migrate(url)
    return url


@pytest_asyncio.fixture
async def session(ledger_url: str) -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is always rolled back."""
    engine = create_async_engine(ledger_url)
    connection = await engine.connect()
    transaction = await connection.begin()
    made = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        yield made
    finally:
        await made.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# The fixture ledger — small enough that every number below can be checked by hand
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Book:
    """One user's whole picture, with the ids the assertions name.

    The arithmetic, written out so the tests can assert against numbers rather than against
    re-derivations of themselves::

        Core (capital, my screen)   INFY 100 @ Zerodha + INFY 20 @ Upstox  x 1200 = 144,000
        Momentum (capital, subscribed)  HDFCBANK 50 @ Zerodha              x 1600 =  80,000
        Unallocated                 TCS 10 @ Zerodha                       x 3500 =  35,000
        Cash                        7,500 unallocated + 2,000 inside Core  =         9,500
                                                                              ------------
        Consolidated net worth                                                     268,500

        Defence watch (MONITORING)  TCS 10 + INFY 100 @ Zerodha            =        155,000
                                    ...and none of it is in the 268,500 above.
    """

    owner_id: int
    owner: Principal
    stranger_id: int
    stranger: Principal
    zerodha: int
    upstox: int
    stranger_broker: int
    infy: int
    hdfc: int
    tcs: int
    core: int
    momentum: int
    watch: int
    stranger_portfolio: int
    basket_id: int
    benchmark_id: int
    item_id: int


CONSOLIDATED_NET_WORTH: Final = Decimal("268500.00")
CORE_VALUE: Final = Decimal("144000.00")
MOMENTUM_VALUE: Final = Decimal("80000.00")
UNALLOCATED_HOLDINGS_VALUE: Final = Decimal("35000.00")
WATCH_VALUE: Final = Decimal("155000.00")
TOTAL_CASH: Final = Decimal("9500.00")


def _principal(user_id: int, public_id: str) -> Principal:
    return Principal(kind=PrincipalKind.USER, user_id=user_id, public_id=public_id)


async def _instrument(session: AsyncSession, exchange_id: int, symbol: str, name: str) -> int:
    """Get-or-create, because ``(exchange_id, symbol, series)`` is UNIQUE.

    Whether INFY is already in this database depends on whether a suite that seeds the docs/13
    reference export ran first, and this module must not care either way — see :func:`ledger_url`.
    """
    found = await session.scalar(
        select(Instrument.id).where(
            Instrument.exchange_id == exchange_id, Instrument.symbol == symbol
        )
    )
    if found is not None:
        return int(found)
    row = Instrument(
        exchange_id=exchange_id,
        symbol=symbol,
        name=name,
        instrument_type="EQ",
        series="EQ",
        is_active=True,
    )
    session.add(row)
    await session.flush()
    return int(row.id)


async def _bar(session: AsyncSession, instrument_id: int, on: dt.date, close: Decimal) -> None:
    """One daily bar. ``close_raw`` is what the portfolio page shows (house rule 6).

    Any bar this database already holds for the same instrument and day is removed first — the
    primary key is ``(instrument_id, date)``, and the seeded reference export's as-of is the same
    August day this fixture prices against. The delete lives inside the test's transaction and is
    rolled back with everything else.
    """
    await session.execute(
        delete(OhlcvDaily).where(OhlcvDaily.instrument_id == instrument_id, OhlcvDaily.date == on)
    )
    session.add(
        OhlcvDaily(
            instrument_id=instrument_id,
            date=on,
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1_000,
            close_raw=close,
            volume_raw=1_000,
            adj_factor=Decimal("1"),
            source="nse",
        )
    )


@dataclass(frozen=True, slots=True)
class _PortfolioSpec:
    """What a portfolio is, in the four facts §3 and §4.1 say make one.

    A value rather than six keyword arguments, for the same reason
    :class:`~baskfy_core.reconciliation.AttentionInputs` is one: the builder below is called four
    times with two dates and two enums between them, and a positional slip there would produce a
    fixture that is wrong in a way every assertion agrees with.
    """

    user_id: int
    name: str
    kind: PortfolioKind
    source: PortfolioSource
    started_on: dt.date
    benchmark_index_id: int | None = None


async def _portfolio(session: AsyncSession, spec: _PortfolioSpec) -> int:
    row = Portfolio(
        user_id=spec.user_id,
        name=spec.name,
        kind=spec.kind.value,
        source=spec.source.value,
        started_on=spec.started_on,
        benchmark_index_id=spec.benchmark_index_id,
    )
    session.add(row)
    await session.flush()
    return int(row.id)


@dataclass(frozen=True, slots=True)
class _HoldSpec:
    """One ``portfolio_holding`` row. ``kind`` is the owner's, copied as 0021 requires."""

    portfolio_id: int
    kind: PortfolioKind
    instrument_id: int
    broker_account_id: int
    quantity: Decimal
    avg_price: Decimal | None = None


def _hold(session: AsyncSession, spec: _HoldSpec) -> None:
    session.add(
        PortfolioHolding(
            portfolio_id=spec.portfolio_id,
            instrument_id=spec.instrument_id,
            broker_account_id=spec.broker_account_id,
            portfolio_kind=spec.kind.value,
            quantity=spec.quantity,
            avg_price=spec.avg_price,
            added_on=dt.date(2026, 1, 2),
        )
    )


def _nav(
    session: AsyncSession, *, user_id: int, portfolio_id: int | None, on: dt.date, value: Decimal
) -> None:
    """One stored EOD mark. Cash and flow are zero: the fixture's transfers predate the marks."""
    session.add(
        PortfolioNavDaily(
            user_id=user_id,
            portfolio_id=portfolio_id,
            date=on,
            market_value=value,
            cash=Decimal("0"),
            net_flow=Decimal("0"),
        )
    )


async def _exchange(session: AsyncSession) -> int:
    """The NSE row, created only if this database does not already carry one.

    ``exchange.code`` is UNIQUE, and whether the row is there depends on whether a seeded suite
    ran first — which this module must not care about. Get-or-create is the shape that does not.
    """
    found = await session.scalar(select(Exchange.id).where(Exchange.code == "NSE"))
    if found is not None:
        return int(found)
    row = Exchange(id=NSE_EXCHANGE_ID, code="NSE")
    session.add(row)
    await session.flush()
    return int(row.id)


@pytest_asyncio.fixture
async def book(session: AsyncSession) -> Book:
    """Build the ledger described in :class:`Book`. One user, one stranger, one of everything."""
    exchange_id = await _exchange(session)

    owner_id, owner_public = await make_user(session, "ledger.owner@example.com")
    stranger_id, stranger_public = await make_user(session, "ledger.stranger@example.com")

    zerodha = BrokerAccount(user_id=owner_id, broker_id="zerodha", label="primary")
    upstox = BrokerAccount(user_id=owner_id, broker_id="upstox", label="second")
    theirs = BrokerAccount(user_id=stranger_id, broker_id="zerodha", label="primary")
    session.add_all([zerodha, upstox, theirs])
    await session.flush()

    infy = await _instrument(session, exchange_id, "INFY", "Infosys")
    hdfc = await _instrument(session, exchange_id, "HDFCBANK", "HDFC Bank")
    tcs = await _instrument(session, exchange_id, "TCS", "Tata Consultancy Services")

    for instrument_id, previous, latest in (
        (infy, Decimal("1100"), Decimal("1200")),
        (hdfc, Decimal("1600"), Decimal("1600")),
        (tcs, Decimal("3400"), Decimal("3500")),
    ):
        await _bar(session, instrument_id, YESTERDAY, previous)
        await _bar(session, instrument_id, TODAY, latest)

    # A slug of this suite's own: `nifty-500` may already exist in a seeded database, and
    # `index_def.slug` is UNIQUE. The name is what the payload shows and what the test asserts.
    index = IndexDef(
        id=BENCHMARK_INDEX_ID,
        slug="ledger-suite-benchmark",
        name="Nifty 500",
        is_universe=False,
        sort_order=0,
    )
    session.add(index)
    await session.flush()
    for on, level in (
        (EARLIER, Decimal("1000")),
        (YESTERDAY, Decimal("1010")),
        (TODAY, Decimal("1030")),
    ):
        session.add(IndexSnapshotDaily(index_id=index.id, date=on, level=level))

    core = await _portfolio(
        session,
        _PortfolioSpec(
            user_id=owner_id,
            name="Core",
            kind=PortfolioKind.CAPITAL,
            source=PortfolioSource.MY_SCREEN,
            started_on=dt.date(2026, 1, 1),
            benchmark_index_id=int(index.id),
        ),
    )
    momentum = await _portfolio(
        session,
        _PortfolioSpec(
            user_id=owner_id,
            name="Momentum",
            kind=PortfolioKind.CAPITAL,
            source=PortfolioSource.SUBSCRIBED,
            started_on=dt.date(2026, 2, 1),
        ),
    )
    watch = await _portfolio(
        session,
        _PortfolioSpec(
            user_id=owner_id,
            name="Defence watch",
            kind=PortfolioKind.MONITORING,
            source=PortfolioSource.HOLDING_GROUP,
            started_on=dt.date(2026, 3, 1),
        ),
    )
    stranger_portfolio = await _portfolio(
        session,
        _PortfolioSpec(
            user_id=stranger_id,
            name="Not yours",
            kind=PortfolioKind.CAPITAL,
            source=PortfolioSource.HOLDING_GROUP,
            started_on=dt.date(2026, 1, 1),
        ),
    )

    # Core holds INFY at two brokers — §6.7's "HDFC Bank — 320 (Zerodha 200 · Upstox 120)" case.
    for spec in (
        _HoldSpec(
            core, PortfolioKind.CAPITAL, infy, int(zerodha.id), Decimal("100"), Decimal("1000")
        ),
        _HoldSpec(
            core, PortfolioKind.CAPITAL, infy, int(upstox.id), Decimal("20"), Decimal("1000")
        ),
        _HoldSpec(
            momentum, PortfolioKind.CAPITAL, hdfc, int(zerodha.id), Decimal("50"), Decimal("1500")
        ),
        # TCS is in a monitoring view and in no capital portfolio: unallocated (§4.1, §6.6).
        _HoldSpec(
            watch, PortfolioKind.MONITORING, tcs, int(zerodha.id), Decimal("10"), Decimal("3000")
        ),
        # ...and the lens overlaps Core, which is exactly what a lens is for.
        _HoldSpec(
            watch, PortfolioKind.MONITORING, infy, int(zerodha.id), Decimal("100"), Decimal("1000")
        ),
        # The stranger's money, at the stranger's broker, in the stranger's portfolio.
        _HoldSpec(
            stranger_portfolio,
            PortfolioKind.CAPITAL,
            hdfc,
            int(theirs.id),
            Decimal("999"),
            Decimal("1500"),
        ),
    ):
        _hold(session, spec)

    session.add(BrokerCash(broker_account_id=int(zerodha.id), balance=Decimal("5000"), as_of=TODAY))
    session.add(BrokerCash(broker_account_id=int(upstox.id), balance=Decimal("2500"), as_of=TODAY))

    session.add(
        PortfolioCashFlow(
            broker_account_id=int(zerodha.id),
            portfolio_id=None,
            kind="EXTERNAL_DEPOSIT",
            amount=Decimal("100000.00"),
            occurred_on=dt.date(2026, 5, 1),
        )
    )
    session.add(
        PortfolioCashFlow(
            broker_account_id=int(zerodha.id),
            portfolio_id=core,
            kind="ASSIGN",
            amount=Decimal("10000.00"),
            occurred_on=dt.date(2026, 6, 1),
        )
    )
    session.add(
        PortfolioCashFlow(
            broker_account_id=int(zerodha.id),
            portfolio_id=core,
            kind="BUY",
            amount=Decimal("8000.00"),
            occurred_on=dt.date(2026, 6, 2),
            instrument_id=infy,
            quantity=Decimal("5"),
        )
    )

    for on, core_value, momentum_value, whole in (
        (EARLIER, Decimal("130000"), Decimal("70000"), Decimal("200000")),
        (YESTERDAY, Decimal("132000"), Decimal("75000"), Decimal("207000")),
        (TODAY, Decimal("144000"), Decimal("80000"), Decimal("224000")),
    ):
        _nav(session, user_id=owner_id, portfolio_id=core, on=on, value=core_value)
        _nav(session, user_id=owner_id, portfolio_id=momentum, on=on, value=momentum_value)
        _nav(session, user_id=owner_id, portfolio_id=None, on=on, value=whole)

    manager = CbManager(slug="ledger-suite-manager", name="Baskfy Research", kind="ENGINE")
    session.add(manager)
    await session.flush()
    basket = CbBasket(
        slug="ledger-suite-basket",
        name="Momentum 20",
        manager_id=manager.id,
        type="STOCK",
        access="FREE",
        visibility="PUBLISHED",
        rebalance_frequency="QUARTERLY",
        source="SCAN",
    )
    session.add(basket)
    await session.flush()
    session.add(
        CbBasketVersion(
            basket_id=basket.id, version_no=1, effective_date=dt.date(2026, 1, 1), label="GENESIS"
        )
    )
    session.add(
        PortfolioSleeve(
            portfolio_id=momentum,
            name="Momentum 20",
            kind="basket",
            basket_id=basket.id,
            capital=Decimal("80000.00"),
            top_n=20,
        )
    )
    # Two anchors, so the publisher's return over *this user's* window is a real number:
    # (1 + 32/100) / (1 + 10/100) - 1 = 0.20 exactly.
    for on, pct in ((dt.date(2026, 2, 1), Decimal("10.00")), (TODAY, Decimal("32.00"))):
        session.add(
            CbMetrics(
                basket_id=basket.id,
                as_of_date=on,
                since_inception_pct=pct,
                return_convention="PRICE_RETURN",
                dividends_included=False,
                computed_at=dt.datetime(2026, 8, 18, tzinfo=dt.UTC),
            )
        )

    item = ReconciliationItem(
        user_id=owner_id,
        instrument_id=tcs,
        broker_account_id=int(zerodha.id),
        quantity=Decimal("10"),
        reason="UNALLOCATED_HOLDING",
        state="OPEN",
        detected_on=dt.date(2026, 8, 15),
        suggested_portfolio_id=core,
    )
    session.add(item)
    await session.flush()

    return Book(
        owner_id=owner_id,
        owner=_principal(owner_id, owner_public),
        stranger_id=stranger_id,
        stranger=_principal(stranger_id, stranger_public),
        zerodha=int(zerodha.id),
        upstox=int(upstox.id),
        stranger_broker=int(theirs.id),
        infy=infy,
        hdfc=hdfc,
        tcs=tcs,
        core=core,
        momentum=momentum,
        watch=watch,
        stranger_portfolio=stranger_portfolio,
        basket_id=int(basket.id),
        benchmark_id=int(index.id),
        item_id=int(item.id),
    )


# ---------------------------------------------------------------------------
# Walking a rendered payload — the criterion-3 and §9 assertions need every string
# ---------------------------------------------------------------------------


def _walk(value: object) -> Iterator[object]:
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _figures(payload: object) -> list[dict[str, object]]:
    """Every rate- or move-shaped object in a payload: anything carrying a ``label``.

    Found structurally rather than by naming the fields, so a figure added to a response later is
    covered by criterion 3's assertions without anyone remembering to extend a list.
    """
    return [
        node
        for node in _walk(payload)
        if isinstance(node, dict) and "label" in node and ("value" in node or "amount" in node)
    ]


def _strings(payload: object) -> list[str]:
    return [node for node in _walk(payload) if isinstance(node, str)]


# ---------------------------------------------------------------------------
# Criterion 2 / §4.1 — a monitoring view is never in a total
# ---------------------------------------------------------------------------


@requires_db
async def test_the_overview_totals_exclude_monitoring_views(
    session: AsyncSession, book: Book
) -> None:
    """Criterion 1 and 2 together: the parts add to the whole, and the lens is not one of them.

    The monitoring view is worth ₹1,55,000 — more than half the account — and overlaps Core
    entirely. If it were in the total, the total would be 4,23,500 and would still *look* like a
    number. The assertion is therefore not "the lens is absent" but "the total is exactly the sum
    of the capital rows and Unallocated", which is the only statement that could not be satisfied
    by a lens quietly slipping in.
    """
    view = await portfolio_overview(session, book.owner)

    assert view.hero.current_value == CONSOLIDATED_NET_WORTH
    capital = {row.portfolio_id: row for row in view.portfolios}
    assert set(capital) == {book.core, book.momentum}
    assert capital[book.core].value == CORE_VALUE
    assert capital[book.momentum].value == MOMENTUM_VALUE

    # Criterion 1, in its own words: "sum of all capital portfolios + unallocated (stocks **and
    # cash**) equals consolidated net worth, to the paisa". A portfolio's own cash (§4.4's
    # assigned-but-not-yet-invested money) is a separate field from its market value, and both
    # are in the total — leaving it out here would be a test that passed while the page was short.
    parts = sum(row.value + row.cash for row in view.portfolios) + view.unallocated.total_value
    assert parts == view.hero.current_value

    [lens] = view.monitoring_views
    assert lens.portfolio_id == book.watch
    assert lens.value == WATCH_VALUE
    assert lens.counts_toward_total is False
    assert lens.excluded_note is not None and "excluded from totals" in lens.excluded_note
    assert all(row.counts_toward_total for row in view.portfolios)


@requires_db
async def test_a_monitoring_view_is_never_in_the_portfolio_table(
    session: AsyncSession, book: Book
) -> None:
    """The lens is in a different list, not merely flagged inside the same one.

    A client that sums ``portfolios`` is doing the arithmetic §4.1 permits, and no flag it forgot
    to read can make that sum wrong. That is the difference between a rule and a convention.
    """
    view = await portfolio_overview(session, book.owner)
    assert book.watch not in {row.portfolio_id for row in view.portfolios}
    assert book.watch in {row.portfolio_id for row in view.monitoring_views}


# ---------------------------------------------------------------------------
# Criterion 3 — no unlabelled return, anywhere
# ---------------------------------------------------------------------------


@requires_db
async def test_every_return_number_carries_a_label_and_a_start_date(
    session: AsyncSession, book: Book
) -> None:
    """Criterion 3, asserted over the whole rendered payload rather than field by field.

    Two halves, and both matter. A figure with a value must say what it is and when it starts —
    otherwise it is the unlabelled column the criterion forbids. A figure *without* a value must
    say why, because an empty cell cannot be told apart from a zero, and "unknown" and "flat" are
    opposite pieces of news.
    """
    payload = (await portfolio_overview(session, book.owner)).model_dump(mode="python")
    figures = _figures(payload)
    assert figures, "the overview should carry return figures at all"

    for figure in figures:
        label = figure.get("label")
        assert isinstance(label, str) and label.strip(), figure
        has_number = figure.get("value") is not None or figure.get("amount") is not None
        if has_number:
            assert figure.get("since") is not None or "amount" in figure, figure
        else:
            reason = figure.get("unavailable_reason")
            assert isinstance(reason, str) and reason.strip(), figure


@requires_db
async def test_a_headline_metric_says_which_metric_it_is(session: AsyncSession, book: Book) -> None:
    """§5.2's table, read off the wire: the metric follows the source, and it is named.

    Core was built from a screen, so its headline is a TWR measured from the day it was created.
    A holding group would say "Since grouped" instead, and the two claims are different — which
    is precisely why the router routes them to different functions rather than to one.
    """
    view = await portfolio_overview(session, book.owner)
    core = next(row for row in view.portfolios if row.portfolio_id == book.core)
    assert core.headline_return.kind.value == "TWR_SINCE_CREATED"
    assert core.headline_return.label == "TWR since created"
    assert core.headline_return.since == dt.date(2026, 1, 1)
    assert core.headline_return.value is not None

    lens = next(row for row in view.monitoring_views if row.portfolio_id == book.watch)
    assert lens.headline_return.kind.value == "SINCE_GROUPED"
    assert lens.headline_return.value is None
    assert lens.headline_return.unavailable_reason


@requires_db
async def test_the_consolidated_row_shows_xirr_and_twr_as_two_labelled_numbers(
    session: AsyncSession, book: Book
) -> None:
    """§5.2: "show XIRR and TWR as two labeled numbers" — two fields, two labels, never one."""
    view = await portfolio_overview(session, book.owner)
    assert view.hero.xirr.label != view.hero.twr.label
    assert "XIRR" in view.hero.xirr.label
    assert "ime-weighted" in view.hero.twr.label
    assert view.hero.twr.value is not None
    assert view.hero.twr.since == EARLIER
    # One assignment, on 2026-06-01, valued at the latest close.
    assert view.hero.xirr.since == dt.date(2026, 6, 1)


# ---------------------------------------------------------------------------
# Criterion 5 — model and actual are two fields
# ---------------------------------------------------------------------------


@requires_db
async def test_a_subscribed_portfolios_model_figure_is_a_separate_field(
    session: AsyncSession, book: Book
) -> None:
    """Criterion 5. The publisher's record and the user's own are never combined.

    They are measured over the same window — which is what makes them worth showing side by side
    — and they are still two numbers in two fields with two labels, one of which says it is a
    model. The publisher's 20% comes from ``cb_metrics``; the user's comes from their own NAV
    series; nothing in the payload is their average, their sum, or either one standing for both.
    """
    view = await portfolio_overview(session, book.owner)
    momentum = next(row for row in view.portfolios if row.portfolio_id == book.momentum)

    assert momentum.model_return is not None
    assert momentum.model_return.is_model is True
    assert momentum.headline_return.is_model is False
    assert momentum.model_return.value == Decimal("0.200000")
    assert momentum.headline_return.value is not None
    assert momentum.headline_return.value != momentum.model_return.value
    assert momentum.model_return.label != momentum.headline_return.label
    assert momentum.model_return.label.lower().startswith("model")


@requires_db
async def test_a_portfolio_the_user_built_has_no_model_figure_at_all(
    session: AsyncSession, book: Book
) -> None:
    """There is no publisher whose record it could be, so the field is absent rather than empty.

    An empty labelled model figure would imply a publisher who has not reported. For a portfolio
    the user cut from their own screen, that implication is simply false.
    """
    view = await portfolio_overview(session, book.owner)
    core = next(row for row in view.portfolios if row.portfolio_id == book.core)
    assert core.model_return is None


@requires_db
async def test_the_subscribed_badge_uses_section_9s_wording(
    session: AsyncSession, book: Book
) -> None:
    """§9: "Subscribed model by {publisher}". Never managed, never advisory, never PMS.

    Asserted over every string in two rendered payloads rather than over the source, so a phrase
    assembled at runtime out of a publisher's own name cannot slip past it.
    """
    view = await portfolio_overview(session, book.owner)
    momentum = next(row for row in view.portfolios if row.portfolio_id == book.momentum)
    assert momentum.source_badge == "Subscribed model by Baskfy Research"

    detail = await portfolio_detail(session, book.owner, book.momentum)
    assert detail.source_panel.headline == "Subscribed model by Baskfy Research"

    for payload in (view.model_dump(mode="python"), detail.model_dump(mode="python")):
        for value in _strings(payload):
            lowered = value.lower()
            for word in FORBIDDEN_WORDS:
                assert not re.search(rf"\b{re.escape(word)}\b", lowered), (word, value)


# ---------------------------------------------------------------------------
# §6.6 — the unallocated section
# ---------------------------------------------------------------------------


@requires_db
async def test_the_unallocated_section_reports_holdings_with_no_allocation(
    session: AsyncSession, book: Book
) -> None:
    """§6.6, and the case that is easy to get wrong: a lens is not an allocation (§4.1).

    TCS sits in "Defence watch" and in no capital portfolio. It is therefore unallocated — and a
    reading that treated membership of a monitoring view as being filed somewhere would report an
    empty Unallocated section on an account with ₹35,000 sitting outside every portfolio.
    """
    view = await portfolio_overview(session, book.owner)
    unallocated = view.unallocated

    assert unallocated.holdings_count == 1
    [row] = unallocated.holdings
    assert row.instrument.symbol == "TCS"
    assert row.quantity == Decimal("10")
    assert row.value == UNALLOCATED_HOLDINGS_VALUE
    assert [view_ref.name for view_ref in row.monitoring_views] == ["Defence watch"]

    assert unallocated.cash == Decimal("7500.00")
    assert unallocated.holdings_value == UNALLOCATED_HOLDINGS_VALUE
    assert unallocated.total_value == Decimal("42500.00")
    assert unallocated.cta == "Organize into portfolios"


@requires_db
async def test_the_ribbon_names_the_unallocated_holdings_and_the_open_question(
    session: AsyncSession, book: Book
) -> None:
    """§6.4's ribbon, from core's own sentences. A row exists only when there is work to do."""
    view = await portfolio_overview(session, book.owner)
    kinds = {item.kind for item in view.attention}
    assert "HOLDINGS_UNALLOCATED" in kinds
    assert "RECONCILIATION_PENDING" in kinds
    unallocated_item = next(i for i in view.attention if i.kind == "HOLDINGS_UNALLOCATED")
    assert unallocated_item.message == "1 holding is not in any portfolio yet"


# ---------------------------------------------------------------------------
# §6.1 — two timestamps, separately
# ---------------------------------------------------------------------------


@requires_db
async def test_the_price_date_and_the_sync_date_are_separate_fields(
    session: AsyncSession, book: Book
) -> None:
    """§6.1 requires both, and requires them apart.

    A single "last updated" that quietly reports the older of the two is how a user comes to
    believe a stale holdings list is a stale price — two different problems with two different
    fixes.
    """
    view = await portfolio_overview(session, book.owner)
    assert view.prices_as_of == TODAY
    assert view.prices_label == "Prices: close of 2026-08-18"
    assert view.holdings_synced_on == TODAY
    assert view.holdings_synced_label == "Holdings synced: 2026-08-18"
    assert view.prices_label != view.holdings_synced_label
    assert {status.broker.broker_id for status in view.sync_status} == {"zerodha", "upstox"}


# ---------------------------------------------------------------------------
# §2 / §6.7 — the flat holdings truth
# ---------------------------------------------------------------------------


@requires_db
async def test_a_stock_held_at_two_brokers_aggregates_with_the_breakdown_kept(
    session: AsyncSession, book: Book
) -> None:
    """§6.7: aggregated for display, broker breakdown preserved on drill-down.

    The two INFY positions are one row of 120 shares *and* two lines of 100 and 20. Collapsing to
    the row alone would make "sell 100 at Zerodha" unattributable, which is the reason the ledger
    keys a holding by ``(instrument, broker)`` in the first place.
    """
    holdings = await portfolio_holdings(session, book.owner)
    infy = next(row for row in holdings.rows if row.instrument.symbol == "INFY")

    assert infy.quantity == Decimal("120")
    assert infy.value == Decimal("144000.00")
    assert {line.broker.broker_id: line.quantity for line in infy.brokers} == {
        "zerodha": Decimal("100"),
        "upstox": Decimal("20"),
    }
    assert infy.allocation is not None and infy.allocation.portfolio_id == book.core
    assert infy.split_across_portfolios is False
    assert [ref.name for ref in infy.monitoring_views] == ["Defence watch"]

    tcs = next(row for row in holdings.rows if row.instrument.symbol == "TCS")
    assert tcs.allocated is False
    assert tcs.allocation is None
    assert holdings.unallocated_count == 1


@requires_db
async def test_the_holdings_view_never_shows_another_tenants_shares(
    session: AsyncSession, book: Book
) -> None:
    """The stranger holds 999 HDFC Bank. The owner's flat truth must not contain a single one."""
    holdings = await portfolio_holdings(session, book.owner)
    hdfc = next(row for row in holdings.rows if row.instrument.symbol == "HDFCBANK")
    assert hdfc.quantity == Decimal("50")
    assert book.stranger_broker not in {
        line.broker.broker_account_id for row in holdings.rows for line in row.brokers
    }


# ---------------------------------------------------------------------------
# Tenancy — a foreign id is NOT_FOUND, never FORBIDDEN
# ---------------------------------------------------------------------------


@requires_db
async def test_another_users_portfolio_is_not_found_on_every_route(
    session: AsyncSession, book: Book
) -> None:
    """404 and not 403, on the detail page, the chart, the activity filter and the inbox answer.

    A 403 would confirm the id names a real row and whose it is, which is the fact a stranger is
    probing for. Every route that takes a portfolio id is asserted, because the leak only needs
    one of them to have forgotten.
    """
    for call in (
        portfolio_detail(session, book.owner, book.stranger_portfolio),
        portfolio_nav(session, book.owner, book.stranger_portfolio, NavRange.Y1),
        portfolio_activity(session, book.owner, book.stranger_portfolio, 50),
        resolve_reconciliation_item(
            ResolveBody(portfolio_id=book.stranger_portfolio),
            session,
            book.owner,
            book.item_id,
        ),
    ):
        with pytest.raises(Problem) as raised:
            await call
        assert raised.value.type is ProblemType.NOT_FOUND
        assert raised.value.status == 404


@requires_db
async def test_the_stranger_sees_none_of_the_owners_money(
    session: AsyncSession, book: Book
) -> None:
    """The scoping filters rows, rather than merely appearing in a compiled statement."""
    view = await portfolio_overview(session, book.stranger)
    assert {row.portfolio_id for row in view.portfolios} == {book.stranger_portfolio}
    assert view.monitoring_views == []
    assert book.core not in {row.portfolio_id for row in view.portfolios}
    assert view.hero.current_value == Decimal("1598400.00")  # 999 x 1600, and nothing else


@requires_db
async def test_a_reconciliation_item_of_another_tenant_is_not_found(
    session: AsyncSession, book: Book
) -> None:
    """Somebody else's question is answered exactly as one that never existed is."""
    with pytest.raises(Problem) as raised:
        await resolve_reconciliation_item(
            ResolveBody(portfolio_id=book.stranger_portfolio),
            session,
            book.stranger,
            book.item_id,
        )
    assert raised.value.type is ProblemType.NOT_FOUND


# ---------------------------------------------------------------------------
# §4.3 — the inbox and its one answer
# ---------------------------------------------------------------------------


@requires_db
async def test_the_inbox_asks_the_question_and_says_what_it_freezes(
    session: AsyncSession, book: Book
) -> None:
    """§4.3. An open item is a question, and its existence is what stops a guess.

    The blast radius is the point: this item is about a holding in no capital portfolio, so it
    marks *Unallocated* pending and the lens that shows the same shares — and it makes the
    consolidated figure one the product says it cannot state honestly.
    """
    inbox = await portfolio_reconciliation(session, book.owner, None)
    [item] = inbox.items
    assert item.item_id == book.item_id
    assert item.state is ReconciliationState.OPEN
    assert item.freezes is True
    assert item.instrument.symbol == "TCS"
    assert item.question.endswith("which portfolio should it count against?")
    assert item.suggested_portfolio is not None
    assert item.suggested_portfolio.portfolio_id == book.core
    assert item.resolved_portfolio is None

    assert inbox.open_count == 1
    assert inbox.unallocated_pending is True
    assert inbox.consolidated_pending is True
    assert book.watch in inbox.pending_portfolio_ids

    view = await portfolio_overview(session, book.owner)
    assert view.hero.pending_reconciliation is True
    assert view.unallocated.pending_reconciliation is True


@requires_db
async def test_resolving_moves_the_item_to_resolved_and_records_the_portfolio(
    session: AsyncSession, book: Book
) -> None:
    """§4.3's answer, both halves of it, in one transaction.

    The state change alone would leave an item marked RESOLVED whose holding is still in no
    portfolio — an unfrozen holding with nowhere to contribute, and therefore a quietly wrong
    total in place of a loudly pending one. So the assertions are: the row is terminal, it names
    the portfolio, and the shares have actually moved into it.
    """
    answer = await resolve_reconciliation_item(
        ResolveBody(portfolio_id=book.core), session, book.owner, book.item_id
    )

    assert answer.item.state is ReconciliationState.RESOLVED
    assert answer.item.resolved_portfolio is not None
    assert answer.item.resolved_portfolio.portfolio_id == book.core
    assert answer.item.resolved_at is not None
    assert answer.item.freezes is False
    assert answer.allocated_to.portfolio_id == book.core
    assert answer.unfroze is True

    stored = await session.scalar(
        select(ReconciliationItem).where(ReconciliationItem.id == book.item_id)
    )
    assert stored is not None
    assert stored.state == "RESOLVED"
    assert stored.resolved_portfolio_id == book.core
    assert stored.resolved_at is not None

    # The implied allocation landed: TCS now counts against Core and has left Unallocated.
    allocated = await session.scalar(
        select(PortfolioHolding.portfolio_id).where(
            PortfolioHolding.instrument_id == book.tcs,
            PortfolioHolding.broker_account_id == book.zerodha,
            PortfolioHolding.portfolio_kind == "CAPITAL",
        )
    )
    assert allocated == book.core

    view = await portfolio_overview(session, book.owner)
    assert view.unallocated.holdings == []
    assert view.hero.pending_reconciliation is False
    assert view.hero.current_value == CONSOLIDATED_NET_WORTH
    core = next(row for row in view.portfolios if row.portfolio_id == book.core)
    assert core.value == CORE_VALUE + UNALLOCATED_HOLDINGS_VALUE


@requires_db
async def test_resolving_to_a_monitoring_view_is_refused(session: AsyncSession, book: Book) -> None:
    """§4.1: a lens holds no allocation, so "resolve it to Defence watch" has no meaning.

    Permitting it would leave the shares in no capital portfolio and the consolidated total
    short, while the inbox showed the question as answered — the worst of both states.
    """
    with pytest.raises(Problem) as raised:
        await resolve_reconciliation_item(
            ResolveBody(portfolio_id=book.watch), session, book.owner, book.item_id
        )
    assert raised.value.status == 400
    assert "monitoring view" in raised.value.detail


@requires_db
async def test_an_answered_question_cannot_be_answered_twice(
    session: AsyncSession, book: Book
) -> None:
    """Changing one's mind is a new event with its own date — the reason ``resolved_at`` exists."""
    await resolve_reconciliation_item(
        ResolveBody(portfolio_id=book.core), session, book.owner, book.item_id
    )
    with pytest.raises(Problem) as raised:
        await resolve_reconciliation_item(
            ResolveBody(portfolio_id=book.momentum), session, book.owner, book.item_id
        )
    assert raised.value.status == 400


# ---------------------------------------------------------------------------
# §7 — the detail page and §6.3's chart
# ---------------------------------------------------------------------------


@requires_db
async def test_the_detail_page_weights_and_contributions_add_up(
    session: AsyncSession, book: Book
) -> None:
    """§7's holdings tab: weight, today's contribution, total contribution, broker.

    INFY moved 1,100 → 1,200, so Core's 120 shares contributed ₹12,000 today; they cost ₹1,000
    each, so the total contribution is ₹24,000. Both are per-line and both are checkable by hand,
    which is the only kind of assertion worth writing about money.
    """
    detail = await portfolio_detail(session, book.owner, book.core)
    assert detail.summary.value == CORE_VALUE
    assert detail.summary.invested == Decimal("120000.00")
    assert detail.summary.total_pnl.amount == Decimal("24000.00")
    assert detail.summary.todays_pnl.amount == Decimal("12000.00")
    assert detail.summary.cash == Decimal("2000.00")

    assert sum(line.weight or Decimal("0") for line in detail.holdings) == Decimal("1.000000")
    assert sum(line.todays_contribution or Decimal("0") for line in detail.holdings) == Decimal(
        "12000.00"
    )
    assert sum(line.total_contribution or Decimal("0") for line in detail.holdings) == Decimal(
        "24000.00"
    )

    assert detail.source_panel.source is PortfolioSource.MY_SCREEN
    assert "never places an order" in detail.source_panel.execution_note


@requires_db
async def test_the_detail_page_of_a_monitoring_view_says_it_is_not_in_the_totals(
    session: AsyncSession, book: Book
) -> None:
    """A lens is a legitimate thing to look at; it is only an illegitimate thing to add up."""
    detail = await portfolio_detail(session, book.owner, book.watch)
    assert detail.summary.value == WATCH_VALUE
    assert detail.summary.counts_toward_total is False
    assert detail.summary.excluded_note == (
        "Monitoring view — overlaps with other portfolios, excluded from totals."
    )
    assert detail.summary.xirr.value is None
    assert detail.summary.xirr.unavailable_reason is not None


@requires_db
async def test_the_nav_route_serves_the_stored_series_with_its_derived_views(
    session: AsyncSession, book: Book
) -> None:
    """§6.3 and §5.1: read, never recomputed. Three marks in, two day-moves and a drawdown out."""
    series = await portfolio_nav(session, book.owner, book.core, NavRange.ALL)
    assert series.portfolio_id == book.core
    assert [point.on for point in series.points] == [EARLIER, YESTERDAY, TODAY]
    assert [point.value for point in series.points] == [
        Decimal("130000.00"),
        Decimal("132000.00"),
        Decimal("144000.00"),
    ]
    assert len(series.daily_pnl) == 2
    assert series.daily_pnl[0].amount == Decimal("2000.00")
    assert series.total_return.value is not None
    assert series.total_return.label == "TWR since created"

    assert series.benchmark is not None
    assert series.benchmark.name == "Nifty 500"
    assert series.benchmark.benchmark.is_model is False
    assert series.benchmark.difference is not None


@requires_db
async def test_the_range_filter_narrows_the_series(session: AsyncSession, book: Book) -> None:
    """1M measured back from the last mark, so a range means the same thing every morning."""
    everything = await portfolio_nav(session, book.owner, book.core, NavRange.ALL)
    month = await portfolio_nav(session, book.owner, book.core, NavRange.M1)
    assert len(month.points) == len(everything.points)
    year = await portfolio_nav(session, book.owner, book.core, NavRange.Y3)
    assert year.range is NavRange.Y3


@requires_db
async def test_the_combined_chart_is_the_consolidated_series(
    session: AsyncSession, book: Book
) -> None:
    """§6.3's combined chart reads ``portfolio_nav_daily.portfolio_id IS NULL`` — one table."""
    view = await portfolio_overview(session, book.owner, NavRange.ALL)
    assert view.chart.portfolio_id is None
    assert [point.value for point in view.chart.points] == [
        Decimal("200000.00"),
        Decimal("207000.00"),
        Decimal("224000.00"),
    ]
    assert view.chart.total_return.value is not None


# ---------------------------------------------------------------------------
# §7 — activity
# ---------------------------------------------------------------------------


@requires_db
async def test_the_activity_feed_merges_the_five_sources(session: AsyncSession, book: Book) -> None:
    """§7: buys/sells, internal cash assignments, dividends, corporate actions, inbox history."""
    activity = await portfolio_activity(session, book.owner, None, 50)
    kinds = {item.kind.value for item in activity.items}
    assert {"EXTERNAL_DEPOSIT", "ASSIGN", "BUY", "RECONCILIATION"} <= kinds
    assert [item.on for item in activity.items] == sorted(
        (item.on for item in activity.items), reverse=True
    )

    assign = next(item for item in activity.items if item.kind.value == "ASSIGN")
    assert assign.portfolio is not None and assign.portfolio.portfolio_id == book.core
    assert assign.amount == Decimal("10000.00")

    question = next(item for item in activity.items if item.kind.value == "RECONCILIATION")
    assert question.is_pnl_event is False
    assert question.reconciliation_item_id == book.item_id


@requires_db
async def test_a_corporate_action_is_reported_and_is_not_a_pnl_event(
    session: AsyncSession, book: Book
) -> None:
    """§4.5 and criterion 6: a split changes quantity and average price and produces zero P&L.

    The row exists — a user whose share count doubled overnight has a question — and it is marked
    so that a client summing the feed cannot turn a split into a profit.
    """
    await session.execute(delete(CorporateAction).where(CorporateAction.instrument_id == book.infy))
    session.add(
        CorporateAction(
            instrument_id=book.infy,
            action_type="split",
            ex_date=dt.date(2026, 7, 1),
            ratio_from=Decimal("1"),
            ratio_to=Decimal("2"),
            raw={},
        )
    )
    await session.flush()

    activity = await portfolio_activity(session, book.owner, None, 50)
    action = next(item for item in activity.items if item.kind.value == "CORPORATE_ACTION")
    assert action.is_pnl_event is False
    assert action.instrument is not None and action.instrument.symbol == "INFY"


@requires_db
async def test_the_activity_filter_scopes_to_one_portfolio(
    session: AsyncSession, book: Book
) -> None:
    """A filter that returned an empty list for a foreign id would say "yours, and empty"."""
    activity = await portfolio_activity(session, book.owner, book.core, 50)
    for item in activity.items:
        if item.portfolio is not None:
            assert item.portfolio.portfolio_id == book.core


# ---------------------------------------------------------------------------
# §9 — there is no order path here, and there is not going to be one
# ---------------------------------------------------------------------------


def test_the_router_has_no_order_path() -> None:
    """Non-negotiable #1 and §9, as a grep over the module that serves these pages.

    The read API for a portfolio is exactly the surface where an "and place the trades" button
    would be asked for. It cannot appear by accident: nothing here may name a side, a product, a
    venue, an order type or a broker session, and this test is what makes adding one a failing
    build rather than a review comment.
    """
    source = MODULE.read_text(encoding="utf-8")
    for word in ORDER_WORDS:
        assert not re.search(word, source), (
            f"{word!r} is order vocabulary and has no place in this router"
        )


#: Every write ``portfolio_overview`` is allowed to declare, and why each is bookkeeping rather
#: than an order. A route not in this set fails the guard below until somebody adds it here with
# ---------------------------------------------------------------------------
# Audit 0.4 / 0.9 — real valuations and live-overlay previous close
# ---------------------------------------------------------------------------


def test_sync_summary_is_one_sentence_for_every_surface() -> None:
    """Audit 1.3: connected ≠ synced; Activity and the command centre must share one field."""
    from baskfy_api.routers.portfolio_overview import (  # noqa: PLC0415
        BrokerRefOut,
        SyncStatusOut,
        _sync_summary,
    )

    none = _sync_summary([])
    assert none == "No broker connected"

    never = _sync_summary(
        [
            SyncStatusOut(
                broker=BrokerRefOut(
                    broker_account_id=1, broker_id="zerodha", label="primary"
                ),
                synced_on=None,
                label="Holdings not synced yet",
            )
        ]
    )
    assert never == "Holdings not synced yet"

    dated = _sync_summary(
        [
            SyncStatusOut(
                broker=BrokerRefOut(
                    broker_account_id=1, broker_id="zerodha", label="primary"
                ),
                synced_on=TODAY,
                label=f"Holdings synced: {TODAY.isoformat()}",
            )
        ]
    )
    assert dated == f"Holdings synced: {TODAY.isoformat()}"


def test_twr_and_drawdown_are_none_until_two_real_valuations() -> None:
    """Audit 0.4: a ₹1 seed mark must not produce −100 % TWR / drawdown / a ₹1 peak.

    The old code chain-linked every stored mark. A placeholder first row of ₹1 against a later
    real mark made every derived return −100 % (or a peak of ₹1.00 on the wealth index). Refuse
    until ≥2 marks strictly above ₹1 exist.
    """
    from baskfy_api.routers.portfolio_overview import (  # noqa: PLC0415 - local to this pin
        _SeriesMeta,
        _consolidated_twr,
        _series_out,
    )

    seed = PortfolioNavDaily(
        user_id=1,
        portfolio_id=None,
        date=EARLIER,
        market_value=Decimal("1.00"),
        cash=Decimal("0"),
        net_flow=Decimal("0"),
        pending_reconciliation=False,
    )
    alone = PortfolioNavDaily(
        user_id=1,
        portfolio_id=None,
        date=YESTERDAY,
        market_value=Decimal("9980000.00"),
        cash=Decimal("0"),
        net_flow=Decimal("0"),
        pending_reconciliation=False,
    )
    second = PortfolioNavDaily(
        user_id=1,
        portfolio_id=None,
        date=TODAY,
        market_value=Decimal("10100000.00"),
        cash=Decimal("0"),
        net_flow=Decimal("0"),
        pending_reconciliation=False,
    )

    # One real mark after a ₹1 seed: still not enough for a return.
    thin = _series_out(
        [seed, alone],
        _SeriesMeta(
            portfolio_id=None,
            window=NavRange.ALL,
            label="Consolidated time-weighted return",
            since=None,
        ),
    )
    assert thin.total_return.value is None
    assert thin.max_drawdown is None
    assert thin.drawdown == []
    assert _consolidated_twr([seed, alone]).value is None

    # Two real marks: TWR and drawdown are computable, and the ₹1 seed is ignored.
    ready = _series_out(
        [seed, alone, second],
        _SeriesMeta(
            portfolio_id=None,
            window=NavRange.ALL,
            label="Consolidated time-weighted return",
            since=None,
        ),
    )
    assert ready.total_return.value is not None
    assert ready.max_drawdown is not None
    assert _consolidated_twr([seed, alone, second]).value is not None
    # The seed mark remains on the chart; derived views start from the first real mark.
    assert [point.value for point in ready.points][0] == Decimal("1.00")
    assert ready.total_return.since == YESTERDAY


@requires_db
async def test_live_overlay_sets_previous_to_the_stored_recency_one_close(
    session: AsyncSession, book: Book, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit 0.9: when LTP overlays `latest`, `previous` is the stored recency-1 close.

    The bug left `previous` at recency-2, so Today's P&L spanned two sessions (Monday LTP minus
    Thursday close). Overlay must push the stored latest into previous before replacing latest.
    """
    from baskfy_api.routers import portfolio_overview as overview_mod  # noqa: PLC0415

    # Book fixture: INFY closes are 1100 (earlier / recency-2) then 1200 (yesterday / recency-1).
    live = {book.infy: Decimal("1250.00")}
    monkeypatch.setattr(
        overview_mod,
        "live_prices_by_instrument",
        AsyncMock(return_value=live),
    )
    prices = await overview_mod._load_prices(session, [book.infy])
    assert prices.latest[book.infy] == Decimal("1250.00")
    assert prices.previous[book.infy] == Decimal("1200"), (
        "previous must be the stored recency-1 close, not recency-2"
    )


#: its reason — which is the review this test exists to force.
BOOKKEEPING_WRITES = {
    # §4.3: answers a question about a holdings change the sync already observed.
    "/reconciliation/{item_id}/resolve",
    # §6.7: files holdings the user already owns into a grouping they just named.
    "",
    # PF9: the same act against a portfolio that already exists rather than a new one.
    "/{portfolio_id}/holdings",
}


def test_only_bookkeeping_mutates() -> None:
    """Every write on this router is bookkeeping. Every other route is a read.

    Each of these moves an *allocation* — which logical portfolio a holding counts against — and
    none moves a share. There is no PUT and no DELETE: deleting a portfolio destroys a return
    series and belongs behind a confirmation, not on this router.

    **THIS USED TO ASSERT ``count == 2`` AND IT HAD BEEN FAILING SINCE PF9** (``20b6ea9``,
    "shares can be filed into a portfolio that already exists"), which added a third POST that is
    just as much bookkeeping as the other two. The guard was right about its subject and wrong
    about its instrument: a count fails on legitimate growth, so it was red for days and reported
    nothing, which is the worst state a safety test can be in — the kind of red a reader learns to
    scroll past.

    It now asserts the SET against a named allowlist. A new write fails until somebody writes down
    why it is bookkeeping, which is the question worth forcing; growth alone does not break it.
    """
    source = MODULE.read_text(encoding="utf-8")

    # Every POST this router declares, read off the source rather than counted.
    declared = set(re.findall(r'@router\.post\(\s*"([^"]*)"', source))
    assert declared == BOOKKEEPING_WRITES, (
        f"a write arrived on this router that is not named as bookkeeping: "
        f"{sorted(declared - BOOKKEEPING_WRITES)}. If it moves an ALLOCATION, add it to "
        f"BOOKKEEPING_WRITES with its reason. If it moves a SHARE, it does not belong here at "
        f"all — this router is not an order path (non-negotiable 2)."
    )
    assert "@router.put(" not in source
    assert "@router.delete(" not in source
