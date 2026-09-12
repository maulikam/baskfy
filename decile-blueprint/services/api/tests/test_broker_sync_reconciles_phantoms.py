"""A filed holding the broker stopped reporting must be answered, not kept forever (PKTEA).

THE BUG THIS EXISTS TO CATCH
---------------------------
On 12 Sep 2026 Maulik said he does not own PKTEA. The production database held 147 shares of it,
``kind=CAPITAL``, in a group he created on 10 Sep. Nothing had written a wrong row: the row was
true the day it was written, the shares left the demat afterwards, and **no path in production
ever read it again**. `sync_holdings_into_portfolio` rewrites the broker's own pile and touches
other portfolios only to subtract from that pile — so a phantom slice does not even show up as an
imbalance. It quietly shrinks Unallocated by its own size and every total still adds up.

The question "we record this and the broker does not" already had an answer — 828 lines of
``baskfy_worker.tasks.holdings_sync``, built on ``allocation_ledger.attribute_sell`` — and that
module has no caller outside its own tests. ``reconciliation_item`` had 0 rows on a box that had
been live for eleven days.

WHAT THESE TESTS ASSERT
-----------------------
The **spec**, house rule 2, not what the code does today:

* criterion 4 — *"a sell detected by sync either auto-attributes (whole-holding case) or creates
  a reconciliation item; it never silently alters a return series"*;
* §4.3 — an unanswerable difference **freezes** the holding and asks; not one share moves;
* house rule 7 — running the same sync again produces the same rows, so the inbox does not grow
  a duplicate question per login.

Every one of them would have failed before this leaf, and the first is the PKTEA case itself.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from baskfy_execution.broker_ports import HoldingRow
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import baskfy_api.broker_holdings_sync as sync_module
from baskfy_api.broker_holdings import HoldingsResult
from baskfy_api.broker_holdings_sync import (
    reconcile_missing_positions,
    sync_holdings_into_portfolio,
)
from baskfy_core.allocation_ledger import PortfolioKind, PortfolioSource, ReconciliationReason
from baskfy_core.models import PortfolioHolding
from baskfy_core.models.accounts import ReconciliationItem

pytestmark = [pytest.mark.db]

AS_OF = dt.date(2026, 9, 12)
EMAIL = "phantom@example.test"

#: Reported by the broker in every live read below, so the read is never empty — an empty live
#: read is `source="empty"` by construction and is refused before any of this runs.
ANCHOR = "PHANTOMANCHOR"
#: Filed by the user, and absent from the broker's read. PKTEA's shape.
VANISHED = "PHANTOMGONE"
#: Filed at 147, reported at 100.
SHRUNK = "PHANTOMLESS"
#: Filed across two capital portfolios, and absent from the broker's read.
SPLIT = "PHANTOMSPLIT"
#: An old symbol two renames both claim, so it resolves to neither. `portfolios.py`'s own
#: docstring names this case: "a symbol_alias from a rename can collide with a live symbol".
AMBIGUOUS = "PHANTOMTWO"
RENAMED_A = "PHANTOMTWOA"
RENAMED_B = "PHANTOMTWOB"

SYMBOLS = (ANCHOR, VANISHED, SHRUNK, SPLIT, AMBIGUOUS, RENAMED_A, RENAMED_B)


@pytest_asyncio.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """One transactional session per test, scoped by this module's own symbols and user.

    Scoped rather than truncating: these tables are shared with every other db-marked module and
    a TRUNCATE here would make this file's result depend on execution order.
    """
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await _wipe(session)
        yield session
        await _wipe(session)
        await session.commit()


async def _wipe(session: AsyncSession) -> None:
    # `portfolio.user_id` has no ON DELETE action on purpose — a user with portfolios is not
    # deleted out from under them — so the rows come off in dependency order rather than relying
    # on a cascade that deliberately is not there.
    mine = "(SELECT id FROM app_user WHERE email = :email)"
    for statement in (
        f"DELETE FROM reconciliation_item WHERE user_id IN {mine}",
        f"DELETE FROM portfolio WHERE user_id IN {mine}",
        f"DELETE FROM broker_account WHERE user_id IN {mine}",
        "DELETE FROM app_user WHERE email = :email",
    ):
        await session.execute(text(statement), {"email": EMAIL})
    await session.execute(
        text("DELETE FROM symbol_alias WHERE old_symbol = ANY(:symbols)"),
        {"symbols": list(SYMBOLS)},
    )
    await session.execute(
        text("DELETE FROM instrument WHERE symbol = ANY(:symbols)"), {"symbols": list(SYMBOLS)}
    )
    await session.commit()


async def _user(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO app_user (public_id, email, name) "
            "VALUES ('pub-phantom', :email, 'phantom') RETURNING id"
        ),
        {"email": EMAIL},
    )
    return int(row.scalar_one())


async def _broker(session: AsyncSession, user_id: int) -> int:
    row = await session.execute(
        text(
            "INSERT INTO broker_account (user_id, broker_id, label) "
            "VALUES (:user_id, 'zerodha', 'PHANTOM') RETURNING id"
        ),
        {"user_id": user_id},
    )
    return int(row.scalar_one())


async def _instrument(session: AsyncSession, symbol: str, *, series: str = "EQ") -> int:
    """Written as SQL, like the other db-marked tests: the column set is the schema's."""
    row = await session.execute(
        text(
            "INSERT INTO instrument "
            "(exchange_id, symbol, name, instrument_type, series, is_active, listed_on) "
            "VALUES (1, :symbol, :symbol, 'EQ', :series, true, :listed_on) RETURNING id"
        ),
        {"symbol": symbol, "series": series, "listed_on": dt.date(2020, 1, 1)},
    )
    return int(row.scalar_one())


async def _alias(session: AsyncSession, old_symbol: str, instrument_id: int) -> None:
    await session.execute(
        text(
            "INSERT INTO symbol_alias (instrument_id, old_symbol, changed_on) "
            "VALUES (:instrument_id, :old_symbol, :changed_on)"
        ),
        {
            "instrument_id": instrument_id,
            "old_symbol": old_symbol,
            "changed_on": dt.date(2025, 1, 1),
        },
    )


async def _portfolio(session: AsyncSession, user_id: int, name: str) -> int:
    row = await session.execute(
        text(
            "INSERT INTO portfolio (user_id, name, kind, source, started_on) "
            "VALUES (:user_id, :name, :kind, :source, :started_on) RETURNING id"
        ),
        {
            "user_id": user_id,
            "name": name,
            "kind": PortfolioKind.CAPITAL.value,
            "source": PortfolioSource.MY_STRATEGY.value,
            "started_on": AS_OF,
        },
    )
    return int(row.scalar_one())


async def _file(
    session: AsyncSession,
    *,
    portfolio_id: int,
    instrument_id: int,
    broker_account_id: int,
    quantity: str,
) -> None:
    """A holding the user filed — the row class PKTEA belongs to."""
    session.add(
        PortfolioHolding(
            portfolio_id=portfolio_id,
            instrument_id=instrument_id,
            broker_account_id=broker_account_id,
            portfolio_kind=PortfolioKind.CAPITAL.value,
            quantity=Decimal(quantity),
            added_on=dt.date(2026, 9, 10),
        )
    )
    await session.flush()


def _live(*rows: tuple[str, str]) -> HoldingsResult:
    """What the broker reports this morning: ``(symbol, quantity)`` pairs."""
    return HoldingsResult.live(
        [
            HoldingRow(
                symbol=symbol,
                exchange="NSE",
                quantity=Decimal(quantity),
                t1_quantity=Decimal("0"),
                collateral_quantity=Decimal("0"),
                average_price=Decimal("100.00"),
            )
            for symbol, quantity in rows
        ]
    )


async def _sync(
    session: AsyncSession, result: HoldingsResult, user_id: int, broker_account_id: int
) -> sync_module.BrokerSyncResult:
    outcome = await sync_holdings_into_portfolio(
        session,
        result,
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker_name="Zerodha",
        as_of=AS_OF,
    )
    await session.flush()
    return outcome


async def _quantity(session: AsyncSession, portfolio_id: int, instrument_id: int) -> Decimal | None:
    return await session.scalar(
        select(PortfolioHolding.quantity).where(
            PortfolioHolding.portfolio_id == portfolio_id,
            PortfolioHolding.instrument_id == instrument_id,
        )
    )


async def _items(session: AsyncSession, user_id: int) -> list[ReconciliationItem]:
    rows = await session.execute(
        select(ReconciliationItem)
        .where(ReconciliationItem.user_id == user_id)
        .order_by(ReconciliationItem.instrument_id, ReconciliationItem.id)
    )
    return list(rows.scalars())


# ---------------------------------------------------------------------------
# Criterion 4, first half: one owner, so the sell attributes itself.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sole_owner_of_a_vanished_position_has_the_slice_removed(db: AsyncSession) -> None:
    """**PKTEA, exactly.** 147 shares filed into one portfolio; the broker stops naming them.

    One capital owner is `attribute_sell`'s whole-holding case, so the sell attributes itself and
    the slice goes. The spec sentence being asserted is criterion 4's first branch — *auto-
    attributes (whole-holding case)* — and its consequence: the Portfolio page stops claiming a
    position the account does not hold. Before this leaf the row survived every sync forever.
    """
    user_id = await _user(db)
    broker_account_id = await _broker(db, user_id)
    anchor = await _instrument(db, ANCHOR)
    gone = await _instrument(db, VANISHED)
    mine = await _portfolio(db, user_id, "Swing Manual")
    await _file(
        db,
        portfolio_id=mine,
        instrument_id=gone,
        broker_account_id=broker_account_id,
        quantity="147",
    )

    outcome = await _sync(db, _live((ANCHOR, "10")), user_id, broker_account_id)

    assert await _quantity(db, mine, gone) is None, "the phantom slice must not survive the sync"
    assert await _items(db, user_id) == [], "one owner is an answer, not a question"
    assert outcome.reconciled == 1
    assert outcome.questions == 0
    # The anchor is untouched — a sweep that took the whole account with it would also pass the
    # assertion above, and would be the worse bug.
    assert await db.scalar(
        select(func.sum(PortfolioHolding.quantity)).where(PortfolioHolding.instrument_id == anchor)
    ) == Decimal("10")


@pytest.mark.asyncio
async def test_sole_owner_of_a_shrunken_position_has_the_slice_reduced(db: AsyncSession) -> None:
    """147 filed, 100 at the broker: an attributed sell of 47, and the slice becomes 100.

    The same branch of criterion 4 with a smaller difference. It matters separately because the
    arithmetic is where a phantom hides best: `_minus_what_is_filed_elsewhere` clamps the pile at
    zero when the filed total exceeds the broker's, so before this leaf the 47 missing shares
    produced no error, no question and no visible inconsistency — just a total that was 47 too
    big. Its own docstring said the inbox was supposed to ask. Nothing asked.
    """
    user_id = await _user(db)
    broker_account_id = await _broker(db, user_id)
    await _instrument(db, ANCHOR)
    shrunk = await _instrument(db, SHRUNK)
    mine = await _portfolio(db, user_id, "Long term")
    await _file(
        db,
        portfolio_id=mine,
        instrument_id=shrunk,
        broker_account_id=broker_account_id,
        quantity="147",
    )

    await _sync(db, _live((ANCHOR, "10"), (SHRUNK, "100")), user_id, broker_account_id)

    assert await _quantity(db, mine, shrunk) == Decimal("100")
    assert await _items(db, user_id) == []
    # The total across every capital slice equals what the broker says is there. That is the
    # property the phantom broke, and it is the one worth asserting.
    total = await db.scalar(
        select(func.sum(PortfolioHolding.quantity)).where(
            PortfolioHolding.instrument_id == shrunk,
            PortfolioHolding.portfolio_kind == PortfolioKind.CAPITAL.value,
        )
    )
    assert total == Decimal("100")


# ---------------------------------------------------------------------------
# Criterion 4, second half: more than one owner, so it asks and freezes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_split_position_raises_one_question_and_moves_nothing(db: AsyncSession) -> None:
    """60 in one portfolio, 40 in another, gone from the broker: **not one share moves.**

    §4.3: *"Unresolved reconciliation items freeze that holding's contribution to performance
    rather than guessing. Never silently corrupt a portfolio's return series."* Pro-rata would be
    the tempting guess and `attribute_sell` refuses it on purpose — a person who sells out of a
    split position has almost always sold one lot, not a share of each. So the assertion is that
    both quantities are exactly what they were, and that a question exists.
    """
    user_id = await _user(db)
    broker_account_id = await _broker(db, user_id)
    await _instrument(db, ANCHOR)
    split = await _instrument(db, SPLIT)
    first = await _portfolio(db, user_id, "Core")
    second = await _portfolio(db, user_id, "Satellite")
    await _file(
        db,
        portfolio_id=first,
        instrument_id=split,
        broker_account_id=broker_account_id,
        quantity="60",
    )
    await _file(
        db,
        portfolio_id=second,
        instrument_id=split,
        broker_account_id=broker_account_id,
        quantity="40",
    )

    outcome = await _sync(db, _live((ANCHOR, "10")), user_id, broker_account_id)

    assert await _quantity(db, first, split) == Decimal("60")
    assert await _quantity(db, second, split) == Decimal("40")
    assert outcome.questions == 1
    assert outcome.reconciled == 0
    items = await _items(db, user_id)
    assert len(items) == 1
    assert items[0].state == "OPEN"
    assert items[0].reason == ReconciliationReason.SPLIT_HOLDING.value


@pytest.mark.asyncio
async def test_split_question_names_the_whole_missing_quantity(db: AsyncSession) -> None:
    """The item says 100, the number that left — not 60, not 40, not a share of each.

    The inbox's resolve step allocates against this quantity, so a question that understated it
    would let a user answer "all of it" and still leave shares unaccounted for.
    """
    user_id = await _user(db)
    broker_account_id = await _broker(db, user_id)
    await _instrument(db, ANCHOR)
    split = await _instrument(db, SPLIT)
    first = await _portfolio(db, user_id, "Core")
    second = await _portfolio(db, user_id, "Satellite")
    await _file(
        db,
        portfolio_id=first,
        instrument_id=split,
        broker_account_id=broker_account_id,
        quantity="60",
    )
    await _file(
        db,
        portfolio_id=second,
        instrument_id=split,
        broker_account_id=broker_account_id,
        quantity="40",
    )

    await _sync(db, _live((ANCHOR, "10")), user_id, broker_account_id)

    (item,) = await _items(db, user_id)
    assert item.quantity == Decimal("100")
    assert item.instrument_id == split
    assert item.broker_account_id == broker_account_id
    assert item.detected_on == AS_OF
    assert item.resolved_portfolio_id is None, "a question is not an attribution"


@pytest.mark.asyncio
async def test_syncing_twice_is_idempotent_and_asks_once(db: AsyncSession) -> None:
    """House rule 7. A sync runs on every login; the inbox must not grow a row per login.

    Forty copies of one question is an inbox nobody opens, which would defeat the freeze §4.3 is
    asking for. The second run updates the open item in place.
    """
    user_id = await _user(db)
    broker_account_id = await _broker(db, user_id)
    await _instrument(db, ANCHOR)
    split = await _instrument(db, SPLIT)
    first = await _portfolio(db, user_id, "Core")
    second = await _portfolio(db, user_id, "Satellite")
    await _file(
        db,
        portfolio_id=first,
        instrument_id=split,
        broker_account_id=broker_account_id,
        quantity="60",
    )
    await _file(
        db,
        portfolio_id=second,
        instrument_id=split,
        broker_account_id=broker_account_id,
        quantity="40",
    )

    await _sync(db, _live((ANCHOR, "10")), user_id, broker_account_id)
    await _sync(db, _live((ANCHOR, "10")), user_id, broker_account_id)

    assert len(await _items(db, user_id)) == 1
    assert await _quantity(db, first, split) == Decimal("60")
    assert await _quantity(db, second, split) == Decimal("40")


# ---------------------------------------------------------------------------
# The refusals. Each one is a way this sweep could have deleted real money.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unresolved_symbol_the_broker_named_is_never_treated_as_sold(
    db: AsyncSession,
) -> None:
    """Two renames claim the same old symbol, so the broker's row resolves to neither.

    This is `portfolios.py`'s own documented failure mode — *"a `symbol_alias` from a rename can
    collide with a live symbol belonging to somebody else"* — and it is the dangerous shape,
    because a name that resolved yesterday stops resolving today while **nothing has happened to
    the shares**. Judging on instrument id alone would read that as "the broker no longer reports
    it" and delete a real position over a reference-data change.

    So every instrument the unresolvable symbol *could* have meant is exempt, and the skipped
    judgement is named in `unjudged` rather than passing as a clean bill of health.
    """
    user_id = await _user(db)
    broker_account_id = await _broker(db, user_id)
    await _instrument(db, ANCHOR)
    held = await _instrument(db, RENAMED_A)
    other = await _instrument(db, RENAMED_B)
    await _alias(db, AMBIGUOUS, held)
    await _alias(db, AMBIGUOUS, other)
    mine = await _portfolio(db, user_id, "Long term")
    await _file(
        db,
        portfolio_id=mine,
        instrument_id=held,
        broker_account_id=broker_account_id,
        quantity="147",
    )

    outcome = await _sync(db, _live((ANCHOR, "10"), (AMBIGUOUS, "147")), user_id, broker_account_id)

    assert await _quantity(db, mine, held) == Decimal("147"), "an ambiguous symbol is not a sale"
    assert await _items(db, user_id) == []
    assert outcome.reconciled == 0
    assert AMBIGUOUS in outcome.unjudged, "a judgement not taken must be named, not silent"
    assert AMBIGUOUS in outcome.unresolved


@pytest.mark.asyncio
async def test_an_empty_read_can_never_retire_a_position(db: AsyncSession) -> None:
    """A broker that answers with nothing is a broken session, not an emptied account.

    `HoldingsResult.live([])` is `source="empty"` by construction, `_PERSISTABLE_SOURCES` is
    `{"live"}`, and the sweep additionally refuses a rowless result itself — so the property holds
    whether it is reached through the sync or called directly. Asserted at both doors, because a
    later caller could reach the second one without the first.

    The cost is real and deliberate: selling the *entire* account is the one disappearance this
    does not reconcile. Failing towards "we still think you own it" is the safe half, and closing
    it needs the layer-1 broker ledger `holdings_sync`'s docstring asks for.
    """
    user_id = await _user(db)
    broker_account_id = await _broker(db, user_id)
    gone = await _instrument(db, VANISHED)
    mine = await _portfolio(db, user_id, "Swing Manual")
    await _file(
        db,
        portfolio_id=mine,
        instrument_id=gone,
        broker_account_id=broker_account_id,
        quantity="147",
    )

    nothing = HoldingsResult.live([])
    assert nothing.source == "empty", "an empty live fetch is not live"

    outcome = await _sync(db, nothing, user_id, broker_account_id)
    assert outcome.persisted is False
    assert await _quantity(db, mine, gone) == Decimal("147")

    direct = await reconcile_missing_positions(
        db,
        nothing,
        broker_quantities={},
        unjudgeable={},
        user_id=user_id,
        broker_account_id=broker_account_id,
        pile_portfolio_id=0,
        as_of=AS_OF,
    )
    assert direct == sync_module.DisappearanceReport(reconciled=0, questions=0, unjudged=())
    assert await _quantity(db, mine, gone) == Decimal("147")


def test_the_reconcile_path_is_money_free() -> None:
    """Nothing on this path may reach an order. Law 2, and the plan's rule 3 for this run.

    A source scan rather than a mock: the point is that no future edit can add the import either.
    `baskfy_execution.broker_ports` is a record shape and is the only thing this module is allowed
    to take from the execution package.
    """
    source = Path(sync_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("OrderGateway", "place_order", "place_gtt", "kite_client", ".place("):
        assert forbidden not in source, f"{forbidden} must not appear in the holdings sync"
    execution_imports = [
        line
        for line in source.splitlines()
        if line.startswith("from baskfy_execution") or line.startswith("import baskfy_execution")
    ]
    assert execution_imports == ["from baskfy_execution.broker_ports import total_quantity"]


@pytest.mark.asyncio
async def test_every_reason_the_ledger_can_return_can_actually_be_stored(db: AsyncSession) -> None:
    """`attribute_sell` may answer with any `ReconciliationReason`. The table must accept all of
    them — asserted over the enum, so the next member added is covered without editing this file.

    **This found a live defect.** ``SPLIT_HOLDING`` arrived in core on 10 Sep 2026 and 0022's
    CHECK still listed the original three, so the answer `attribute_sell` gives for *every* sell
    out of a split position could not be written: the INSERT raised
    ``ck_reconciliation_item_reconciliation_item_reason_known`` and took the transaction with it.
    Two days passed unnoticed because nothing in production writes a reconciliation item at all —
    the only writer, `run_holdings_sync`, has no caller. 0043 widens the constraint.

    Written over `ReconciliationReason` rather than a hard-coded list on purpose: a test that
    repeated the four values would pass for the same reason the constraint was wrong.
    """
    user_id = await _user(db)
    broker_account_id = await _broker(db, user_id)
    instrument_id = await _instrument(db, ANCHOR)

    for reason in ReconciliationReason:
        db.add(
            ReconciliationItem(
                user_id=user_id,
                instrument_id=instrument_id,
                broker_account_id=broker_account_id,
                quantity=Decimal("1"),
                reason=reason.value,
                state="OPEN",
                detected_on=AS_OF,
            )
        )
        await db.flush()

    stored = {item.reason for item in await _items(db, user_id)}
    assert stored == {reason.value for reason in ReconciliationReason}
