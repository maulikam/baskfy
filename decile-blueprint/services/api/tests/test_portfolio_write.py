"""``GET /portfolio/suggestions`` and ``POST /portfolio`` — ``PORTFOLIO_REDESIGN.md`` §6.6 / §6.7.

These are the two halves of activation, and the spec says so in as many words:

    "First-run experience: connect broker → everything lands in Unallocated → the product actively
    helps sort it... **Getting from 40 unallocated holdings to 4 named portfolios IS activation.**"

The suggestions route is the "helps sort it" half and the create route is the "4 named portfolios"
half. Everything below is read against a **real** PostgreSQL, for the same reason
``test_portfolio_overview.py`` is: the facts under test are database facts.

* Acceptance criterion 2 — *a holding can never be in two capital portfolios* — was **reversed on
  10 Sep 2026** (`PORTFOLIO_REDESIGN.md` §11.2a), and migration ``0035`` dropped the partial
  unique index that enforced it. What is asserted now is the invariant that replaced it: the
  slices of a holding never sum past the holding, refused in words with the numbers attached.
  Monitoring views still overlap freely, and always did.
* §4.2's whole-holding rule became **a quantity per holding** in the same change. It is asserted
  against the request schema itself, not against a handler's behaviour: that the field exists,
  that omitting it means "all of it", and that zero and negative are refused by the schema rather
  than left for the route to notice.
* Tenancy is a predicate that has to actually filter rows. A foreign broker account, a foreign
  holding and an unknown benchmark all answer ``NOT_FOUND`` and never ``FORBIDDEN`` — a 403 would
  confirm the id names a real row, which is the fact a stranger is probing for.
* The ranking is the pure module's, and the test proves it by *computing the expected order from
  the values* rather than by hard-coding a list: the three bases produce groups worth ₹1,85,000,
  ₹1,55,000 and ₹1,50,000, and ``rank_suggestions`` puts the biggest decision first.

The handlers are called directly with a live ``AsyncSession``, the arrangement
``test_portfolio_overview.py`` uses — it keeps the suite off Redis while exercising the real query
path, the real problem responses and the real foreign keys.

WHY A SEPARATE MODULE
---------------------
``test_portfolio_overview.py`` builds one fixture ledger and asserts the read payloads over it. A
write suite needs a *different* ledger — one with a pile big enough to suggest three ways of
grouping it and a holding already spoken for — and adding a second fixture to that module would
have made every read assertion there depend on rows it does not care about.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
import pytest_asyncio
import screener_helpers
from api_helpers import make_user
from pydantic import ValidationError
from screener_helpers import requires_db
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from baskfy_api.auth import Principal, PrincipalKind
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.routers.portfolio_overview import (
    _NO_INPUTS,
    _NOTHING_TO_SORT,
    HoldingKeyIn,
    NewPortfolioIn,
    new_portfolio,
    portfolio_suggestions,
)
from baskfy_core.allocation_ledger import PortfolioKind, PortfolioSource
from baskfy_core.grouping_suggestions import SuggestionBasis
from baskfy_core.models import (
    BrokerAccount,
    CbBasket,
    CbBasketVersion,
    CbConstituent,
    CbInvestment,
    CbManager,
    Exchange,
    IndexDef,
    IndexMemberDaily,
    Instrument,
    OhlcvDaily,
    Portfolio,
    PortfolioHolding,
)

#: The day this fixture's prices are the close of.
TODAY: Final = dt.date(2026, 8, 18)
YESTERDAY: Final = dt.date(2026, 8, 17)

#: ``index_def.id`` is a ``SmallIntPk`` assigned by hand, never by a sequence — see
#: ``test_portfolio_overview.py`` for why. Well clear of the 1..31 universe range and of the
#: dashboard ids from 100 up, so neither can be mistaken for the other.
IT_INDEX_ID: Final = 910
ENERGY_INDEX_ID: Final = 911
NSE_EXCHANGE_ID: Final = 1

#: The three groups the fixture is built to produce, valued by hand so the assertions below check
#: arithmetic rather than re-derive it::
#:
#:     INFY      100 @ 1200 = 1,20,000
#:     TCS        10 @ 3500 =   35,000
#:     RELIANCE   20 @ 1500 =   30,000
#:
#:     basket overlap  INFY + TCS + RELIANCE = 1,85,000   (3 of the model's 4 stocks)
#:     sector "IT"     INFY + TCS            = 1,55,000
#:     purchase era    INFY + RELIANCE       = 1,50,000
OVERLAP_VALUE: Final = Decimal("185000.00")
SECTOR_VALUE: Final = Decimal("155000.00")
ERA_VALUE: Final = Decimal("150000.00")

MODULE: Final = (
    Path(__file__).resolve().parents[1] / "src" / "baskfy_api" / "routers" / "portfolio_overview.py"
)


# ---------------------------------------------------------------------------
# A migrated database and a rolled-back transaction, as next door
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ledger_url() -> str:
    """A schema migrated to head; nothing dropped. See ``test_portfolio_overview.ledger_url``."""
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
# The fixture pile
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pile:
    """One user with an unsorted pile, one with nothing to sort, one with no inputs at all.

    ``owner`` holds INFY, TCS and RELIANCE in a monitoring view and in **no** capital portfolio —
    which is what unallocated looks like in this schema (§4.1: a lens is not an allocation) — plus
    HDFCBANK inside the capital portfolio ``Core``.

    ``settled`` holds one stock, in a capital portfolio, and nothing else: the "everything is
    already filed" case, which must answer with a reason rather than with an empty list.

    ``bare`` holds two stocks in a lens with no sector, no purchase date and no model behind
    them: the "we have none of the three inputs" case.
    """

    owner_id: int
    owner: Principal
    settled_id: int
    settled: Principal
    bare_id: int
    bare: Principal
    zerodha: int
    upstox: int
    their_broker: int
    bare_broker: int
    infy: int
    tcs: int
    reliance: int
    hdfc: int
    wipro: int
    airtel: int
    core: int
    watch: int
    their_portfolio: int
    basket_id: int
    benchmark_id: int


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


async def _instrument(session: AsyncSession, exchange_id: int, symbol: str, name: str) -> int:
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
        series="EQ",
        is_active=True,
    )
    session.add(row)
    await session.flush()
    return int(row.id)


async def _bar(session: AsyncSession, instrument_id: int, on: dt.date, close: Decimal) -> None:
    """One daily bar. ``close_raw`` is what a portfolio page shows (house rule 6)."""
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
    user_id: int
    name: str
    kind: PortfolioKind
    source: PortfolioSource
    started_on: dt.date = dt.date(2026, 1, 1)


async def _portfolio(session: AsyncSession, spec: _PortfolioSpec) -> int:
    row = Portfolio(
        user_id=spec.user_id,
        name=spec.name,
        kind=spec.kind.value,
        source=spec.source.value,
        started_on=spec.started_on,
    )
    session.add(row)
    await session.flush()
    return int(row.id)


@dataclass(frozen=True, slots=True)
class _HoldSpec:
    portfolio_id: int
    kind: PortfolioKind
    instrument_id: int
    broker_account_id: int
    quantity: Decimal
    avg_price: Decimal | None = None
    first_bought_on: dt.date | None = None


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
            first_bought_on=spec.first_bought_on,
            history_source="BROKER" if spec.first_bought_on is not None else "NONE",
        )
    )


async def _sector_index(
    session: AsyncSession, index_id: int, slug: str, name: str, members: list[int]
) -> None:
    """A sector index and its point-in-time membership — the only sector source this schema has.

    ``is_universe=False`` is what makes it a sector rather than a selectable universe, and it is
    the predicate ``_sector_map`` reads. Two membership dates are written so the handler's
    "most recent row wins" ordering is exercised rather than assumed.
    """
    session.add(IndexDef(id=index_id, slug=slug, name=name, is_universe=False, sort_order=0))
    await session.flush()
    for on in (YESTERDAY, TODAY):
        for instrument_id in members:
            session.add(
                IndexMemberDaily(
                    index_id=index_id, date=on, instrument_id=instrument_id, source="nse_file"
                )
            )


@pytest_asyncio.fixture
async def pile(session: AsyncSession) -> Pile:
    """Build the ledger described in :class:`Pile`."""
    exchange_id = await _exchange(session)

    owner_id, owner_public = await make_user(session, "pile.owner@example.com")
    settled_id, settled_public = await make_user(session, "pile.settled@example.com")
    bare_id, bare_public = await make_user(session, "pile.bare@example.com")

    zerodha = BrokerAccount(user_id=owner_id, broker_id="zerodha", label="primary")
    upstox = BrokerAccount(user_id=owner_id, broker_id="upstox", label="second")
    theirs = BrokerAccount(user_id=settled_id, broker_id="zerodha", label="primary")
    bare_broker = BrokerAccount(user_id=bare_id, broker_id="zerodha", label="primary")
    session.add_all([zerodha, upstox, theirs, bare_broker])
    await session.flush()

    infy = await _instrument(session, exchange_id, "INFY", "Infosys")
    tcs = await _instrument(session, exchange_id, "TCS", "Tata Consultancy Services")
    reliance = await _instrument(session, exchange_id, "RELIANCE", "Reliance Industries")
    hdfc = await _instrument(session, exchange_id, "HDFCBANK", "HDFC Bank")
    wipro = await _instrument(session, exchange_id, "WIPRO", "Wipro")
    airtel = await _instrument(session, exchange_id, "BHARTIARTL", "Bharti Airtel")

    for instrument_id, close in (
        (infy, Decimal("1200")),
        (tcs, Decimal("3500")),
        (reliance, Decimal("1500")),
        (hdfc, Decimal("1600")),
        (wipro, Decimal("500")),
        (airtel, Decimal("1800")),
    ):
        await _bar(session, instrument_id, YESTERDAY, close)
        await _bar(session, instrument_id, TODAY, close)

    # INFY and TCS share a sector; RELIANCE has one of its own, which is why it produces no sector
    # suggestion — a group of one is a rename, not a grouping.
    await _sector_index(session, IT_INDEX_ID, "pile-suite-it", "Nifty IT", [infy, tcs])
    await _sector_index(session, ENERGY_INDEX_ID, "pile-suite-energy", "Nifty Energy", [reliance])

    benchmark = IndexDef(
        id=IT_INDEX_ID + 5,
        slug="pile-suite-benchmark",
        name="Nifty 500",
        is_universe=False,
        sort_order=0,
    )
    session.add(benchmark)
    await session.flush()

    core = await _portfolio(
        session,
        _PortfolioSpec(
            user_id=owner_id,
            name="Core",
            kind=PortfolioKind.CAPITAL,
            source=PortfolioSource.MY_SCREEN,
        ),
    )
    watch = await _portfolio(
        session,
        _PortfolioSpec(
            user_id=owner_id,
            name="Things I am watching",
            kind=PortfolioKind.MONITORING,
            source=PortfolioSource.HOLDING_GROUP,
        ),
    )
    their_portfolio = await _portfolio(
        session,
        _PortfolioSpec(
            user_id=settled_id,
            name="All of it",
            kind=PortfolioKind.CAPITAL,
            source=PortfolioSource.HOLDING_GROUP,
        ),
    )
    bare_view = await _portfolio(
        session,
        _PortfolioSpec(
            user_id=bare_id,
            name="Everything",
            kind=PortfolioKind.MONITORING,
            source=PortfolioSource.HOLDING_GROUP,
        ),
    )

    # HDFCBANK is spoken for: it is in a capital portfolio, which is what makes it the holding a
    # second capital portfolio must be refused.
    _hold(
        session,
        _HoldSpec(
            core, PortfolioKind.CAPITAL, hdfc, int(zerodha.id), Decimal("50"), Decimal("1500")
        ),
    )
    # The pile. In a lens and in no capital portfolio, so every one of these is unallocated.
    # INFY and RELIANCE were bought in the same financial year; TCS's date is unknown.
    _hold(
        session,
        _HoldSpec(
            watch,
            PortfolioKind.MONITORING,
            infy,
            int(zerodha.id),
            Decimal("100"),
            Decimal("1000"),
            dt.date(2025, 6, 10),
        ),
    )
    _hold(
        session,
        _HoldSpec(
            watch, PortfolioKind.MONITORING, tcs, int(zerodha.id), Decimal("10"), Decimal("3000")
        ),
    )
    _hold(
        session,
        _HoldSpec(
            watch,
            PortfolioKind.MONITORING,
            reliance,
            int(upstox.id),
            Decimal("20"),
            Decimal("1400"),
            dt.date(2025, 9, 3),
        ),
    )
    _hold(
        session,
        _HoldSpec(
            their_portfolio,
            PortfolioKind.CAPITAL,
            hdfc,
            int(theirs.id),
            Decimal("999"),
            Decimal("1500"),
        ),
    )
    for instrument_id in (wipro, airtel):
        _hold(
            session,
            _HoldSpec(
                bare_view,
                PortfolioKind.MONITORING,
                instrument_id,
                int(bare_broker.id),
                Decimal("5"),
            ),
        )

    manager = CbManager(slug="pile-suite-manager", name="Baskfy Research", kind="ENGINE")
    session.add(manager)
    await session.flush()
    basket = CbBasket(
        slug="pile-suite-basket",
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
    version = CbBasketVersion(
        basket_id=basket.id, version_no=1, effective_date=dt.date(2026, 1, 1), label="GENESIS"
    )
    session.add(version)
    await session.flush()
    for instrument_id in (infy, tcs, reliance, hdfc):
        session.add(
            CbConstituent(
                version_id=version.id,
                instrument_id=instrument_id,
                segment="EQ",
                weight=Decimal("0.25"),
            )
        )
    # An ACTIVE investment is what makes this a model the owner *follows*. A watchlist entry would
    # not do: `suggest_by_basket_overlap` writes "You subscribe to ...", and a bookmark is not a
    # subscription.
    session.add(
        CbInvestment(
            user_id=owner_id,
            broker_account_id=int(zerodha.id),
            basket_id=basket.id,
            status="ACTIVE",
            version_applied_id=version.id,
        )
    )
    await session.flush()

    return Pile(
        owner_id=owner_id,
        owner=_principal(owner_id, owner_public),
        settled_id=settled_id,
        settled=_principal(settled_id, settled_public),
        bare_id=bare_id,
        bare=_principal(bare_id, bare_public),
        zerodha=int(zerodha.id),
        upstox=int(upstox.id),
        their_broker=int(theirs.id),
        bare_broker=int(bare_broker.id),
        infy=infy,
        tcs=tcs,
        reliance=reliance,
        hdfc=hdfc,
        wipro=wipro,
        airtel=airtel,
        core=core,
        watch=watch,
        their_portfolio=their_portfolio,
        basket_id=int(basket.id),
        benchmark_id=int(benchmark.id),
    )


async def _capital_row(
    session: AsyncSession, instrument_id: int, broker_account_id: int
) -> int | None:
    """Which capital portfolio a physical holding sits in, straight from the table.

    Read from ``portfolio_holding`` rather than from a payload on purpose: criterion 2 is a
    statement about stored rows, and a handler that returned the right JSON while writing the
    wrong row would pass every assertion made against its own output.
    """
    found: int | None = await session.scalar(
        select(PortfolioHolding.portfolio_id).where(
            PortfolioHolding.instrument_id == instrument_id,
            PortfolioHolding.broker_account_id == broker_account_id,
            PortfolioHolding.portfolio_kind == PortfolioKind.CAPITAL.value,
        )
    )
    return found


# ---------------------------------------------------------------------------
# §6.6 — suggestions
# ---------------------------------------------------------------------------


@requires_db
@pytest.mark.asyncio
async def test_suggestions_come_back_ranked_the_way_the_pure_module_ranks_them(
    session: AsyncSession, pile: Pile
) -> None:
    """All three of §6.6's bases fire, and the biggest decision is offered first.

    The order is value-descending because that is what
    ``grouping_suggestions._rank_key`` says: the primary question this screen answers is "what is
    the biggest single decision available to me now", and the confidence tier between bases only
    breaks ties. So the basket overlap leading here is *not* because overlap outranks sector —
    it is because ₹1,85,000 outranks ₹1,55,000.
    """
    out = await portfolio_suggestions(session, pile.owner)

    assert out.unavailable_reason is None
    assert out.unallocated_count == 3
    assert out.unallocated_value == OVERLAP_VALUE
    assert [(s.basis, s.value) for s in out.suggestions] == [
        (SuggestionBasis.BASKET_OVERLAP, OVERLAP_VALUE),
        (SuggestionBasis.SECTOR, SECTOR_VALUE),
        (SuggestionBasis.PURCHASE_ERA, ERA_VALUE),
    ]
    assert [s.value for s in out.suggestions] == sorted(
        (s.value for s in out.suggestions), reverse=True
    )


@requires_db
@pytest.mark.asyncio
async def test_the_basket_overlap_carries_its_coverage_and_what_is_missing(
    session: AsyncSession, pile: Pile
) -> None:
    """§6.7's drawer needs both: how much of the model is here, and what would still be short.

    Coverage is constituents matched over constituents in the model — three of four — and never
    over holdings, because a second broker account does not make a user hold more of a model.
    """
    out = await portfolio_suggestions(session, pile.owner)
    overlap = next(s for s in out.suggestions if s.basis is SuggestionBasis.BASKET_OVERLAP)

    assert overlap.basket_id == pile.basket_id
    assert overlap.basket_coverage == Decimal("0.7500")
    assert overlap.missing_instrument_ids == [pile.hdfc]
    assert overlap.proposed_name == "Momentum 20"
    assert "Momentum 20" in overlap.rationale
    assert overlap.suggested_kind is PortfolioKind.CAPITAL


@requires_db
@pytest.mark.asyncio
async def test_the_sector_map_is_served_for_the_picker_and_names_the_index_it_came_from(
    session: AsyncSession, pile: Pile
) -> None:
    """§6.7's left panel filters by sector, so the map travels with the suggestions.

    Keys are strings because that is what a JSON object key is, and the client
    (``organize.ts::filterHoldings``) looks them up as ``sectors[String(instrument_id)]``.
    """
    out = await portfolio_suggestions(session, pile.owner)

    assert out.sectors[str(pile.infy)] == "Nifty IT"
    assert out.sectors[str(pile.tcs)] == "Nifty IT"
    assert out.sectors[str(pile.reliance)] == "Nifty Energy"
    sector = next(s for s in out.suggestions if s.basis is SuggestionBasis.SECTOR)
    assert sector.proposed_name == "Nifty IT"
    assert {key.instrument_id for key in sector.keys} == {pile.infy, pile.tcs}


@requires_db
@pytest.mark.asyncio
async def test_a_user_with_nothing_unallocated_gets_no_suggestions_and_a_stated_reason(
    session: AsyncSession, pile: Pile
) -> None:
    """§6.6's screen is only shown "when anything is unallocated" — so say that, do not imply it.

    An empty list with no sentence beside it reads as "we looked at your holdings and found
    nothing worth grouping", which is a verdict. The truth here is the opposite and much better
    news: everything is already filed.
    """
    out = await portfolio_suggestions(session, pile.settled)

    assert out.suggestions == []
    assert out.unallocated_count == 0
    assert out.unavailable_reason == _NOTHING_TO_SORT
    assert "already in a portfolio" in out.unavailable_reason


@requires_db
@pytest.mark.asyncio
async def test_a_missing_input_is_named_rather_than_shown_as_an_empty_list(
    session: AsyncSession, pile: Pile
) -> None:
    """Fewer suggestions rather than worse ones — and the payload says which input was missing.

    This user has an unsorted pile and none of the three inputs: their stocks are in no sector
    index, their purchase dates are unknown and they follow no model. Every basis reports itself
    unavailable with a sentence naming *the input*, never the user's holdings.
    """
    out = await portfolio_suggestions(session, pile.bare)

    assert out.suggestions == []
    assert out.unallocated_count == 2
    assert out.unavailable_reason == _NO_INPUTS
    assert {status.basis for status in out.bases} == set(SuggestionBasis)
    assert all(status.available is False for status in out.bases)
    for status in out.bases:
        assert status.unavailable_reason is not None
        assert status.considered == 0
    reasons = " ".join(status.unavailable_reason or "" for status in out.bases)
    assert "sector" in reasons
    assert "bought" in reasons
    assert "model" in reasons


@requires_db
@pytest.mark.asyncio
async def test_suggestions_never_reach_across_tenants(session: AsyncSession, pile: Pile) -> None:
    """The pile is the caller's own. Another user's holdings are not in it, at all.

    Not filtered out of a result — never in one. Every statement behind ``_load_ledger`` starts
    from ``portfolio.user_id`` or ``broker_account.user_id``, which is the difference between a
    predicate that can be forgotten in one branch and a shape with nowhere to put a foreign row.
    """
    out = await portfolio_suggestions(session, pile.settled)
    named = {key.instrument_id for suggestion in out.suggestions for key in suggestion.keys}

    assert named == set()
    assert str(pile.infy) not in out.sectors


# ---------------------------------------------------------------------------
# §4.2 — the request schema has no quantity, and cannot grow one by accident
# ---------------------------------------------------------------------------


def _property_names(schema: object) -> set[str]:
    """Every property name anywhere in a JSON Schema, including nested definitions."""
    found: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                found |= {str(name) for name in value}
            found |= _property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            found |= _property_names(item)
    return found


def test_the_create_body_takes_a_quantity_per_holding() -> None:
    """The reversal of §4.2, asserted against the schema (0035 / PF2, 10 Sep 2026).

    This test used to assert the opposite, at length: that there was no quantity field and there
    must not be one, because "v1 allocates a holding whole" and partial allocation "arrives, if it
    arrives, as a migration and a new field". This is that migration and that field. Maulik:
    *"one stock can appear in multiple portfolios, so if stock a bought 100 qty for shortterm 20
    for long term 34 for some swing 36 for momentum"*.

    It lives on the holding entry and nowhere else: a quantity on the *body* would be a portfolio
    with one number for every holding in it, which is not a thing.
    """
    assert "quantity" in HoldingKeyIn.model_fields
    assert "quantity" not in NewPortfolioIn.model_fields


def test_a_quantity_is_optional_and_omitting_it_means_all_of_it() -> None:
    """The common case — "file this whole holding into Long term" — stays the shortest to write,
    and every client written before 0035 keeps working unchanged.

    ``None`` is resolved against the ledger by the route, not defaulted here, because the number
    it means is the position's unallocated remainder and a schema cannot see that.
    """
    whole = HoldingKeyIn.model_validate({"instrument_id": 1, "broker_account_id": 1})
    part = HoldingKeyIn.model_validate(
        {"instrument_id": 1, "broker_account_id": 1, "quantity": "20"}
    )

    assert whole.quantity is None
    assert part.quantity == Decimal("20")


def test_a_quantity_that_is_not_a_quantity_is_refused_by_the_schema() -> None:
    """Zero and negative are refused here rather than in the handler: neither is a slice, and a
    schema that accepted them would make the route responsible for a shape it never needs."""
    for bad in ("0", "-20"):
        with pytest.raises(ValidationError):
            HoldingKeyIn.model_validate(
                {"instrument_id": 1, "broker_account_id": 1, "quantity": bad}
            )


def test_an_unknown_key_is_still_refused_rather_than_ignored() -> None:
    """``extra="forbid"`` survives the change: an unknown key is a key somebody expected to mean
    something, and silently dropping it is how a client ships a bug it cannot see."""
    with pytest.raises(ValidationError):
        HoldingKeyIn.model_validate({"instrument_id": 1, "broker_account_id": 1, "qty": "600"})


def test_the_write_half_says_in_its_own_source_that_it_is_not_an_order_path() -> None:
    """§9 and non-negotiable #1, over the source these two routes live in.

    ``test_portfolio_overview.py`` greps the same module for order vocabulary and asserts §9's
    forbidden words over *rendered payloads* — which is the right place for them, since a phrase
    assembled at runtime never appears in the source and the source has to be able to name a
    forbidden word in order to forbid it. What this asserts is narrower and structural: the write
    half carries the promise in writing, and there are exactly two writes.
    """
    source = MODULE.read_text(encoding="utf-8")

    assert "**Not an order path.**" in source
    assert source.count("@router.post(") == 2
    assert "@router.put(" not in source
    assert "@router.delete(" not in source


# ---------------------------------------------------------------------------
# §6.7 — creating a portfolio
# ---------------------------------------------------------------------------


@requires_db
@pytest.mark.asyncio
async def test_creating_a_capital_portfolio_allocates_the_named_holdings(
    session: AsyncSession, pile: Pile
) -> None:
    """The whole point of §6.7: the pile shrinks and a named portfolio holds what it named.

    The value is checked against the fixture's own arithmetic (₹1,20,000 + ₹35,000) and the
    allocation is checked against ``portfolio_holding`` itself, because criterion 2 is a statement
    about stored rows.
    """
    body = NewPortfolioIn(
        name="Indian IT",
        kind=PortfolioKind.CAPITAL,
        source=PortfolioSource.HOLDING_GROUP,
        benchmark_index_id=pile.benchmark_id,
        holdings=[
            HoldingKeyIn(instrument_id=pile.infy, broker_account_id=pile.zerodha),
            HoldingKeyIn(instrument_id=pile.tcs, broker_account_id=pile.zerodha),
        ],
    )
    detail = await new_portfolio(body, session, pile.owner)

    assert detail.summary.name == "Indian IT"
    assert detail.summary.kind is PortfolioKind.CAPITAL
    assert detail.summary.source is PortfolioSource.HOLDING_GROUP
    assert detail.summary.counts_toward_total is True
    assert detail.summary.value == SECTOR_VALUE
    assert detail.summary.started_on == dt.datetime.now(tz=dt.UTC).date()
    assert {row.instrument.instrument_id for row in detail.holdings} == {pile.infy, pile.tcs}

    new_id = detail.summary.portfolio_id
    assert await _capital_row(session, pile.infy, pile.zerodha) == new_id
    assert await _capital_row(session, pile.tcs, pile.zerodha) == new_id
    # RELIANCE was not named, so it is still unallocated — a create files what it was given.
    assert await _capital_row(session, pile.reliance, pile.upstox) is None

    after = await portfolio_suggestions(session, pile.owner)
    assert after.unallocated_count == 1


@requires_db
@pytest.mark.asyncio
async def test_an_empty_capital_portfolio_is_a_legitimate_thing_to_make(
    session: AsyncSession, pile: Pile
) -> None:
    """§6.7 offers "Empty" as a starting point, so a body with no holdings is not an error."""
    body = NewPortfolioIn(
        name="For later",
        kind=PortfolioKind.CAPITAL,
        source=PortfolioSource.MY_STRATEGY,
    )
    detail = await new_portfolio(body, session, pile.owner)

    assert detail.holdings == []
    assert detail.summary.value == Decimal("0.00")


@requires_db
@pytest.mark.asyncio
async def test_asking_for_more_shares_than_are_free_is_refused_with_the_numbers(
    session: AsyncSession, pile: Pile
) -> None:
    """§11.2a's refusal, replacing criterion 2's (10 Sep 2026).

    This test asserted the opposite until Maulik reversed the rule: filing a holding that was
    already in another capital portfolio used to be a 400 that said "a holding belongs to exactly
    one capital portfolio". Filing the same stock into several portfolios is now the feature, so
    the only thing left to refuse is arithmetic — you cannot file more shares than you own.

    The message carries the numbers, not just the fact, because §6.7's picker needs something the
    user can correct *to*: how many were asked for, how many are free, and where the rest already
    are.
    """
    body = NewPortfolioIn(
        name="Banks",
        kind=PortfolioKind.CAPITAL,
        source=PortfolioSource.HOLDING_GROUP,
        holdings=[
            HoldingKeyIn(
                instrument_id=pile.hdfc,
                broker_account_id=pile.zerodha,
                quantity=Decimal("100000"),
            )
        ],
    )
    with pytest.raises(Problem) as raised:
        await new_portfolio(body, session, pile.owner)

    problem = raised.value
    assert problem.status == 400
    assert "HDFCBANK" in problem.detail
    assert "more shares than you own" in problem.detail
    assert "are free" in problem.detail
    # ...and nothing was created: a portfolio that could not hold what it named is not a portfolio.
    assert await _capital_row(session, pile.hdfc, pile.zerodha) == pile.core
    assert (
        await session.scalar(
            select(Portfolio.id).where(
                Portfolio.user_id == pile.owner_id, Portfolio.name == "Banks"
            )
        )
        is None
    )


@requires_db
@pytest.mark.asyncio
async def test_a_monitoring_view_may_overlap_an_existing_capital_portfolio(
    session: AsyncSession, pile: Pile
) -> None:
    """§4.1: a lens overlaps by design, and refusing it would be refusing it for doing its job.

    The same HDFCBANK position that a second *capital* portfolio cannot have is taken by a
    monitoring view without complaint — and, crucially, **without moving**. Its capital allocation
    still points at ``Core`` afterwards, which is what "takes no capital allocation" means.
    """
    body = NewPortfolioIn(
        name="Everything I own in banks",
        kind=PortfolioKind.MONITORING,
        source=PortfolioSource.HOLDING_GROUP,
        holdings=[
            HoldingKeyIn(instrument_id=pile.hdfc, broker_account_id=pile.zerodha),
            HoldingKeyIn(instrument_id=pile.infy, broker_account_id=pile.zerodha),
        ],
    )
    detail = await new_portfolio(body, session, pile.owner)

    assert detail.summary.kind is PortfolioKind.MONITORING
    assert detail.summary.counts_toward_total is False
    assert detail.summary.excluded_note is not None
    assert "excluded from totals" in detail.summary.excluded_note
    assert {row.instrument.instrument_id for row in detail.holdings} == {pile.hdfc, pile.infy}

    # Nothing moved. HDFCBANK still counts against Core; INFY is still unallocated.
    assert await _capital_row(session, pile.hdfc, pile.zerodha) == pile.core
    assert await _capital_row(session, pile.infy, pile.zerodha) is None
    still = await portfolio_suggestions(session, pile.owner)
    assert still.unallocated_count == 3


@requires_db
@pytest.mark.asyncio
async def test_two_monitoring_views_may_watch_the_same_holding(
    session: AsyncSession, pile: Pile
) -> None:
    """ "A holding may appear in many" (§4.1). The fixture's lens already holds INFY."""
    body = NewPortfolioIn(
        name="Second opinion",
        kind=PortfolioKind.MONITORING,
        source=PortfolioSource.HOLDING_GROUP,
        holdings=[HoldingKeyIn(instrument_id=pile.infy, broker_account_id=pile.zerodha)],
    )
    detail = await new_portfolio(body, session, pile.owner)

    watching = (
        await session.scalars(
            select(PortfolioHolding.portfolio_id).where(
                PortfolioHolding.instrument_id == pile.infy,
                PortfolioHolding.broker_account_id == pile.zerodha,
                PortfolioHolding.portfolio_kind == PortfolioKind.MONITORING.value,
            )
        )
    ).all()
    assert {int(value) for value in watching} == {pile.watch, detail.summary.portfolio_id}


# ---------------------------------------------------------------------------
# Tenancy — NOT_FOUND, never FORBIDDEN
# ---------------------------------------------------------------------------


@requires_db
@pytest.mark.asyncio
async def test_another_users_broker_account_answers_not_found(
    session: AsyncSession, pile: Pile
) -> None:
    """A 403 would confirm the id names a real account and whose it is. 404 confirms nothing."""
    body = NewPortfolioIn(
        name="Not mine",
        kind=PortfolioKind.CAPITAL,
        source=PortfolioSource.HOLDING_GROUP,
        holdings=[HoldingKeyIn(instrument_id=pile.hdfc, broker_account_id=pile.their_broker)],
    )
    with pytest.raises(Problem) as raised:
        await new_portfolio(body, session, pile.owner)

    assert raised.value.type is ProblemType.NOT_FOUND
    assert raised.value.status == 404
    assert "broker account" in raised.value.detail


@requires_db
@pytest.mark.asyncio
async def test_a_holding_the_caller_does_not_hold_answers_not_found(
    session: AsyncSession, pile: Pile
) -> None:
    """The account is theirs; the position is not. A pair the ledger does not know is not a thing
    to allocate, and saying so is the same answer as for a stranger's account."""
    body = NewPortfolioIn(
        name="Wishful",
        kind=PortfolioKind.CAPITAL,
        source=PortfolioSource.HOLDING_GROUP,
        holdings=[HoldingKeyIn(instrument_id=pile.wipro, broker_account_id=pile.zerodha)],
    )
    with pytest.raises(Problem) as raised:
        await new_portfolio(body, session, pile.owner)

    assert raised.value.type is ProblemType.NOT_FOUND
    assert "holding" in raised.value.detail


@requires_db
@pytest.mark.asyncio
async def test_an_unknown_benchmark_answers_not_found(session: AsyncSession, pile: Pile) -> None:
    """Storing an id that names nothing would look, in the database, like a deliberate choice."""
    body = NewPortfolioIn(
        name="Benchmarked to nothing",
        kind=PortfolioKind.CAPITAL,
        source=PortfolioSource.HOLDING_GROUP,
        benchmark_index_id=32_000,
    )
    with pytest.raises(Problem) as raised:
        await new_portfolio(body, session, pile.owner)

    assert raised.value.type is ProblemType.NOT_FOUND
    assert "benchmark index" in raised.value.detail


@requires_db
@pytest.mark.asyncio
async def test_a_created_portfolio_renders_none_of_section_nines_forbidden_words(
    session: AsyncSession, pile: Pile
) -> None:
    """§9, over the payload rather than over the source — the only place it can be checked.

    Baskfy is not SEBI-registered, so "managed", "managed portfolio", "advisory" and "PMS"
    describe a service it does not provide; using one would be a regulatory claim. The walk is
    over the *rendered* response so that a sentence assembled at runtime cannot slip past it.
    """
    body = NewPortfolioIn(
        name="Indian IT",
        kind=PortfolioKind.CAPITAL,
        source=PortfolioSource.SUBSCRIBED,
        holdings=[HoldingKeyIn(instrument_id=pile.infy, broker_account_id=pile.zerodha)],
    )
    detail = await new_portfolio(body, session, pile.owner)

    rendered = detail.model_dump_json().lower()
    for word in ("managed", "advisory", "pms", "portfolio management service"):
        assert word not in rendered
