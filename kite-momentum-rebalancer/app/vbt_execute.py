"""The volume-breakout sleeve's execute logic — one confirmed plan line becomes an order.

VB6's half that holds no page: ``app/vbt_desk.py`` owns the route and the Postgres store, this
module owns what happens between "confirm" and the book. It is written against the
:class:`VbtStore` protocol so the same code runs over the desk's Postgres adapter on a weekday
morning and over an in-memory dict in a test, and it is **handed** its gateway rather than
building one, so a test can prove the whole path with a broker client that explodes on contact.

THE RULES IT ENFORCES, AND WHERE THEY COME FROM

* ``docs/vbt/02`` Track C §3 — **no auto-execution, and no flag that changes it.** Nothing here
  runs without ``confirm="true"``, a ``plan_id`` issued in the last thirty minutes, and a line
  still ``PROPOSED``; the refusals are 400 / 404 / 410 / 409. Non-negotiable 1's named exception
  belongs to the swing sleeve (DECISIONS-VB PACK.2) and there is no ``VBT_AUTO_EXECUTE`` to find.
* Track C §5 — **the sleeve never sells a holding it did not buy.** A ``SELL_AT_OPEN`` is checked
  against ``vb_position.quantity_open``, never against the broker's holdings, which hold the
  weekly book and the swing book too.
* ``04`` §6.1 — **a stop never falls**, and every filled quantity gets one the same session. The
  GTT is armed in the same request as the fill that created it; the evening's ``ARM_GTT`` line is
  the backstop for the case where that did not happen.
* ``04`` §7.1 — **a ``PLACE_LIMIT`` is a LIMIT at the signal's close.** Not a market order and
  not a GTT-buy: the level *is* the strategy, and a fill anywhere above it is a different trade
  from the one the backtest measured.
* ``04`` §9.4 — ``client_id = plan_id:symbol:kind``, so a re-posted form cannot double-send even
  before the gateway's idempotency map is consulted.
* ``02`` Track B — with ``BASKFY_VBT_EXECUTION_ENABLED=false`` the **same** code path runs through
  the gateway's dry-run branch and records ``simulated=true`` whatever ``DRY_RUN`` says
  (:func:`vbt_gates`). There is no second branch for paper trading: the twenty DRY_RUN sessions
  ``02`` §3.1 counts are a rehearsal of this code.

Every order — the buy, the market sell, the stop and the cancel of a working limit — goes through
the :class:`OrderGateway` this module is handed. Nothing here names a broker method.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import os
from contextlib import AbstractContextManager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Protocol

from baskfy_execution.gtt import GTT_PLACED_STATUSES, StopBand
from baskfy_execution.tenancy import TenantIds
from fastapi import HTTPException

from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG
from baskfy_core.vbt.plan import LineKind

from . import config as C
from .core import gateway as _gateway_module
from .core.gateway import OrderGateway, ProductGates

log = logging.getLogger("vbt.execute")

#: The four kinds a confirm may act on. There is no fifth, and no kind that opens a short.
EXECUTABLE_KINDS: Final[frozenset[str]] = frozenset(kind.value for kind in LineKind)

#: What a place() answers when the order reached the broker and when it did not.
PLACED_STATUSES: Final[frozenset[str]] = frozenset({"PLACED", "DUPLICATE"})
DRY_RUN_STATUSES: Final[frozenset[str]] = frozenset({"DRY_RUN"})

#: This sleeve's own journal file, beside the weekly book's and the swing book's. Three books,
#: three journals: an operator reading one should not have to filter out the other two.
VBT_JOURNAL_NAME: Final = "vbt_orders_journal.jsonl"


def vbt_gates() -> ProductGates:
    """`02` Track B: an order is real only when the desk is out of DRY_RUN **and** the flag is on.

    Two switches, deliberately independent — going live and turning this sleeve on are separate
    decisions, and either can be reversed without the other. Intraday and options are false
    unconditionally rather than read from config: this sleeve is CNC-only (Track C §1) whatever
    the weekly desk is allowed to do.
    """
    return ProductGates(
        dry_run=C.DRY_RUN or not C.VBT_EXECUTION_ENABLED,
        intraday_enabled=False,
        options_enabled=False,
    )


def vbt_stop_band() -> StopBand:
    """`04` §6.1 — 0.5-15% below the entry. The desk's 8-12% is the weekly book's and the swing
    sleeve's 0.5-10% is its own; a flat 12% stop with a 15% ceiling needs this one."""
    return StopBand(min_pct=C.VBT_STOP_BAND_MIN, max_pct=C.VBT_STOP_BAND_MAX)


def vbt_journal_path() -> str:
    """``vbt_orders_journal.jsonl`` beside the desk's journal — read off the shim at call time,
    so a test that isolates one isolates all three."""
    return os.path.join(os.path.dirname(_gateway_module.JOURNAL), VBT_JOURNAL_NAME)


def build_vbt_gateway(kc: Any, risk: Any) -> OrderGateway:  # noqa: ANN401 - the desk's clients
    """The desk's gateway, bound to **this** sleeve's gates, band and journal.

    A separate instance from the weekly book's and the swing book's, for the reason
    ``build_swing_gateway`` gives: the idempotency maps are per instance and keyed on
    ``client_id``, the three books mint ids from different plan namespaces, and a shared instance
    would let one book's gates callable be swapped for another's by whichever caller built it
    first.
    """
    return OrderGateway(
        kc,
        risk,
        gates=vbt_gates,
        journal_path=vbt_journal_path(),
        stop_band=vbt_stop_band(),
    )


def is_simulated() -> bool:
    """Whether a confirm right now would be journalled ``simulated=true``."""
    return bool(vbt_gates().dry_run)


class VbtStore(Protocol):
    """One user's ``vb_`` rows. All prices ``Decimal``."""

    def plan(self, plan_id: str) -> dict | None:
        """keys: id, plan_id, session_date, source, built_at, expires_at, gate"""
        ...

    def line(self, line_id: int) -> dict | None:
        """keys: id, plan_pk, plan_id, kind, instrument_id, symbol, quantity, limit_price,
        stop_price, value_inr, note, state, client_id, order_id, position_id"""
        ...

    def set_line(
        self,
        line_id: int,
        *,
        state: str,
        journal_ref: str | None = None,
        order_id: int | None = None,
        position_id: int | None = None,
        note: str | None = None,
    ) -> None: ...

    def open_position_for(self, instrument_id: int) -> dict | None: ...

    def position(self, position_id: int) -> dict | None: ...

    def create_position(self, fields: dict) -> int: ...

    def update_position(self, position_id: int, fields: dict) -> None: ...

    def add_fill(self, fields: dict) -> int: ...

    def order(self, order_id: int) -> dict | None: ...

    def create_order(self, fields: dict) -> int: ...

    def update_order(self, order_id: int, fields: dict) -> None: ...

    def working_order_for(self, instrument_id: int) -> dict | None: ...

    def bump_session(
        self, day: dt.date, *, mode: str, confirms: int = 0, fills: int = 0, exits: int = 0
    ) -> None: ...

    def lock_session_for_update(self, day: dt.date) -> AbstractContextManager[None]:
        """The day's ``vb_session`` row locked for the whole of a confirm — the re-read, the
        gateway call and the writes — so two browser tabs cannot confirm past a cap together."""
        ...

    def entries_taken(self, day: dt.date) -> int:
        """`04` §5.3 — orders of this session already CONFIRMED, SENT or FILLED."""
        ...


@dataclass(frozen=True)
class ExecOutcome:
    """What one confirmed line came to.

    ``SIMULATED`` — the gateway's dry-run branch ran end to end and the book was written with
    ``simulated=true``. ``SENT`` — a real order was accepted and is not yet filled. ``FILLED`` —
    the action is complete at the broker; for an ``ARM_GTT`` line it means the trigger is
    resting. ``BLOCKED`` — refused here or by the gateway, with its words in ``reason``, and
    nothing was sent. ``REJECTED`` — the broker refused.
    """

    status: str
    reason: str
    order: dict | None
    gtt: dict | None
    order_row_id: int | None
    position_id: int | None
    simulated: bool

    def as_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _blocked(reason: str, *, simulated: bool) -> ExecOutcome:
    return ExecOutcome(
        status="BLOCKED",
        reason=reason,
        order=None,
        gtt=None,
        order_row_id=None,
        position_id=None,
        simulated=simulated,
    )


def _validate(store: VbtStore, plan_id: str, line_id: int, confirm: str, now: dt.datetime) -> dict:
    """The four refusals, in the order a caller hits them. Raises ``HTTPException``.

    Order matters: a missing ``confirm`` is answered before anything is read, so a form posted by
    accident costs no database work and no broker read.
    """
    if str(confirm).lower() != "true":
        raise HTTPException(400, "confirm=true is required — nothing fires without it")
    plan = store.plan(plan_id)
    if plan is None:
        raise HTTPException(404, f"no plan {plan_id}")
    expires = plan["expires_at"]
    if expires is not None and now > expires:
        raise HTTPException(
            410, f"plan {plan_id} expired at {expires.isoformat()}; rebuild it and confirm again"
        )
    line = store.line(line_id)
    if line is None or str(line["plan_id"]) != str(plan_id):
        raise HTTPException(404, f"no line {line_id} on plan {plan_id}")
    if line["kind"] not in EXECUTABLE_KINDS:
        raise HTTPException(400, f"a {line['kind']} line cannot be executed")
    if line["state"] != "PROPOSED":
        raise HTTPException(
            409, f"line {line_id} is already {line['state']} — a line is confirmed once"
        )
    return line


def _tenant() -> TenantIds:
    return TenantIds(user_id=int(C.SOLE_USER_ID), broker_account_id=int(C.SOLE_BROKER_ACCOUNT_ID))


async def _place_limit(  # noqa: PLR0913 - a confirm is its line, its gateway and its clock
    store: VbtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    line: dict,
    plan: dict,
    gates: ProductGates,
    now: dt.datetime,
) -> ExecOutcome:
    """`04` §7.1 and §9.5 — a LIMIT buy at the signal's close, and a ``vb_order`` that records it.

    **Not a market order.** The whole strategy is that it bids and waits: a fill above the signal
    close is the trade the study measured at 9.6% a year rather than 18.2%. If the tape never
    comes back, the order expires after three sessions and the book is unchanged — which is the
    outcome the rule is content with.

    The session cap is re-read here, under the row lock the caller holds: a plan line's size is a
    preview, and two tabs confirming a fourth entry between them is exactly what the lock exists
    to stop (``04`` §9.4).
    """
    taken = store.entries_taken(plan["session_date"])
    cap = DEFAULT_VBT_CONFIG.sizing.max_new_entries_per_session
    if taken >= cap:
        reason = f"SESSION_CAP: {taken} entries already taken today; {cap} is the session's cap"
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run)
    if store.open_position_for(line["instrument_id"]) is not None:
        reason = "ALREADY_HELD: the sleeve holds this name; it is never averaged down"
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run)
    if store.working_order_for(line["instrument_id"]) is not None:
        reason = "ALREADY_WORKING: a limit is already resting in this name"
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run)

    limit = Decimal(str(line["limit_price"]))
    quantity = int(line["quantity"])
    result = await gateway.place(
        symbol=line["symbol"],
        qty=quantity,
        side="BUY",
        product="CNC",
        order_type="LIMIT",
        price=float(limit),
        client_id=line["client_id"],
        gross_exposure=float(limit * quantity),
        tenant=_tenant(),
        plan_tenant=_tenant(),
    )
    status = str(result.get("status") or "")
    if status not in PLACED_STATUSES | DRY_RUN_STATUSES:
        reason = str(result.get("error") or f"the gateway answered {status}")
        store.set_line(line["id"], state="REJECTED", note=reason)
        return ExecOutcome(
            "BLOCKED" if status.endswith("BLOCKED") else "REJECTED",
            reason,
            result,
            None,
            None,
            None,
            gates.dry_run,
        )

    order_row = store.create_order(
        {
            "instrument_id": line["instrument_id"],
            "signal_date": plan["session_date"],
            "limit_price": limit,
            "stop_price": Decimal(str(line["stop_price"])),
            "quantity": quantity,
            "state": "SENT",
            "broker_order_id": result.get("order_id"),
            "client_id": line["client_id"],
            "simulated": gates.dry_run,
        }
    )
    store.set_line(
        line["id"],
        state="SENT",
        journal_ref=str(result.get("order_id") or ""),
        order_id=order_row,
    )
    store.bump_session(
        plan["session_date"], mode="DRY_RUN" if gates.dry_run else "LIVE", confirms=1
    )
    if status in DRY_RUN_STATUSES:
        # `02` Track B: the dry-run branch is a complete fill at the limit, so the rehearsal
        # exercises the position, the fill and the stop rather than stopping at the order.
        return await _simulate_fill(store, gateway, line, plan, order_row, gates, now)
    return ExecOutcome("SENT", "", result, None, order_row, None, gates.dry_run)


async def _simulate_fill(  # noqa: PLR0913 - the rehearsal needs the whole line's context
    store: VbtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    line: dict,
    plan: dict,
    order_row: int,
    gates: ProductGates,
    now: dt.datetime,
) -> ExecOutcome:
    """The DRY_RUN fill: a position, its fill row, and its GTT — the whole path, simulated."""
    limit = Decimal(str(line["limit_price"]))
    quantity = int(line["quantity"])
    stop = Decimal(str(line["stop_price"]))
    position_id = store.create_position(
        {
            "instrument_id": line["instrument_id"],
            "entry_date": plan["session_date"],
            "entry_avg": limit,
            "quantity_entered": quantity,
            "quantity_open": quantity,
            "initial_stop": stop,
            "stop_price": stop,
            "state": "OPEN",
            "simulated": True,
        }
    )
    store.add_fill(
        {
            "position_id": position_id,
            "order_id": order_row,
            "side": "BUY",
            "quantity": quantity,
            "price": limit,
            "filled_at": now,
            "simulated": True,
        }
    )
    # The order-to-position link is `vb_order.position_id` (`03` §5), and it is set **here**.
    # Until VB10's drill ran this against a real Postgres it was written the other way round, as
    # a `vb_position.order_id` the schema has never had: the in-memory store the unit tests use
    # accepted any key, so the mistake was invisible to every test that existed.
    store.update_order(
        order_row,
        {
            "state": "FILLED",
            "filled_quantity": quantity,
            "avg_fill_price": limit,
            "position_id": position_id,
        },
    )
    store.set_line(line["id"], state="FILLED", position_id=position_id)
    store.bump_session(plan["session_date"], mode="DRY_RUN", fills=1)
    gtt = await _arm_stop(store, gateway, line["symbol"], position_id, quantity, stop, limit, now)
    return ExecOutcome("SIMULATED", "", None, gtt, order_row, position_id, True)


async def _arm_stop(  # noqa: PLR0913 - a stop is its instrument, its size and its two prices
    store: VbtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    symbol: str,
    position_id: int,
    quantity: int,
    stop: Decimal,
    last_price: Decimal,
    now: dt.datetime,
) -> dict | None:
    """Non-negotiable 4: every buy gets a GTT stop the same session.

    The cushion (`04` §6.1) rests the GTT's own LIMIT leg 3% under its trigger, so it fills on the
    way down the way a market stop would; the weekly book keeps the gateway's own value and never
    sees this one.
    """
    result = await gateway.place_gtt_stop(
        symbol=symbol,
        qty=quantity,
        trigger=float(stop),
        last_price=float(last_price),
        limit_fraction=DEFAULT_VBT_CONFIG.exits.gtt_limit_fraction,
        tenant=_tenant(),
        plan_tenant=_tenant(),
    )
    status = str(result.get("status") or "")
    if status in GTT_PLACED_STATUSES or status.startswith("DRY_RUN"):
        store.update_position(
            position_id,
            {"gtt_id": str(result.get("gtt_id") or ""), "gtt_trigger": stop, "gtt_armed_at": now},
        )
    else:
        log.warning("%s: the GTT was not armed (%s)", symbol, status)
    return result


async def _sell_at_open(  # noqa: PLR0913 - a sell is its line, its book and its gateway
    store: VbtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    line: dict,
    plan: dict,
    gates: ProductGates,
    now: dt.datetime,
    last_price: Decimal | None = None,
) -> ExecOutcome:
    """`04` §6.2 — and Track C §5's whole content: never more than the sleeve owns, and only
    what it owns."""
    position = store.open_position_for(line["instrument_id"])
    if position is None:
        reason = (
            f"{line['symbol']} is not in this sleeve's book — it may be the weekly book's or "
            f"the swing book's, and this sleeve never sells a holding it did not buy"
        )
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run)
    owned = int(position["quantity_open"])
    quantity = int(line["quantity"])
    if quantity > owned:
        reason = f"the line sells {quantity} and the sleeve holds {owned}"
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run)

    # A MARKET order has no price of its own, and the risk layer will not value one without a
    # reference. A live read is the right answer and the desk supplies it per confirm; the entry
    # is the fallback, and it is a fallback rather than a refusal **because this is an exit**.
    # Blocking a sell for want of a quote would leave a position the rules have decided to close
    # sitting in the book, which is the failure the rule exists to prevent. A buy in the same
    # position is refused, not guessed — see `04` §7.1: the level is the strategy.
    reference = last_price or Decimal(str(position["entry_avg"]))
    result = await gateway.place(
        symbol=line["symbol"],
        qty=quantity,
        side="SELL",
        product="CNC",
        order_type="MARKET",
        client_id=line["client_id"],
        reference_price=float(reference),
        gross_exposure=float(reference * quantity),
        tenant=_tenant(),
        plan_tenant=_tenant(),
    )
    status = str(result.get("status") or "")
    if status not in PLACED_STATUSES | DRY_RUN_STATUSES:
        reason = str(result.get("error") or f"the gateway answered {status}")
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run)

    remaining = owned - quantity
    store.add_fill(
        {
            "position_id": position["id"],
            "order_id": None,
            "side": "SELL",
            "quantity": quantity,
            # A simulated exit has no fill of its own; the reference price is what it was
            # valued at, and the row says `simulated` so nothing reads it as a trade.
            "price": reference,
            "filled_at": now,
            "simulated": gates.dry_run,
        }
    )
    store.update_position(
        position["id"],
        {
            "quantity_open": remaining,
            "state": "CLOSED" if remaining == 0 else "OPEN",
            "closed_on": plan["session_date"] if remaining == 0 else None,
            "close_reason": "EMA_EXIT" if remaining == 0 else None,
            "exit_queued_for": None,
            "exit_reason_queued": None,
        },
    )
    store.set_line(
        line["id"],
        state="FILLED" if status in DRY_RUN_STATUSES else "SENT",
        journal_ref=str(result.get("order_id") or ""),
        position_id=int(position["id"]),
    )
    store.bump_session(
        plan["session_date"],
        mode="DRY_RUN" if gates.dry_run else "LIVE",
        confirms=1,
        exits=1,
    )
    return ExecOutcome(
        "SIMULATED" if gates.dry_run else "SENT",
        "",
        result,
        None,
        None,
        int(position["id"]),
        gates.dry_run,
    )


async def _cancel_limit(  # noqa: PLR0913 - a cancel is its line, its order and its gateway
    store: VbtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    line: dict,
    plan: dict,
    gates: ProductGates,
    now: dt.datetime,
) -> ExecOutcome:
    """VB7: the limit stops being an order. The **only** way a working order leaves the book."""
    order = store.working_order_for(line["instrument_id"])
    if order is None:
        reason = f"no working order in {line['symbol']} to cancel"
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run)
    broker_id = order.get("broker_order_id")
    result: dict[str, Any] = {"status": "NO_BROKER_ORDER"}
    if broker_id:
        result = await gateway.cancel_order(
            order_id=str(broker_id),
            symbol=line["symbol"],
            client_id=f"{line['client_id']}:cancel",
            tenant=_tenant(),
            plan_tenant=_tenant(),
        )
        status = str(result.get("status") or "")
        if status.endswith("BLOCKED") or status == "ERROR":
            reason = str(result.get("error") or f"the gateway answered {status}")
            store.set_line(line["id"], state="REJECTED", note=reason)
            return _blocked(reason, simulated=gates.dry_run)
    store.update_order(
        int(order["id"]),
        {
            "state": "CANCELLED",
            "cancel_reason": "EXPIRY_SWEEP",
            "cancelled_on": plan["session_date"],
        },
    )
    store.set_line(line["id"], state="FILLED", order_id=int(order["id"]))
    store.bump_session(
        plan["session_date"], mode="DRY_RUN" if gates.dry_run else "LIVE", confirms=1
    )
    return ExecOutcome(
        "SIMULATED" if gates.dry_run else "FILLED",
        "",
        result,
        None,
        int(order["id"]),
        None,
        gates.dry_run,
    )


async def _arm_gtt_line(  # noqa: PLR0913 - a re-arm is its line, its book and its gateway
    store: VbtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    line: dict,
    plan: dict,
    gates: ProductGates,
    now: dt.datetime,
) -> ExecOutcome:
    """The backstop for non-negotiable 4: a filled position that somehow has no resting stop."""
    position = store.open_position_for(line["instrument_id"])
    if position is None:
        reason = f"{line['symbol']} is not in this sleeve's book"
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run)
    stop = Decimal(str(line["stop_price"] or position["stop_price"]))
    gtt = await _arm_stop(
        store,
        gateway,
        line["symbol"],
        int(position["id"]),
        int(position["quantity_open"]),
        stop,
        Decimal(str(position["entry_avg"])),
        now,
    )
    store.set_line(line["id"], state="FILLED", position_id=int(position["id"]))
    store.bump_session(
        plan["session_date"], mode="DRY_RUN" if gates.dry_run else "LIVE", confirms=1
    )
    return ExecOutcome(
        "SIMULATED" if gates.dry_run else "FILLED",
        "",
        None,
        gtt,
        None,
        int(position["id"]),
        gates.dry_run,
    )


async def execute_line(  # noqa: PLR0913 - a confirm is its store, its gateway and its request
    store: VbtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    *,
    plan_id: str,
    line_id: int,
    confirm: str,
    now: dt.datetime | None = None,
    last_price: Decimal | None = None,
) -> ExecOutcome:
    """One confirmed line becomes an order, a stop, a cancel, or a refusal with its reason.

    The whole of it runs under a row lock on the day's ``vb_session`` (``04`` §9.4): the re-read
    of the book, the gateway call and the writes. Two browser tabs confirming a fourth entry
    between them is exactly what the lock exists to stop.
    """
    stamp = now or dt.datetime.now(tz=dt.UTC)
    gates = vbt_gates()
    line = _validate(store, plan_id, line_id, confirm, stamp)
    plan = store.plan(plan_id)
    assert plan is not None  # `_validate` has already refused a missing one
    handlers = {
        LineKind.PLACE_LIMIT.value: _place_limit,
        LineKind.SELL_AT_OPEN.value: _sell_at_open,
        LineKind.CANCEL_LIMIT.value: _cancel_limit,
        LineKind.ARM_GTT.value: _arm_gtt_line,
    }
    with store.lock_session_for_update(plan["session_date"]):
        if line["kind"] == LineKind.SELL_AT_OPEN.value:
            return await _sell_at_open(
                store, gateway, line, plan, gates, stamp, last_price=last_price
            )
        return await handlers[line["kind"]](store, gateway, line, plan, gates, stamp)
