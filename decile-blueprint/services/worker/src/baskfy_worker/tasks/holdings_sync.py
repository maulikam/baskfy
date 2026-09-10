"""Broker holdings sync — ``PORTFOLIO_REDESIGN.md`` §10 phase 1, the first link in the spine.

§10's build order opens with *broker holdings sync -> allocation ledger -> Unallocated bucket +
cash ledger -> nightly EOD NAV -> since-grouped marks -> reconciliation inbox*. This module is
the first of those and it feeds the last: it reads what a broker says the user holds, compares it
with what the Baskfy ledger records, and turns every difference into either an attributed change
or a question. It never guesses, and it never places an order — law 2 owns that path and this
file does not import ``packages/execution``.

THE ONE SENTENCE THIS FILE IS FOR
---------------------------------
Acceptance criterion 4: *"A sell detected by sync either auto-attributes (whole-holding case) or
creates a reconciliation item — it never silently alters a return series."* The decision itself
belongs to :func:`baskfy_core.allocation_ledger.attribute_sell`, which is pure and knows nothing
about brokers or databases. This module's whole job is to assemble an honest picture for it, and
to write down what it answered.

WHAT "BELIEVED" MEANS, AND WHY THE DIFF IS NOT RECORDED-VERSUS-BROKER
--------------------------------------------------------------------
The obvious diff is ``portfolio_holding.quantity`` against the broker's number. It is wrong, and
the way it is wrong matters, because §4.6's layer 1 — a broker ledger of quantities — **does not
exist in the schema yet**. Migration 0022 gave layer 1 exactly one table, ``broker_cash``. There
is nowhere to store "what the broker said yesterday", so ``portfolio_holding`` is doing double
duty as both the allocation record and the sync's memory, and those are not the same number: a
holding this product has never been allowed to attribute is not recorded at all.

So the sync's memory is the ledger *plus the questions already open about it*:

    believed = recorded quantity + the quantity of any OPEN unknown-inflow question

That single definition makes the whole run coherent:

* First sight of 100 shares: recorded 0, no question, believed 0, broker 100. An inflow of 100
  we cannot attribute -> one OPEN ``UNKNOWN_INFLOW`` item saying so.
* Second sync, nothing changed: recorded 0, question 100, believed 100, broker 100 -> *unchanged*.
  **Idempotency is not a special case here; it falls out of the arithmetic**, which is the only
  kind of idempotency that survives someone editing this file later.
* Those 100 shares are then sold: believed 100, broker 0 -> a detected sell of 100 against a
  recorded holding of 0, which ``attribute_sell`` answers with a question rather than a guess.
* An allocated holding of 100 drops to 60: believed 100, broker 60, sell of 40, one capital
  portfolio -> attributed silently, and the recorded quantity moves to 60.

THE RULE ABOUT WRITING: AN UNANSWERED QUESTION FREEZES THE HOLDING
-----------------------------------------------------------------
``portfolio_holding.quantity`` moves in exactly one case — an **attributed** sell — and it moves
by the attributed amount, not to the broker's number (the two differ when an unrelated inflow
question is also open, and using the broker's number there would silently absorb shares nobody
has accounted for into a portfolio's return series).

Every other difference leaves the row alone. §4.3: *"Unresolved reconciliation items freeze that
holding's contribution to performance (show as 'pending reconciliation') rather than guessing.
Never silently corrupt a portfolio's return series."* A frozen row is what that freeze looks like
in the data, and ``portfolio_nav_daily.pending_reconciliation`` is where the nightly NAV job
makes it visible.

WHAT "LANDS UNALLOCATED" MEANS HERE, STATED PLAINLY
---------------------------------------------------
§6.6: *"connect broker -> everything lands in Unallocated"*. In this schema Unallocated is **not
a portfolio** — ``baskfy_core.allocation_ledger`` is explicit that its sentinel is ``None`` and
that giving it a row "would let it be renamed, deleted or given a benchmark". A holding lands
unallocated by having **no capital allocation at all**, and this sync therefore inserts no
``portfolio_holding`` row for a position it has never seen. The OPEN ``UNKNOWN_INFLOW`` item *is*
the landing record: it names the instrument, the broker account and the quantity, and it asks the
one question §6.7's grouping flow exists to answer.

**Open, and deliberately not solved here:** because nothing records an unallocated quantity, a
consolidated total computed from ``portfolio_holding`` alone under-reports until those questions
are resolved. The right fix is a layer-1 ``broker_holding`` table, which is a migration and
belongs to the schema leaf, not to this one. Until then the Unallocated section is derived from
the broker fetch plus these items.

CREDENTIALS
-----------
This task reads. ``DRY_RUN`` is the default everywhere (CLAUDE.md safety rails) and changes
nothing about that, because there is no order path to suppress. What it does need is a live
broker session, and on a box with none the holdings capability is simply not available — the
composite raises rather than substituting fixture positions for a user's real money. See
``FixtureHoldingsProvider``'s docstring for why no fallback is registered.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.allocation_ledger import (
    Allocation,
    DetectedSell,
    Holding,
    HoldingKey,
    PortfolioKind,
    ReconciliationReason,
    attribute_sell,
)
from baskfy_core.gst import money
from baskfy_core.models import BrokerAccount, Instrument, Portfolio, PortfolioHolding
from baskfy_core.models.accounts import BrokerCash, ReconciliationItem
from baskfy_providers.errors import ProviderError
from baskfy_providers.ports import HoldingsProvider
from baskfy_providers.records import BrokerAccountRef, BrokerHoldingRecord
from baskfy_worker.steps import StepOutcome, StepStatus

__all__ = [
    "HoldingsSyncResult",
    "PositionOutcome",
    "run_holdings_sync",
]

#: ``portfolio_holding.quantity`` is ``numeric(20, 4)``. House rule 8 — round at write time, so
#: the API, the UI and a CSV export cannot disagree about a quantity later.
QUANTITY_PLACES: Decimal = Decimal("0.0001")

#: ``reconciliation_item.state``. The other two (RESOLVED, DISMISSED) are a human's answer and
#: are never written by a sync — this module only ever asks.
OPEN: str = "OPEN"


class PositionOutcome:
    """Namespace of the six mutually exclusive verdicts a position can receive.

    Not a ``StrEnum``: these are counter names on :class:`HoldingsSyncResult`, and the accounting
    identity below is asserted over exactly this list. Keeping them here as one tuple means a
    seventh verdict cannot be added without the identity noticing.
    """

    NAMES: tuple[str, ...] = (
        "unchanged",
        "new",
        "sells_attributed",
        "sells_questioned",
        "inflows_questioned",
        "skipped",
    )


@dataclass(slots=True)
class HoldingsSyncResult:
    """Every position accounted for, in counters that must add up — and are checked.

    Two identities, both enforced by :meth:`verify`:

    1. ``fetched == resolved + failed`` — every row the broker sent either became a position or
       is named in ``unmatched_symbols``. A symbol cannot fall out of the run silently.
    2. ``positions == unchanged + new + sells_attributed + sells_questioned +
       inflows_questioned + skipped`` — every position considered got exactly one verdict.

    They are asserted rather than trusted because a sync that quietly drops a name is
    indistinguishable, in its own log line, from a sync that had nothing to do.
    """

    #: Rows the broker reported.
    fetched: int = 0
    #: Fetched rows whose symbol resolved to an ``instrument`` row.
    resolved: int = 0
    #: Fetched rows whose symbol did not. Named in :attr:`unmatched_symbols`.
    failed: int = 0
    #: Distinct positions considered: fetched, or recorded, or already under question.
    positions: int = 0
    unchanged: int = 0
    #: A position with no record and no open question — first sight of these shares.
    new: int = 0
    sells_attributed: int = 0
    sells_questioned: int = 0
    inflows_questioned: int = 0
    #: A recorded holding whose quantity is NULL. Nothing can be diffed against unknown.
    skipped: int = 0
    #: ``reconciliation_item`` rows inserted by this run.
    items_raised: int = 0
    #: Open items that already existed and were re-stated (the idempotent path).
    items_refreshed: int = 0
    #: Positions we know about that the broker did not report at all.
    vanished: int = 0
    #: ``portfolio_holding`` rows moved by an attributed sell.
    holdings_updated: int = 0
    cash_written: bool = False
    unmatched_symbols: tuple[str, ...] = ()
    #: Positions whose recorded rows disagree with each other about the quantity.
    inconsistent: tuple[int, ...] = ()

    def verdicts(self) -> int:
        return sum(getattr(self, name) for name in PositionOutcome.NAMES)

    def verify(self) -> None:
        """Raise if a symbol or a position went unaccounted for. Never repairs, never logs."""
        if self.fetched != self.resolved + self.failed:
            raise ValueError(
                f"holdings sync lost a broker row: fetched {self.fetched} but accounted for "
                f"{self.resolved} resolved + {self.failed} unresolvable"
            )
        if self.positions != self.verdicts():
            raise ValueError(
                f"holdings sync lost a position: considered {self.positions} but recorded "
                f"{self.verdicts()} verdicts across {PositionOutcome.NAMES}"
            )

    def as_detail(self) -> dict[str, object]:
        """The counters as ``pipeline_run_step.error`` detail — an operator's whole picture."""
        detail: dict[str, object] = {
            "fetched": self.fetched,
            "resolved": self.resolved,
            "failed": self.failed,
            "positions": self.positions,
            "unchanged": self.unchanged,
            "new": self.new,
            "sells_attributed": self.sells_attributed,
            "sells_questioned": self.sells_questioned,
            "inflows_questioned": self.inflows_questioned,
            "skipped": self.skipped,
            "items_raised": self.items_raised,
            "items_refreshed": self.items_refreshed,
            "vanished": self.vanished,
            "holdings_updated": self.holdings_updated,
            "cash_written": self.cash_written,
        }
        if self.unmatched_symbols:
            detail["unmatched_symbols"] = list(self.unmatched_symbols)
        if self.inconsistent:
            detail["inconsistent_instrument_ids"] = list(self.inconsistent)
        return detail


@dataclass(frozen=True, slots=True)
class _Recorded:
    """What ``portfolio_holding`` says about one physical position, folded across its rows.

    A position can appear in one capital portfolio and any number of monitoring views (§4.1), so
    "the recorded quantity" needs a rule. The capital row wins, because it is the row an
    attributed sell will move and the one criterion 1 counts. Monitoring rows are consulted only
    when there is no capital row at all — which is precisely the *unallocated but known* state
    that ``attribute_sell`` answers with ``UNALLOCATED_HOLDING``.
    """

    instrument_id: int
    quantity: Decimal | None
    avg_price: Decimal | None
    #: 0035: ``{capital portfolio -> quantity}``. It was a single ``capital_portfolio_id`` until
    #: a holding could be filed into several, and the sum of these is the position.
    capital_slices: Mapping[int, Decimal]

    @property
    def sole_portfolio_id(self) -> int | None:
        """The one capital portfolio this holding sits in, or ``None`` if split or unfiled."""
        return next(iter(self.capital_slices)) if len(self.capital_slices) == 1 else None

    portfolio_ids: tuple[int, ...]
    inconsistent: bool = False


@dataclass(slots=True)
class _Position:
    """One physical position as this run sees it, from all three sources at once."""

    instrument_id: int
    broker_quantity: Decimal
    in_fetch: bool
    recorded: _Recorded | None = None
    open_items: dict[str, ReconciliationItem] = field(default_factory=dict)

    @property
    def recorded_quantity(self) -> Decimal:
        if self.recorded is None or self.recorded.quantity is None:
            return Decimal("0")
        return self.recorded.quantity

    @property
    def open_inflow(self) -> Decimal:
        """The quantity already under an unanswered "where did these come from" question."""
        item = self.open_items.get(ReconciliationReason.UNKNOWN_INFLOW.value)
        return Decimal("0") if item is None else item.quantity

    @property
    def believed_quantity(self) -> Decimal:
        """What we think the broker held before this fetch. See the module docstring."""
        return self.recorded_quantity + self.open_inflow


async def run_holdings_sync(
    session: AsyncSession,
    provider: object,
    outcome: StepOutcome,
    *,
    broker_account_id: int,
    on: dt.date,
) -> HoldingsSyncResult:
    """Sync one broker account's holdings and cash as of ``on``.

    Follows ``tasks/fundamentals.py``'s shape: the session and the ``StepOutcome`` come in, the
    provider is duck-typed at the boundary, and a provider failure is noted and **re-raised**
    (house rule 3 — nothing swallowed). ``on`` is a parameter rather than a clock read, so a
    re-run for a past date produces the same rows; the module owns no clock at all.

    The whole run is one transaction, owned by the caller. That is not incidental: holdings and
    cash are one picture of one moment, and a run that wrote the positions and then failed on the
    cash would leave a net worth that is wrong and still adds up.

    ``provider`` is typed as ``object`` and narrowed by ``isinstance`` against the runtime
    Protocol rather than by ``getattr`` — ``getattr`` on an untyped object returns ``Any``, which
    is banned here, and the Protocol check is the same question asked in a way mypy can see.
    """
    account = await _load_account(session, broker_account_id)
    result = HoldingsSyncResult()

    if not isinstance(provider, HoldingsProvider):
        outcome.status = StepStatus.SKIPPED
        outcome.note(
            reason="no provider offers broker_holdings",
            broker_account_id=broker_account_id,
        )
        return result

    ref = BrokerAccountRef(
        broker_account_id=account.id,
        broker_id=account.broker_id,
        kite_user_id=account.kite_user_id,
    )

    try:
        fetched = list(provider.broker_holdings(ref))
    except ProviderError as exc:
        # Noted for the operator, then re-raised. A holdings sync that "succeeded" with zero
        # rows because the broker was down would read as "the user sold everything".
        outcome.note(error=str(exc), stage="broker_holdings")
        raise

    result.fetched = len(fetched)
    by_instrument, unmatched = await _resolve_symbols(session, fetched)
    result.resolved = result.fetched - sum(count for _, count in unmatched)
    result.failed = sum(count for _, count in unmatched)
    result.unmatched_symbols = tuple(symbol for symbol, _ in unmatched)

    recorded = await _recorded_positions(session, account.user_id, broker_account_id)
    open_items = await _open_items(session, account.user_id, broker_account_id)
    positions = _assemble(by_instrument, recorded, open_items)
    result.positions = len(positions)
    result.vanished = sum(1 for position in positions.values() if not position.in_fetch)
    result.inconsistent = tuple(
        sorted(
            position.instrument_id
            for position in positions.values()
            if position.recorded is not None and position.recorded.inconsistent
        )
    )

    holdings, allocations = _ledger_view(broker_account_id, recorded)
    for position in positions.values():
        await _reconcile(
            session,
            position,
            result,
            holdings=holdings,
            allocations=allocations,
            user_id=account.user_id,
            broker_account_id=broker_account_id,
            on=on,
        )

    await _sync_cash(session, provider, ref, outcome, result, on=on)

    result.verify()
    outcome.rows_in = result.fetched
    outcome.rows_out = result.holdings_updated + result.items_raised + result.items_refreshed
    outcome.note(broker_account_id=broker_account_id, as_of=on.isoformat(), **result.as_detail())
    return result


# ---------------------------------------------------------------------------
# Reading the three sources
# ---------------------------------------------------------------------------


async def _load_account(session: AsyncSession, broker_account_id: int) -> BrokerAccount:
    """The account row, or a loud failure.

    The user is read from the row rather than passed in alongside it. Passing both would make a
    mismatch representable, and a holdings sync run against the wrong tenant's ledger is the
    single worst thing this module could do — the read-side of the two laws' clause that "the
    gateway refuses a mismatch".
    """
    account = (
        await session.execute(select(BrokerAccount).where(BrokerAccount.id == broker_account_id))
    ).scalar_one_or_none()
    if account is None:
        raise ValueError(
            f"broker account {broker_account_id} does not exist; a holdings sync cannot invent "
            "the tenant it is syncing for"
        )
    return account


async def _resolve_symbols(
    session: AsyncSession, fetched: Sequence[BrokerHoldingRecord]
) -> tuple[dict[int, Decimal], list[tuple[str, int]]]:
    """Fold the broker's rows onto ``instrument_id``, and name the symbols that do not resolve.

    Two rows can fold onto one instrument — the same name held under two products, or quoted on
    two exchanges — so quantities are summed. That is right for the physical position, which is
    what ``HoldingKey`` means, and it keeps the accounting identity honest: rows are counted as
    rows, positions as positions, and the two are reported separately.

    An unresolvable symbol is **counted and named**, never dropped. ``reconciliation_item``
    requires an ``instrument_id``, so there is no row we could write for it; what we can do is
    refuse to be quiet about it.
    """
    symbols = {record.symbol.upper() for record in fetched}
    mapped: dict[str, int] = {}
    if symbols:
        rows = await session.execute(
            select(Instrument.symbol, Instrument.id)
            .where(Instrument.symbol.in_(symbols))
            .order_by(Instrument.symbol, Instrument.id)
        )
        for symbol, instrument_id in rows.tuples():
            # First id wins, deterministically. The unique constraint is
            # (exchange_id, symbol, series), so one symbol under two series is permitted by the
            # schema; folding both onto one physical position is what the broker means anyway.
            mapped.setdefault(str(symbol).upper(), int(instrument_id))

    by_instrument: dict[int, Decimal] = {}
    unresolved: dict[str, int] = {}
    for record in fetched:
        resolved_id = mapped.get(record.symbol.upper())
        if resolved_id is None:
            unresolved[record.symbol.upper()] = unresolved.get(record.symbol.upper(), 0) + 1
            continue
        by_instrument[resolved_id] = (
            by_instrument.get(resolved_id, Decimal("0")) + record.total_quantity
        )
    return by_instrument, sorted(unresolved.items())


async def _recorded_positions(
    session: AsyncSession, user_id: int, broker_account_id: int
) -> dict[int, _Recorded]:
    """What this user's ledger records for this broker account, folded per instrument.

    Scoped by ``user_id`` as well as by account. The account already belongs to exactly one user,
    so the extra predicate is redundant against correct data — and it is exactly the predicate
    that turns a corrupt ``portfolio_holding.broker_account_id`` from a cross-tenant read into an
    empty result.
    """
    rows = await session.execute(
        select(
            PortfolioHolding.instrument_id,
            PortfolioHolding.portfolio_id,
            PortfolioHolding.portfolio_kind,
            PortfolioHolding.quantity,
            PortfolioHolding.avg_price,
        )
        .join(Portfolio, Portfolio.id == PortfolioHolding.portfolio_id)
        .where(
            PortfolioHolding.broker_account_id == broker_account_id,
            Portfolio.user_id == user_id,
        )
        .order_by(PortfolioHolding.instrument_id, PortfolioHolding.portfolio_id)
    )

    collected: dict[int, list[tuple[int, str, Decimal | None, Decimal | None]]] = {}
    for instrument_id, portfolio_id, kind, quantity, avg_price in rows.tuples():
        collected.setdefault(int(instrument_id), []).append(
            (int(portfolio_id), str(kind), quantity, avg_price)
        )

    recorded: dict[int, _Recorded] = {}
    for instrument_id, entries in collected.items():
        capital = sorted(
            (entry for entry in entries if entry[1] == PortfolioKind.CAPITAL),
            key=lambda entry: entry[0],
        )
        monitoring = [entry for entry in entries if entry[1] != PortfolioKind.CAPITAL]
        # 0035: THE POSITION IS THE SUM OF ITS CAPITAL SLICES. There used to be at most one
        # capital row — 0021's partial unique index guaranteed it and this code read its quantity
        # as the position. A holding filed 20/34/36 has three, and reading the first would have
        # reported 20 shares of a 90-share position, which the sync would then have seen as a
        # 70-share sell that never happened.
        slices = {entry[0]: entry[2] for entry in capital if entry[2] is not None}
        position_quantity: Decimal | None = (
            sum(slices.values(), Decimal("0"))
            if slices
            else _sole({entry[2] for entry in monitoring if entry[2] is not None})
        )
        # `inconsistent` compares the LENSES against the position, not the rows against each
        # other. Before 0035 every row carried the whole holding, so any disagreement was a data
        # error; now capital rows are meant to differ — they are slices — and comparing them
        # would flag every split holding the moment it was created.
        seen_by_lenses = {entry[2] for entry in monitoring if entry[2] is not None}
        recorded[instrument_id] = _Recorded(
            instrument_id=instrument_id,
            quantity=position_quantity,
            avg_price=capital[0][3] if capital else (entries[0][3] if entries else None),
            capital_slices=slices,
            portfolio_ids=tuple(entry[0] for entry in entries),
            inconsistent=bool(
                position_quantity is not None
                and seen_by_lenses
                and seen_by_lenses != {position_quantity}
            ),
        )
    return recorded


def _sole(quantities: set[Decimal]) -> Decimal | None:
    """The one quantity a position's rows agree on, or ``None`` when there is nothing to agree.

    Disagreement is reported (``_Recorded.inconsistent``) rather than averaged. The largest is
    taken because under-stating a position invents a sell, and inventing a sell is the one
    mistake §4.3 is written to prevent.
    """
    return max(quantities) if quantities else None


async def _open_items(
    session: AsyncSession, user_id: int, broker_account_id: int
) -> dict[int, dict[str, ReconciliationItem]]:
    """Open questions for this account, keyed by instrument and then by reason.

    One open question per (position, reason) is the invariant this run maintains: re-asking a
    question nobody has answered yet is noise, and a growing pile of identical rows would make
    the inbox useless exactly when it matters. A reason is part of the key because two different
    questions about one holding are two different questions.
    """
    rows = (
        (
            await session.execute(
                select(ReconciliationItem)
                .where(
                    ReconciliationItem.user_id == user_id,
                    ReconciliationItem.broker_account_id == broker_account_id,
                    ReconciliationItem.state == OPEN,
                )
                .order_by(ReconciliationItem.instrument_id, ReconciliationItem.id)
            )
        )
        .scalars()
        .all()
    )
    items: dict[int, dict[str, ReconciliationItem]] = {}
    for item in rows:
        # `setdefault` keeps the oldest row for a (position, reason) pair. Any duplicate that
        # predates this module is then left alone rather than silently merged away — it is a
        # human's inbox, and this task does not delete from it.
        items.setdefault(item.instrument_id, {}).setdefault(item.reason, item)
    return items


def _assemble(
    by_instrument: Mapping[int, Decimal],
    recorded: Mapping[int, _Recorded],
    open_items: Mapping[int, dict[str, ReconciliationItem]],
) -> dict[int, _Position]:
    """The union of all three sources — because a position can be missing from any one of them.

    A holding the broker stopped reporting is not in the fetch and is exactly the case that
    matters most (a full exit). A holding under an open question but recorded nowhere is not in
    ``portfolio_holding``. Iterating the fetch alone would miss both.
    """
    keys = set(by_instrument) | set(recorded) | set(open_items)
    return {
        instrument_id: _Position(
            instrument_id=instrument_id,
            broker_quantity=by_instrument.get(instrument_id, Decimal("0")),
            in_fetch=instrument_id in by_instrument,
            recorded=recorded.get(instrument_id),
            open_items=dict(open_items.get(instrument_id, {})),
        )
        for instrument_id in sorted(keys)
    }


def _ledger_view(
    broker_account_id: int, recorded: Mapping[int, _Recorded]
) -> tuple[list[Holding], list[Allocation]]:
    """Translate ``portfolio_holding`` into the pure ledger's own vocabulary.

    This is the whole boundary between the database and ``baskfy_core``. Two translations happen
    and both are load-bearing:

    * a **monitoring** row produces no :class:`Allocation` at all. A lens is not an allocation
      (§4.1), so a holding that appears only in monitoring views is Unallocated as far as the
      ledger is concerned — and ``attribute_sell`` will answer ``UNALLOCATED_HOLDING`` for it,
      which is exactly right;
    * a row with a NULL quantity produces no :class:`Holding`, because ``Holding`` requires a
      number and inventing a zero would turn "we do not know" into "they hold nothing".
    """
    holdings: list[Holding] = []
    allocations: list[Allocation] = []
    for entry in recorded.values():
        key = HoldingKey(instrument_id=entry.instrument_id, broker_account_id=broker_account_id)
        if entry.quantity is not None:
            holdings.append(Holding(key=key, quantity=entry.quantity, avg_price=entry.avg_price))
        # Unallocated is a REMAINDER since 10 Sep 2026, not a row: a holding filed nowhere simply
        # has no allocation, and `unallocated_quantity` derives the rest. Writing an explicit
        # `portfolio_id=None` row is now refused by `Allocation` itself.
        for portfolio_id, quantity in entry.capital_slices.items():
            if quantity > 0:
                allocations.append(
                    Allocation(key=key, portfolio_id=portfolio_id, quantity=quantity)
                )
    return holdings, allocations


# ---------------------------------------------------------------------------
# The verdict, one position at a time
# ---------------------------------------------------------------------------


async def _reconcile(  # noqa: PLR0913 - every argument is a distinct fact about one position
    session: AsyncSession,
    position: _Position,
    result: HoldingsSyncResult,
    *,
    holdings: Sequence[Holding],
    allocations: Sequence[Allocation],
    user_id: int,
    broker_account_id: int,
    on: dt.date,
) -> None:
    """Give one position exactly one verdict, and write whatever that verdict implies.

    The three-way split is ``believed`` against the broker's number, and nothing else. Every
    branch either increments one counter and writes nothing, or increments one counter and writes
    precisely what it decided — there is no path that changes a quantity *and* asks a question,
    because that would be attributing and doubting the same shares at once.
    """
    if position.recorded is not None and position.recorded.quantity is None:
        # A weight-based sleeve row carries no quantity. Nothing can be diffed against unknown,
        # and treating it as zero would report a sell of everything the user owns.
        result.skipped += 1
        return

    believed = position.believed_quantity
    broker_quantity = position.broker_quantity

    if broker_quantity == believed:
        result.unchanged += 1
        return

    key = HoldingKey(instrument_id=position.instrument_id, broker_account_id=broker_account_id)

    if broker_quantity > believed:
        # Shares we cannot account for. The stored question always restates the *whole* gap
        # between the record and the broker, so re-running never accumulates deltas.
        if position.recorded is None and not position.open_items:
            result.new += 1
        else:
            result.inflows_questioned += 1
        _raise_item(
            session,
            position,
            result,
            user_id=user_id,
            broker_account_id=broker_account_id,
            reason=ReconciliationReason.UNKNOWN_INFLOW,
            quantity=broker_quantity - position.recorded_quantity,
            # Suggested only when there is ONE portfolio to suggest (0035). A split holding has
            # no single answer, and pre-selecting the first of four would be a suggestion that is
            # wrong three times out of four — on a question about shares arriving, where a wrong
            # answer accepted in one click puts real money in the wrong return series.
            suggested=(None if position.recorded is None else position.recorded.sole_portfolio_id),
            on=on,
        )
        return

    sell = DetectedSell(key=key, quantity=believed - broker_quantity)
    attribution = attribute_sell(sell, holdings, allocations)
    if attribution.item is not None:
        result.sells_questioned += 1
        _raise_item(
            session,
            position,
            result,
            user_id=user_id,
            broker_account_id=broker_account_id,
            reason=ReconciliationReason(attribution.item.reason),
            quantity=attribution.item.quantity,
            suggested=attribution.item.suggested_portfolio_id,
            on=on,
        )
        return

    result.sells_attributed += 1
    await _apply_attributed_sell(
        session,
        position,
        result,
        sold=sell.quantity,
        broker_account_id=broker_account_id,
    )


async def _apply_attributed_sell(
    session: AsyncSession,
    position: _Position,
    result: HoldingsSyncResult,
    *,
    sold: Decimal,
    broker_account_id: int,
) -> None:
    """Move the recorded quantity down by the attributed amount — every row for this position.

    All of the position's rows move together, capital and monitoring alike, for the reason §4.5
    gives for corporate actions: a quantity that is true in one portfolio and stale in a lens
    over the same shares is two answers to one question. ``update`` over the row set rather than
    a loop of ORM mutations, so it is one statement and cannot half-apply.

    Down by ``sold``, not down *to* the broker's number: when an unrelated inflow question is
    also open, the broker's number includes shares this portfolio was never given, and writing it
    here would fold them into a return series nobody has approved.

    Scoped by ``broker_account_id`` as well as by instrument, because the same stock held at two
    brokers is two positions (§6.7) and one broker's sell must not move the other's rows.
    """
    if position.recorded is None:
        raise ValueError(
            f"instrument {position.instrument_id}: a sell was attributed to a portfolio but "
            "nothing is recorded to reduce; the ledger and this sync disagree"
        )
    remaining = (position.recorded_quantity - sold).quantize(
        QUANTITY_PLACES, rounding=ROUND_HALF_UP
    )
    await session.execute(
        update(PortfolioHolding)
        .where(
            PortfolioHolding.instrument_id == position.instrument_id,
            PortfolioHolding.broker_account_id == broker_account_id,
            PortfolioHolding.portfolio_id.in_(position.recorded.portfolio_ids),
        )
        .values(quantity=remaining)
    )
    result.holdings_updated += len(position.recorded.portfolio_ids)


def _raise_item(  # noqa: PLR0913 - a reconciliation item is defined by exactly these facts
    session: AsyncSession,
    position: _Position,
    result: HoldingsSyncResult,
    *,
    user_id: int,
    broker_account_id: int,
    reason: ReconciliationReason,
    quantity: Decimal,
    suggested: int | None,
    on: dt.date,
) -> None:
    """Ask the question, or re-state the one already open. Never a second identical row.

    This is where idempotency is *written down* rather than derived: an OPEN item for the same
    (position, reason) is updated in place with the current gap and today's date, so a nightly
    run produces one row per unanswered question no matter how many nights it runs. A resolved or
    dismissed item is never revived — a human answered it, and if the same gap reappears it is a
    new question about a new day.
    """
    existing = position.open_items.get(reason.value)
    rounded = quantity.quantize(QUANTITY_PLACES, rounding=ROUND_HALF_UP)
    if existing is not None:
        existing.quantity = rounded
        existing.suggested_portfolio_id = suggested
        existing.detected_on = on
        result.items_refreshed += 1
        return

    item = ReconciliationItem(
        user_id=user_id,
        instrument_id=position.instrument_id,
        broker_account_id=broker_account_id,
        quantity=rounded,
        reason=reason.value,
        state=OPEN,
        suggested_portfolio_id=suggested,
        detected_on=on,
    )
    session.add(item)
    position.open_items[reason.value] = item
    result.items_raised += 1


# ---------------------------------------------------------------------------
# Cash — §4.4
# ---------------------------------------------------------------------------


async def _sync_cash(  # noqa: PLR0913 - the session, the provider, the ref and both records
    session: AsyncSession,
    provider: HoldingsProvider,
    ref: BrokerAccountRef,
    outcome: StepOutcome,
    result: HoldingsSyncResult,
    *,
    on: dt.date,
) -> None:
    """Write the broker's reported cash into ``broker_cash`` — §4.4's Unallocated bucket.

    A ``None`` balance leaves the stored row untouched. ``broker_cash.balance`` is part of
    consolidated net worth (§6.2's hero row), so overwriting a real balance with a zero we
    inferred from a broker that publishes nothing would take money off the user's screen and
    still balance.

    The failure is re-raised, like the holdings fetch. Cash and positions are one picture of one
    moment; a run that stored today's positions against yesterday's cash would be a net worth
    nobody could reproduce.
    """
    try:
        balance = provider.broker_cash(ref)
    except ProviderError as exc:
        outcome.note(error=str(exc), stage="broker_cash")
        raise

    if balance is None:
        outcome.note(cash="the broker reported no cash balance")
        return

    stmt = insert(BrokerCash).values(
        broker_account_id=ref.broker_account_id,
        balance=money(balance),
        as_of=on,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[BrokerCash.broker_account_id],
            set_={"balance": stmt.excluded.balance, "as_of": stmt.excluded.as_of},
        )
    )
    result.cash_written = True
