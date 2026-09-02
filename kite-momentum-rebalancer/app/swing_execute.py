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

Every order — the buy, the market sell, the stop and its cancellation — goes through the
:class:`OrderGateway` this module is handed. Nothing here names a broker method.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from baskfy_core.swing.journal import ClosedTrade, exit_average
from baskfy_core.swing.sizing import r_multiple
from baskfy_execution.gtt import (
    DRY_RUN_GTT_DELETE,
    GTT_DELETED_STATUSES,
    GTT_PLACED,
    GTT_PLACED_STATUSES,
    StopBand,
)
from baskfy_execution.tenancy import TenantIds
from fastapi import HTTPException

from . import config as C
from .core import gateway as _gateway_module
from .core.gateway import OrderGateway, ProductGates

log = logging.getLogger("swing.execute")

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

#: SW7.2 — the sessions on which this process has already counted down
#: ``first_live_sessions_left``. In-process, like the desk's ``PLANS`` dict: the counter is
#: decremented once per session, on the first live buy, and a second live buy on the same
#: morning must not decrement it again.
_FIRST_LIVE_COUNTED: set[dt.date] = set()


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
        """keys: sleeve_capital_inr, risk_per_trade_pct, first_live_sessions_left,
        exposure_level"""
        ...

    def set_first_live_sessions_left(self, value: int) -> None: ...


@dataclass(frozen=True)
class ExecOutcome:
    """What one confirmed line came to. ``status`` is C1's vocabulary, not the gateway's.

    ``SIMULATED`` — the gateway's dry-run branch ran end to end and the book was written with
    ``simulated=true``. ``SENT`` — a real order was accepted by the broker and is not yet
    filled; nothing was written to the book (SW7.1). ``FILLED`` — a real action is complete
    at the broker; for a ``RAISE_GTT_STOP`` line or a re-arm it means the trigger is resting.
    ``BLOCKED`` — refused here or by the gateway, with its words in ``reason``; nothing was
    sent. ``REJECTED`` — the broker refused or the call failed; ``order`` says which.
    """

    status: str  # "SIMULATED" | "SENT" | "FILLED" | "BLOCKED" | "EXPIRED" | "REJECTED"
    reason: str  # human sentence, "" on success
    order: dict | None  # the gateway's place() result, if any
    gtt: dict | None  # the gateway's place_gtt_stop() result, if any
    position_id: int | None
    simulated: bool


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


def first_live_quantity(quantity: int, *, sessions_left: int, simulated: bool) -> int:
    """SW7.2 / ``02`` §3.5: the first live sessions run at half size.

    Halved only for a REAL order: a simulated line at half size would make the paper record
    smaller than the rules it is meant to rehearse. Rounded down, never below one share — a
    one-share line halved to nothing would be a refusal dressed as a size.
    """
    if simulated or sessions_left <= 0 or quantity <= 1:
        return quantity
    return max(1, quantity // 2)


def is_simulated_gtt(gtt_id) -> bool:
    return isinstance(gtt_id, str) and gtt_id.startswith(_SIMULATED_GTT_PREFIX)


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
) -> ExecOutcome:
    """Execute one confirmed plan line through the gateway and write what happened.

    Raises ``HTTPException`` 400 / 404 / 410 / 409 before anything is touched (see
    :func:`_validate`). Past that point the line is marked ``CONFIRMED`` BEFORE the gateway is
    called, so a request that dies mid-way leaves a line that answers 409 on re-post rather
    than one that can be sent again.

    ``last_price`` is the instrument's last traded price when the page has one. A BUY needs
    none (its entry is the trigger); a SELL's simulated fill and a RAISE's stop check both do,
    and are ``BLOCKED`` — never guessed — without it.
    """
    line = _validate(store, plan_id=plan_id, line_id=line_id, confirm=confirm, now=now)
    kind = line["kind"]
    simulated = swing_gates().dry_run
    day = _session_day(now)
    mode = "DRY_RUN" if simulated else "LIVE"
    store.set_line(line_id, state="CONFIRMED")
    store.bump_session(day, mode=mode, confirms=1)
    try:
        if kind == BUY_ON_TRIGGER:
            outcome = await _buy(store, gw, line=line, plan_id=plan_id, now=now,
                                 simulated=simulated)
        elif kind == SELL_AT_OPEN:
            outcome = await _sell(store, gw, line=line, plan_id=plan_id, now=now,
                                  simulated=simulated, last_price=last_price)
        elif kind == RAISE_GTT_STOP:
            outcome = await _raise_stop(store, gw, line=line, plan_id=plan_id, now=now,
                                        simulated=simulated, last_price=last_price)
        else:
            outcome = ExecOutcome("BLOCKED", f"{kind!r} is not a kind this desk executes",
                                  None, None, None, simulated)
    except Exception:
        # Not swallowed: the line is marked so it cannot be re-posted, then the error goes up
        # to the route, which is where an untouchable-instrument refusal belongs.
        store.set_line(line_id, state="REJECTED")
        raise
    _record_line(store, line_id, outcome)
    if outcome.status in ("SIMULATED", "FILLED") and kind in (BUY_ON_TRIGGER, SELL_AT_OPEN):
        store.bump_session(day, mode=mode, fills=1)
    elif outcome.status in ("SIMULATED", "FILLED"):
        store.bump_session(day, mode=mode, manage_actions=1)
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


async def _buy(store, gw, *, line: dict, plan_id: str, now: dt.datetime,
               simulated: bool) -> ExecOutcome:
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
    sessions_left = int(store.config().get("first_live_sessions_left") or 0)
    qty = first_live_quantity(quantity, sessions_left=sessions_left, simulated=simulated)
    order = await gw.place(
        symbol=symbol, qty=qty, side="BUY", product="CNC", order_type="LIMIT",
        price=float(trigger), exchange="NSE", client_id=f"{plan_id}:{symbol}:BUY",
        gross_exposure=float(trigger * qty),
        tenant=_sole_tenant(), plan_tenant=_sole_tenant(),
    )
    status = _ORDER_STATUS.get(order.get("status", ""), "REJECTED")
    if status not in ("SIMULATED", "SENT"):
        return ExecOutcome(status, _blocked_reason(order, what="the buy"), order, None, None,
                           simulated)
    if not simulated and sessions_left > 0 and _session_day(now) not in _FIRST_LIVE_COUNTED:
        # SW7.2: the countdown is per session, not per order, and it starts the moment a real
        # order of this session went out — only then has the session been traded at half size.
        _FIRST_LIVE_COUNTED.add(_session_day(now))
        store.set_first_live_sessions_left(sessions_left - 1)
    if status == "SENT":
        # SW7.1: a live LIMIT order is not a fill. The gateway returns as soon as the broker
        # accepts it; the fill arrives later, and a stop armed for shares not yet held is a
        # trigger that sells what is not there. The line stays SENT with the order id; the
        # position and its GTT are written when the fill is known.
        return ExecOutcome("SENT", f"{symbol}: order {order.get('order_id')} accepted, "
                           f"not yet filled — no position, no stop, until it fills",
                           order, None, None, simulated)
    # Simulated: the whole line fills at the trigger, now.
    position_id = store.create_position({
        "instrument_id": int(line["instrument_id"]),
        "symbol": symbol,
        "setup": line.get("setup"),
        "entry_date": _session_day(now),
        "entry_avg": trigger,
        "quantity_entered": qty,
        "quantity_open": qty,
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
        "simulated": True,
    })
    store.add_fill({
        "position_id": position_id, "side": "BUY", "quantity": qty, "price": trigger,
        "filled_at": _aware(now), "journal_ref": order.get("order_id"), "simulated": True,
    })
    gtt = await _arm(gw, symbol=symbol, qty=qty, stop=stop, last_price=trigger,
                     client_id=f"{plan_id}:{symbol}:GTT")
    if gtt.get("status") in GTT_PLACED_STATUSES:
        store.update_position(position_id, _gtt_fields(gtt, stop=stop, now=now))
        return ExecOutcome("SIMULATED", "", order, gtt, position_id, True)
    # The shares are (simulated as) held and the stop is not resting: the honest record is a
    # NAKED position, which the page leads with and ``rearm_gtt`` exists for.
    return ExecOutcome("SIMULATED",
                       f"{symbol}: bought, but the stop was not armed — "
                       f"{_blocked_reason(gtt, what='the GTT')}; position {position_id} is "
                       f"NAKED, re-arm it", order, gtt, position_id, True)


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
    the client id is added to the result so the position row can carry the same handle."""
    result = await gw.place_gtt_stop(
        symbol=symbol, qty=int(qty), trigger=float(stop), last_price=float(last_price),
        exchange="NSE", client_id=client_id,
        tenant=_sole_tenant(), plan_tenant=_sole_tenant(),
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
