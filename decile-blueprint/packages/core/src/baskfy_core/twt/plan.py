"""A session's plan: what the desk shows and a person confirms (``docs/twt/04`` §10).

The plan is the only thing that becomes an order, and **a plan is not honest without its skips**:
every signal the rules passed over leaves a :class:`Skipped` with the reason that stopped it, in the
order ``04`` §10.1 checks them, because the order decides which reason a row carries.

Pure. This module builds dataclasses; the desk gives the plan a ``plan_id``, a 30-minute expiry and
a gateway, and **a person presses Confirm**. ``02`` Track C §3: a TWT order exists only because a
human pressed Confirm on an unexpired plan, there is no auto-execute flag for this sleeve, and none
is added — in particular the ratchet, which will want to fire on ten lines a night for months, is a
plan line a person confirms and never a background job that talks to the broker.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, TICK_INR, Gate, TwtConfig
from baskfy_core.twt.exits import initial_stop, stop_is_below_entry, tick_floor
from baskfy_core.twt.sizing import SizeCap, SizedEntry, SizeRefusal, size_entry

_ZERO = Decimal(0)


class LineKind(StrEnum):
    """What a confirmed line does (``03`` §7)."""

    #: A signal from last night: buy at the next session's open, at market (``04`` §5.1).
    BUY_AT_OPEN = "BUY_AT_OPEN"
    #: A filled position with no resting stop — the same-session GTT of non-negotiable 4, and the
    #: 15:15 sweep's re-arm.
    ARM_GTT = "ARM_GTT"
    #: **The ratchet** (``04`` §7.2): a position whose ``next_trigger`` exceeds its resting
    #: ``gtt_trigger``. The line carries the new trigger, the old one and ``high_since``, so the
    #: page can show the move rather than a number.
    RAISE_GTT_STOP = "RAISE_GTT_STOP"
    #: **Present in the schema, produced by nothing in TWT-1.** The strategy has no end-of-day sell
    #: rule; the GTT is the exit (``01`` §5). The kind exists so a person can be given a line for a
    #: ``MANUAL`` exit without a migration, and TW10 asserts that no TWT rule ever emits one — a
    #: check the swing and VBT packs did not need and this one does, because the shape it copied
    #: has such a rule and copying shapes is how rules get imported by accident.
    SELL_AT_OPEN = "SELL_AT_OPEN"


#: The order ``04`` §10.3 renders a plan in, and it is an argument rather than a convention: what
#: leaves the book first, then what protects a position that has no stop at all, then what tightens
#: a stop that has one, and only then what commits new money. A person reading down the page reads
#: the risk coming off before the risk going on.
LINE_ORDER: Final[tuple[LineKind, ...]] = (
    LineKind.SELL_AT_OPEN,
    LineKind.ARM_GTT,
    LineKind.RAISE_GTT_STOP,
    LineKind.BUY_AT_OPEN,
)

#: The kinds :func:`exit_lines` is allowed to produce. ``BUY_AT_OPEN`` is an entry and
#: ``SELL_AT_OPEN`` is nobody's — ``03`` §7 and TW10.
EXIT_LINE_KINDS: Final[frozenset[LineKind]] = frozenset({LineKind.ARM_GTT, LineKind.RAISE_GTT_STOP})


class SkipReason(StrEnum):
    """Why a signal did not become a line. ``04`` §10.1's order is the order they are checked in,
    and ``03`` §7 check-constrains ``tw_plan_skip.reason`` to exactly these."""

    GATE_SHUT = "GATE_SHUT"
    ALREADY_HELD = "ALREADY_HELD"
    BELOW_LIQUIDITY_FLOOR = "BELOW_LIQUIDITY_FLOOR"
    NO_BAR = "NO_BAR"
    LOCKED_UPPER_CIRCUIT = "LOCKED_UPPER_CIRCUIT"
    SESSION_CAP = "SESSION_CAP"
    SLOTS_FULL = "SLOTS_FULL"
    NO_SLEEVE_CAPITAL = "NO_SLEEVE_CAPITAL"
    STOP_NOT_BELOW_ENTRY = "STOP_NOT_BELOW_ENTRY"
    BELOW_MIN_TRADE_VALUE = "BELOW_MIN_TRADE_VALUE"
    EXPOSURE_FULL = "EXPOSURE_FULL"
    #: In the schema because ``03`` §7 constrains the column to it, and **emitted by nothing**:
    #: ``04`` §10.1 says the turnover cap alone binding is *not* a skip, it is a smaller line with
    #: a note, and a line too small to clear the floor is ``BELOW_MIN_TRADE_VALUE`` whichever cap
    #: made it small. The reason exists so a person can record one by hand.
    TURNOVER_CAP = "TURNOVER_CAP"


_REFUSAL_TO_SKIP: dict[SizeRefusal, SkipReason] = {
    SizeRefusal.NO_SLEEVE_CAPITAL: SkipReason.NO_SLEEVE_CAPITAL,
    SizeRefusal.STOP_NOT_BELOW_ENTRY: SkipReason.STOP_NOT_BELOW_ENTRY,
    SizeRefusal.BELOW_MIN_TRADE_VALUE: SkipReason.BELOW_MIN_TRADE_VALUE,
}


@dataclass(frozen=True, slots=True)
class EntryBar:
    """The session the order would fill in, when it is known.

    The **evening** plan is built after the signal session's close, for tomorrow's open, and
    tomorrow has no bar yet: it passes ``None`` and the two bar-shaped skips below cannot fire. The
    backtest and TW9 know the bar and pass it. ``04`` §11.3's rule is unaffected either way — a plan
    rebuilt in the morning reads the **same** signal session; it re-sizes, it does not re-detect.
    """

    session: dt.date
    #: ``None`` means the instrument printed no bar: ``NO_BAR`` (``04`` §5.2).
    open: Decimal | None
    #: ``open == high == low``: limit-locked at the open, and no fill is possible at a price the
    #: book would accept (``04`` §5.2).
    limit_locked: bool = False


@dataclass(frozen=True, slots=True)
class Candidate:
    """One entry event, as the plan needs it. Levels are **exchange prices**."""

    instrument_id: int
    symbol: str
    signal_date: dt.date
    #: ``04`` §6.3 and DECISIONS-TW TW0.2 — the **signal session's own** rupee turnover.
    rank_key: Decimal
    #: The 20-session average turnover of the signal session. ``None`` when the window has no
    #: value under ``04`` §2.2's tolerance, which fails the liquidity floor.
    turnover_avg_inr: Decimal | None
    #: The price the line is sized against. The evening plan previews against the signal session's
    #: ``close_raw``; the backtest and the morning re-size pass the entry session's open, plus
    #: costs where the caller models them (``04`` §5.3).
    entry_price: Decimal
    #: The price ``04`` §7.1's stop is computed from — the exchange's **open**, never the
    #: cost-inclusive book entry. The evening plan previews it from the signal close.
    stop_reference_price: Decimal
    entry_bar: EntryBar | None = None


@dataclass(frozen=True, slots=True)
class Skipped:
    instrument_id: int
    symbol: str
    reason: SkipReason
    detail: str = ""


@dataclass(frozen=True, slots=True)
class NakedPosition:
    """An open position with no resting stop — the one state the method forbids (``04`` §10.2)."""

    instrument_id: int
    symbol: str
    quantity: int
    stop_price: Decimal


@dataclass(frozen=True, slots=True)
class RatchetDue:
    """A position whose evening arithmetic produced a higher trigger (``04`` §7.2, §10.2)."""

    instrument_id: int
    symbol: str
    quantity: int
    gtt_trigger: Decimal
    next_trigger: Decimal
    #: The session ``next_trigger`` was computed for. **A plan never reads a trigger computed for
    #: another session** (``03`` §5): a stale trigger is a stop derived from a price that is no
    #: longer the highest high.
    next_trigger_for: dt.date
    high_since: Decimal
    note: str = ""


@dataclass(frozen=True, slots=True)
class PlanLine:
    """One confirmable line. ``client_id`` is minted by the desk — :func:`client_id_for`."""

    kind: LineKind
    instrument_id: int
    symbol: str
    quantity: int
    entry_price: Decimal | None = None
    stop_price: Decimal | None = None
    previous_stop: Decimal | None = None
    high_since: Decimal | None = None
    value_inr: Decimal = _ZERO
    cap: SizeCap | None = None
    note: str = ""

    def canonical(self) -> dict[str, str | int]:
        """The form ``plan_hash`` sees. Prices as strings of their exact decimal, never floats."""
        return {
            "kind": self.kind.value,
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "quantity": self.quantity,
            "entry_price": str(self.entry_price) if self.entry_price is not None else "",
            "stop_price": str(self.stop_price) if self.stop_price is not None else "",
        }


@dataclass(frozen=True, slots=True)
class BookState:
    """What the sleeve holds and what it has already done this session."""

    open_instrument_ids: frozenset[int] = frozenset()
    open_exposure_inr: Decimal = _ZERO
    cash_available_inr: Decimal = _ZERO
    #: ``04`` §6.3 — the session's already-confirmed or sent orders, whatever plan they came from,
    #: so a fourth confirm of an evening is a refusal rather than a surprise.
    entries_already_this_session: int = 0
    positions_naked_of_gtt: tuple[NakedPosition, ...] = ()
    ratchets_due: tuple[RatchetDue, ...] = ()

    @property
    def slots_taken(self) -> int:
        """Open positions. **There are no working orders in this sleeve** to hold a slot (``03``
        §6): TWT-1 buys at the next open, at market, so an entry is either a position or nothing."""
        return len(self.open_instrument_ids)


@dataclass(frozen=True, slots=True)
class TwtPlan:
    """Exits first, then entries (``04`` §10.3)."""

    session: dt.date
    gate: Gate
    sleeve_equity_inr: Decimal
    lines: tuple[PlanLine, ...] = ()
    skips: tuple[Skipped, ...] = ()
    generated_from: str = "EVENING"

    @property
    def entries(self) -> tuple[PlanLine, ...]:
        return tuple(line for line in self.lines if line.kind is LineKind.BUY_AT_OPEN)

    @property
    def total_new_exposure_inr(self) -> Decimal:
        return sum((line.value_inr for line in self.entries), _ZERO)

    def plan_hash(self) -> str:
        """sha256 of the canonical lines. **The same plan hashes the same** (``04`` §10.3)."""
        payload = json.dumps(
            {
                "session": self.session.isoformat(),
                "gate": self.gate.value,
                "lines": [line.canonical() for line in self.lines],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def client_id_for(plan_id: str, symbol: str, kind: LineKind) -> str:
    """``04`` §10.4's idempotency key, so a re-posted plan cannot double-send.

    ``plan_id:symbol`` for an order and ``plan_id:symbol:kind`` for a GTT leg — the desk's own
    convention (non-negotiable 6), spelled once here so the route and the journal cannot disagree.
    """
    if kind in EXIT_LINE_KINDS:
        return f"{plan_id}:{symbol}:{kind.value}"
    return f"{plan_id}:{symbol}"


def stop_for(reference_price: Decimal, config: TwtConfig, tick: Decimal) -> Decimal:
    """``04`` §7.1's initial stop, from the **exchange's** price (``04`` §5.3)."""
    return initial_stop(reference_price, config.exits, tick)


def build_entries(  # noqa: PLR0913 - each input is a distinct gate the plan must consult
    candidates: list[Candidate],
    *,
    gate: Gate,
    equity: Decimal,
    book: BookState,
    config: TwtConfig = DEFAULT_TWT_CONFIG,
    slot_multiplier: Decimal = Decimal(1),
    max_open_positions: int | None = None,
    tick: Decimal | None = None,
) -> tuple[list[PlanLine], list[Skipped]]:
    """``04`` §10.1, in its order. **Cash spent by earlier lines is not spent twice.**

    Signals are sorted by ``rank_key`` descending then ``symbol`` ascending — a total order, so two
    builds of the same session produce the same plan and the same hash.

    Then, for each, in order: the gate; the name already held (**the book never averages down**);
    the liquidity floor; no bar on the entry session; limit-locked at the open; the session cap;
    the slots; the sleeve's capital; the minimum trade value; the exposure ceiling. The turnover
    cap alone binding is **not** a skip — it is a smaller line with a note.

    ``max_open_positions`` is the trader's own ``tw_config`` cap and it governs, defaulting to
    ``sizing.max_slots`` [10]. ``02`` bounds it at 15 from above, and the slot *size* stays a tenth
    of equity, so a book run at more than ten names is capped by its own cash rather than by a
    silently smaller slot. (VBT-1 takes the minimum of the two instead; there the setting is the
    weaker of a pair, and here ``04`` §10.1 names the setting.)
    """
    sizing = config.sizing
    tick_size = tick if tick is not None else Decimal(TICK_INR)
    slot_ceiling = max_open_positions if max_open_positions is not None else sizing.max_slots
    lines: list[PlanLine] = []
    skips: list[Skipped] = []
    cash = book.cash_available_inr
    exposure = book.open_exposure_inr
    lined = 0

    for candidate in sorted(candidates, key=lambda c: (-c.rank_key, c.symbol)):
        refused = _before_sizing(
            candidate,
            gate=gate,
            equity=equity,
            book=book,
            lined=lined,
            slot_ceiling=slot_ceiling,
            config=config,
        )
        if refused is not None:
            skips.append(Skipped(candidate.instrument_id, candidate.symbol, *refused))
            continue

        stop = stop_for(candidate.stop_reference_price, config, tick_size)
        if not stop_is_below_entry(candidate.stop_reference_price, stop):
            skips.append(_skip(candidate, SkipReason.STOP_NOT_BELOW_ENTRY, _STOP_NOT_BELOW))
            continue
        sized = size_entry(
            equity=equity,
            cash_available=cash,
            entry_price=candidate.entry_price,
            stop_price=stop,
            turnover_avg_inr=candidate.turnover_avg_inr,
            config=sizing,
            slot_multiplier=slot_multiplier,
        )
        if not sized.placed:
            assert sized.refusal is not None
            skips.append(_skip(candidate, _REFUSAL_TO_SKIP[sized.refusal], _refusal_detail(sized)))
            continue
        if exposure + sized.value_inr > equity:
            skips.append(
                _skip(candidate, SkipReason.EXPOSURE_FULL, "the sleeve's cash is fully committed")
            )
            continue

        lines.append(_entry_line(candidate, sized))
        cash -= sized.value_inr
        exposure += sized.value_inr
        lined += 1
    return lines, skips


def _skip(candidate: Candidate, reason: SkipReason, detail: str) -> Skipped:
    return Skipped(candidate.instrument_id, candidate.symbol, reason, detail)


_STOP_NOT_BELOW = "the stop is not below the entry"


def _before_sizing(  # noqa: PLR0911, PLR0913 - `04` §10.1 is a list of refusals, in its order
    candidate: Candidate,
    *,
    gate: Gate,
    equity: Decimal,
    book: BookState,
    lined: int,
    slot_ceiling: int,
    config: TwtConfig,
) -> tuple[SkipReason, str] | None:
    """``04`` §10.1's checks that depend on nothing the sizing produces, **in its order**.

    One early return per clause and no ``elif``, because the document is a list and the order is the
    contract: a name that is both already held and below the liquidity floor is ``ALREADY_HELD``,
    since that is the first thing a person needs to know about it.
    """
    sizing = config.sizing
    if gate is Gate.SHUT:
        return SkipReason.GATE_SHUT, "breadth is at or below the gate"
    if candidate.instrument_id in book.open_instrument_ids:
        return SkipReason.ALREADY_HELD, "one position per name; the book never averages down"
    floor_inr = config.entry.min_turnover_inr
    if candidate.turnover_avg_inr is None or candidate.turnover_avg_inr < floor_inr:
        return (
            SkipReason.BELOW_LIQUIDITY_FLOOR,
            f"20-session average turnover under the floor of {floor_inr}",
        )
    if candidate.entry_bar is not None and candidate.entry_bar.open is None:
        return SkipReason.NO_BAR, "the instrument printed no bar on the entry session"
    if candidate.entry_bar is not None and candidate.entry_bar.limit_locked:
        return (
            SkipReason.LOCKED_UPPER_CIRCUIT,
            "open == high == low: no fill is possible at a price the book would accept",
        )
    if lined + book.entries_already_this_session >= sizing.max_new_entries_per_session:
        return SkipReason.SESSION_CAP, f"{sizing.max_new_entries_per_session} new entries a session"
    if book.slots_taken + lined >= slot_ceiling:
        return SkipReason.SLOTS_FULL, f"{slot_ceiling} slots, all of them held"
    if equity <= _ZERO:
        return SkipReason.NO_SLEEVE_CAPITAL, "the sleeve has no capital set"
    return None


def _refusal_detail(sized: SizedEntry) -> str:
    if sized.refusal is SizeRefusal.BELOW_MIN_TRADE_VALUE:
        return "the sized value is below the minimum trade"
    if sized.refusal is SizeRefusal.STOP_NOT_BELOW_ENTRY:
        return "the stop is not below the entry"
    return "the sleeve has no capital set"


def _entry_line(candidate: Candidate, sized: SizedEntry) -> PlanLine:
    bound = sized.cap.value.lower() if sized.cap else "nothing"
    return PlanLine(
        kind=LineKind.BUY_AT_OPEN,
        instrument_id=candidate.instrument_id,
        symbol=candidate.symbol,
        quantity=sized.quantity,
        entry_price=sized.entry_price,
        stop_price=sized.stop_price,
        value_inr=sized.value_inr,
        cap=sized.cap,
        note=f"buy at the next open, at market; {bound} bound the size",
    )


def exit_lines(book: BookState, session: dt.date) -> list[PlanLine]:
    """``04`` §10.2, and **nothing else**.

    For every ``OPEN`` position: no ``gtt_id`` while ``quantity_open > 0`` is an ``ARM_GTT``; a
    ``next_trigger`` that exceeds the resting ``gtt_trigger`` **and was computed for the session
    just closed** is a ``RAISE_GTT_STOP``.

    In particular this function can emit **no ``SELL_AT_OPEN``** — TWT-1 has no end-of-day sell
    rule, the GTT is the exit, and TW10 asserts the absence rather than trusting it (``03`` §7).

    A trigger at or below the resting one is not a line either. **A stop never falls**, and this is
    the second of the three places that say so — the first is the ``max`` inside the ratchet, the
    third is the desk's own refusal of a ``RAISE_GTT_STOP`` at or below the resting trigger.
    """
    lines: list[PlanLine] = [
        PlanLine(
            kind=LineKind.ARM_GTT,
            instrument_id=naked.instrument_id,
            symbol=naked.symbol,
            quantity=naked.quantity,
            stop_price=naked.stop_price,
            note="a position without a resting stop is the one state the method forbids",
        )
        for naked in book.positions_naked_of_gtt
    ]
    lines.extend(
        PlanLine(
            kind=LineKind.RAISE_GTT_STOP,
            instrument_id=due.instrument_id,
            symbol=due.symbol,
            quantity=due.quantity,
            stop_price=due.next_trigger,
            previous_stop=due.gtt_trigger,
            high_since=due.high_since,
            note=due.note or f"the trail ratcheted on {due.next_trigger_for.isoformat()}",
        )
        for due in book.ratchets_due
        if due.next_trigger_for == session and due.next_trigger > due.gtt_trigger
    )
    return lines


def assemble(  # noqa: PLR0913, PLR0917 - a plan is its session, its gate, its money and its lines
    session: dt.date,
    gate: Gate,
    equity: Decimal,
    exits: list[PlanLine],
    entries: list[PlanLine],
    skips: list[Skipped],
    source: str = "EVENING",
) -> TwtPlan:
    """Exits first, then entries; the totals are over the entries (``04`` §10.3)."""
    lines = sorted([*exits, *entries], key=lambda line: (LINE_ORDER.index(line.kind), line.symbol))
    return TwtPlan(
        session=session,
        gate=gate,
        sleeve_equity_inr=equity,
        lines=tuple(lines),
        skips=tuple(skips),
        generated_from=source,
    )


def gtt_limit_price(trigger: Decimal, config: TwtConfig, tick: Decimal | None = None) -> Decimal:
    """``04`` §10.6: the resting LIMIT a TWT GTT fires, 3 % under its trigger.

    A GTT fires a LIMIT order and Maulik uses market stops, so the limit sits far enough under the
    trigger to fill on the way down the way a market stop would. The weekly book keeps the
    gateway's own 0.995 and never sees this number; the keyword is additive (Track C §8).
    """
    tick_size = tick if tick is not None else Decimal(TICK_INR)
    return tick_floor(trigger * config.exits.gtt_limit_fraction, tick_size)
