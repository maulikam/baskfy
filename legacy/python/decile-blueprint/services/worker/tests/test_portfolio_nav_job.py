"""The nightly EOD NAV job — ``PORTFOLIO_REDESIGN.md`` §5.1, §4.3, §5.2, criterion 1.

Every test here asserts the **spec**, not the implementation (house rule 2). Concretely, each one
is traceable to a sentence somebody wrote before the code existed:

* criterion 1 — *"Sum of all capital portfolios + unallocated (stocks + cash) equals consolidated
  net worth, to the paisa, at all times"*;
* §4.1 — *"Monitoring view … excluded from consolidated totals"*;
* §4.3 — *"Unresolved reconciliation items freeze that holding's contribution … rather than
  guessing. Never silently corrupt a portfolio's return series"*;
* house rule 7 — *"Re-running any day's job produces identical rows"*;
* §5.1 — an EOD NAV is a claim about a day the market printed, so a day with no close gets no
  row rather than a zero.

They run against a real PostgreSQL, because all five are claims about database state and a fake
would only prove the fake behaves.

WHY THESE FIXTURES DO NOT USE ``conftest.clean_db``
---------------------------------------------------
Two reasons, and both are about not entangling this leaf with the rest of the tree.

The first is that ``clean_db`` deletes ``app_user`` rows whose e-mail ends ``@example.com``, and
``portfolio.user_id`` has no ``ON DELETE`` action — so a portfolio left behind by *this* module
would make that delete fail and break every other test module in the tree. These users therefore
live under their own domain and :func:`_wipe` removes them in foreign-key order.

The second is that ``clean_db`` also runs ``seed_reference``, which seeds the curated catalogue,
which needs a published ``pipeline_run`` — and it currently raises ``NoPublishedData`` on a freshly
truncated database, taking every database-backed worker test with it. That fault belongs to
another tree and none of it is needed here: this job values holdings against bars, and the only
reference row it wants is the NSE exchange. So the fixture builds exactly that and nothing else,
which also makes these tests fast and independent of whatever the pipeline suite last left behind.

The dates are this module's own for the same reason. :func:`_market_printed` asks whether the
exchange printed *anything* on the valuation date, so a stray bar written by a neighbouring test
on a shared date would quietly turn "no close that day" into "a close exists" and the §5.1 refusal
would stop being tested.
"""

from __future__ import annotations

import datetime as dt
import inspect
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Final, cast

import pytest
import pytest_asyncio
from celery.schedules import crontab
from helpers import add_bar, make_instrument
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from baskfy_core.allocation_ledger import PortfolioKind, PortfolioSource
from baskfy_core.cash_ledger import CashFlowKind
from baskfy_core.models import PortfolioHolding
from baskfy_core.models.accounts import PortfolioNavDaily
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_COMPUTE, TASK_ROUTES, build_celery
from baskfy_worker.settings import WorkerSettings
from baskfy_worker.tasks import portfolio_nav_job
from baskfy_worker.tasks.portfolio_nav_job import NavRow, UserNav, run_portfolio_nav

#: ``asyncio_mode = "auto"`` (pyproject), so the async tests need no marker of their own. ``db``
#: is what ``make test-db`` selects on; the three wiring tests below need no database and are
#: happy to be swept up with it, while every database-backed test skips through
#: ``helpers.database_url`` when ``BASKFY_TEST_DATABASE_URL`` is unset.
pytestmark = [pytest.mark.db]

#: Their own domain, so ``conftest.clean_db``'s ``@example.com`` sweep never meets a portfolio it
#: cannot delete. See the module docstring.
TEST_DOMAIN: Final = "navjob.test"

#: Every instrument these tests create carries this prefix, so :func:`_wipe` can remove exactly
#: the reference rows this module owns and nobody else's.
SYMBOL_PREFIX: Final = "NAVJOB"

#: The day being valued, and the day before it. Both are this module's own — see the module
#: docstring on why a shared date would make the "no close" test unreliable.
VALUATION_DATE: Final = dt.date(2026, 8, 14)
EARLIER: Final = dt.date(2026, 8, 13)


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------


async def _wipe(session: AsyncSession) -> None:
    """Remove every row these tests own, children first, and nothing that belongs to anyone else.

    Written as explicit DELETEs rather than ``TRUNCATE ... CASCADE`` for the reason
    ``conftest.ACCOUNT_TABLES`` records: truncating ``app_user`` takes an ACCESS EXCLUSIVE lock,
    and the API suite in the neighbouring tree holds connections against the same database.

    Scoped by this module's own e-mail domain and symbol prefix rather than by table, so running
    these tests never destroys a neighbouring suite's fixtures — and so a re-run starts from the
    same state as a first run, which is the property the idempotence test is about.
    """
    owned = f"SELECT id FROM app_user WHERE email LIKE '%@{TEST_DOMAIN}'"
    instruments = f"SELECT id FROM instrument WHERE symbol LIKE '{SYMBOL_PREFIX}%'"
    for statement in (
        f"DELETE FROM portfolio_nav_daily WHERE user_id IN ({owned})",
        f"DELETE FROM reconciliation_item WHERE user_id IN ({owned})",
        f"DELETE FROM portfolio_cash_flow WHERE broker_account_id IN "
        f"(SELECT id FROM broker_account WHERE user_id IN ({owned}))",
        f"DELETE FROM portfolio_holding WHERE portfolio_id IN "
        f"(SELECT id FROM portfolio WHERE user_id IN ({owned}))",
        f"DELETE FROM portfolio WHERE user_id IN ({owned})",
        f"DELETE FROM broker_cash WHERE broker_account_id IN "
        f"(SELECT id FROM broker_account WHERE user_id IN ({owned}))",
        f"DELETE FROM broker_account WHERE user_id IN ({owned})",
        f"DELETE FROM app_user WHERE email LIKE '%@{TEST_DOMAIN}'",
        f"DELETE FROM ohlcv_daily WHERE instrument_id IN ({instruments})",
        f"DELETE FROM instrument WHERE symbol LIKE '{SYMBOL_PREFIX}%'",
        # The one reference row a valuation needs. ``instrument.exchange_id`` points at it, and
        # this module deliberately does not run ``seed_reference`` (see the module docstring).
        "INSERT INTO exchange (id, code) VALUES "
        f"({NSE_EXCHANGE_ID}, 'NSE') ON CONFLICT (id) DO NOTHING",
    ):
        await session.execute(text(statement))
    await session.flush()


@pytest_asyncio.fixture
async def ledger(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """One transactional session per test, with this module's rows cleared first.

    Built on ``conftest.engine`` (a migrated database) rather than on ``conftest.session``, which
    would drag in ``clean_db`` and its currently-broken reference seed. The transaction commits on
    a clean exit, so a test can assert on rows the job wrote and the next test's :func:`_wipe`
    takes them away again.
    """
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active, active.begin():
        await _wipe(active)
        yield active


async def make_user(session: AsyncSession, handle: str) -> int:
    row = await session.execute(
        text(
            "INSERT INTO app_user (public_id, email, name) "
            "VALUES (:public_id, :email, :name) RETURNING id"
        ),
        {"public_id": f"pub-{handle}", "email": f"{handle}@{TEST_DOMAIN}", "name": handle},
    )
    return int(row.scalar_one())


async def make_broker_account(session: AsyncSession, user_id: int, label: str = "primary") -> int:
    row = await session.execute(
        text(
            "INSERT INTO broker_account (user_id, broker_id, label) "
            "VALUES (:user_id, 'zerodha', :label) RETURNING id"
        ),
        {"user_id": user_id, "label": label},
    )
    return int(row.scalar_one())


async def make_portfolio(  # noqa: PLR0913 - one parameter per column the spec makes meaningful
    session: AsyncSession,
    user_id: int,
    name: str,
    *,
    kind: PortfolioKind = PortfolioKind.CAPITAL,
    source: PortfolioSource = PortfolioSource.HOLDING_GROUP,
    started_on: dt.date = EARLIER,
) -> int:
    """Insert through SQL, not the ORM.

    ``portfolio.started_on`` is NOT NULL as of migration 0022 and the ``Portfolio`` ORM class does
    not carry it yet — that model belongs to another leaf. An ORM insert would omit the column and
    the database would refuse the row.
    """
    row = await session.execute(
        text(
            "INSERT INTO portfolio (user_id, name, kind, source, started_on) "
            "VALUES (:user_id, :name, :kind, :source, :started_on) RETURNING id"
        ),
        {
            "user_id": user_id,
            "name": name,
            "kind": kind.value,
            "source": source.value,
            "started_on": started_on,
        },
    )
    return int(row.scalar_one())


async def add_holding(  # noqa: PLR0913 - the composite key is four columns wide
    session: AsyncSession,
    portfolio_id: int,
    instrument_id: int,
    broker_account_id: int,
    quantity: str,
    *,
    kind: PortfolioKind = PortfolioKind.CAPITAL,
    added_on: dt.date = EARLIER,
) -> None:
    session.add(
        PortfolioHolding(
            portfolio_id=portfolio_id,
            instrument_id=instrument_id,
            broker_account_id=broker_account_id,
            portfolio_kind=kind.value,
            quantity=Decimal(quantity),
            added_on=added_on,
        )
    )
    await session.flush()


async def add_flow(  # noqa: PLR0913 - a flow is defined by exactly these facts and no fewer
    session: AsyncSession,
    broker_account_id: int,
    kind: CashFlowKind,
    amount: str,
    *,
    occurred_on: dt.date,
    portfolio_id: int | None = None,
    instrument_id: int | None = None,
    quantity: str | None = None,
) -> None:
    """Insert one ``portfolio_cash_flow`` row.

    ``instrument_id``/``quantity`` are required for ``BUY`` and ``SELL`` and forbidden for every
    other kind — ``cash_ledger.PortfolioCashFlow`` refuses the row otherwise, on the grounds that
    an activity feed would read a cash-only flow naming a stock as a trade that never happened.
    """
    await session.execute(
        text(
            "INSERT INTO portfolio_cash_flow "
            "(broker_account_id, portfolio_id, kind, amount, occurred_on, instrument_id, "
            "quantity) VALUES (:broker_account_id, :portfolio_id, :kind, :amount, :occurred_on, "
            ":instrument_id, :quantity)"
        ),
        {
            "broker_account_id": broker_account_id,
            "portfolio_id": portfolio_id,
            "kind": kind.value,
            "amount": Decimal(amount),
            "occurred_on": occurred_on,
            "instrument_id": instrument_id,
            "quantity": None if quantity is None else Decimal(quantity),
        },
    )


async def open_reconciliation(
    session: AsyncSession,
    user_id: int,
    instrument_id: int,
    broker_account_id: int,
    *,
    detected_on: dt.date = VALUATION_DATE,
) -> None:
    await session.execute(
        text(
            "INSERT INTO reconciliation_item "
            "(user_id, instrument_id, broker_account_id, quantity, reason, state, detected_on) "
            "VALUES (:user_id, :instrument_id, :broker_account_id, 100, "
            "'UNALLOCATED_HOLDING', 'OPEN', :detected_on)"
        ),
        {
            "user_id": user_id,
            "instrument_id": instrument_id,
            "broker_account_id": broker_account_id,
            "detected_on": detected_on,
        },
    )


async def stored_rows(session: AsyncSession, user_id: int, on: dt.date) -> list[PortfolioNavDaily]:
    """Every stored NAV row for a user on a date, consolidated first then by portfolio."""
    rows = await session.scalars(
        select(PortfolioNavDaily)
        .where(PortfolioNavDaily.user_id == user_id, PortfolioNavDaily.date == on)
        .order_by(PortfolioNavDaily.portfolio_id.nulls_first())
    )
    return list(rows.all())


def row_for(user: UserNav, portfolio_id: int) -> NavRow:
    match = [row for row in user.portfolio_rows if row.portfolio_id == portfolio_id]
    assert match, f"no NAV row was produced for portfolio {portfolio_id}"
    return match[0]


def _settings() -> WorkerSettings:
    return WorkerSettings(
        _env_file=None,
        redis_url="redis://localhost:6380/0",
        celery_broker_url="",
        task_max_retries=3,
        task_retry_backoff_seconds=60,
    )


# ---------------------------------------------------------------------------
# Wiring — the job has to actually be scheduled, or none of the rest happens
# ---------------------------------------------------------------------------


def test_beat_entry_runs_nightly_after_the_pipeline_publishes() -> None:
    """§5.1 says *nightly*, and docs/11 puts the publish deadline at 20:15 IST.

    The entry must land after the jobs whose rows it reads — curated metrics at 20:20 and
    dividends (which are portfolio cash flows) at 20:25 — or it would value the day against a
    ledger that is still being written.
    """
    entry = BEAT_SCHEDULE["portfolio-eod-nav"]
    assert entry["task"] == "baskfy.portfolio.eod_nav"
    schedule = cast(crontab, entry["schedule"])
    assert schedule.hour == {20}
    assert schedule.minute == {35}
    assert schedule.day_of_week == {1, 2, 3, 4, 5}
    options = entry["options"]
    assert isinstance(options, dict), "the beat entry must carry an options mapping"
    assert options["queue"] == QUEUE_COMPUTE
    # The publish SLO and the two jobs this one must follow.
    assert cast(crontab, BEAT_SCHEDULE["cb-eod-metrics"]["schedule"]).minute == {20}
    assert cast(crontab, BEAT_SCHEDULE["cb-dividends"]["schedule"]).minute == {25}


def test_task_is_registered_and_routed_to_compute() -> None:
    app = build_celery(_settings())
    assert "baskfy.portfolio.eod_nav" in app.tasks
    assert TASK_ROUTES["baskfy.portfolio.*"]["queue"] == QUEUE_COMPUTE


def test_the_job_is_not_an_order_path() -> None:
    """The guard ``allocation_ledger`` and ``portfolio_units`` carry, applied to this module.

    Valuation reads prices; it never places anything. Asserting it on the source means an edit
    that quietly crosses the line fails a test rather than a review (non-negotiable 6).
    """
    source = inspect.getsource(portfolio_nav_job)
    for forbidden in (
        "place_order",
        "place_gtt",
        "OrderGateway",
        "kite_client",
        "transaction_type",
    ):
        assert forbidden not in source, f"{forbidden!r} has no business in a valuation job"


# ---------------------------------------------------------------------------
# Criterion 1 — the parts and the whole, to the paisa
# ---------------------------------------------------------------------------


async def test_consolidated_equals_the_sum_of_its_parts_to_the_paisa(
    ledger: AsyncSession,
) -> None:
    """Criterion 1, on quantities and prices a ``float`` gets wrong.

    Every product below lands exactly on a half-paisa — ``3 x 10.5050 = 31.515`` — where binary
    floating point falls *just under* (``31.514999999999997``) and rounds down. Decimal with
    ROUND_HALF_UP rounds up. So each of the three holdings differs by a paisa between the two
    implementations, and the equality asserted here is only reachable with ``Decimal`` end to end
    (house rule 9).

    The identity checked is the criterion's own wording: capital portfolios + unallocated (stocks
    *and* cash) == consolidated net worth.
    """
    user_id = await make_user(ledger, "criterion1")
    broker = await make_broker_account(ledger, user_id)
    momentum = await make_portfolio(ledger, user_id, "Momentum")
    longterm = await make_portfolio(ledger, user_id, "Long term")

    alpha = await make_instrument(ledger, "NAVJOBALPHA", token=910001)
    beta = await make_instrument(ledger, "NAVJOBBETA", token=910002)
    gamma = await make_instrument(ledger, "NAVJOBGAMMA", token=910003)
    await add_bar(ledger, alpha, VALUATION_DATE, "10.5050")
    await add_bar(ledger, beta, VALUATION_DATE, "20.0350")
    await add_bar(ledger, gamma, VALUATION_DATE, "30.1050")

    await add_holding(ledger, momentum, alpha, broker, "3")
    await add_holding(ledger, momentum, beta, broker, "7")
    await add_holding(ledger, longterm, gamma, broker, "9")

    await add_flow(ledger, broker, CashFlowKind.EXTERNAL_DEPOSIT, "5000.00", occurred_on=EARLIER)
    await add_flow(
        ledger,
        broker,
        CashFlowKind.ASSIGN,
        "1000.00",
        occurred_on=VALUATION_DATE,
        portfolio_id=momentum,
    )

    result = await run_portfolio_nav(ledger, VALUATION_DATE)

    assert result.skipped_reason is None
    assert len(result.users) == 1
    user = result.users[0]

    # The paisa that separates Decimal from float, holding by holding.
    assert row_for(user, momentum).market_value == Decimal("171.77")  # 31.52 + 140.25
    assert row_for(user, longterm).market_value == Decimal("270.95")

    parts = (
        sum((row.total_value for row in user.portfolio_rows), start=Decimal("0"))
        + user.unallocated_market_value
        + user.unallocated_cash
    )
    assert user.consolidated.total_value == parts
    assert user.consolidated.total_value == Decimal("5442.72")

    # And the same identity holds over what was actually written, not merely what was computed.
    stored = await stored_rows(ledger, user_id, VALUATION_DATE)
    assert [row.portfolio_id for row in stored] == [None, momentum, longterm]
    consolidated_row, *portfolio_stored = stored
    assert (
        consolidated_row.market_value + consolidated_row.cash
        == sum((row.market_value + row.cash for row in portfolio_stored), start=Decimal("0"))
        + user.unallocated_market_value
        + user.unallocated_cash
    )


async def test_net_flow_divides_the_day_for_the_right_pot(ledger: AsyncSession) -> None:
    """§4.4 and §5.1: TWR needs the flow that changed the pot without being a return.

    An ``ASSIGN`` fills a portfolio, so it is that portfolio's flow. It moves money between two
    pockets of the *consolidated* pot, so it is not the consolidated flow — a deposit is. A
    ``BUY`` is cash turning into stock and is neither.
    """
    user_id = await make_user(ledger, "flows")
    broker = await make_broker_account(ledger, user_id)
    momentum = await make_portfolio(ledger, user_id, "Momentum")
    alpha = await make_instrument(ledger, "NAVJOBFLOW", token=910010)
    await add_bar(ledger, alpha, VALUATION_DATE, "100.0000")
    await add_holding(ledger, momentum, alpha, broker, "5")

    await add_flow(
        ledger, broker, CashFlowKind.EXTERNAL_DEPOSIT, "9000.00", occurred_on=VALUATION_DATE
    )
    await add_flow(
        ledger,
        broker,
        CashFlowKind.ASSIGN,
        "2000.00",
        occurred_on=VALUATION_DATE,
        portfolio_id=momentum,
    )
    await add_flow(
        ledger,
        broker,
        CashFlowKind.RELEASE,
        "500.00",
        occurred_on=VALUATION_DATE,
        portfolio_id=momentum,
    )
    await add_flow(
        ledger,
        broker,
        CashFlowKind.BUY,
        "400.00",
        occurred_on=VALUATION_DATE,
        portfolio_id=momentum,
        instrument_id=alpha,
        quantity="4",
    )
    # Yesterday's assignment is not today's flow — the series divides *this* day only.
    await add_flow(
        ledger, broker, CashFlowKind.ASSIGN, "700.00", occurred_on=EARLIER, portfolio_id=momentum
    )

    user = (await run_portfolio_nav(ledger, VALUATION_DATE)).users[0]

    assert row_for(user, momentum).net_flow == Decimal("1500.00")  # 2000 assigned - 500 released
    assert user.consolidated.net_flow == Decimal("9000.00")  # the deposit, and nothing internal


# ---------------------------------------------------------------------------
# §4.1 — a monitoring view never enters a total
# ---------------------------------------------------------------------------


async def test_a_monitoring_view_gets_no_row_and_no_share_of_the_total(
    ledger: AsyncSession,
) -> None:
    """§4.1: *"Excluded from consolidated totals."*

    The lens holds the *same* physical holding as the capital portfolio, which is the whole point
    of a lens and the exact shape that double-counts if the exclusion is a filter someone forgets.
    Consolidated net worth must be the holding's value once.
    """
    user_id = await make_user(ledger, "lens")
    broker = await make_broker_account(ledger, user_id)
    capital = await make_portfolio(ledger, user_id, "Core")
    lens = await make_portfolio(
        ledger,
        user_id,
        "All defence stocks",
        kind=PortfolioKind.MONITORING,
        source=PortfolioSource.MY_SCREEN,
    )
    alpha = await make_instrument(ledger, "NAVJOBLENS", token=910020)
    await add_bar(ledger, alpha, VALUATION_DATE, "250.0000")
    await add_holding(ledger, capital, alpha, broker, "4")
    await add_holding(ledger, lens, alpha, broker, "4", kind=PortfolioKind.MONITORING)

    user = (await run_portfolio_nav(ledger, VALUATION_DATE)).users[0]

    assert [row.portfolio_id for row in user.portfolio_rows] == [capital]
    assert user.consolidated.market_value == Decimal("1000.00")

    stored = await stored_rows(ledger, user_id, VALUATION_DATE)
    assert lens not in [row.portfolio_id for row in stored]
    assert len(stored) == 2  # the consolidated row and the one capital portfolio


async def test_cash_assigned_to_a_monitoring_view_is_refused_not_quietly_counted(
    ledger: AsyncSession,
) -> None:
    """A lens holds no cash either, and dropping the flow would break criterion 1 invisibly.

    ``total_cash`` sums flows, not portfolios, so a flow naming a monitoring view would land in
    consolidated cash and in no portfolio row — the parts short of the whole by exactly that
    amount, on a day nobody changed anything. Refusing is the only outcome that surfaces it.
    """
    user_id = await make_user(ledger, "lenscash")
    broker = await make_broker_account(ledger, user_id)
    lens = await make_portfolio(
        ledger,
        user_id,
        "Bought in 2026",
        kind=PortfolioKind.MONITORING,
        source=PortfolioSource.MY_SCREEN,
    )
    alpha = await make_instrument(ledger, "NAVJOBLENSC", token=910021)
    await add_bar(ledger, alpha, VALUATION_DATE, "10.0000")
    await add_flow(
        ledger, broker, CashFlowKind.ASSIGN, "100.00", occurred_on=VALUATION_DATE, portfolio_id=lens
    )

    with pytest.raises(ValueError, match="monitoring view"):
        await run_portfolio_nav(ledger, VALUATION_DATE)


# ---------------------------------------------------------------------------
# §4.3 — an open question freezes the holding rather than being guessed at
# ---------------------------------------------------------------------------


async def test_an_open_reconciliation_item_marks_pending_and_invents_no_value(
    ledger: AsyncSession,
) -> None:
    """§4.3: *"freeze that holding's contribution … rather than guessing."*

    Two assertions, and the second is the one that matters. The affected portfolio's row is
    flagged — and the flagged holding's value is *absent* from every total, not estimated from
    yesterday's close or from its average price. The unaffected portfolio is untouched: a frozen
    holding freezes its own portfolio's number, not the whole account's arithmetic.
    """
    user_id = await make_user(ledger, "frozen")
    broker = await make_broker_account(ledger, user_id)
    disputed = await make_portfolio(ledger, user_id, "Momentum")
    clean = await make_portfolio(ledger, user_id, "Long term")

    alpha = await make_instrument(ledger, "NAVJOBFROZ", token=910030)
    beta = await make_instrument(ledger, "NAVJOBCLEAN", token=910031)
    await add_bar(ledger, alpha, VALUATION_DATE, "500.0000")
    await add_bar(ledger, beta, VALUATION_DATE, "200.0000")
    await add_holding(ledger, disputed, alpha, broker, "10")
    await add_holding(ledger, clean, beta, broker, "3")

    await open_reconciliation(ledger, user_id, alpha, broker)

    user = (await run_portfolio_nav(ledger, VALUATION_DATE)).users[0]

    disputed_row = row_for(user, disputed)
    assert disputed_row.pending_reconciliation is True
    # Not 5000.00, and not a stale carry-forward either: the holding is simply not valued.
    assert disputed_row.market_value == Decimal("0.00")
    assert user.frozen_holdings == 1

    clean_row = row_for(user, clean)
    assert clean_row.pending_reconciliation is False
    assert clean_row.market_value == Decimal("600.00")

    # The day as a whole is incomplete, and says so.
    assert user.consolidated.pending_reconciliation is True
    assert user.consolidated.market_value == Decimal("600.00")

    stored = {row.portfolio_id: row for row in await stored_rows(ledger, user_id, VALUATION_DATE)}
    assert stored[disputed].pending_reconciliation is True
    assert stored[None].pending_reconciliation is True
    assert stored[clean].pending_reconciliation is False


async def test_a_resolved_item_no_longer_freezes_anything(ledger: AsyncSession) -> None:
    """Only ``OPEN`` freezes. A resolved item names the portfolio it was resolved to (0022's
    CHECK), so the holding has an owner again and there is nothing left to guess."""
    user_id = await make_user(ledger, "resolved")
    broker = await make_broker_account(ledger, user_id)
    momentum = await make_portfolio(ledger, user_id, "Momentum")
    alpha = await make_instrument(ledger, "NAVJOBRESOL", token=910040)
    await add_bar(ledger, alpha, VALUATION_DATE, "500.0000")
    await add_holding(ledger, momentum, alpha, broker, "10")
    await ledger.execute(
        text(
            "INSERT INTO reconciliation_item (user_id, instrument_id, broker_account_id, "
            "quantity, reason, state, resolved_portfolio_id, detected_on, resolved_at) "
            "VALUES (:user_id, :instrument_id, :broker, 10, 'UNALLOCATED_HOLDING', 'RESOLVED', "
            ":portfolio_id, :detected_on, now())"
        ),
        {
            "user_id": user_id,
            "instrument_id": alpha,
            "broker": broker,
            "portfolio_id": momentum,
            "detected_on": EARLIER,
        },
    )

    user = (await run_portfolio_nav(ledger, VALUATION_DATE)).users[0]

    assert row_for(user, momentum).market_value == Decimal("5000.00")
    assert user.consolidated.pending_reconciliation is False


async def test_an_item_raised_after_the_date_does_not_poison_that_date(
    ledger: AsyncSession,
) -> None:
    """A day is valued with what was known about it (house rule 5, applied to the inbox).

    Re-running an old date must not be retroactively frozen by a question raised last week, or
    every historical point in the chart would decay into "pending" as the inbox filled up.
    """
    user_id = await make_user(ledger, "later")
    broker = await make_broker_account(ledger, user_id)
    momentum = await make_portfolio(ledger, user_id, "Momentum")
    alpha = await make_instrument(ledger, "NAVJOBLATER", token=910050)
    await add_bar(ledger, alpha, VALUATION_DATE, "100.0000")
    await add_holding(ledger, momentum, alpha, broker, "2")
    await open_reconciliation(
        ledger, user_id, alpha, broker, detected_on=VALUATION_DATE + dt.timedelta(days=1)
    )

    user = (await run_portfolio_nav(ledger, VALUATION_DATE)).users[0]

    assert user.consolidated.pending_reconciliation is False
    assert row_for(user, momentum).market_value == Decimal("200.00")


# ---------------------------------------------------------------------------
# House rule 7 — re-running a date changes nothing
# ---------------------------------------------------------------------------


async def test_rerunning_the_same_date_changes_nothing(ledger: AsyncSession) -> None:
    """*"Re-running any day's job produces identical rows."*

    Asserted on the whole table for the date rather than on a row count, because the failure this
    guards against is an *append*: under a plain unique index the consolidated row's NULL
    ``portfolio_id`` would not collide with itself and a second run would silently double the
    user's net worth in every chart that sums the series.
    """
    user_id = await make_user(ledger, "idempotent")
    broker = await make_broker_account(ledger, user_id)
    momentum = await make_portfolio(ledger, user_id, "Momentum")
    alpha = await make_instrument(ledger, "NAVJOBIDEM", token=910060)
    await add_bar(ledger, alpha, VALUATION_DATE, "123.4500")
    await add_holding(ledger, momentum, alpha, broker, "8")
    await add_flow(ledger, broker, CashFlowKind.EXTERNAL_DEPOSIT, "250.00", occurred_on=EARLIER)

    def snapshot(rows: list[PortfolioNavDaily]) -> list[tuple[object, ...]]:
        return [
            (
                row.portfolio_id,
                row.market_value,
                row.cash,
                row.net_flow,
                row.pending_reconciliation,
            )
            for row in rows
        ]

    first = await run_portfolio_nav(ledger, VALUATION_DATE)
    before = snapshot(await stored_rows(ledger, user_id, VALUATION_DATE))

    second = await run_portfolio_nav(ledger, VALUATION_DATE)
    after = snapshot(await stored_rows(ledger, user_id, VALUATION_DATE))

    assert before == after
    assert len(after) == 2
    assert first.rows_written == second.rows_written == 2
    # The marks are written once; the second pass finds nothing left to mark (§5.2).
    assert first.marks_written == 1
    assert second.marks_written == 0


async def test_a_portfolio_that_became_a_lens_loses_its_row_on_the_next_run(
    ledger: AsyncSession,
) -> None:
    """Idempotence is convergence, not refresh: the set of rows can shrink (§4.1).

    An upsert alone would leave yesterday's row behind for a portfolio that is now a monitoring
    view, and a chart summing the day would keep counting it — the double-count §4.1 exists to
    prevent, arriving a day late.
    """
    user_id = await make_user(ledger, "converge")
    broker = await make_broker_account(ledger, user_id)
    switching = await make_portfolio(ledger, user_id, "Was capital")
    alpha = await make_instrument(ledger, "NAVJOBCONV", token=910070)
    await add_bar(ledger, alpha, VALUATION_DATE, "50.0000")
    await add_holding(ledger, switching, alpha, broker, "2")

    await run_portfolio_nav(ledger, VALUATION_DATE)
    assert len(await stored_rows(ledger, user_id, VALUATION_DATE)) == 2

    # ON UPDATE CASCADE carries `portfolio_holding.portfolio_kind` along with it (0021).
    await ledger.execute(
        text("UPDATE portfolio SET kind = 'MONITORING' WHERE id = :id"), {"id": switching}
    )

    result = await run_portfolio_nav(ledger, VALUATION_DATE)

    assert result.rows_removed == 1
    stored = await stored_rows(ledger, user_id, VALUATION_DATE)
    assert [row.portfolio_id for row in stored] == [None]
    assert stored[0].market_value == Decimal("0.00")


# ---------------------------------------------------------------------------
# §5.1 — a NAV is a claim about a day the market printed
# ---------------------------------------------------------------------------


async def test_a_date_with_no_bars_produces_no_rows_at_all(ledger: AsyncSession) -> None:
    """Not zero-valued rows. A gap reads as "no close"; a zero reads as "you lost everything".

    The holdings, the portfolio and the cash all exist — only the day's closes are missing, which
    is a Saturday, a holiday, or an ingest that has not landed yet. None of those is a day whose
    NAV can be stated, and §5.1's whole posture is that the number is official or absent.
    """
    user_id = await make_user(ledger, "noclose")
    broker = await make_broker_account(ledger, user_id)
    momentum = await make_portfolio(ledger, user_id, "Momentum")
    alpha = await make_instrument(ledger, "NAVJOBNOBAR", token=910080)
    await add_holding(ledger, momentum, alpha, broker, "10")
    await add_flow(ledger, broker, CashFlowKind.EXTERNAL_DEPOSIT, "1000.00", occurred_on=EARLIER)

    result = await run_portfolio_nav(ledger, VALUATION_DATE)

    assert result.rows_written == 0
    assert result.users == []
    assert result.skipped_reason is not None
    assert await stored_rows(ledger, user_id, VALUATION_DATE) == []


async def test_a_holding_with_no_price_is_flagged_rather_than_valued_at_zero(
    ledger: AsyncSession,
) -> None:
    """A missing price is not a price of zero — ``holding_value`` refuses one for this reason.

    The market printed (another instrument has a bar), so the date is valuable; this one name has
    no print on or before it. Valuing it at zero would give a total that is wrong and still
    balances, which is the one failure criterion 1 cannot catch. It is dropped and its portfolio
    says the day is incomplete.
    """
    user_id = await make_user(ledger, "unpriced")
    broker = await make_broker_account(ledger, user_id)
    momentum = await make_portfolio(ledger, user_id, "Momentum")
    priced = await make_instrument(ledger, "NAVJOBPRICED", token=910090)
    unpriced = await make_instrument(ledger, "NAVJOBUNPRICED", token=910091)
    await add_bar(ledger, priced, VALUATION_DATE, "40.0000")
    await add_holding(ledger, momentum, priced, broker, "2")
    await add_holding(ledger, momentum, unpriced, broker, "999")

    user = (await run_portfolio_nav(ledger, VALUATION_DATE)).users[0]

    assert user.unpriced_holdings == 1
    assert row_for(user, momentum).market_value == Decimal("80.00")
    assert row_for(user, momentum).pending_reconciliation is True


async def test_a_stock_that_did_not_trade_is_carried_at_its_last_print(
    ledger: AsyncSession,
) -> None:
    """Standard fund practice, and safe because :func:`_market_printed` gates it.

    The date is known to have closes, so this can only carry a single illiquid name forward, never
    a whole weekend. ``close_raw`` is used, not ``close`` (house rule 6) — a portfolio's market
    value is the most literal "the user expects a real price" there is.
    """
    user_id = await make_user(ledger, "illiquid")
    broker = await make_broker_account(ledger, user_id)
    momentum = await make_portfolio(ledger, user_id, "Momentum")
    liquid = await make_instrument(ledger, "NAVJOBLIQ", token=910100)
    illiquid = await make_instrument(ledger, "NAVJOBILLIQ", token=910101)
    await add_bar(ledger, liquid, VALUATION_DATE, "10.0000")
    await add_bar(ledger, illiquid, EARLIER, "77.0000")
    await add_holding(ledger, momentum, illiquid, broker, "3")

    user = (await run_portfolio_nav(ledger, VALUATION_DATE)).users[0]

    assert user.unpriced_holdings == 0
    assert row_for(user, momentum).market_value == Decimal("231.00")


async def test_a_user_with_nothing_gets_no_row(ledger: AsyncSession) -> None:
    """A flat line at zero since sign-up is true and useless.

    §6.6's empty state — *connect your broker* — is the honest surface for an account with nothing
    in it, not a chart of nil.
    """
    user_id = await make_user(ledger, "empty")
    await make_broker_account(ledger, user_id)
    alpha = await make_instrument(ledger, "NAVJOBEMPTY", token=910110)
    await add_bar(ledger, alpha, VALUATION_DATE, "10.0000")

    result = await run_portfolio_nav(ledger, VALUATION_DATE)

    assert result.users == []
    assert await stored_rows(ledger, user_id, VALUATION_DATE) == []


# ---------------------------------------------------------------------------
# §5.2 — the "since grouped" marks
# ---------------------------------------------------------------------------


async def test_the_grouping_date_is_stamped_without_unlocking_xirr(
    ledger: AsyncSession,
) -> None:
    """§5.2: *"since grouped" … Until then, do NOT display XIRR or since-purchase P&L.*

    ``first_bought_on`` gets the grouping date, because it is the earliest day any EOD mark
    exists. ``history_source`` stays ``NONE``, because a grouping date is not a purchase record —
    and it is ``history_source`` that a reader consults before offering XIRR. Setting both would
    be the lie that quietly unlocks the number §5.2 forbids.
    """
    user_id = await make_user(ledger, "marks")
    broker = await make_broker_account(ledger, user_id)
    group = await make_portfolio(ledger, user_id, "My demat")
    alpha = await make_instrument(ledger, "NAVJOBMARK", token=910120)
    await add_bar(ledger, alpha, VALUATION_DATE, "15.0000")
    await add_holding(ledger, group, alpha, broker, "6", added_on=EARLIER)

    await run_portfolio_nav(ledger, VALUATION_DATE)

    row = (
        await ledger.execute(
            text(
                "SELECT first_bought_on, history_source FROM portfolio_holding "
                "WHERE portfolio_id = :portfolio_id"
            ),
            {"portfolio_id": group},
        )
    ).one()
    assert row[0] == EARLIER
    assert row[1] == "NONE"


async def test_an_imported_purchase_date_is_never_overwritten(ledger: AsyncSession) -> None:
    """A CAS import (§5.3) is a real purchase record; the grouping mark must not clobber it."""
    user_id = await make_user(ledger, "cas")
    broker = await make_broker_account(ledger, user_id)
    group = await make_portfolio(ledger, user_id, "My demat")
    alpha = await make_instrument(ledger, "NAVJOBCAS", token=910130)
    await add_bar(ledger, alpha, VALUATION_DATE, "15.0000")
    await add_holding(ledger, group, alpha, broker, "6", added_on=EARLIER)
    bought = dt.date(2019, 4, 1)
    await ledger.execute(
        text(
            "UPDATE portfolio_holding SET first_bought_on = :bought, history_source = 'CAS' "
            "WHERE portfolio_id = :portfolio_id"
        ),
        {"bought": bought, "portfolio_id": group},
    )

    result = await run_portfolio_nav(ledger, VALUATION_DATE)

    assert result.marks_written == 0
    row = (
        await ledger.execute(
            text(
                "SELECT first_bought_on, history_source FROM portfolio_holding "
                "WHERE portfolio_id = :portfolio_id"
            ),
            {"portfolio_id": group},
        )
    ).one()
    assert row[0] == bought
    assert row[1] == "CAS"


async def test_a_holding_grouped_after_the_date_is_not_stamped_by_a_backfill(
    ledger: AsyncSession,
) -> None:
    """Valuing January must not import March into it (house rule 5)."""
    user_id = await make_user(ledger, "backfill")
    broker = await make_broker_account(ledger, user_id)
    group = await make_portfolio(ledger, user_id, "My demat")
    alpha = await make_instrument(ledger, "NAVJOBBACK", token=910140)
    await add_bar(ledger, alpha, VALUATION_DATE, "15.0000")
    await add_holding(
        ledger, group, alpha, broker, "6", added_on=VALUATION_DATE + dt.timedelta(days=7)
    )

    result = await run_portfolio_nav(ledger, VALUATION_DATE)

    assert result.marks_written == 0
    stamped = await ledger.scalar(
        text("SELECT first_bought_on FROM portfolio_holding WHERE portfolio_id = :portfolio_id"),
        {"portfolio_id": group},
    )
    assert stamped is None


# ---------------------------------------------------------------------------
# Two users at once — one user's ledger never leaks into another's
# ---------------------------------------------------------------------------


async def test_each_user_gets_their_own_consolidated_row(ledger: AsyncSession) -> None:
    """Tenancy, asserted at the arithmetic rather than at the query.

    Both users hold the same instrument at the same price; neither total may include the other's
    shares. The consolidated row is keyed on ``user_id``, so a leak here would show up as one
    user's net worth containing the other's.
    """
    first_id = await make_user(ledger, "tenant-a")
    second_id = await make_user(ledger, "tenant-b")
    first_broker = await make_broker_account(ledger, first_id)
    second_broker = await make_broker_account(ledger, second_id)
    first_portfolio = await make_portfolio(ledger, first_id, "A")
    second_portfolio = await make_portfolio(ledger, second_id, "B")
    alpha = await make_instrument(ledger, "NAVJOBTEN", token=910150)
    await add_bar(ledger, alpha, VALUATION_DATE, "100.0000")
    await add_holding(ledger, first_portfolio, alpha, first_broker, "2")
    await add_holding(ledger, second_portfolio, alpha, second_broker, "5")

    result = await run_portfolio_nav(ledger, VALUATION_DATE)

    by_user = {user.user_id: user for user in result.users}
    assert by_user[first_id].consolidated.market_value == Decimal("200.00")
    assert by_user[second_id].consolidated.market_value == Decimal("500.00")
    assert result.rows_written == 4

    payload = result.as_json()
    assert payload["users"] == 2
    assert payload["rows_written"] == 4
