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
from baskfy_api.portfolios import SymbolResolution, replace_holdings, resolve_symbols
from baskfy_core.allocation_ledger import (
    Allocation,
    DetectedSell,
    Holding,
    HoldingKey,
    PortfolioKind,
    PortfolioSource,
    attribute_sell,
)
from baskfy_core.models import Instrument, Portfolio, PortfolioHolding
from baskfy_core.models.accounts import ReconciliationItem
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
    #: Filed slices the broker no longer backs that had exactly one capital owner, so the sell
    #: attributed itself and the row moved. See :func:`reconcile_missing_positions`.
    reconciled: int = 0
    #: Filed positions the broker no longer backs that are split across capital portfolios, so
    #: nothing moved and the inbox was asked instead. Each is one OPEN ``reconciliation_item``.
    questions: int = 0
    #: Symbols whose filed rows could not be judged because the broker named the symbol but this
    #: build could not resolve it. Named rather than silently treated as sold.
    unjudged: tuple[str, ...] = ()


@dataclass(frozen=True)
class DisappearanceReport:
    """What one pass of :func:`reconcile_missing_positions` decided."""

    reconciled: int
    questions: int
    unjudged: tuple[str, ...]


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

    Looked up by `is_broker_pile` (0038), not by name, so renaming it in the UI does not orphan it
    and cause the next sync to create a second one.

    It used to be looked up by `(user_id, broker_account_id, source)`, which a user's own grouping
    of one broker's holdings matches exactly: `POST /portfolio` sets `broker_account_id` when
    every chosen leg comes from one account, and HOLDING_GROUP is one of §6.7's offered sources.
    So `scalar_one_or_none()` could raise on a second match — or, worse, return the user's own
    portfolio and let the next sync write over it. 0038's partial unique index now makes two piles
    per account unrepresentable, so the `one_or_none` is a fact rather than a hope.
    """
    existing = (
        await session.execute(
            select(Portfolio).where(
                Portfolio.user_id == user_id,
                Portfolio.broker_account_id == broker_account_id,
                Portfolio.is_broker_pile.is_(True),
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
        #: 0038. The whole point of this row: it is the system's container for shares nobody has
        #: sorted yet, which is why §6.6 draws it as Unallocated rather than as a portfolio.
        is_broker_pile=True,
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

    # Before the pile is recomputed, and after the desk has filed its own: what the user filed
    # that this live read no longer backs. Runs here rather than earlier so it sees the settled
    # state — `file_swing_positions` has already released anything the strategy closed, and the
    # sweep is not left asking a question about a row that is about to be rewritten anyway.
    disappeared = await reconcile_missing_positions(
        session,
        result,
        broker_quantities={
            instrument_id: quantity for instrument_id, quantity, _ in wanted if quantity is not None
        },
        unjudgeable=_instruments_a_symbol_might_have_meant(resolutions),
        user_id=user_id,
        broker_account_id=broker_account_id,
        pile_portfolio_id=int(portfolio.id),
        as_of=as_of,
    )

    wanted = await _minus_what_is_filed_elsewhere(
        session,
        wanted,
        user_id=user_id,
        broker_account_id=broker_account_id,
        pile_portfolio_id=int(portfolio.id),
    )
    written = await replace_holdings(session, portfolio, wanted, added_on=as_of)
    reason = f"{written} holding(s) written to {portfolio.name} from a live read"
    if disappeared.reconciled:
        reason += f"; {disappeared.reconciled} filed holding(s) the broker no longer reports closed"
    if disappeared.questions:
        reason += f"; {disappeared.questions} left for the reconciliation inbox to answer"
    if disappeared.unjudged:
        reason += f"; not judged (unresolved symbol): {', '.join(disappeared.unjudged)}"
    return BrokerSyncResult(
        persisted=True,
        written=written,
        unresolved=tuple(unresolved),
        portfolio_id=portfolio.id,
        reason=reason,
        reconciled=disappeared.reconciled,
        questions=disappeared.questions,
        unjudged=disappeared.unjudged,
    )


def _instruments_a_symbol_might_have_meant(
    resolutions: Mapping[str, SymbolResolution],
) -> dict[int, str]:
    """``{instrument_id -> the symbol that may have meant it}`` for every symbol that did NOT
    resolve to exactly one instrument.

    The point is narrow and load-bearing. A symbol the broker reports and this build cannot pin
    down is a symbol whose shares are somewhere in this table and we do not know where — so every
    instrument it *could* have meant has to be exempt from "the broker no longer reports this".
    Without it the sweep would read a resolution failure as a sale and delete a real position.

    An ``AMBIGUOUS`` resolution carries its candidates, so the exemption is exact rather than a
    blanket "judge nothing this pass". An ``UNMATCHED`` one carries none and needs none: it means
    no instrument and no alias has that symbol, so it cannot be any row in the table.
    """
    return {
        candidate.instrument_id: resolution.symbol
        for resolution in resolutions.values()
        if resolution.instrument_id is None
        for candidate in resolution.candidates
    }


async def reconcile_missing_positions(  # noqa: PLR0913
    session: AsyncSession,
    result: HoldingsResult,
    *,
    broker_quantities: Mapping[int, Decimal],
    unjudgeable: Mapping[int, str],
    user_id: int,
    broker_account_id: int,
    pile_portfolio_id: int,
    as_of: dt.date,
) -> DisappearanceReport:
    """Answer, for every filed holding, the question nothing in production was asking:
    **the broker does not report this any more — what happened to it?**

    THE BUG THIS EXISTS TO FIX, AND WHY IT OUTLIVED EVERYTHING ELSE
    ---------------------------------------------------------------
    On 12 Sep 2026 the box held 147 shares of PKTEA in a group the user made on 10 Sep. He does
    not own them. Nothing had written a bad row: the row was true when it was written, the shares
    left the account afterwards, and **no code path in production ever looked at it again**.
    :func:`sync_holdings_into_portfolio` rewrites the broker's own pile and reads other
    portfolios only to *subtract* from that pile — so a phantom slice does not even show up as an
    imbalance; it quietly shrinks Unallocated by its own size and the arithmetic still adds up.
    A filed row was, until this function, write-once.

    The machinery to answer the question already existed twice over and neither copy ran:
    ``baskfy_worker.tasks.holdings_sync.run_holdings_sync`` is 828 lines whose whole subject this
    is, and it has no caller outside its own tests — ``reconciliation_item`` has 0 rows on a box
    that has been live for eleven days. This is the smallest thing that makes the question get
    asked on the path that actually runs.

    WHY IT IS NOT A DELETE
    ----------------------
    §4.3: *"Unresolved reconciliation items freeze that holding's contribution to performance
    rather than guessing. Never silently corrupt a portfolio's return series."* The decision is
    therefore not taken here at all — it is
    :func:`baskfy_core.allocation_ledger.attribute_sell`'s, unchanged and pure:

    * **one capital owner** -> the sell attributes itself, the slice shrinks by the missing
      quantity and a slice drained to zero is removed. Criterion 4's whole-holding case. PKTEA is
      this case, and the row goes.
    * **several capital owners** -> ``SPLIT_HOLDING``. One OPEN ``reconciliation_item``, and *not
      one share moves*. The inbox, the attention ribbon and the command centre's
      ``open_reconciliation_count`` already render it, which is why this leaf built no new screen.

    THE THREE THINGS IT REFUSES TO JUDGE
    ------------------------------------
    1. **A read that is not live.** Unreachable here — the caller is past ``is_persistable`` — but
       stated because it is the property that matters most: a fixture must never be able to
       retire a real position.
    2. **An empty read.** ``HoldingsResult.live([])`` returns ``source="empty"`` by construction,
       so a broker that answers with nothing (an expired session, an upstream hiccup) never
       reaches this function. The cost is real and is recorded rather than hidden: selling the
       *entire* account is the one disappearance this does not reconcile. That needs the layer-1
       ``broker_holding`` table ``holdings_sync``'s docstring already asks for; failing in the
       direction of "we still think you own it" is the safe half.
    3. **A position a symbol the broker named might have been.** Resolution fails by becoming
       *ambiguous*: a rename leaves two ``symbol_alias`` rows pointing at different instruments,
       or a second listing appears, and a symbol that resolved yesterday does not today. The
       shares did not move; our ability to name them did. So every candidate of every unresolved
       symbol is exempt (``unjudgeable``, from
       :func:`_instruments_a_symbol_might_have_meant`), and the raw symbol is compared as well.
       Those names come back in ``unjudged`` and are printed in the sync note, because a
       judgement not taken has to be visible or it is indistinguishable from a clean bill.

    The pile itself is out of scope: its rows are this sync's own output, recomputed from the
    live read a few lines below, not an allocation anybody made.
    """
    if not result.is_live or not result.rows:
        return DisappearanceReport(reconciled=0, questions=0, unjudged=())

    reported_symbols = {row.symbol.strip().upper() for row in result.rows}

    filed = (
        await session.execute(
            select(
                PortfolioHolding.instrument_id,
                PortfolioHolding.portfolio_id,
                PortfolioHolding.quantity,
                PortfolioHolding.avg_price,
                Instrument.symbol,
            )
            .join(Portfolio, Portfolio.id == PortfolioHolding.portfolio_id)
            .join(Instrument, Instrument.id == PortfolioHolding.instrument_id)
            .where(
                Portfolio.user_id == user_id,
                PortfolioHolding.broker_account_id == broker_account_id,
                PortfolioHolding.portfolio_kind == PortfolioKind.CAPITAL.value,
                PortfolioHolding.portfolio_id != pile_portfolio_id,
                #: "We do not know how many" is not "minus one" and it is not a sale either.
                #: 0022 permits a NULL quantity; a NULL cannot be differenced, so it is left.
                PortfolioHolding.quantity.is_not(None),
                #: And a zero row is not a holding — `Allocation` refuses to represent one, so
                #: a stale zero left by some earlier writer would raise here and take the whole
                #: sync down rather than being the no-op it plainly is.
                PortfolioHolding.quantity > 0,
            )
        )
    ).all()
    if not filed:
        return DisappearanceReport(reconciled=0, questions=0, unjudged=())

    slices: dict[int, dict[int, Decimal]] = {}
    symbols: dict[int, str] = {}
    avg_prices: dict[int, Decimal | None] = {}
    for row in filed:
        instrument_id = int(row.instrument_id)
        symbols[instrument_id] = row.symbol
        slices.setdefault(instrument_id, {})[int(row.portfolio_id)] = Decimal(row.quantity)
        if avg_prices.get(instrument_id) is None:
            avg_prices[instrument_id] = row.avg_price

    reconciled = 0
    questions = 0
    unjudged: list[str] = []
    for instrument_id in sorted(slices):
        held_by = slices[instrument_id]
        recorded = sum(held_by.values(), Decimal(0))
        at_broker = broker_quantities.get(instrument_id)
        if at_broker is None:
            # Refusal 3, both halves: a symbol the broker named that could have meant this
            # instrument, and the instrument's own symbol appearing in the raw read.
            maybe_this = unjudgeable.get(instrument_id)
            if maybe_this is not None:
                unjudged.append(maybe_this)
                continue
            if symbols[instrument_id].strip().upper() in reported_symbols:
                unjudged.append(symbols[instrument_id])
                continue
            at_broker = Decimal(0)
        missing = recorded - at_broker
        if missing <= 0:
            continue

        key = HoldingKey(instrument_id=instrument_id, broker_account_id=broker_account_id)
        attribution = attribute_sell(
            DetectedSell(key=key, quantity=missing),
            [Holding(key=key, quantity=recorded, avg_price=avg_prices.get(instrument_id))],
            [
                Allocation(key=key, portfolio_id=portfolio_id, quantity=quantity)
                for portfolio_id, quantity in sorted(held_by.items())
            ],
        )
        owner = attribution.portfolio_id
        if owner is not None:
            await _close_slice(
                session,
                portfolio_id=owner,
                instrument_id=instrument_id,
                broker_account_id=broker_account_id,
                remaining=held_by[owner] - missing,
            )
            reconciled += 1
            continue

        item = attribution.item
        if item is None:
            # Unreachable: `SellAttribution.__post_init__` refuses "neither". Raised rather than
            # skipped, because a sell that produced no answer at all is a broken ledger and
            # continuing past it would file the silence away as "nothing happened".
            raise RuntimeError(
                f"attribute_sell returned neither an attribution nor a question for "
                f"instrument {instrument_id} at broker account {broker_account_id}"
            )
        await _ask_once(
            session,
            item_reason=str(item.reason),
            quantity=item.quantity,
            suggested_portfolio_id=item.suggested_portfolio_id,
            user_id=user_id,
            instrument_id=instrument_id,
            broker_account_id=broker_account_id,
            as_of=as_of,
        )
        questions += 1

    await session.flush()
    return DisappearanceReport(reconciled=reconciled, questions=questions, unjudged=tuple(unjudged))


async def _close_slice(
    session: AsyncSession,
    *,
    portfolio_id: int,
    instrument_id: int,
    broker_account_id: int,
    remaining: Decimal,
) -> None:
    """Shrink one attributed slice, or remove it when nothing is left.

    A zero row is not "holds none of it"; ``Allocation`` refuses to represent one and
    :func:`_apply_allocation` deletes rather than zeroes for the same reason. A slice that goes
    negative is impossible here — ``missing`` is bounded by ``recorded``, and ``recorded`` is this
    slice's own quantity whenever it is the sole owner — but the clamp is written anyway, because
    a negative quantity would breach ``portfolio_holding_quantity_not_negative`` at flush time and
    take the whole sync down with it.
    """
    if remaining > 0:
        await session.execute(
            update(PortfolioHolding)
            .where(
                PortfolioHolding.portfolio_id == portfolio_id,
                PortfolioHolding.instrument_id == instrument_id,
                PortfolioHolding.broker_account_id == broker_account_id,
            )
            .values(quantity=remaining)
        )
        return
    await session.execute(
        delete(PortfolioHolding).where(
            PortfolioHolding.portfolio_id == portfolio_id,
            PortfolioHolding.instrument_id == instrument_id,
            PortfolioHolding.broker_account_id == broker_account_id,
        )
    )


async def _ask_once(  # noqa: PLR0913
    session: AsyncSession,
    *,
    item_reason: str,
    quantity: Decimal,
    suggested_portfolio_id: int | None,
    user_id: int,
    instrument_id: int,
    broker_account_id: int,
    as_of: dt.date,
) -> None:
    """Write the question, or refresh the one already open. House rule 7 — idempotent.

    A sync runs on every login and several times a day. Inserting a row each time would give a
    user forty copies of one question by the end of a week, and an inbox that grows on its own is
    an inbox nobody opens — which would defeat the freeze §4.3 is asking for. So the key is
    ``(user, instrument, broker account, reason)`` among OPEN rows, and a repeat *updates* the
    quantity rather than adding a sibling.

    A RESOLVED or DISMISSED row is deliberately not matched. Those are a human's answer and
    terminal; if the same condition is still true afterwards it is a new question, and silently
    reopening somebody's decision would be the product overruling them.
    """
    existing = (
        (
            await session.execute(
                select(ReconciliationItem).where(
                    ReconciliationItem.user_id == user_id,
                    ReconciliationItem.instrument_id == instrument_id,
                    ReconciliationItem.broker_account_id == broker_account_id,
                    ReconciliationItem.reason == item_reason,
                    ReconciliationItem.state == "OPEN",
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        existing.quantity = quantity
        existing.suggested_portfolio_id = suggested_portfolio_id
        return
    session.add(
        ReconciliationItem(
            user_id=user_id,
            instrument_id=instrument_id,
            broker_account_id=broker_account_id,
            quantity=quantity,
            reason=item_reason,
            state="OPEN",
            suggested_portfolio_id=suggested_portfolio_id,
            detected_on=as_of,
        )
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
