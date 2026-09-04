"""The swing book's execute logic — one confirmed plan line becomes orders, a stop, and rows.

This is SW7's half that holds no page: ``app/swing_desk.py`` owns the route and the Postgres
store, this module owns what happens between "confirm" and the book. It is written against the
:class:`SwingStore` protocol so that the same code runs over the desk's Postgres adapter on a
weekday morning and over an in-memory dict in a test, and it is handed its gateway rather than
building one, so a test can prove the whole path with a broker client that explodes on contact.

THE RULES IT ENFORCES, AND WHERE THEY COME FROM

* ``docs/swing/02`` Track C §3 — no auto-execution. Nothing here runs without ``confirm="true"``,
  a ``plan_id`` issued in the last thirty minutes and a ``line_id`` still ``PROPOSED``; the
  three refusals are the desk's own 400 / 404 / 410 (``app/main.py:520-525``) plus a 409 for a
  line that was already confirmed, so a re-posted form cannot send twice even before the
  gateway's idempotency map is consulted.
* Track C §5 — the sleeve never sells a holding it did not buy. A ``SELL_AT_OPEN`` is checked
  against ``sw_position.quantity_open``, never against the broker's holdings, which contain the
  weekly momentum book as well.
* ``docs/swing/04`` §6.5 — a stop never falls. A ``RAISE_GTT_STOP`` below the resting trigger is
  ``BLOCKED`` here, before the gateway sees it.
* ``docs/swing/04`` §9.4 — every ``BUY_ON_TRIGGER`` is a LIMIT buy at the trigger, and its GTT
  is armed in the same call, with the swing band (PACK.3) rather than the weekly book's.
* ``docs/swing/02`` Track B — with ``BASKFY_SWING_EXECUTION_ENABLED=false`` the SAME code path
  runs through the gateway's dry-run branch and journals ``simulated=true`` whatever ``DRY_RUN``
  says (:func:`swing_gates`). There is no second branch for paper trading: the twenty paper
  sessions the real-money gate counts are a rehearsal of this code.
* ``docs/swing/04`` §5.3, §8.4, §9.1 at the moment of the confirm (STANDING-ANSWERS A5, SW10.4)
  — a plan line's size is a **preview**; the confirm is the gate. Under a row lock on the day's
  ``sw_session`` the book is re-derived (open positions at cost + every BUY line confirmed today
  that is not a position yet + cash) and the BUY is re-sized through the same ``build_entries``
  the plan used, against the rung's exposure ceiling, the position count (``min(rung,
  max_open_positions)``) and the per-session entry cap. A line that no longer fits is shrunk to
  the ceiling and written back with a note; one that cannot be lined at all is ``BLOCKED`` with
  ``EXPOSURE_FULL`` / ``TIER_FULL`` / ``SESSION_CAP`` / ``SIZE_REFUSED`` and never sent. The
  lock spans the gateway call, so two browser tabs cannot confirm past the ceiling together.

* STANDING-ANSWERS A7 (SW10.5) — a ``PENDING_RANGE`` line (a live gap on the MORNING plan with
  no quantity and no stop) is not executable: it is refused with a 400 before anything is read,
  the route refuses it too, and :data:`EXECUTABLE_KINDS` is the source-level set.
* A8, **as amended by Maulik on 4 Sep 2026** (STANDING-ANSWERS B16: the later letter wins) —
  the cap is unchanged, the order type is not. ``min(trigger x 1.005, range_high + 0.25 x ADR)``
  (:func:`baskfy_core.swing.plan.marketable_limit`) is still the most this setup is worth
  paying; it is now sent as Kite's ``market_protection`` on a **MARKET** order rather than as a
  resting LIMIT price, because a LIMIT the tape runs past does not fill and the breakout is
  missed. A live price is read per confirm; a price already at or above the cap is refused
  outright rather than chased (:func:`baskfy_core.swing.plan.market_protection_pct`). The
  request then
  polls the order for up to ``fill_poll_seconds`` [10] at ``fill_poll_interval_seconds`` [0.5]
  through an injectable :class:`OrderSource` and clock: COMPLETE → the position, its fill and
  its GTT in the same request; partial or open → ``SENT`` with the quantity filled so far and
  a GTT for that quantity if it is above zero. Later fills arrive through
  :func:`on_order_update` (the postback handler), which grows the position and **modifies**
  the GTT's quantity — never a second GTT — and is idempotent on a repeated update. At 10:45
  :func:`cutoff_open_orders` cancels whatever is still open and frees the pending-range slots
  nothing claimed. The dry-run branch follows the same path: the gateway's simulated order is
  a complete fill at the trigger, ``simulated=true``.
* A9 — the first live sessions run at **half risk at plan time**: ``risk_multiplier`` 0.5 is
  applied to ``risk_per_trade_pct`` before ``size_position`` (:func:`sizing_config`), only while
  ``sw_config.first_live_sessions_left`` is above zero AND the order would be real. Nothing
  here halves a quantity at send time, nothing here counts the sessions down — the evening
  job does, once per LIVE session — and a SELL or a RAISE is never touched. ``sw_position.
  half_risk`` tags the entry for the journal.

Every order — the buy, the market sell, the stop, its cancellation, its re-sizing and the
cancel of an open remainder — goes through the :class:`OrderGateway` this module is handed.
Nothing here names a broker method.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Protocol

from baskfy_core.swing.config import (
    DEFAULT_SWING_CONFIG,
    TRADEABLE_SETUPS,
    Setup,
    SizingConfig,
    SwingConfig,
)
from baskfy_core.swing.journal import ClosedTrade, exit_average
from baskfy_core.swing.plan import (
    EXECUTABLE_KINDS as _CORE_EXECUTABLE_KINDS,
)
from baskfy_core.swing.plan import (
    SkipReason,
    WatchItem,
    first_live_multiplier,
    market_protection_pct,
    marketable_limit,
)
from baskfy_core.swing.sizing import r_multiple
from baskfy_execution.gateway import ORDER_CANCELLED_STATUSES
from baskfy_execution.gtt import (
    DRY_RUN_GTT_DELETE,
    DRY_RUN_GTT_MODIFY,
    GTT_DELETED_STATUSES,
    GTT_MODIFIED_STATUSES,
    GTT_PLACED,
    GTT_PLACED_STATUSES,
    StopBand,
)
from baskfy_execution.tenancy import TenantIds
from fastapi import HTTPException

from . import config as C
from . import telemetry as _tel
from .core import gateway as _gateway_module
from .core.gateway import OrderGateway, ProductGates
from .swing_monitor import SignalContext, entries_now

log = logging.getLogger("swing.execute")


# --- SW11: observability that cannot reach the order path -----------------------------------
#
# ``app.telemetry``'s helpers already swallow their own failures; these wrappers exist so that
# even a helper that has been replaced, or a sink that raises through, cannot stop a confirm.
# ``tests/test_swing_execute.py::test_telemetry_never_raises_into_execute_line`` hands them a
# sink that raises on every call and asserts the line is still filled.


def _span(name: str, **attributes: object) -> contextlib.AbstractContextManager[object]:
    try:
        return _tel.span(name, **attributes)
    except Exception:  # noqa: BLE001 - a span is never a reason not to trade
        log.debug("span %s unavailable", name, exc_info=True)
        return contextlib.nullcontext()


def _observe_confirm(*, kind: str, outcome: str, seconds: float) -> None:
    try:
        _tel.count("swing_confirms", kind=kind, outcome=outcome)
        _tel.observe("swing_confirm_seconds", seconds)
    except Exception:  # noqa: BLE001 - a metric is never a reason not to trade
        log.debug("swing confirm metrics unavailable", exc_info=True)


def _capture(exc: BaseException, **context: object) -> None:
    try:
        _tel.capture(exc, **context)
    except Exception:  # noqa: BLE001 - error capture failing is not a second error
        log.debug("swing error capture unavailable", exc_info=True)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

#: The swing book's own journal file, beside the weekly book's. The desk's execution report and
#: its options page read ``orders_journal.jsonl`` as the record of what the WEEKLY book did;
#: twenty sessions of simulated swing fills in the same file would make that record answer the
#: wrong question. Same directory, so the conftest fixture that isolates the journal in tests
#: (``tests/conftest.py::isolate_order_journal``) isolates this one too.
SWING_JOURNAL_NAME = "swing_orders_journal.jsonl"

#: ``sw_plan_line.kind`` values this module knows how to execute.
BUY_ON_TRIGGER = "BUY_ON_TRIGGER"
SELL_AT_OPEN = "SELL_AT_OPEN"
RAISE_GTT_STOP = "RAISE_GTT_STOP"
#: A7: a live gap on the MORNING plan — no quantity, no stop, a reserved slot. Never executed.
PENDING_RANGE = "PENDING_RANGE"
#: The kinds a confirm may turn into an order — the source-level set, written out here and
#: asserted equal to the core's ``EXECUTABLE_KINDS`` so neither can grow without the other.
EXECUTABLE_KINDS: frozenset[str] = frozenset({BUY_ON_TRIGGER, SELL_AT_OPEN, RAISE_GTT_STOP})
assert EXECUTABLE_KINDS == {k.value for k in _CORE_EXECUTABLE_KINDS}  # noqa: S101 - import-time contract
assert PENDING_RANGE not in EXECUTABLE_KINDS  # noqa: S101

#: The only two order types the swing book can ever send (Track C §1/§2, asserted from the AST
#: by `test_swing_track_c.py`). `OpeningRangeConfig.entry_order_type` picks between them —
#: MARKET since 4 Sep 2026, LIMIT is A8's original and one setting away.
ENTRY_ORDER_TYPES: Final[frozenset[str]] = frozenset({"LIMIT", "MARKET"})

#: Kite's order statuses, as the postback and the order book report them (A8).
ORDER_COMPLETE = "COMPLETE"
ORDER_OPEN_STATUSES: frozenset[str] = frozenset({"OPEN", "TRIGGER PENDING", "PUT ORDER REQ RECEIVED",
                                                 "VALIDATION PENDING", "OPEN PENDING",
                                                 "MODIFY PENDING", "MODIFY VALIDATION PENDING"})
ORDER_DEAD_STATUSES: frozenset[str] = frozenset({"REJECTED", "CANCELLED", "CANCEL PENDING"})

#: ``sw_position.close_reason`` accepts an ``ActionReason`` value or ``MANUAL`` (``03`` §7); a
#: SELL line's ``note`` carries the rule that produced it (``plan.exit_lines``), so a line whose
#: note is one of these closes with that reason and anything else closes as ``MANUAL``.
_KNOWN_CLOSE_REASONS: frozenset[str] = frozenset(
    {
        "PARTIAL_INTO_STRENGTH",
        "BREAKEVEN_AFTER_PARTIAL",
        "BREAKEVEN_AT_R",
        "CLOSE_BELOW_TRAIL_MA",
        "EP_FAILED_RED_ON_DAY",
        "HARD_STOP_HIT",
        "NOTHING_TO_DO",
    }
)

#: A simulated GTT id, as the gateway mints it (``DRY-<client_id>``). There is nothing at the
#: exchange under it, so it is never handed to ``delete_gtt`` — which would (rightly) refuse a
#: non-integer id and journal a block for a trigger that does not exist.
_SIMULATED_GTT_PREFIX = "DRY-"

#: The gateway's own statuses, mapped onto :class:`ExecOutcome.status` (contract C1).
_ORDER_STATUS = {
    "DRY_RUN": "SIMULATED",
    "PLACED": "SENT",
    "BLOCKED": "BLOCKED",
    "RISK_BLOCKED": "BLOCKED",
    "DUPLICATE": "BLOCKED",
    "REJECTED": "REJECTED",
    "ERROR": "REJECTED",
}

_TWO_DP = Decimal("0.01")
_FOUR_DP = Decimal("0.0001")
_ZERO = Decimal(0)
_ONE = Decimal(1)


# --- the contract surface (C1) ------------------------------------------------------------


class SwingStore(Protocol):
    """One user's sw_ rows. Implemented by app/swing_desk.py (Postgres via the desk's
    analytics.pg adapter with `?` placeholders; sqlite in tests). All prices Decimal."""

    def plan(self, plan_id: str) -> dict | None:
        """keys: id(int pk), plan_id(str uuid), as_of(date), source, built_at(datetime,
        tz-aware), expires_at(datetime, tz-aware), gate, exposure_level"""
        ...

    def line(self, line_id: int) -> dict | None:
        """keys: id, plan_pk(int), plan_id(str uuid), kind, instrument_id, symbol, setup,
        quantity(int), trigger(Decimal|None), stop(Decimal|None), risk_inr, position_value,
        trail, note, state, client_id"""
        ...

    def set_line(
        self,
        line_id: int,
        *,
        state: str,
        journal_ref: str | None = None,
        position_id: int | None = None,
    ) -> None: ...

    def open_position_for(self, instrument_id: int) -> dict | None:
        """the OPEN/PARTIAL position with quantity_open > 0 for this instrument, or None"""
        ...

    def position(self, position_id: int) -> dict | None:
        """keys: id, instrument_id, symbol, setup, entry_date, entry_avg, quantity_entered,
        quantity_open, initial_stop, stop, gtt_id, gtt_trigger, gtt_armed_at, trail,
        partial_done, partial_date, state, closed_on, exit_avg, close_reason, r_multiple,
        pnl_inr, simulated"""
        ...

    def create_position(self, fields: dict) -> int:
        """returns the new id"""
        ...

    def update_position(self, position_id: int, fields: dict) -> None: ...

    def add_fill(self, fields: dict) -> int:
        """fields: position_id, side, quantity, price(Decimal), filled_at(datetime tz),
        journal_ref, simulated(bool)"""
        ...

    def bump_session(
        self,
        day: dt.date,
        *,
        mode: str,
        confirms: int = 0,
        fills: int = 0,
        manage_actions: int = 0,
    ) -> None:
        """upsert on (user_id, session_date)"""
        ...

    def config(self) -> dict:
        """keys: sleeve_capital_inr, risk_per_trade_pct, max_position_pct, max_open_positions,
        first_live_sessions_left, exposure_level"""
        ...

    def set_first_live_sessions_left(self, value: int) -> None:
        """C1's write. Since SW10.5 (A9) NOTHING in this module calls it: the countdown is the
        evening job's, once per LIVE session that closes, never a request's."""
        ...

    # -- SW10.4 (STANDING-ANSWERS A5): the confirm-time gate --

    def lock_session_for_update(self, day: dt.date) -> AbstractContextManager[None]:
        """A transaction holding the day's ``sw_session`` row locked (``SELECT … FOR UPDATE``
        on Postgres, ``BEGIN IMMEDIATE`` on sqlite; the row is inserted first if absent) for
        the whole of a confirm — the re-derivation, the gateway call and the writes — so two
        confirms of one session run one after the other. Committed on exit, rolled back on an
        exception."""
        ...

    def session_context(self, day: dt.date) -> SignalContext:
        """The book as it is now (``app.swing_monitor.load_context``): the last close's gate
        and rung, the sleeve's capital, open positions at cost + today's CONFIRMED/SENT lines,
        each name's ADR/turnover/score, and how many entries the session has taken."""
        ...

    def resize_line(
        self,
        line_id: int,
        *,
        quantity: int,
        risk_inr: Decimal,
        position_value: Decimal,
        note: str,
    ) -> None:
        """Write a re-sized BUY back to its row, so the record shows the size that was sent."""
        ...

    # -- SW10.5 (STANDING-ANSWERS A7, A8): the marketable limit, the late fill, the cutoff --

    def range_high_for(self, line_id: int) -> Decimal | None:
        """The opening-range high of the signal that became this line (``sw_signal.range_high``
        where ``plan_line_id = line_id``), or None for a line no signal produced."""
        ...

    def line_by_order(self, order_id: str) -> dict | None:
        """The ``SENT`` BUY line whose ``journal_ref`` is this broker order id, or None."""
        ...

    def sent_buy_lines(self, day: dt.date) -> list[dict]:
        """Today's ``BUY_ON_TRIGGER`` lines in ``SENT`` — live orders not yet complete."""
        ...

    def note_line(self, line_id: int, note: str) -> None:
        """Append to the line's note — what a later fill or the cutoff did to it."""
        ...

    def expire_pending(self, day: dt.date) -> int:
        """Every ``PENDING_RANGE`` line of ``day`` still ``PROPOSED`` → ``EXPIRED``: the slots
        nothing claimed, freed at 10:45. Returns how many."""
        ...


class OrderStatus(Protocol):
    """One order as the broker reports it (A8) — the order book's row or the postback's."""

    @property
    def status(self) -> str: ...
    @property
    def filled_quantity(self) -> int: ...
    @property
    def average_price(self) -> Decimal: ...


@dataclass(frozen=True)
class OrderReport:
    """An :class:`OrderStatus` as a value — what the desk's order source and a test hand back."""

    status: str
    filled_quantity: int
    average_price: Decimal


class OrderSource(Protocol):
    """Where the confirm asks how its order is doing (A8). The desk backs it with the broker's
    order history through the Kite wrapper — a READ; a test answers from a script."""

    def order_status(self, order_id: str) -> OrderStatus: ...


@dataclass(frozen=True)
class ExecOutcome:
    """What one confirmed line came to. ``status`` is C1's vocabulary, not the gateway's.

    ``SIMULATED`` — the gateway's dry-run branch ran end to end and the book was written with
    ``simulated=true``. ``SENT`` — a real order was accepted by the broker and is not yet
    filled; nothing was written to the book (SW7.1). ``FILLED`` — a real action is complete
    at the broker; for a ``RAISE_GTT_STOP`` line or a re-arm it means the trigger is resting.
    ``BLOCKED`` — refused here or by the gateway, with its words in ``reason``; nothing was
    sent. A refusal by the confirm-time gate (SW10.4) starts its ``reason`` with the skip
    code — ``EXPOSURE_FULL``, ``TIER_FULL``, ``SESSION_CAP``, ``SIZE_REFUSED``, ``GATE_RED``,
    ``DRAWDOWN_LOCKOUT``, ``ALREADY_HELD`` — the same vocabulary as ``sw_plan_skip.reason``.
    ``REJECTED`` — the broker refused or the call failed; ``order`` says which.
    """

    status: str  # "SIMULATED" | "SENT" | "FILLED" | "BLOCKED" | "EXPIRED" | "REJECTED"
    reason: str  # human sentence, "" on success
    order: dict | None  # the gateway's place() result, if any
    gtt: dict | None  # the gateway's place_gtt_stop() result, if any
    position_id: int | None
    simulated: bool
    #: A8: how many shares had filled when the request answered (a partial is `SENT` with a
    #: number here and a GTT for exactly that number).
    filled_quantity: int = 0


def swing_gates() -> ProductGates:
    """The swing book's product switches, read at the moment of the order.

    ``dry_run`` is true unless BOTH the desk's ``DRY_RUN`` is off AND
    ``BASKFY_SWING_EXECUTION_ENABLED`` is on — the flag is the swing book's own dry-run and it
    defaults false (``02`` Track B). Intraday and options are false unconditionally, not read
    from config: the swing book is CNC-only (Track C §1) whatever the weekly desk is allowed.
    """
    return ProductGates(
        dry_run=C.DRY_RUN or not C.SWING_EXECUTION_ENABLED,
        intraday_enabled=False,
        options_enabled=False,
    )


def swing_stop_band() -> StopBand:
    """PACK.3: 0.5–10 % below the entry, from the desk's config, not the weekly 8–12 %."""
    return StopBand(min_pct=C.SWING_STOP_BAND_MIN, max_pct=C.SWING_STOP_BAND_MAX)


def swing_journal_path() -> str:
    """``swing_orders_journal.jsonl`` in the directory the desk's journal lives in — read off
    the shim at call time, so a test that isolates one isolates both."""
    return os.path.join(os.path.dirname(_gateway_module.JOURNAL), SWING_JOURNAL_NAME)


def build_swing_gateway(kc, risk) -> OrderGateway:
    """The desk's gateway, bound to the swing book's gates, band and journal.

    A separate instance from the weekly book's, deliberately: the gateway's idempotency maps
    are per instance and keyed on ``client_id``, and the two books mint ids from different plan
    namespaces, so sharing one would gain nothing — while a shared instance would let the swing
    gates callable be swapped for the weekly one by whichever caller constructed it first.
    """
    return OrderGateway(
        kc,
        risk,
        gates=swing_gates,
        journal_path=swing_journal_path(),
        stop_band=swing_stop_band(),
    )


# --- helpers -----------------------------------------------------------------------------


def _sole_tenant() -> TenantIds:
    """The one account this run trades (Track C §6). The desk shim stamps it on ``place``; the
    GTT methods are called with it explicitly so every call carries the same pair."""
    return TenantIds(
        user_id=int(C.SOLE_USER_ID), broker_account_id=int(C.SOLE_BROKER_ACCOUNT_ID)
    )


def _aware(stamp: dt.datetime) -> dt.datetime:
    """A naive timestamp from a store is read as IST — the only clock a plan is built on."""
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=IST)


def _session_day(now: dt.datetime) -> dt.date:
    return _aware(now).astimezone(IST).date()


def _price(value) -> Decimal:
    """Decimal at the boundary; the store may hand back a float from sqlite in a test."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _blocked_reason(result: dict, *, what: str) -> str:
    """The gateway's own words for a refusal, in one sentence."""
    status = result.get("status", "?")
    if status == "DUPLICATE":
        ref = result.get("order_id", result.get("gtt_id"))
        return f"{what} already sent for this plan ({status}: {ref})"
    error = result.get("error") or status
    return f"{what} refused by the gateway ({status}): {error}"


def risk_multiplier_for(config: dict, *, simulated: bool) -> Decimal:
    """A9: 0.5 while ``first_live_sessions_left`` is above zero AND the order would be real.

    The reading STANDING-ANSWERS A9 asks to be recorded: a paper confirm (``DRY_RUN`` or the
    flag off) is full size — the paper record rehearses the rules at the size the rules
    describe — and only real money starts small. Nothing here changes the count: that is the
    evening job's, once per LIVE session that closes.
    """
    sessions_left = int(config.get("first_live_sessions_left") or 0)
    return first_live_multiplier(
        sessions_left=sessions_left, execution_enabled=not simulated,
        config=DEFAULT_SWING_CONFIG.sizing,
    )


def is_simulated_gtt(gtt_id) -> bool:
    return isinstance(gtt_id, str) and gtt_id.startswith(_SIMULATED_GTT_PREFIX)


# --- SW10.4: the confirm-time gate (STANDING-ANSWERS A5) ------------------------------------

def sizing_config(config: dict, *, risk_multiplier: Decimal = _ONE) -> SwingConfig:
    """The gate's config: the pack's defaults with the person's three sizing knobs (`03` §1)
    from `SwingStore.config()` — the same three the worker (SW9.5.3) and the monitor hand
    `build_entries`, so the confirm sizes with the numbers the plan was sized with — and, since
    SW10.5 (A9), ``risk_per_trade_pct`` scaled by ``risk_multiplier`` *before* sizing, the one
    place the first-live half risk is applied at the desk."""
    sizing = DEFAULT_SWING_CONFIG.sizing
    risk = Decimal(str(config.get("risk_per_trade_pct", sizing.risk_per_trade_pct)))
    return replace(
        DEFAULT_SWING_CONFIG,
        sizing=replace(
            SizingConfig(),
            risk_per_trade_pct=float(risk * risk_multiplier),
            max_position_pct=float(config.get("max_position_pct", sizing.max_position_pct)),
            max_open_positions=int(config.get("max_open_positions", sizing.max_open_positions)),
        ),
    )


@dataclass(frozen=True)
class Resized:
    """What the gate answers for one BUY line: the size to send, or the refusal."""

    quantity: int
    risk_inr: Decimal
    position_value: Decimal
    refusal: str  # "" when sized; else the skip code (`SkipReason`), e.g. "EXPOSURE_FULL"
    detail: str  # the skip's detail, or how the size was changed
    #: True when the line was shrunk (never grown) to fit — the row is rewritten with `detail`.
    changed: bool = False


def _pct(value: Decimal, equity: Decimal) -> str:
    if equity <= 0:
        return "n/a"
    return f"{(value / equity * 100).quantize(_TWO_DP)}%"


def resize_buy(line: dict, context: SignalContext, config: SwingConfig, *, day: dt.date,
               live: bool = False) -> Resized:
    """Re-size one BUY line against the book as it is now (`app.swing_monitor.entries_now`).

    ``live`` (A9) says whether a real order would go out: `entries_now` applies the first-live
    half risk from the context's countdown exactly when it is true — the ONE place the
    multiplier is applied at the desk, so `config` arrives unscaled.

    The line's trigger and stop are what the person saw and confirmed; the *quantity* is what
    the rules allow now — the same function that sized the SIGNAL preview, so the page and the
    confirm agree: the gate, the drawdown lock, `ALREADY_HELD`, the per-session cap counting
    today's entries, the position count (`min(rung, max_open_positions)`), the size and the
    rung's exposure ceiling with A5's re-size to the headroom. The quantity is never raised
    above the line's: the page said N, and N is the most that goes.
    """
    symbol = str(line["symbol"])
    setup_name = line.get("setup")
    try:
        setup = Setup(str(setup_name))
    except ValueError:
        return Resized(0, _ZERO, _ZERO, SkipReason.NOT_TRADEABLE_SETUP.value,
                       f"setup {setup_name!r} is not one this book trades")
    if setup not in TRADEABLE_SETUPS:
        return Resized(0, _ZERO, _ZERO, SkipReason.NOT_TRADEABLE_SETUP.value, setup.value)
    stats = context.detected.get(symbol)
    if stats is None:
        # SW9.5.2's rule at the desk: the widest stop is one ADR, and an ADR nobody measured
        # is a stop nobody can check. The evening refuses such a line; so does the confirm.
        return Resized(0, _ZERO, _ZERO, SkipReason.SIZE_REFUSED.value,
                       f"no detection row for {symbol} before {day} — its ADR is unknown")
    adr, turnover, score = stats
    trigger, stop = _price(line["trigger"]), _price(line["stop"])
    item = WatchItem(symbol=symbol, setup=setup, trigger=trigger, stop_ref=stop, adr_pct=adr,
                     avg_turnover_inr=turnover, score=score, locked_upper_circuit=False)
    lines, skipped = entries_now(item, context, config, day=day, live=live)
    if not lines:
        skip = skipped[0]
        return Resized(0, _ZERO, _ZERO, skip.reason.value, skip.detail)
    allowed = int(lines[0].quantity)
    planned = int(line.get("quantity") or 0)
    quantity = min(planned, allowed)
    risk = ((trigger - stop) * quantity).quantize(_TWO_DP)
    value = (trigger * quantity).quantize(_TWO_DP)
    if quantity == planned:
        return Resized(quantity, risk, value, "", f"{quantity} as planned")
    account = context.account
    return Resized(
        quantity, risk, value, "",
        f"re-sized at confirm {planned} → {quantity} ({lines[0].note.split('; ')[2]}): book "
        f"₹{account.open_exposure_inr:,.2f} + ₹{value:,.2f} = "
        f"{_pct(account.open_exposure_inr + value, account.equity)} of the sleeve, ceiling "
        f"{context.tier.max_exposure_pct:g}% at rung {context.tier.level}, "
        f"{context.entries_today} entr{'y' if context.entries_today == 1 else 'ies'} today",
        changed=True,
    )


def _broker_gtt_id(gtt_id) -> int | None:
    """The integer trigger id the exchange knows, or None for a simulated / absent one."""
    if gtt_id is None or is_simulated_gtt(gtt_id):
        return None
    try:
        return int(gtt_id)
    except (TypeError, ValueError):
        return None


# --- the entry points --------------------------------------------------------------------


def _validate(
    store: SwingStore, *, plan_id: str, line_id: int, confirm: str, now: dt.datetime
) -> dict:
    """The four refusals that are the caller's, not the gateway's. Raises; never returns a
    line that may not be executed."""
    if confirm != "true":
        raise HTTPException(400, "Execution requires explicit confirmation.")
    plan = store.plan(plan_id)
    if plan is None:
        raise HTTPException(404, "Unknown plan_id — reload the swing page.")
    line = store.line(line_id)
    if line is None or str(line.get("plan_id")) != str(plan_id):
        raise HTTPException(404, "Unknown line_id for this plan — reload the swing page.")
    if line.get("kind") not in EXECUTABLE_KINDS:
        # A7: a PENDING_RANGE line has no quantity and no stop — it is information, a slot held
        # for the opening range, and nothing on it can be sent. Refused before the plan's
        # expiry or the line's state is even looked at, and the row is left exactly as it was.
        raise HTTPException(400, f"A {line.get('kind')} line cannot be executed — it is "
                                 f"information only; the SIGNAL plan at range close is the line.")
    if _aware(now) > _aware(plan["expires_at"]):
        # The line is marked so the page shows why it can no longer be confirmed; the plan
        # row itself is what expired, and the next plan build replaces it.
        if line.get("state") == "PROPOSED":
            store.set_line(line_id, state="EXPIRED")
        raise HTTPException(410, "Plan older than 30 minutes — prices stale, wait for the next "
                                 "plan.")
    if line.get("state") != "PROPOSED":
        raise HTTPException(409, f"Line is {line.get('state')}, not PROPOSED — it cannot be "
                                 f"confirmed twice.")
    return line


async def execute_line(  # noqa: PLR0913 - the request's parts, named
    store: SwingStore,
    gw,
    *,
    plan_id: str,
    line_id: int,
    confirm: str,
    now: dt.datetime,
    last_price: Decimal | None = None,
    orders: OrderSource | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ExecOutcome:
    """Execute one confirmed plan line through the gateway and write what happened.

    Raises ``HTTPException`` 400 / 404 / 410 / 409 before anything is touched (see
    :func:`_validate`). Past that point the line is marked ``CONFIRMED`` BEFORE the gateway is
    called, so a request that dies mid-way leaves a line that answers 409 on re-post rather
    than one that can be sent again.

    ``last_price`` is the instrument's last traded price, read per confirm. **Every kind needs
    one now** (Maulik, 4 Sep 2026): the BUY goes out as MARKET, so the price decides its
    protection percentage, its refusal against the entry cap and its value to the risk layer;
    a SELL's simulated fill and a RAISE's stop check needed one already. A live line without a
    price is ``BLOCKED`` — never guessed. It used to say "a BUY needs none (its entry is the
    trigger)", which was true only while the entry was a resting LIMIT at that trigger.

    ``orders``, ``clock`` and ``sleep`` (A8) are the live buy's fill poll: the order source is
    asked at most every ``fill_poll_interval_seconds`` for up to ``fill_poll_seconds`` after a
    real order is accepted. Injectable so a test runs the ten seconds in no time; the dry-run
    path never polls — the gateway's simulated order is a complete fill.
    """
    line = _validate(store, plan_id=plan_id, line_id=line_id, confirm=confirm, now=now)
    kind = line["kind"]
    simulated = swing_gates().dry_run
    day = _session_day(now)
    mode = "DRY_RUN" if simulated else "LIVE"
    started = time.perf_counter()
    try:
        with _span("swing.execute_line", kind=kind, simulated=simulated), \
                store.lock_session_for_update(day):
            # SW10.4: the whole confirm — the re-derivation, the gateway call, the writes —
            # runs with the day's session row locked, so a second confirm of the same
            # session (another tab, a double click) waits and then sees this one's book. The
            # line's state is read again under the lock: two requests that both passed
            # `_validate` on a PROPOSED line must not both send it.
            current = store.line(line_id)
            if current is None or current.get("state") != "PROPOSED":
                raise HTTPException(409, f"Line is {current.get('state') if current else '?'}, "
                                         f"not PROPOSED — it cannot be confirmed twice.")
            line = current
            # The book is read BEFORE this line is marked, so the line being confirmed is not
            # counted against itself; everything confirmed earlier today is.
            context = store.session_context(day) if kind == BUY_ON_TRIGGER else None
            store.set_line(line_id, state="CONFIRMED")
            store.bump_session(day, mode=mode, confirms=1)
            if kind == BUY_ON_TRIGGER:
                outcome = await _buy(store, gw, line=line, plan_id=plan_id, now=now,
                                     simulated=simulated, context=context, orders=orders,
                                     clock=clock, sleep=sleep, last_price=last_price)
            elif kind == SELL_AT_OPEN:
                outcome = await _sell(store, gw, line=line, plan_id=plan_id, now=now,
                                      simulated=simulated, last_price=last_price)
            elif kind == RAISE_GTT_STOP:
                outcome = await _raise_stop(store, gw, line=line, plan_id=plan_id, now=now,
                                            simulated=simulated, last_price=last_price)
            else:
                outcome = ExecOutcome("BLOCKED", f"{kind!r} is not a kind this desk executes",
                                      None, None, None, simulated)
            _record_line(store, line_id, outcome)
            if outcome.status in ("SIMULATED", "FILLED") and kind in (BUY_ON_TRIGGER, SELL_AT_OPEN):
                store.bump_session(day, mode=mode, fills=1)
            elif outcome.status == "SENT" and outcome.filled_quantity > 0:
                store.bump_session(day, mode=mode, fills=1)  # a partial is a fill (A8)
            elif outcome.status in ("SIMULATED", "FILLED"):
                store.bump_session(day, mode=mode, manage_actions=1)
    except HTTPException:
        _observe_confirm(kind=kind, outcome="REFUSED", seconds=time.perf_counter() - started)
        raise
    except Exception as exc:
        # Not swallowed: the transaction rolled back with the lock (nothing half-written),
        # then the line is marked so it cannot be re-posted, the click is counted, and the
        # error goes up to the route, which is where an untouchable-instrument refusal belongs.
        store.set_line(line_id, state="REJECTED")
        store.bump_session(day, mode=mode, confirms=1)
        _observe_confirm(kind=kind, outcome="ERROR", seconds=time.perf_counter() - started)
        _capture(exc, kind=kind, line_id=line_id)
        raise
    _observe_confirm(kind=kind, outcome=outcome.status, seconds=time.perf_counter() - started)
    log.info("swing %s line %s %s: %s %s", kind, line_id, line.get("symbol"),
             outcome.status, outcome.reason)
    return outcome


async def rearm_gtt(
    store: SwingStore, gw, *, position_id: int, confirm: str, now: dt.datetime,
    last_price: Decimal | None = None,
) -> ExecOutcome:
    """Arm a stop for a NAKED position — ``gtt_id`` None with shares open. Nothing else.

    A position that already carries a trigger is ``BLOCKED``: two triggers on one position sell
    twice what is held when they fire (the desk's ``protection.EXCESS``). Re-sizing a resting
    stop is a ``RAISE_GTT_STOP`` line's job, which cancels the old one first.
    """
    if confirm != "true":
        raise HTTPException(400, "Arming a stop requires explicit confirmation.")
    pos = store.position(position_id)
    if pos is None:
        raise HTTPException(404, "Unknown position_id.")
    simulated = swing_gates().dry_run
    if pos.get("gtt_id") is not None:
        return ExecOutcome("BLOCKED", f"{pos['symbol']}: a GTT is already resting "
                           f"({pos['gtt_id']}); re-arming would leave two", None, None,
                           position_id, simulated)
    open_qty = int(pos.get("quantity_open") or 0)
    if open_qty <= 0 or pos.get("state") == "CLOSED":
        return ExecOutcome("BLOCKED", f"{pos['symbol']}: nothing is open to protect",
                           None, None, position_id, simulated)
    stop = _price(pos["stop"])
    reference = _price(last_price) if last_price is not None else _price(pos["entry_avg"])
    if stop >= reference:
        return ExecOutcome("BLOCKED", f"{pos['symbol']}: stop {stop} is not below "
                           f"{'the last price' if last_price is not None else 'the entry'} "
                           f"{reference} — pass a last_price above it", None, None,
                           position_id, simulated)
    gtt = await _arm(gw, symbol=pos["symbol"], qty=open_qty, stop=stop, last_price=reference,
                     client_id=f"rearm:{position_id}:{pos['symbol']}:GTT:"
                               f"{_session_day(now).isoformat()}")
    if gtt.get("status") not in GTT_PLACED_STATUSES:
        return ExecOutcome("BLOCKED", _blocked_reason(gtt, what="the GTT"), None, gtt,
                           position_id, simulated)
    store.update_position(position_id, _gtt_fields(gtt, stop=stop, now=now))
    store.bump_session(_session_day(now), mode="DRY_RUN" if simulated else "LIVE",
                       manage_actions=1)
    return ExecOutcome("SIMULATED" if simulated else "FILLED", "", None, gtt, position_id,
                       simulated)


# --- the three kinds ---------------------------------------------------------------------


async def _buy(store, gw, *, line: dict, plan_id: str, now: dt.datetime,  # noqa: PLR0913
               simulated: bool, context: SignalContext | None = None,
               orders: OrderSource | None = None, last_price: Decimal | None = None,
               clock: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> ExecOutcome:
    symbol = line["symbol"]
    quantity = int(line.get("quantity") or 0)
    trigger = line.get("trigger")
    stop = line.get("stop")
    if quantity <= 0:
        return ExecOutcome("BLOCKED", f"{symbol}: a line for {quantity} shares is not a buy",
                           None, None, None, simulated)
    if trigger is None or stop is None:
        return ExecOutcome("BLOCKED", f"{symbol}: a buy line needs a trigger and a stop",
                           None, None, None, simulated)
    trigger, stop = _price(trigger), _price(stop)
    if stop >= trigger:
        # ``04`` §6.1: a stop at or above the entry is an error, not a position. The planner
        # cannot produce one; a hand-edited row could, and the gateway would refuse the GTT
        # only AFTER the buy had gone — leaving a bought position with no stop.
        return ExecOutcome("BLOCKED", f"{symbol}: stop {stop} is not below entry {trigger}",
                           None, None, None, simulated)
    if store.open_position_for(int(line["instrument_id"])) is not None:
        # ``04`` §6.5: never averaged down. The planner skips a held name (ALREADY_HELD); a
        # line built before this morning's fill would not know.
        return ExecOutcome("BLOCKED", f"{symbol}: already held by the swing book",
                           None, None, None, simulated)
    config = store.config()
    if context is None:
        context = store.session_context(_session_day(now))
    # A9: half risk at plan time, and only for real money. `entries_now` scales the risk
    # budget from the context's countdown when `live`; the quantity that comes out IS the
    # quantity sent. (Scaling `sizing_config` here as well would halve twice — the defect the
    # `..._is_applied_once` test pins.)
    multiplier = first_live_multiplier(
        sessions_left=context.first_live_sessions_left, execution_enabled=not simulated,
        config=DEFAULT_SWING_CONFIG.sizing,
    )
    sized = resize_buy(line, context, sizing_config(config), day=_session_day(now),
                       live=not simulated)
    if sized.refusal:
        # A5: the book as it is now does not have room for this line. Nothing is sent; the
        # reason leads with the skip code so the row and the page say exactly why.
        return ExecOutcome("BLOCKED", f"{sized.refusal}: {symbol} — {sized.detail}",
                           None, None, None, simulated)
    if sized.changed:
        # Fewer shares than the plan said: the row is rewritten with the risk and the value
        # re-derived for the size actually sent, and the note says what was done and why, so
        # the journal and the fill agree with the line.
        store.resize_line(line["id"], quantity=sized.quantity, risk_inr=sized.risk_inr,
                          position_value=sized.position_value, note=sized.detail)
        log.info("swing BUY %s %s", symbol, sized.detail)
    qty = sized.quantity
    # THE ENTRY CAP IS STILL A8'S; HOW IT IS ENFORCED IS NOT (Maulik, 4 Sep 2026).
    #
    # `marketable_limit` is unchanged and still the most this setup is worth paying. What
    # changed is that it is no longer sent as a resting LIMIT price — a LIMIT the tape runs
    # past does not fill, and the name that was "in swing" gets bought by everybody but us.
    # It is sent as Kite's `market_protection` on a MARKET order instead: same ceiling, at the
    # exchange, on an order that actually catches the breakout.
    window = DEFAULT_SWING_CONFIG.opening_range
    adr = context.detected.get(symbol, (_ZERO, None, _ZERO))[0]
    cap = marketable_limit(trigger=trigger, range_high=store.range_high_for(int(line["id"])),
                           adr_pct=adr, config=window)
    order_type = window.entry_order_type
    if order_type not in ENTRY_ORDER_TYPES:
        # Track C §1/§2 is a SOURCE-LEVEL property (`test_swing_track_c.py`): the set of order
        # types this module can send has to be readable in this file, not inferred from whatever
        # a config field happens to hold. The field chooses between the two; it cannot invent a
        # third, and a deployment that tries gets a refusal rather than an order.
        return ExecOutcome("BLOCKED",
                           f"{symbol}: entry_order_type {order_type!r} is not one of "
                           f"{sorted(ENTRY_ORDER_TYPES)}",
                           None, None, None, simulated)
    # THE PRICE IS READ NOW, NOT TAKEN FROM THE PAGE. A confirm can sit behind a person
    # reading the row; the plan itself may be half an hour old (`_validate` allows 30 minutes).
    # A market order priced off either would be sized against a price that no longer exists.
    # `last_price` is the desk's own per-confirm Kite read (interactive lane, M85).
    reference = _price(last_price) if last_price is not None else None
    if reference is None:
        if not simulated:
            # Live, and blind. Refusing is the only honest answer: without a price there is
            # no protection percentage to compute and no value for the risk layer to check.
            return ExecOutcome("BLOCKED",
                               f"{symbol}: no live price — a market entry is not sent without "
                               f"one (no Kite session, or the quote read failed)",
                               None, None, None, simulated)
        # The drill has no market. The trigger is what the dry-run gateway fills at anyway.
        reference = trigger
    protection = market_protection_pct(cap=cap, last_price=reference, config=window)
    if protection is None and not simulated:
        # A5's refusal, one step earlier and out loud. The price is at or above the cap: the
        # breakout has run past what this setup justifies, and the stop does not move up with
        # a chased entry, so every share bought here carries more risk than the plan sized for.
        # A8 expressed the same decision as a LIMIT nobody filled; this says it.
        return ExecOutcome("BLOCKED",
                           f"{symbol}: the price has run past the entry cap — {reference} is at "
                           f"or above {cap} (trigger {trigger}). Not chased; the setup is gone "
                           f"for today, not cheaper later",
                           None, None, None, simulated)
    order = await gw.place(
        symbol=symbol, qty=qty, side="BUY", product="CNC", order_type=order_type,
        price=float(cap) if order_type == "LIMIT" else None,
        market_protection=None if protection is None else float(protection),
        # What the risk layer values a MARKET order at — it has no price of its own, and a
        # zero there would disarm every notional cap the layer has.
        reference_price=float(reference),
        exchange="NSE", client_id=f"{plan_id}:{symbol}:BUY",
        gross_exposure=float(cap * qty),
        tenant=_sole_tenant(), plan_tenant=_sole_tenant(),
    )
    status = _ORDER_STATUS.get(order.get("status", ""), "REJECTED")
    if status not in ("SIMULATED", "SENT"):
        return ExecOutcome(status, _blocked_reason(order, what="the buy"), order, None, None,
                           simulated)
    half_risk = multiplier < _ONE
    if status == "SIMULATED":
        # The dry-run branch is the fill: the whole line, at the trigger, now — the same
        # bookkeeping as a live COMPLETE, with simulated=true on every row.
        return await _apply_buy_fill(
            store, gw, line=line, plan_id=plan_id, now=now, order=order, filled=qty,
            average=trigger, complete=True, simulated=True, half_risk=half_risk,
        )
    # A8: a live LIMIT is accepted, not filled. Ask the order book for up to ten seconds, at
    # most twice a second, and write whatever has filled by then; the postback handler and
    # the 10:45 sweep take it from there.
    report = await _poll_fill(orders, str(order.get("order_id")), clock=clock, sleep=sleep)
    if report is None:
        return ExecOutcome("SENT", f"{symbol}: order {order.get('order_id')} accepted, no "
                           f"order source to poll — no position, no stop, until a fill is "
                           f"reported", order, None, None, simulated)
    if report.status in ORDER_DEAD_STATUSES and report.filled_quantity <= 0:
        return ExecOutcome("REJECTED", f"{symbol}: order {order.get('order_id')} is "
                           f"{report.status} with nothing filled", order, None, None, simulated)
    return await _apply_buy_fill(
        store, gw, line=line, plan_id=plan_id, now=now, order=order,
        filled=int(report.filled_quantity), average=_price(report.average_price),
        complete=report.status == ORDER_COMPLETE, simulated=False, half_risk=half_risk,
    )


async def _poll_fill(orders: OrderSource | None, order_id: str, *,
                     clock: Callable[[], float],
                     sleep: Callable[[float], Awaitable[None]]) -> OrderReport | None:
    """A8's poll: ≤ ``fill_poll_seconds`` at ≤ 1 / ``fill_poll_interval_seconds`` a second.
    Stops early on COMPLETE or a dead status; answers the last report it saw."""
    if orders is None:
        return None
    window = DEFAULT_SWING_CONFIG.opening_range
    started = clock()
    last: OrderReport | None = None
    while True:
        seen = orders.order_status(order_id)
        last = OrderReport(status=str(seen.status), filled_quantity=int(seen.filled_quantity),
                           average_price=_price(seen.average_price or 0))
        if last.status == ORDER_COMPLETE or last.status in ORDER_DEAD_STATUSES:
            return last
        if clock() - started + window.fill_poll_interval_seconds > window.fill_poll_seconds:
            return last
        await sleep(window.fill_poll_interval_seconds)


async def _apply_buy_fill(store, gw, *, line: dict, plan_id: str, now: dt.datetime,  # noqa: PLR0913
                          order: dict, filled: int, average: Decimal, complete: bool,
                          simulated: bool, half_risk: bool) -> ExecOutcome:
    """What a buy's fill — whole, partial, or none yet — writes (A8), one path for all three.

    ``filled`` shares at ``average``: a position for exactly that many, one fill row, and a
    GTT for exactly that many in the same call. Nothing filled yet → ``SENT`` with no position
    and no stop (a stop for shares not held sells what is not there). ``complete`` decides
    the line's state: ``FILLED``, or ``SENT`` with the position on it so the postback handler
    can grow it.
    """
    symbol = line["symbol"]
    order_id = order.get("order_id")
    if filled <= 0:
        return ExecOutcome("SENT", f"{symbol}: order {order_id} accepted, not yet filled — "
                           f"no position, no stop, until it fills", order, None, None,
                           simulated, filled_quantity=0)
    stop = _price(line["stop"])
    position_id = store.create_position({
        "instrument_id": int(line["instrument_id"]),
        "symbol": symbol,
        "setup": line.get("setup"),
        "entry_date": _session_day(now),
        "entry_avg": average.quantize(_FOUR_DP),
        "quantity_entered": filled,
        "quantity_open": filled,
        "initial_stop": stop,
        "stop": stop,
        "gtt_id": None,
        "gtt_trigger": None,
        "gtt_armed_at": None,
        "trail": line.get("trail") or "MA20",
        "partial_done": False,
        "partial_date": None,
        "state": "OPEN",
        "closed_on": None,
        "exit_avg": None,
        "close_reason": None,
        "r_multiple": None,
        "pnl_inr": None,
        "simulated": simulated,
        "half_risk": half_risk,
    })
    store.add_fill({
        "position_id": position_id, "side": "BUY", "quantity": filled, "price": average,
        "filled_at": _aware(now), "journal_ref": order_id, "simulated": simulated,
    })
    gtt = await _arm(gw, symbol=symbol, qty=filled, stop=stop, last_price=average,
                     client_id=f"{plan_id}:{symbol}:GTT")
    status = "SIMULATED" if simulated else ("FILLED" if complete else "SENT")
    planned = int(line.get("quantity") or 0)
    partial = "" if complete else (f"; {filled} of {planned} filled so far, order {order_id} "
                                   f"still open for the rest")
    if gtt.get("status") in GTT_PLACED_STATUSES:
        store.update_position(position_id, _gtt_fields(gtt, stop=stop, now=now))
        return ExecOutcome(status, partial.lstrip("; "), order, gtt, position_id, simulated,
                           filled_quantity=filled)
    # The shares are held and the stop is not resting: the honest record is a NAKED position,
    # which the page leads with and ``rearm_gtt`` exists for.
    return ExecOutcome(status,
                       f"{symbol}: bought {filled}, but the stop was not armed — "
                       f"{_blocked_reason(gtt, what='the GTT')}; position {position_id} is "
                       f"NAKED, re-arm it{partial}", order, gtt, position_id, simulated,
                       filled_quantity=filled)


# --- A8: the late fill, the cutoff, the 15:15 sweep -----------------------------------------


async def on_order_update(store: SwingStore, gw, update: dict, *, now: dt.datetime) -> ExecOutcome | None:
    """The desk's postback handler (A8): a broker order update for a swing buy.

    ``update`` is Kite's postback / order-book shape — ``order_id``, ``status``,
    ``filled_quantity``, ``average_price``. Anything that is not a ``SENT`` swing BUY line's
    order is ignored (the weekly book's updates arrive on the same channel and are not this
    module's). Under the session lock:

    * no position yet and shares filled → the position, its fill and its GTT for exactly the
      filled quantity (the same bookkeeping as the confirm's own partial);
    * a position and *more* shares filled than it holds → one fill row for the difference at
      the price that makes the averages agree, the position grown, and the resting GTT's
      quantity **modified** to the new open quantity — never a second GTT; a naked position
      is armed for the whole;
    * a position and no more shares than it holds → nothing to write: **idempotent** on a
      repeated update, an out-of-order one, or a duplicate postback;
    * ``COMPLETE`` closes the line as ``FILLED``; a cancel or rejection with shares held
      closes it ``FILLED`` too (the position stands for what filled) and with none held
      ``EXPIRED``.

    Protection is never withheld because the stop distance grew past one ADR: the rule bounds
    entries, not protection (MD11).
    """
    order_id = str(update.get("order_id") or "")
    line = store.line_by_order(order_id) if order_id else None
    if line is None:
        return None
    day = _session_day(now)
    simulated = swing_gates().dry_run
    mode = "DRY_RUN" if simulated else "LIVE"
    with store.lock_session_for_update(day):
        current = store.line(int(line["id"]))
        if current is None or current.get("state") != "SENT":
            return None
        return await _apply_update(store, gw, line=current, update=update, now=now, mode=mode)


async def _apply_update(store, gw, *, line: dict, update: dict, now: dt.datetime,  # noqa: PLR0913
                        mode: str) -> ExecOutcome:
    symbol = line["symbol"]
    plan_id = str(line["plan_id"])
    order_id = str(update.get("order_id"))
    status = str(update.get("status") or "")
    filled = int(update.get("filled_quantity") or 0)
    average = _price(update.get("average_price") or 0)
    simulated = swing_gates().dry_run
    half_risk = risk_multiplier_for(store.config(), simulated=simulated) < _ONE
    position = store.position(int(line["position_id"])) if line.get("position_id") else None
    outcome: ExecOutcome
    if position is None:
        if filled <= 0:
            outcome = ExecOutcome("SENT", f"{symbol}: {order_id} {status}, nothing filled",
                                  {"order_id": order_id, "status": status}, None, None, simulated)
        else:
            outcome = await _apply_buy_fill(
                store, gw, line=line, plan_id=plan_id, now=now,
                order={"order_id": order_id, "status": status}, filled=filled, average=average,
                complete=status == ORDER_COMPLETE, simulated=simulated, half_risk=half_risk,
            )
            store.bump_session(_session_day(now), mode=mode, fills=1)
    else:
        outcome = await _grow_position(store, gw, position=position, line=line, order_id=order_id,
                                       status=status, filled=filled, average=average, now=now,
                                       simulated=simulated)
    _close_line_after_update(store, line, outcome, status=status, order_id=order_id)
    return outcome


async def _grow_position(store, gw, *, position: dict, line: dict, order_id: str,  # noqa: PLR0913
                         status: str, filled: int, average: Decimal, now: dt.datetime,
                         simulated: bool) -> ExecOutcome:
    """More of the same order filled: grow the position and re-size — never re-arm — its GTT."""
    symbol = position["symbol"]
    position_id = int(position["id"])
    entered = int(position["quantity_entered"])
    open_qty = int(position["quantity_open"])
    delta = filled - entered
    if delta <= 0:
        # Already applied (a repeated postback, the poll's own fill, an older report). Nothing
        # to write; the position and its stop are what they were.
        return ExecOutcome("SENT" if status != ORDER_COMPLETE else "FILLED", "", None, None,
                           position_id, simulated, filled_quantity=entered)
    old_avg = _price(position["entry_avg"])
    # The price of the new shares alone: the averages must agree before and after.
    delta_price = ((average * filled - old_avg * entered) / delta).quantize(_FOUR_DP)
    if delta_price <= _ZERO:
        delta_price = average
    new_open = open_qty + delta
    store.add_fill({
        "position_id": position_id, "side": "BUY", "quantity": delta, "price": delta_price,
        "filled_at": _aware(now), "journal_ref": order_id, "simulated": simulated,
    })
    store.update_position(position_id, {
        "quantity_entered": filled, "quantity_open": new_open,
        "entry_avg": average.quantize(_FOUR_DP),
    })
    stop = _price(position["stop"])
    gtt_id = position.get("gtt_id")
    if gtt_id is None:
        # Naked (the first arm was refused): arm now, for everything open.
        gtt = await _arm(gw, symbol=symbol, qty=new_open, stop=stop, last_price=average,
                         client_id=f"{line['plan_id']}:{symbol}:GTT")
        ok = gtt.get("status") in GTT_PLACED_STATUSES
        if ok:
            store.update_position(position_id, _gtt_fields(gtt, stop=stop, now=now))
    else:
        gtt = await _modify(gw, gtt_id=gtt_id, symbol=symbol, qty=new_open, stop=stop,
                            last_price=average, client_id=f"{line['plan_id']}:{symbol}:GTT")
        ok = gtt.get("status") in GTT_MODIFIED_STATUSES
        if ok:
            store.update_position(position_id, {"gtt_armed_at": _aware(now)})
    final = "FILLED" if status == ORDER_COMPLETE else "SENT"
    if ok:
        return ExecOutcome(final, f"{symbol}: +{delta} filled at {delta_price}, GTT now covers "
                           f"{new_open}", {"order_id": order_id, "status": status}, gtt,
                           position_id, simulated, filled_quantity=filled)
    return ExecOutcome(final, f"{symbol}: +{delta} filled, but the GTT could not be re-sized "
                       f"to {new_open} — {_blocked_reason(gtt, what='the GTT')}; it still "
                       f"covers {open_qty}, re-arm", {"order_id": order_id, "status": status},
                       gtt, position_id, simulated, filled_quantity=filled)


def _close_line_after_update(store: SwingStore, line: dict, outcome: ExecOutcome, *,
                             status: str, order_id: str) -> None:
    """The line's state after an update: FILLED on COMPLETE (or a dead order with shares
    held), EXPIRED on a dead order with nothing held, SENT otherwise — and the position id
    once there is one."""
    line_id = int(line["id"])
    position_id = outcome.position_id
    if status == ORDER_COMPLETE:
        store.set_line(line_id, state="FILLED", journal_ref=order_id, position_id=position_id)
    elif status in ORDER_DEAD_STATUSES:
        if position_id is not None:
            store.set_line(line_id, state="FILLED", journal_ref=order_id, position_id=position_id)
            store.note_line(line_id, f"{status}: {outcome.filled_quantity} filled, the rest "
                                     f"never did")
        else:
            store.set_line(line_id, state="EXPIRED", journal_ref=order_id)
            store.note_line(line_id, f"{status} with nothing filled")
    elif position_id is not None:
        store.set_line(line_id, state="SENT", journal_ref=order_id, position_id=position_id)


@dataclass(frozen=True)
class CutoffReport:
    """What the 10:45 sweep did (A8, A7)."""

    reconciled: int
    cancelled: int
    cancel_failed: int
    slots_freed: int
    outcomes: tuple[ExecOutcome, ...]


async def cutoff_open_orders(store: SwingStore, gw, *, orders: OrderSource | None,
                             now: dt.datetime) -> CutoffReport:
    """The monitor-close hook (A8, A7): cancel every open remainder, free every unclaimed slot.

    For each of today's ``SENT`` BUY lines: the order book is asked once more and the answer
    applied through the same path as a postback (so a fill that arrived between the last
    update and now is written first); if the order is still open, its remainder is cancelled
    through the gateway — the GTT covering what filled is **untouched** — and the line closes
    as ``FILLED`` (shares held) or ``EXPIRED`` (none). Then every ``PENDING_RANGE`` line still
    ``PROPOSED`` is ``EXPIRED``: the slot it held is free. Each cancel is journalled; a cancel
    the gateway refuses leaves the line ``SENT`` and is counted, so the alert SW11 adds
    (``SWING_ORDER_OPEN_AFTER_CUTOFF``) has something to read.
    """
    day = _session_day(now)
    outcomes: list[ExecOutcome] = []
    reconciled = cancelled = failed = 0
    for line in store.sent_buy_lines(day):
        order_id = str(line.get("journal_ref") or "")
        if not order_id:
            continue
        if orders is not None:
            seen = orders.order_status(order_id)
            update = {"order_id": order_id, "status": str(seen.status),
                      "filled_quantity": int(seen.filled_quantity),
                      "average_price": _price(seen.average_price or 0)}
            applied = await on_order_update(store, gw, update, now=now)
            if applied is not None:
                outcomes.append(applied)
                reconciled += 1
            if update["status"] == ORDER_COMPLETE or update["status"] in ORDER_DEAD_STATUSES:
                continue
        current = store.line(int(line["id"]))
        if current is None or current.get("state") != "SENT":
            continue
        cancel = await gw.cancel_order(
            order_id=order_id, symbol=current["symbol"],
            client_id=f"{current['plan_id']}:{current['symbol']}:CANCEL",
            tenant=_sole_tenant(), plan_tenant=_sole_tenant(),
        )
        if cancel.get("status") not in ORDER_CANCELLED_STATUSES:
            failed += 1
            store.note_line(int(current["id"]), f"10:45 cancel refused: "
                                                f"{_blocked_reason(cancel, what='the cancel')}")
            continue
        cancelled += 1
        with store.lock_session_for_update(day):
            position_id = current.get("position_id")
            held = int(store.position(int(position_id))["quantity_open"]) if position_id else 0
            planned = int(current.get("quantity") or 0)
            if position_id is not None:
                store.set_line(int(current["id"]), state="FILLED", position_id=int(position_id))
                store.note_line(int(current["id"]), f"10:45 cutoff: {held} filled, the "
                                                    f"remaining {max(planned - held, 0)} "
                                                    f"cancelled ({order_id}); GTT untouched")
            else:
                store.set_line(int(current["id"]), state="EXPIRED")
                store.note_line(int(current["id"]), f"10:45 cutoff: nothing filled, order "
                                                    f"{order_id} cancelled")
    freed = store.expire_pending(day)
    log.info("swing 10:45 sweep: %d reconciled, %d cancelled, %d refused, %d slots freed",
             reconciled, cancelled, failed, freed)
    return CutoffReport(reconciled, cancelled, failed, freed, tuple(outcomes))


async def eod_gtt_sweep(store: SwingStore, gw, *, now: dt.datetime,
                        prices: dict[str, Decimal] | None = None) -> list[ExecOutcome]:
    """The 15:15 sweep (A8): every open position with shares and no resting GTT is re-armed
    through ``rearm_gtt`` — given a last price above its stop — and the outcomes say which are
    still naked. Run by the desk's clock (`app.swing_clock`, SW11); `SWING_GTT_MISSING_AT_1515`
    reads what is left."""
    outcomes: list[ExecOutcome] = []
    for pos in _naked_positions(store):
        price = (prices or {}).get(str(pos["symbol"]))
        outcomes.append(await rearm_gtt(store, gw, position_id=int(pos["id"]), confirm="true",
                                        now=now, last_price=price))
    return outcomes


def _naked_positions(store: SwingStore) -> list[dict]:
    reader = getattr(store, "open_positions", None)
    if reader is None:
        return []
    return [p for p in reader() if p.get("gtt_id") is None and int(p.get("quantity_open") or 0) > 0]


async def _sell(store, gw, *, line: dict, plan_id: str, now: dt.datetime,  # noqa: PLR0913
                simulated: bool, last_price: Decimal | None) -> ExecOutcome:
    symbol = line["symbol"]
    quantity = int(line.get("quantity") or 0)
    pos = store.open_position_for(int(line["instrument_id"]))
    if pos is None:
        # Track C §5: the sleeve never sells a holding it did not buy — and it does not look
        # at the broker's holdings to find out, because those hold the weekly book too.
        return ExecOutcome("BLOCKED", f"{symbol}: not a swing position", None, None, None,
                           simulated)
    open_qty = int(pos.get("quantity_open") or 0)
    if quantity <= 0:
        return ExecOutcome("BLOCKED", f"{symbol}: a line for {quantity} shares is not a sell",
                           None, None, pos["id"], simulated)
    if quantity > open_qty:
        return ExecOutcome("BLOCKED", f"{symbol}: SELL for {quantity} exceeds the {open_qty} "
                           f"open", None, None, pos["id"], simulated)
    if simulated and last_price is None:
        return ExecOutcome("BLOCKED", f"{symbol}: no last price to simulate a market fill at "
                           f"— pass last_price", None, None, pos["id"], simulated)
    order = await gw.place(
        symbol=symbol, qty=quantity, side="SELL", product="CNC", order_type="MARKET",
        price=float(last_price) if last_price is not None else None, exchange="NSE",
        client_id=f"{plan_id}:{symbol}:SELL",
        tenant=_sole_tenant(), plan_tenant=_sole_tenant(),
    )
    status = _ORDER_STATUS.get(order.get("status", ""), "REJECTED")
    if status not in ("SIMULATED", "SENT"):
        return ExecOutcome(status, _blocked_reason(order, what="the sell"), order, None,
                           pos["id"], simulated)
    if status == "SENT":
        # SW7.1, the other side: a market sell is accepted, not filled. The book changes when
        # the fill is known; until then the resting GTT still covers the full open quantity.
        return ExecOutcome("SENT", f"{symbol}: sell order {order.get('order_id')} accepted, "
                           f"not yet filled — the book is unchanged until it fills",
                           order, None, pos["id"], simulated)
    price = _price(last_price)
    return await _apply_sell_fill(store, gw, pos=pos, line=line, plan_id=plan_id, now=now,
                                  quantity=quantity, price=price, order=order)


async def _apply_sell_fill(store, gw, *, pos: dict, line: dict, plan_id: str,  # noqa: PLR0913
                           now: dt.datetime, quantity: int, price: Decimal,
                           order: dict) -> ExecOutcome:
    """Write a simulated SELL fill: the fill row, the book, and the re-sized stop."""
    position_id = int(pos["id"])
    symbol = pos["symbol"]
    day = _session_day(now)
    store.add_fill({
        "position_id": position_id, "side": "SELL", "quantity": quantity, "price": price,
        "filled_at": _aware(now), "journal_ref": order.get("order_id"), "simulated": True,
    })
    entered = int(pos["quantity_entered"])
    open_qty = int(pos["quantity_open"])
    sold_before = entered - open_qty
    fills: list[tuple[int, Decimal]] = []
    if sold_before > 0 and pos.get("exit_avg") is not None:
        fills.append((sold_before, _price(pos["exit_avg"])))
    fills.append((quantity, price))
    exit_avg = exit_average(fills)  # share-weighted over every partial, ``04`` §10
    remaining = open_qty - quantity
    note = str(line.get("note") or "")
    # The old trigger covers the old quantity; whichever way this goes it must not rest.
    # One GTT id per (plan, symbol, kind): the evening plan legitimately carries a SELL partial
    # AND a RAISE to breakeven for the same name, and the gateway's idempotency map would
    # answer the second with DUPLICATE if both minted ``plan:symbol:GTT``.
    gtt_cid = f"{plan_id}:{symbol}:SELL:GTT"
    cancel = await _cancel(gw, gtt_id=pos.get("gtt_id"), symbol=symbol, client_id=gtt_cid)
    if cancel is not None and cancel.get("status") not in GTT_DELETED_STATUSES:
        # The sell filled but the stop could not be pulled: an over-covered trigger is the
        # desk's EXCESS — it sells shares that are no longer held when it fires. Reported
        # loudly rather than papered over with a second trigger.
        store.update_position(position_id, _book_after_sell(
            pos, remaining=remaining, exit_avg=exit_avg, note=note, day=day))
        return ExecOutcome("SIMULATED",
                           f"{symbol}: sold {quantity}, but the resting GTT {pos.get('gtt_id')} "
                           f"could not be cancelled — "
                           f"{_blocked_reason(cancel, what='the cancel')}; it still covers "
                           f"{open_qty}", order, cancel, position_id, True)
    fields = _book_after_sell(pos, remaining=remaining, exit_avg=exit_avg, note=note, day=day)
    fields.update({"gtt_id": None, "gtt_trigger": None, "gtt_armed_at": None})
    if remaining == 0:
        store.update_position(position_id, fields)
        return ExecOutcome("SIMULATED", "", order, cancel, position_id, True)
    gtt = await _arm(gw, symbol=symbol, qty=remaining, stop=_price(pos["stop"]),
                     last_price=price, client_id=gtt_cid)
    if gtt.get("status") in GTT_PLACED_STATUSES:
        fields.update(_gtt_fields(gtt, stop=_price(pos["stop"]), now=now))
        store.update_position(position_id, fields)
        return ExecOutcome("SIMULATED", "", order, gtt, position_id, True)
    store.update_position(position_id, fields)
    return ExecOutcome("SIMULATED",
                       f"{symbol}: sold {quantity}, but the stop for the remaining {remaining} "
                       f"was not armed — {_blocked_reason(gtt, what='the GTT')}; position "
                       f"{position_id} is NAKED, re-arm it", order, gtt, position_id, True)


def _book_after_sell(pos: dict, *, remaining: int, exit_avg: Decimal, note: str,
                     day: dt.date) -> dict:
    """The position row after a fill of ``quantity_open - remaining`` shares."""
    if remaining > 0:
        return {
            "quantity_open": remaining,
            "state": "PARTIAL",
            "partial_done": True,
            "partial_date": day,
            "exit_avg": exit_avg,
        }
    entry = _price(pos["entry_avg"])
    initial_stop = _price(pos["initial_stop"])
    closed = ClosedTrade(
        symbol=pos["symbol"], setup=str(pos.get("setup") or ""),
        entry_date=pos["entry_date"], exit_date=day, entry=entry, initial_stop=initial_stop,
        exit_avg=exit_avg, quantity=int(pos["quantity_entered"]),
    )
    return {
        "quantity_open": 0,
        "state": "CLOSED",
        "closed_on": day,
        "exit_avg": exit_avg,
        "close_reason": note if note in _KNOWN_CLOSE_REASONS else "MANUAL",
        "r_multiple": r_multiple(entry=entry, stop=initial_stop, exit_price=exit_avg),
        "pnl_inr": closed.pnl_inr,
    }


async def _raise_stop(store, gw, *, line: dict, plan_id: str, now: dt.datetime,  # noqa: PLR0913
                      simulated: bool, last_price: Decimal | None) -> ExecOutcome:
    symbol = line["symbol"]
    pos = store.open_position_for(int(line["instrument_id"]))
    if pos is None:
        return ExecOutcome("BLOCKED", f"{symbol}: not a swing position", None, None, None,
                           simulated)
    new_stop = line.get("stop")
    if new_stop is None:
        return ExecOutcome("BLOCKED", f"{symbol}: a RAISE line needs a stop", None, None,
                           pos["id"], simulated)
    new_stop = _price(new_stop)
    resting = _price(pos["stop"])
    if new_stop <= resting:
        # ``04`` §6.5: a stop never falls. Equal is refused too — cancelling and re-arming the
        # same trigger is a moment of nakedness for nothing.
        return ExecOutcome("BLOCKED", f"{symbol}: new stop {new_stop} is not above the resting "
                           f"stop {resting} — a stop never falls", None, None, pos["id"],
                           simulated)
    open_qty = int(pos.get("quantity_open") or 0)
    if open_qty <= 0:
        return ExecOutcome("BLOCKED", f"{symbol}: nothing is open to protect", None, None,
                           pos["id"], simulated)
    if last_price is None:
        return ExecOutcome("BLOCKED", f"{symbol}: no last price to check the new stop against "
                           f"— pass last_price", None, None, pos["id"], simulated)
    if new_stop >= _price(last_price):
        return ExecOutcome("BLOCKED", f"{symbol}: new stop {new_stop} is at or above the last "
                           f"price {last_price} — that would fire at once", None, None,
                           pos["id"], simulated)
    gtt_cid = f"{plan_id}:{symbol}:RAISE:GTT"   # distinct from a SELL's in the same plan
    cancel = await _cancel(gw, gtt_id=pos.get("gtt_id"), symbol=symbol, client_id=gtt_cid)
    if cancel is not None and cancel.get("status") not in GTT_DELETED_STATUSES:
        # The old trigger is still resting, so the new one is NOT placed: two triggers sell
        # the position twice. The stop stays where it was, and the reason says why.
        return ExecOutcome("BLOCKED", _blocked_reason(cancel, what="cancelling the old GTT"),
                           None, cancel, pos["id"], simulated)
    gtt = await _arm(gw, symbol=symbol, qty=open_qty, stop=new_stop,
                     last_price=_price(last_price), client_id=gtt_cid)
    if gtt.get("status") in GTT_PLACED_STATUSES:
        store.update_position(int(pos["id"]), _gtt_fields(gtt, stop=new_stop, now=now))
        return ExecOutcome("SIMULATED" if simulated else "FILLED", "", None, gtt, pos["id"],
                           simulated)
    # Cancelled and not re-armed: the intent (the raised stop) is recorded so the next re-arm
    # rests it at the right level, and gtt_id None makes the position NAKED on the page.
    store.update_position(int(pos["id"]), {"stop": new_stop, "gtt_id": None,
                                           "gtt_trigger": None, "gtt_armed_at": None})
    return ExecOutcome("BLOCKED", f"{symbol}: the old stop was cancelled but the new one was "
                       f"not armed — {_blocked_reason(gtt, what='the GTT')}; position "
                       f"{pos['id']} is NAKED, re-arm it", None, gtt, pos["id"], simulated)


# --- the gateway calls -------------------------------------------------------------------


async def _arm(gw, *, symbol: str, qty: int, stop: Decimal, last_price: Decimal,
               client_id: str) -> dict:
    """One GTT through the gateway. Decimal in, float across the boundary.

    The gateway's dry-run result names no id (it minted ``DRY-<client_id>`` in its own map);
    the client id is added to the result so the position row can carry the same handle.
    ``limit_fraction`` is the swing book's own (``C.SWING_GTT_LIMIT_FRACTION``, 0.97 — `04`
    §9.4): the resting LIMIT sits 3% under the trigger so it fills on the way down like the
    market stop he uses; the weekly book's GTTs keep the gateway's default."""
    result = await gw.place_gtt_stop(
        symbol=symbol, qty=int(qty), trigger=float(stop), last_price=float(last_price),
        exchange="NSE", client_id=client_id,
        tenant=_sole_tenant(), plan_tenant=_sole_tenant(),
        limit_fraction=C.SWING_GTT_LIMIT_FRACTION,
    )
    result.setdefault("client_id", client_id)
    return result


async def _cancel(gw, *, gtt_id, symbol: str, client_id: str) -> dict | None:
    """Pull a resting trigger. None when there was nothing resting; a simulated trigger is
    recorded as cancelled without a gateway call, because nothing exists at the exchange."""
    if gtt_id is None:
        return None
    broker_id = _broker_gtt_id(gtt_id)
    if broker_id is None:
        return {"symbol": symbol, "gtt_id": gtt_id, "status": DRY_RUN_GTT_DELETE,
                "simulated": True}
    return await gw.delete_gtt(
        gtt_id=broker_id, symbol=symbol, exchange="NSE", client_id=client_id,
        tenant=_sole_tenant(), plan_tenant=_sole_tenant(),
    )


async def _modify(gw, *, gtt_id, symbol: str, qty: int, stop: Decimal,  # noqa: PLR0913
                  last_price: Decimal, client_id: str) -> dict:
    """Re-size a resting trigger to ``qty`` (A8) — the gateway's ``modify_gtt_quantity``, the
    ONLY way to touch a GTT; a simulated trigger (nothing at the exchange) is recorded as
    re-sized locally, as ``_cancel`` records a simulated delete."""
    broker_id = _broker_gtt_id(gtt_id)
    if broker_id is None:
        return {"symbol": symbol, "gtt_id": gtt_id, "status": DRY_RUN_GTT_MODIFY,
                "qty": int(qty), "trigger": float(stop), "simulated": True}
    result = await gw.modify_gtt_quantity(
        gtt_id=broker_id, symbol=symbol, qty=int(qty), trigger=float(stop),
        last_price=float(last_price), exchange="NSE", client_id=client_id,
        tenant=_sole_tenant(), plan_tenant=_sole_tenant(),
        limit_fraction=C.SWING_GTT_LIMIT_FRACTION,
    )
    result.setdefault("client_id", client_id)
    return result


def _gtt_fields(gtt: dict, *, stop: Decimal, now: dt.datetime) -> dict:
    """The position's three GTT columns from a placed (or simulated) trigger.

    A real trigger carries the exchange's ``gtt_id``. A dry-run result carries none: the
    gateway minted ``DRY-<client_id>`` in its own idempotency map, and the row carries the same
    handle so "simulated" is readable off it — and so it is never handed back to ``delete_gtt``.
    """
    gtt_id = gtt.get("gtt_id")
    if gtt.get("status") != GTT_PLACED or gtt_id is None:
        gtt_id = f"{_SIMULATED_GTT_PREFIX}{gtt.get('client_id') or gtt.get('symbol')}"
    trigger = gtt.get("trigger")
    return {
        "stop": stop,
        "gtt_id": str(gtt_id),
        "gtt_trigger": _price(trigger).quantize(_TWO_DP) if trigger is not None else stop,
        "gtt_armed_at": _aware(now),
    }


def _record_line(store: SwingStore, line_id: int, outcome: ExecOutcome) -> None:
    ref = None
    if outcome.order is not None:
        ref = outcome.order.get("order_id")
    elif outcome.gtt is not None:
        ref = outcome.gtt.get("gtt_id")
    if outcome.status in ("SIMULATED", "FILLED"):
        state = "FILLED"
    elif outcome.status == "SENT":
        state = "SENT"
    else:
        state = "REJECTED"
    store.set_line(line_id, state=state, journal_ref=str(ref) if ref is not None else None,
                   position_id=outcome.position_id)
