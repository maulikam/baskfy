"""A session's plan: what the desk shows and a person confirms (``docs/vbt/04`` §9).

The plan is the only thing that becomes an order, and **a plan is not honest without its skips**:
every signal the rules passed over leaves a :class:`Skipped` with the reason that stopped it, in
the order ``04`` §9.1 checks them, because the order decides which reason a row carries.

Pure. This module builds dataclasses; the desk gives the plan a ``plan_id``, a 30-minute expiry
and a gateway, and a person presses Confirm (``02`` Track C §3 — there is no auto-execute flag
for this sleeve, and none is added).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final

from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, TICK_INR, Gate, VbtConfig
from baskfy_core.vbt.exits import Action, ExitReason, ManageAction, initial_stop
from baskfy_core.vbt.orders import WorkingOrder
from baskfy_core.vbt.sizing import SizeCap, SizedEntry, SizeRefusal, size_entry

_ZERO = Decimal(0)


class LineKind(StrEnum):
    """What a confirmed line does. Four, and no fifth: this sleeve has no trail and no partial."""

    #: Place the working limit at the signal bar's close (``04`` §7).
    PLACE_LIMIT = "PLACE_LIMIT"
    #: Sell a position whose close fell below its 21-day EMA, at the next open (``04`` §6.2).
    SELL_AT_OPEN = "SELL_AT_OPEN"
    #: Cancel a working order at the end of its third session (``04`` §7.2, VB7's sweep).
    CANCEL_LIMIT = "CANCEL_LIMIT"
    #: Arm a GTT for a filled position that has none — the backstop for non-negotiable 4.
    ARM_GTT = "ARM_GTT"


#: The order ``04`` §9.3 renders a plan in, and it is an argument rather than a convention: what
#: leaves the book first, then what stops being an order, then what protects a position that has
#: no stop, and only then what commits new money. A person reading down the page reads the risk
#: coming off before the risk going on.
LINE_ORDER: Final[tuple[LineKind, ...]] = (
    LineKind.SELL_AT_OPEN,
    LineKind.CANCEL_LIMIT,
    LineKind.ARM_GTT,
    LineKind.PLACE_LIMIT,
)


class SkipReason(StrEnum):
    """Why a signal did not become a line. ``04`` §9.1's table, in its order."""

    NO_SLEEVE_CAPITAL = "NO_SLEEVE_CAPITAL"
    GATE_SHUT = "GATE_SHUT"
    LOCKED_UPPER_CIRCUIT = "LOCKED_UPPER_CIRCUIT"
    ALREADY_HELD = "ALREADY_HELD"
    ALREADY_WORKING = "ALREADY_WORKING"
    SESSION_CAP = "SESSION_CAP"
    SLOTS_FULL = "SLOTS_FULL"
    STOP_NOT_BELOW_ENTRY = "STOP_NOT_BELOW_ENTRY"
    BELOW_MIN_TRADE_VALUE = "BELOW_MIN_TRADE_VALUE"
    TURNOVER_CAP = "TURNOVER_CAP"
    EXPOSURE_FULL = "EXPOSURE_FULL"


_REFUSAL_TO_SKIP: dict[SizeRefusal, SkipReason] = {
    SizeRefusal.NO_SLEEVE_CAPITAL: SkipReason.NO_SLEEVE_CAPITAL,
    SizeRefusal.STOP_NOT_BELOW_ENTRY: SkipReason.STOP_NOT_BELOW_ENTRY,
    SizeRefusal.BELOW_MIN_TRADE_VALUE: SkipReason.BELOW_MIN_TRADE_VALUE,
    SizeRefusal.TURNOVER_CAP: SkipReason.TURNOVER_CAP,
}


@dataclass(frozen=True, slots=True)
class Candidate:
    """One ``SIGNAL`` row, as the plan needs it. Levels are **exchange prices**."""

    instrument_id: int
    symbol: str
    signal_date: dt.date
    close_raw: Decimal
    turnover_avg_inr: Decimal | None
    rank_key: int
    locked_upper_circuit: bool = False


@dataclass(frozen=True, slots=True)
class Skipped:
    instrument_id: int
    symbol: str
    reason: SkipReason
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PlanLine:
    """One confirmable line. ``client_id`` is minted by the desk from ``plan_id:symbol:kind``."""

    kind: LineKind
    instrument_id: int
    symbol: str
    quantity: int
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    value_inr: Decimal = _ZERO
    cap: SizeCap | None = None
    reason: ExitReason | None = None
    note: str = ""

    def canonical(self) -> dict[str, str | int]:
        """The form ``plan_hash`` sees. Prices as strings of their exact decimal, never floats."""
        return {
            "kind": self.kind.value,
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "quantity": self.quantity,
            "limit_price": str(self.limit_price) if self.limit_price is not None else "",
            "stop_price": str(self.stop_price) if self.stop_price is not None else "",
        }


@dataclass(frozen=True, slots=True)
class VbtPlan:
    """Exits first, then cancels, then entries (``04`` §9.3)."""

    session: dt.date
    gate: Gate
    sleeve_equity_inr: Decimal
    lines: tuple[PlanLine, ...] = ()
    skips: tuple[Skipped, ...] = ()
    generated_from: str = "EVENING"

    @property
    def total_new_exposure_inr(self) -> Decimal:
        return sum(
            (line.value_inr for line in self.lines if line.kind is LineKind.PLACE_LIMIT), _ZERO
        )

    @property
    def entries(self) -> tuple[PlanLine, ...]:
        return tuple(line for line in self.lines if line.kind is LineKind.PLACE_LIMIT)

    def plan_hash(self) -> str:
        """sha256 of the canonical lines. **The same plan hashes the same.**"""
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


@dataclass(frozen=True, slots=True)
class BookState:
    """What the sleeve holds and what it has already done this session."""

    open_instrument_ids: frozenset[int] = frozenset()
    working_instrument_ids: frozenset[int] = frozenset()
    open_exposure_inr: Decimal = _ZERO
    cash_available_inr: Decimal = _ZERO
    entries_already_this_session: int = 0
    positions_naked_of_gtt: tuple[tuple[int, str, int, Decimal], ...] = field(default=())

    @property
    def slots_taken(self) -> int:
        """A working order holds a slot: the strategy bids and waits, and a book that could line
        eleven limits for ten slots would over-commit its cash the day they all filled."""
        return len(self.open_instrument_ids) + len(self.working_instrument_ids)


def to_tick(price: Decimal, tick: Decimal | None = None) -> Decimal:
    tick = tick or Decimal(TICK_INR)
    return (price / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick


def build_entries(  # noqa: PLR0913 - each input is a distinct gate the plan must consult
    candidates: list[Candidate],
    *,
    gate: Gate,
    equity: Decimal,
    book: BookState,
    config: VbtConfig = DEFAULT_VBT_CONFIG,
    slot_multiplier: Decimal = Decimal(1),
    max_open_positions: int | None = None,
    tick: Decimal | None = None,
) -> tuple[list[PlanLine], list[Skipped]]:
    """``04`` §9.1, in its order. Cash spent by an earlier line is not spent twice.

    ``max_open_positions`` is the trader's own ``vb_config`` cap; the plan takes the smaller of
    it and ``sizing.max_slots``, so lowering the setting lowers the book and raising it can never
    exceed the strategy's ten.
    """
    sizing = config.sizing
    tick = tick or Decimal(TICK_INR)
    slot_ceiling = min(max_open_positions or sizing.max_slots, sizing.max_slots)
    lines: list[PlanLine] = []
    skips: list[Skipped] = []
    cash = book.cash_available_inr
    exposure = book.open_exposure_inr
    lined = 0

    for candidate in sorted(candidates, key=lambda c: (-c.rank_key, c.symbol)):

        def skip(reason: SkipReason, detail: str = "", *, name: Candidate = candidate) -> None:
            skips.append(Skipped(name.instrument_id, name.symbol, reason, detail))

        if equity <= _ZERO:
            skip(SkipReason.NO_SLEEVE_CAPITAL, "the sleeve has no capital set")
            continue
        if gate is Gate.SHUT:
            skip(SkipReason.GATE_SHUT, "breadth is at or below the gate")
            continue
        if candidate.locked_upper_circuit:
            skip(SkipReason.LOCKED_UPPER_CIRCUIT, "the bar printed at the upper band")
            continue
        if candidate.instrument_id in book.open_instrument_ids:
            skip(SkipReason.ALREADY_HELD, "one position per name; never averaged down")
            continue
        if candidate.instrument_id in book.working_instrument_ids:
            skip(SkipReason.ALREADY_WORKING, "a limit is already resting in this name")
            continue
        if lined + book.entries_already_this_session >= sizing.max_new_entries_per_session:
            skip(
                SkipReason.SESSION_CAP,
                f"{sizing.max_new_entries_per_session} new entries a session",
            )
            continue
        if book.slots_taken + lined >= slot_ceiling:
            skip(SkipReason.SLOTS_FULL, f"{slot_ceiling} slots, positions and working orders")
            continue

        limit = to_tick(candidate.close_raw, tick)
        stop = _stop_for(limit, config, tick)
        sized = size_entry(
            equity=equity,
            cash_available=cash,
            limit_price=limit,
            stop_price=stop,
            turnover_avg_inr=candidate.turnover_avg_inr,
            config=sizing,
            slot_multiplier=slot_multiplier,
        )
        if not sized.placed:
            assert sized.refusal is not None
            skip(_REFUSAL_TO_SKIP[sized.refusal], _refusal_detail(sized))
            continue
        if exposure + sized.value_inr > equity:
            skip(SkipReason.EXPOSURE_FULL, "the sleeve's cash is fully committed")
            continue

        lines.append(_entry_line(candidate, sized))
        cash -= sized.value_inr
        exposure += sized.value_inr
        lined += 1
    return lines, skips


def _stop_for(limit: Decimal, config: VbtConfig, tick: Decimal) -> Decimal:
    return initial_stop(limit, config.exits, tick)


def _refusal_detail(sized: SizedEntry) -> str:
    if sized.refusal is SizeRefusal.TURNOVER_CAP:
        return "1% of the name's 20-day turnover cannot fund the minimum trade"
    if sized.refusal is SizeRefusal.BELOW_MIN_TRADE_VALUE:
        return "the sized value is below the minimum trade"
    if sized.refusal is SizeRefusal.STOP_NOT_BELOW_ENTRY:
        return "the stop is not below the limit"
    return "the sleeve has no capital set"


def _entry_line(candidate: Candidate, sized: SizedEntry) -> PlanLine:
    return PlanLine(
        kind=LineKind.PLACE_LIMIT,
        instrument_id=candidate.instrument_id,
        symbol=candidate.symbol,
        quantity=sized.quantity,
        limit_price=sized.limit_price,
        stop_price=sized.stop_price,
        value_inr=sized.value_inr,
        cap=sized.cap,
        note=(
            "limit at the signal close; "
            f"{sized.cap.value.lower() if sized.cap else 'nothing'} bound the size"
        ),
    )


def exit_lines(
    managed: list[tuple[int, str, int, ManageAction]],
    expired: list[tuple[WorkingOrder, str]],
    book: BookState,
) -> list[PlanLine]:
    """``04`` §9.2: sells, then cancels, then the GTT backstop."""
    lines: list[PlanLine] = []
    for instrument_id, symbol, quantity_open, action in managed:
        if action.action is Action.QUEUE_SELL_AT_OPEN:
            lines.append(
                PlanLine(
                    kind=LineKind.SELL_AT_OPEN,
                    instrument_id=instrument_id,
                    symbol=symbol,
                    quantity=quantity_open,
                    reason=action.reason,
                    note=action.note,
                )
            )
    for order, symbol in expired:
        lines.append(
            PlanLine(
                kind=LineKind.CANCEL_LIMIT,
                instrument_id=order.instrument_id,
                symbol=symbol,
                quantity=order.remaining,
                limit_price=order.limit_price,
                note="the third session has closed; the limit stops being an order",
            )
        )
    for instrument_id, symbol, quantity, stop in book.positions_naked_of_gtt:
        lines.append(
            PlanLine(
                kind=LineKind.ARM_GTT,
                instrument_id=instrument_id,
                symbol=symbol,
                quantity=quantity,
                stop_price=stop,
                note="a position without a resting stop is the one state the method forbids",
            )
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
) -> VbtPlan:
    """Exits first, then entries; the totals are over the entries (``04`` §9.3)."""
    lines = sorted([*exits, *entries], key=lambda line: (LINE_ORDER.index(line.kind), line.symbol))
    return VbtPlan(
        session=session,
        gate=gate,
        sleeve_equity_inr=equity,
        lines=tuple(lines),
        skips=tuple(skips),
        generated_from=source,
    )
