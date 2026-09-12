"""The Bonds portfolio — a capital group for the one instrument this system may never trade.

Maulik holds `SGBDE31III`, a Sovereign Gold Bond, and asked for it to live in a portfolio named
for bonds. Two facts about it pull in opposite directions and both have to survive:

* it is **real money**, so it must sum into consolidated net worth like any other holding
  (`PORTFOLIO_REDESIGN.md` §4.1 — that is what a *capital* portfolio is);
* it is **untouchable**, so no portfolio it sits in may ever become a route to an order
  (`CLAUDE.md` non-negotiable #6 — SGB/G-sec blocked at the lowest layer).

WHAT THIS FILE ASSERTS, AND WHY EACH PART IS A SPEC AND NOT A SNAPSHOT
---------------------------------------------------------------------
1. **"Bonds" is a name, not a kind.** `PortfolioKind` is an *arithmetic* distinction — does this
   group enter net worth — and `PortfolioSource` is *provenance*, which decides the headline
   metric (§5.2). An asset class is neither, so the right answer is the boring one: a plain
   CAPITAL / HOLDING_GROUP portfolio that happens to be called Bonds. The test pins the two
   enums as closed sets, so a later agent who "adds a BONDS kind" has to argue with a test that
   says why there isn't one.

   The stronger reason is safety. Untradeability is a property of the **instrument**, enforced in
   `baskfy_execution.guards`, and a user can rename, empty or delete a portfolio. Encoding
   "never trade this" as a portfolio kind would move a guarantee into a folder.

2. **A share filed here is still refused by the gateway** — the same refusal, in the same layer,
   BUY and SELL, with `DRY_RUN` off and a broker object that raises on contact. The order path
   never learns which portfolio a share is filed in, and `test_the_guard_cannot_see_a_portfolio`
   asserts that by its signature rather than by argument.

3. **The bridge that made filing possible at all, and the new risk it carries.** Kite reports the
   holding as `SGBDE31III` while `instrument` stores `SGBDE31III-GB`, so every sync filed it under
   `unresolved` and the bond sat in no portfolio — found by `gates/pktea-phantom.md` P8. Tier 4 of
   `resolve_symbols` bridges the two spellings. The risk it introduces is that a *new* spelling
   could slip past a guard keyed on the old one, so both spellings are asserted refused.

Read against a real PostgreSQL, like its neighbours ``test_portfolio_write.py`` and
``test_portfolio_overview.py``: the facts under test are database facts, and the handler is called
directly with a live ``AsyncSession`` as they do.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import pathlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import pytest
import pytest_asyncio
import screener_helpers
from api_helpers import make_user
from baskfy_execution import OrderGateway, ProductGates, RiskManager, TenantIds
from baskfy_execution import guards as guards_module
from baskfy_execution.broker_ports import normalize_holding
from baskfy_execution.guards import UntouchableInstrumentError, assert_tradeable
from screener_helpers import requires_db
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from baskfy_api import portfolios as portfolios_module
from baskfy_api.auth import Principal, PrincipalKind
from baskfy_api.broker_holdings import HoldingsResult
from baskfy_api.portfolios import GSEC_SERIES_SUFFIXES, resolve_symbols
from baskfy_api.routers import brokers as brokers_module
from baskfy_api.routers.brokers import sync_holdings
from baskfy_api.routers.portfolio_overview import (
    HoldingKeyIn,
    NewPortfolioIn,
    new_portfolio,
)
from baskfy_core.allocation_ledger import PortfolioKind, PortfolioSource
from baskfy_core.models import (
    BrokerAccount,
    Exchange,
    Instrument,
    OhlcvDaily,
    Portfolio,
    PortfolioHolding,
)
from baskfy_core.portfolio_csv import MatchStatus

#: The two spellings Zerodha uses for one instrument at the same time. `HOLDINGS` is what
#: `GET /portfolio/holdings` returns and `MASTER` is what the instruments dump — and therefore the
#: `instrument` table — carries. Measured on the box, 12 Sep 2026.
BOND_AS_HOLDINGS_REPORTS: Final = "SGBDE31III"
BOND_AS_THE_MASTER_SPELLS_IT: Final = "SGBDE31III-GB"

#: The name Maulik asked for. A string, deliberately: see the module docstring.
BONDS: Final = "Bonds"

TODAY: Final = dt.date(2026, 9, 11)
NSE_EXCHANGE_ID: Final = 1

#: 26 grams at ₹9,140 a gram — a real position, so "it reconciles into net worth" is a number
#: this file computes rather than a property it asserts vaguely.
BOND_QUANTITY: Final = Decimal("26")
BOND_CLOSE: Final = Decimal("9140.00")
BOND_VALUE: Final = Decimal("237640.00")

TENANT: Final = TenantIds(user_id=1, broker_account_id=1)


# ---------------------------------------------------------------------------
# A migrated database and a rolled-back transaction, as next door
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ledger_url() -> str:
    """A schema migrated to head; nothing dropped. See ``test_portfolio_write.ledger_url``."""
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
# The fixture: the box's shape, small
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Book:
    """One user, one broker account, one bond sitting in the broker's own pile.

    This is the box's shape reduced to what matters: the bond is *held* and *unfiled*, which in
    this schema means a row in the ``is_broker_pile`` group and none anywhere else. `CUPID` is
    there as an ordinary share so every assertion about the bond can be contrasted with one about
    a stock the system is perfectly willing to trade.
    """

    user_id: int
    principal: Principal
    broker_account_id: int
    pile_id: int
    bond_id: int
    share_id: int


def _principal(user_id: int, public_id: str) -> Principal:
    return Principal(kind=PrincipalKind.USER, user_id=user_id, public_id=public_id)


async def _exchange(session: AsyncSession) -> int:
    """Get-or-create: whether NSE is present depends on whether a seeding suite ran first."""
    found = await session.scalar(select(Exchange.id).where(Exchange.code == "NSE"))
    if found is not None:
        return int(found)
    row = Exchange(id=NSE_EXCHANGE_ID, code="NSE")
    session.add(row)
    await session.flush()
    return int(row.id)


async def _instrument(
    session: AsyncSession, exchange_id: int, symbol: str, name: str, *, series: str = "EQ"
) -> int:
    """Get-or-create, because ``(exchange_id, symbol, series)`` is UNIQUE."""
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
        series=series,
        is_active=True,
    )
    session.add(row)
    await session.flush()
    return int(row.id)


async def _bar(session: AsyncSession, instrument_id: int, close: Decimal) -> None:
    """One daily bar, so the portfolio has a value rather than a hole (§4.1, §5.1)."""
    await session.execute(
        delete(OhlcvDaily).where(
            OhlcvDaily.instrument_id == instrument_id, OhlcvDaily.date == TODAY
        )
    )
    session.add(
        OhlcvDaily(
            instrument_id=instrument_id,
            date=TODAY,
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


@pytest_asyncio.fixture
async def book(session: AsyncSession) -> Book:
    """Build the ledger described in :class:`Book`."""
    exchange_id = await _exchange(session)
    user_id, public_id = await make_user(session, "bonds.owner@example.com")

    account = BrokerAccount(user_id=user_id, broker_id="zerodha", label="primary")
    session.add(account)
    await session.flush()

    bond_id = await _instrument(
        session, exchange_id, BOND_AS_THE_MASTER_SPELLS_IT, "Sovereign Gold Bond 2031-III"
    )
    share_id = await _instrument(session, exchange_id, "CUPID", "Cupid Ltd")
    await _bar(session, bond_id, BOND_CLOSE)
    await _bar(session, share_id, Decimal("130.00"))

    pile = Portfolio(
        user_id=user_id,
        name="Zerodha holdings",
        kind=PortfolioKind.CAPITAL.value,
        source=PortfolioSource.HOLDING_GROUP.value,
        started_on=TODAY,
        broker_account_id=account.id,
        is_broker_pile=True,
    )
    session.add(pile)
    await session.flush()

    for instrument_id, quantity in ((bond_id, BOND_QUANTITY), (share_id, Decimal("744"))):
        session.add(
            PortfolioHolding(
                portfolio_id=pile.id,
                instrument_id=instrument_id,
                broker_account_id=account.id,
                portfolio_kind=PortfolioKind.CAPITAL.value,
                quantity=quantity,
                avg_price=None,
                added_on=TODAY,
                first_bought_on=None,
                history_source="NONE",
            )
        )
    await session.flush()

    return Book(
        user_id=user_id,
        principal=_principal(user_id, public_id),
        broker_account_id=int(account.id),
        pile_id=int(pile.id),
        bond_id=bond_id,
        share_id=share_id,
    )


async def _filed_into(session: AsyncSession, instrument_id: int) -> dict[int, Decimal]:
    """``portfolio_id -> quantity`` for one instrument, straight out of the table.

    Read from ``portfolio_holding`` rather than from the response, because §4.1's exclusivity and
    §4.2's conservation are statements about stored rows.
    """
    rows = (
        await session.execute(
            select(PortfolioHolding.portfolio_id, PortfolioHolding.quantity).where(
                PortfolioHolding.instrument_id == instrument_id
            )
        )
    ).all()
    return {int(portfolio_id): Decimal(quantity) for portfolio_id, quantity in rows}


class ExplodingKC:
    """Anything that reaches this object is a rule that did not hold.

    The same device ``packages/execution/tests/test_non_negotiables.py`` uses, restated here
    rather than imported across a package boundary: this suite must be runnable on its own, and
    the class is four lines of statement about what must never happen.
    """

    GTT_TYPE_SINGLE = "single"
    TRANSACTION_TYPE_SELL = "SELL"
    ORDER_TYPE_LIMIT = "LIMIT"
    PRODUCT_CNC = "CNC"

    def place_order(self, **_: object) -> str:
        raise AssertionError("a refused order reached the broker")

    def place_gtt(self, **_: object) -> dict[str, int]:
        raise AssertionError("a refused GTT reached the broker")


def _gateway(tmp_path: pathlib.Path) -> OrderGateway:
    """A real gateway with **DRY_RUN off**.

    A dry run answers "it would have been simulated", which is not the answer under test. The
    claim is that the order is refused before anything can be sent at all, so the broker is an
    object that raises and the gates are live.
    """
    return OrderGateway(
        ExplodingKC(),
        RiskManager(),
        gates=lambda: ProductGates(dry_run=False),
        journal_path=str(tmp_path / "journal.jsonl"),
    )


# ---------------------------------------------------------------------------
# 1. "Bonds" is a name, not a kind
# ---------------------------------------------------------------------------


def test_bonds_is_a_name_and_the_product_grew_no_new_taxonomy() -> None:
    """The decision, pinned so that reversing it is a conversation rather than an accident.

    `PortfolioKind` answers *does this enter net worth* and `PortfolioSource` answers *who chose
    these names, and therefore which return figure is honest* (§4.1, §5.2). "Bonds" answers
    neither — it is an asset class, and this product computes a bond's value and return exactly
    the way it computes a share's. A ``BONDS`` member of either enum would be a value with no
    rule attached to it, and the first thing a later reader would do is invent one.
    """
    assert {member.value for member in PortfolioKind} == {"CAPITAL", "MONITORING"}
    assert {member.value for member in PortfolioSource} == {
        "SUBSCRIBED",
        "MY_SCREEN",
        "MY_STRATEGY",
        "HOLDING_GROUP",
    }


def test_the_guard_cannot_see_a_portfolio() -> None:
    """Untradeability is a fact about the instrument, and the guard has no way to be told
    otherwise — it takes a symbol and a series and nothing else.

    This is the reason "Bonds" must not become a kind. A portfolio can be renamed, emptied or
    deleted by the person who owns it; if the refusal depended on where a share was filed, then
    deleting a folder would unlock an instrument. Asserted against the signature, because that is
    the level at which the property is guaranteed rather than merely observed.
    """
    parameters = list(inspect.signature(assert_tradeable).parameters)

    assert parameters == ["symbol", "series"]
    source = pathlib.Path(guards_module.__file__).read_text(encoding="utf-8")
    assert "portfolio" not in source.lower()


# ---------------------------------------------------------------------------
# 2. The bridge — and the spelling that must not slip past the guard
# ---------------------------------------------------------------------------


@requires_db
@pytest.mark.asyncio
async def test_the_symbol_the_broker_reports_resolves_to_the_instrument_we_store(
    session: AsyncSession, book: Book
) -> None:
    """The defect, stated as the fix. Kite's holdings endpoint says ``SGBDE31III``; the
    instrument master says ``SGBDE31III-GB``; resolution was exact-match, so every sync since the
    account was connected filed the bond under ``unresolved`` and it ended up in no portfolio at
    all — not even the broker's own pile, which exists precisely to catch what is unfiled.

    The resolved candidate carries the instrument's **own** spelling, so nothing downstream has to
    learn about the quirk: the sync gets an ``instrument_id`` and the page shows the real symbol.
    """
    resolved = await resolve_symbols(session, [BOND_AS_HOLDINGS_REPORTS])
    resolution = resolved[BOND_AS_HOLDINGS_REPORTS]

    assert resolution.status is MatchStatus.MATCHED
    assert resolution.instrument_id == book.bond_id
    assert resolution.candidates[0].symbol == BOND_AS_THE_MASTER_SPELLS_IT
    # Not an alias: nothing was renamed. Both spellings are current, and saying "this used to be
    # called that" would be a false statement in the one field that reports provenance.
    assert resolution.matched_via_alias is False


@requires_db
@pytest.mark.asyncio
async def test_a_symbol_that_stands_on_its_own_is_never_shadowed_by_a_suffixed_one(
    session: AsyncSession, book: Book
) -> None:
    """The tier runs **last**, so bridging can only ever answer a question nothing else could.

    The hazard it is written against is a bare symbol that already names a real company while a
    ``-GB`` row happens to exist with the same stem: the company must win, every time, or the
    bridge becomes a machine for filing one firm's shares under another's name.
    """
    exchange_id = await _exchange(session)
    await _instrument(session, exchange_id, "CUPID-GB", "A decoy that must never be reached")

    resolved = await resolve_symbols(session, ["CUPID"])

    assert resolved["CUPID"].instrument_id == book.share_id
    assert resolved["CUPID"].candidates[0].symbol == "CUPID"


@requires_db
@pytest.mark.asyncio
async def test_only_the_government_paper_suffixes_are_bridged(
    session: AsyncSession, book: Book
) -> None:
    """``-BE`` marks a *different listing of a real company*, not a different spelling of one
    instrument, and 7,238 of the box's 11,215 symbols carry a dash. Bridging that shape would be
    guessing with a user's shares, so the rule is restricted to the two series
    `baskfy_execution.guards.UNTOUCHABLE_SERIES` names — asserted here as the closed pair it is.
    """
    assert set(GSEC_SERIES_SUFFIXES) == {"-GB", "-GS"}

    exchange_id = await _exchange(session)
    await _instrument(session, exchange_id, "SOMECO-BE", "Some Co, trade-to-trade listing")

    resolved = await resolve_symbols(session, ["SOMECO"])

    assert resolved["SOMECO"].status is MatchStatus.UNMATCHED


@requires_db
@pytest.mark.asyncio
async def test_two_suffixed_forms_are_ambiguous_rather_than_guessed_at(
    session: AsyncSession, book: Book
) -> None:
    """A tier that returns several candidates stops there. The bridge inherits that rule instead
    of picking the first row, because picking would put shares the user never chose into a
    portfolio and nothing on the page would say so.
    """
    exchange_id = await _exchange(session)
    await _instrument(session, exchange_id, "TWOWAYS-GB", "Gold bond spelling")
    await _instrument(session, exchange_id, "TWOWAYS-GS", "G-sec spelling")

    resolved = await resolve_symbols(session, ["TWOWAYS"])

    assert resolved["TWOWAYS"].status is MatchStatus.AMBIGUOUS
    assert resolved["TWOWAYS"].instrument_id is None
    assert len(resolved["TWOWAYS"].candidates) == 2


# ---------------------------------------------------------------------------
# 3. The audited path files it, and the arithmetic holds
# ---------------------------------------------------------------------------


@requires_db
@pytest.mark.asyncio
async def test_the_bond_is_filed_into_a_capital_portfolio_named_bonds(
    session: AsyncSession, book: Book
) -> None:
    """Maulik's request, through the route §6.7 already had. No new write path exists for this.

    Conservation is the assertion that matters: the shares do not multiply. They leave the
    broker's pile and appear in Bonds, once, in the quantity the broker reports — which is what
    makes the group's value part of net worth rather than a second copy of it.
    """
    body = NewPortfolioIn(
        name=BONDS,
        kind=PortfolioKind.CAPITAL,
        source=PortfolioSource.HOLDING_GROUP,
        holdings=[
            HoldingKeyIn(instrument_id=book.bond_id, broker_account_id=book.broker_account_id)
        ],
    )
    detail = await new_portfolio(body, session, book.principal)

    assert detail.summary.name == BONDS
    assert detail.summary.kind is PortfolioKind.CAPITAL
    assert detail.summary.source is PortfolioSource.HOLDING_GROUP
    # §4.1: a capital portfolio sums into consolidated net worth. This is the whole reason it is
    # not a monitoring view — the bond is money, and money that is excluded from the total is
    # money the product has lost track of.
    assert detail.summary.counts_toward_total is True
    assert detail.summary.value == BOND_VALUE

    new_id = detail.summary.portfolio_id
    filed = await _filed_into(session, book.bond_id)
    assert filed == {new_id: BOND_QUANTITY}
    assert sum(filed.values()) == BOND_QUANTITY
    # The pile keeps what was not named: filing one holding is not a reshuffle of the account.
    assert await _filed_into(session, book.share_id) == {book.pile_id: Decimal("744")}


@requires_db
@pytest.mark.asyncio
async def test_filing_the_bond_names_no_side_product_or_venue(
    session: AsyncSession, book: Book
) -> None:
    """A bond arriving in a portfolio is bookkeeping. The response that describes it must not
    contain the vocabulary of an order, because the day it does is the day somebody wires the
    page's confirm button to something that means it.
    """
    body = NewPortfolioIn(
        name=BONDS,
        kind=PortfolioKind.CAPITAL,
        source=PortfolioSource.HOLDING_GROUP,
        holdings=[
            HoldingKeyIn(instrument_id=book.bond_id, broker_account_id=book.broker_account_id)
        ],
    )
    detail = await new_portfolio(body, session, book.principal)

    rendered = detail.model_dump_json().lower()
    for forbidden in ("place_order", "transaction_type", '"buy"', '"sell"', "gtt"):
        assert forbidden not in rendered, forbidden


# ---------------------------------------------------------------------------
# 4. It is still untouchable, and being in Bonds changed nothing
# ---------------------------------------------------------------------------


@requires_db
@pytest.mark.asyncio
async def test_a_holding_filed_into_bonds_is_still_refused_by_the_gateway(
    session: AsyncSession, book: Book, tmp_path: pathlib.Path
) -> None:
    """Non-negotiable #6, asserted *after* the bond has a home. This is the delicate half of the
    request: a portfolio named for bonds must never be a route to an order.

    BUY and SELL both, because a guard that blocks only buys traps a position it cannot close and
    is worse than the risk it was written for. `DRY_RUN` is off and the broker raises on contact,
    so "refused" here means refused before any network call — not simulated.
    """
    body = NewPortfolioIn(
        name=BONDS,
        kind=PortfolioKind.CAPITAL,
        source=PortfolioSource.HOLDING_GROUP,
        holdings=[
            HoldingKeyIn(instrument_id=book.bond_id, broker_account_id=book.broker_account_id)
        ],
    )
    detail = await new_portfolio(body, session, book.principal)
    assert detail.summary.value == BOND_VALUE  # it really is filed, and really is worth money

    gateway = _gateway(tmp_path)
    for side in ("BUY", "SELL"):
        with pytest.raises(UntouchableInstrumentError):
            await gateway.place(
                symbol=BOND_AS_THE_MASTER_SPELLS_IT,
                qty=1,
                side=side,
                price=float(BOND_CLOSE),
                tenant=TENANT,
                plan_tenant=TENANT,
                gross_exposure=0.0,
            )


def test_both_of_the_brokers_spellings_are_refused() -> None:
    """The risk tier 4 introduces, closed.

    Bridging two names for one instrument means a name the guard has never been handed can now
    reach the ledger. `SGBDE31III` and `SGBDE31III-GB` must both be untradeable, and they are —
    by the ``SGB`` **prefix**, not by the exact-match set, which is the lesson `baskfy_core.basket`
    records in its own comment: the set held one spelling while the holding wore the other, and a
    planner proposed selling ₹60 lakh of it.
    """
    for spelling in (BOND_AS_HOLDINGS_REPORTS, BOND_AS_THE_MASTER_SPELLS_IT):
        with pytest.raises(UntouchableInstrumentError):
            assert_tradeable(spelling)

    # And the series answer too, for the G-secs the same bridge now resolves.
    with pytest.raises(UntouchableInstrumentError):
        assert_tradeable("ANYTHING", "GS")


def test_an_ordinary_share_is_not_caught_by_any_of_this() -> None:
    """The control. A guard that refused everything would pass every test above and stop the
    product working, so the negative case is asserted in the same file as the positives.
    """
    assert_tradeable("CUPID")

    gateway = OrderGateway(
        ExplodingKC(),
        RiskManager(),
        gates=lambda: ProductGates(dry_run=True),
        journal_path=str(pathlib.Path(__file__).parent / "__pycache__" / "bonds-journal.jsonl"),
    )
    result = asyncio.run(
        gateway.place(
            symbol="CUPID",
            qty=1,
            side="BUY",
            price=130.0,
            tenant=TENANT,
            plan_tenant=TENANT,
            gross_exposure=0.0,
        )
    )
    assert result["status"] != "BLOCKED"


# ---------------------------------------------------------------------------
# 5. The rehearsal — the exact two calls the box command makes, in order
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def committable(ledger_url: str) -> AsyncIterator[AsyncSession]:
    """A session whose ``commit()`` is real to the code under test and undone by the test.

    ``sync_holdings`` commits — it is a route, and a route that returned "written" without
    committing would be lying to the caller. ``join_transaction_mode="create_savepoint"`` lets
    that commit land as a savepoint release inside the test's own transaction, so the handler
    runs exactly as deployed and the database is still left untouched.
    """
    engine = create_async_engine(ledger_url)
    connection = await engine.connect()
    transaction = await connection.begin()
    made = AsyncSession(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield made
    finally:
        await made.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


@requires_db
@pytest.mark.asyncio
async def test_the_box_command_files_the_bond_end_to_end(
    committable: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ops/bonds-portfolio/file-the-bond.py``, rehearsed against a real database.

    This is the test that makes the delivered command believable. It runs the two handlers the
    script runs, in the script's order, against a broker read shaped like the box's — the bond
    under the spelling **Kite actually returns**, which is the whole reason the bond was homeless:

    1. ``sync_holdings`` resolves ``SGBDE31III`` through tier 4 and writes it into the pile.
       ``unresolved`` must come back **empty**; before the bridge that list is exactly where this
       holding went, every time, silently.
    2. ``new_portfolio`` files it into Bonds, and the shares are conserved.

    The fixture ledger is built here rather than reused from ``book`` because this test needs a
    session that can commit, and building it twice is cheaper than making every other test carry
    a savepoint it does not need.
    """
    session = committable
    exchange_id = await _exchange(session)
    user_id, public_id = await make_user(session, "bonds.rehearsal@example.com")
    principal = _principal(user_id, public_id)
    account = BrokerAccount(user_id=user_id, broker_id="zerodha", label="primary")
    session.add(account)
    await session.flush()

    bond_id = await _instrument(
        session, exchange_id, BOND_AS_THE_MASTER_SPELLS_IT, "Sovereign Gold Bond 2031-III"
    )
    await _bar(session, bond_id, BOND_CLOSE)

    live = HoldingsResult.live(
        [
            normalize_holding(
                symbol=BOND_AS_HOLDINGS_REPORTS,
                quantity=BOND_QUANTITY,
                average_price=Decimal("6200"),
            )
        ]
    )
    monkeypatch.setattr(brokers_module, "holdings_for_broker", lambda _broker_id: live)

    synced = await sync_holdings(principal, session, "zerodha")

    assert synced.source == "live"
    assert synced.persisted is True
    # The line that used to say "not judged (unresolved symbol): SGBDE31III" and cost the bond
    # its place in the ledger.
    assert synced.unresolved == []
    pile_id = synced.portfolio_id
    assert pile_id is not None
    assert await _filed_into(session, bond_id) == {int(pile_id): BOND_QUANTITY}

    detail = await new_portfolio(
        NewPortfolioIn(
            name=BONDS,
            kind=PortfolioKind.CAPITAL,
            source=PortfolioSource.HOLDING_GROUP,
            holdings=[HoldingKeyIn(instrument_id=bond_id, broker_account_id=int(account.id))],
        ),
        session,
        principal,
    )

    assert detail.summary.name == BONDS
    assert detail.summary.counts_toward_total is True
    assert detail.summary.value == BOND_VALUE
    # Out of the pile, into Bonds, once — the number the broker reported and no other.
    assert await _filed_into(session, bond_id) == {int(detail.summary.portfolio_id): BOND_QUANTITY}


@requires_db
@pytest.mark.asyncio
async def test_without_the_bridge_the_sync_reports_the_bond_as_unresolved(
    committable: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect itself, reproduced on demand, so the fix cannot quietly stop mattering.

    With tier 4 switched off — the suffix list emptied, nothing else changed — the same live read
    of the same account files **nothing**, and names the bond in ``unresolved``. That is the state
    the box was in from the day the account was connected until 12 Sep 2026: a real position,
    reported by the broker every single sync, in no portfolio at all.
    """
    monkeypatch.setattr(portfolios_module, "GSEC_SERIES_SUFFIXES", ())

    session = committable
    exchange_id = await _exchange(session)
    user_id, public_id = await make_user(session, "bonds.regression@example.com")
    principal = _principal(user_id, public_id)
    session.add(BrokerAccount(user_id=user_id, broker_id="zerodha", label="primary"))
    bond_id = await _instrument(
        session, exchange_id, BOND_AS_THE_MASTER_SPELLS_IT, "Sovereign Gold Bond 2031-III"
    )
    await session.flush()

    live = HoldingsResult.live(
        [
            normalize_holding(
                symbol=BOND_AS_HOLDINGS_REPORTS,
                quantity=BOND_QUANTITY,
                average_price=Decimal("6200"),
            )
        ]
    )
    monkeypatch.setattr(brokers_module, "holdings_for_broker", lambda _broker_id: live)

    synced = await sync_holdings(principal, session, "zerodha")

    assert synced.unresolved == [BOND_AS_HOLDINGS_REPORTS]
    assert synced.written == 0
    assert await _filed_into(session, bond_id) == {}
