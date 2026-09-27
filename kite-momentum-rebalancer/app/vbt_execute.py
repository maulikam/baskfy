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

from baskfy_execution.gtt import GTT_MODIFIED_STATUSES, GTT_PLACED_STATUSES, StopBand
from baskfy_execution.tenancy import TenantIds
from fastapi import HTTPException

from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG
from baskfy_core.vbt.plan import LineKind

from . import config as C
from . import reconcile as _reconcile
from .core import gateway as _gateway_module
from .core.gateway import OrderGateway, ProductGates

log = logging.getLogger("vbt.execute")

#: The four kinds a confirm may act on. There is no fifth, and no kind that opens a short.
EXECUTABLE_KINDS: Final[frozenset[str]] = frozenset(kind.value for kind in LineKind)

#: What a place() answers when the order reached the broker and when it did not.
PLACED_STATUSES: Final[frozenset[str]] = frozenset({"PLACED", "DUPLICATE"})
DRY_RUN_STATUSES: Final[frozenset[str]] = frozenset({"DRY_RUN"})

#: Kite's ``market_protection`` on the LIVE plan's MARKET buy (LV8, 28 Sep 2026; the same value
#: TWT's ``_buy_at_open`` sends, for the same reason: Zerodha refuses an API MARKET order without
#: it — the box's record for 23 and 24 Sep 2026). ``-1`` is Kite's own auto band.
VBT_MARKET_PROTECTION: Final[float] = -1.0

#: Kite's order statuses, as the order book reports them (LV2). A dead order is one the broker
#: will not fill any further; whatever filled before it died is the position.
ORDER_COMPLETE: Final = "COMPLETE"
ORDER_DEAD_STATUSES: Final[frozenset[str]] = frozenset({"REJECTED", "CANCELLED", "CANCELLED AMO"})
#: A simulated GTT id, as the gateway mints it; nothing rests at the exchange under it.
SIMULATED_GTT_PREFIX: Final = "DRY-"
_ZERO: Final = Decimal(0)
_IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))
_FOUR_DP: Final = Decimal("0.0001")

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

    def order_by_broker_id(self, broker_order_id: str) -> dict | None:
        """The ``vb_order`` the broker knows by this id (LV2)."""
        ...

    def open_orders(self) -> list[dict]:
        """Every buy the broker accepted and has not finished (``SENT``/``PARTIAL``) and every
        pending exit (``lv_exit_order`` ``SENT``/``PARTIAL``), each with its ``broker_order_id``,
        ``symbol``, ``quantity``, ``filled_quantity`` and ``reference_price`` (LV2)."""
        ...

    def open_positions(self) -> list[dict]:
        """This sleeve's ``OPEN`` positions with shares (keys as :meth:`position`)."""
        ...

    def open_protection_issues(self) -> list[dict]:
        """The reconciler's open findings for THIS sleeve (``symbol``, ``kind``, ``detail``)."""
        ...

    def create_exit_order(self, fields: dict) -> int:
        """An ``lv_exit_order`` row: a sell the broker accepted, booked only when it fills."""
        ...

    def exit_order_by_broker_id(self, broker_order_id: str) -> dict | None: ...

    def update_exit_order(self, exit_order_id: int, fields: dict) -> None: ...

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
    unresolved = _reconcile.protection_unresolved(store, naked=naked_positions(store))
    if unresolved:
        # LV2: protection first, entries second — a book with a stop it cannot account for does
        # not add a position until the reconciler says the book is whole again.
        reason = _reconcile.refusal(unresolved)
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


async def _buy_at_market(  # noqa: PLR0913 - a confirm is its line, its gateway and its clock
    store: VbtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    line: dict,
    plan: dict,
    gates: ProductGates,
    now: dt.datetime,
    *,
    last_price: Decimal | None = None,
) -> ExecOutcome:
    """LV8 (DECISIONS-VB VB14, Maulik 28 Sep 2026) — a **LIVE** plan's entry: a MARKET buy now.

    The live scan read today's bar so far and found the signal *during* the session; Maulik's
    decision is to take it now, at market, with Kite market protection, rather than bid at a
    close that has not printed. The same refusals as :func:`_place_limit` (session cap, held,
    working, protection unresolved), the same ``vb_order`` row, the same fill path and **the GTT
    armed from the fill** (non-negotiable 4). The order's ``limit_price`` records the reference
    the size was previewed against — the live price the desk supplies per confirm, else the
    plan's own — so the book can say what the buy was worth when it was sent.
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
        reason = "ALREADY_WORKING: an order is already working in this name"
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run)
    unresolved = _reconcile.protection_unresolved(store, naked=naked_positions(store))
    if unresolved:
        reason = _reconcile.refusal(unresolved)
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run)

    preview = Decimal(str(line["limit_price"] or 0))
    reference = last_price if last_price is not None and last_price > _ZERO else preview
    if reference <= _ZERO:
        reason = "no price to value this market buy against"
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run)
    quantity = int(line["quantity"])
    result = await gateway.place(
        symbol=line["symbol"],
        qty=quantity,
        side="BUY",
        product="CNC",
        order_type="MARKET",
        market_protection=VBT_MARKET_PROTECTION,
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
            "limit_price": reference,
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
        note=f"market buy sent against a reference of {reference}",
    )
    store.bump_session(
        plan["session_date"], mode="DRY_RUN" if gates.dry_run else "LIVE", confirms=1
    )
    if status in DRY_RUN_STATUSES:
        return await _apply_fill(
            store,
            gateway,
            line=line,
            order_row=order_row,
            quantity=quantity,
            fill_price=reference,
            entry_date=plan["session_date"],
            gates=gates,
            now=now,
            complete=True,
        )
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
    """The DRY_RUN fill: the whole line, at the limit, now — the same bookkeeping as a live
    COMPLETE (:func:`_apply_fill`), with ``simulated=true`` on every row."""
    return await _apply_fill(
        store,
        gateway,
        line=line,
        order_row=order_row,
        quantity=int(line["quantity"]),
        fill_price=Decimal(str(line["limit_price"])),
        entry_date=plan["session_date"],
        gates=gates,
        now=now,
        complete=True,
    )


async def _apply_fill(  # noqa: PLR0913 - a fill is its order, its price and its size
    store: VbtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    *,
    line: dict | None,
    order_row: int,
    quantity: int,
    fill_price: Decimal,
    entry_date: dt.date,
    gates: ProductGates,
    now: dt.datetime,
    complete: bool,
) -> ExecOutcome:
    """A filled buy becomes a ``vb_position``, a ``vb_fill`` **and a resting GTT** (LV2).

    One path for the rehearsal and the broker's fill: ``gates.dry_run`` decides ``simulated``,
    ``complete`` decides whether the order closes ``FILLED`` or stays ``PARTIAL`` for
    :func:`on_order_update` to grow. The stop is the order's own (``04`` §6.1: 12 % under the
    signal close, fixed at plan time), for exactly the shares filled.
    """
    order = store.order(order_row)
    assert order is not None
    stop = Decimal(str(order["stop_price"]))
    symbol = str(line["symbol"]) if line is not None else str(order["symbol"])
    position_id = store.create_position(
        {
            "instrument_id": int(order["instrument_id"]),
            "entry_date": entry_date,
            "entry_avg": fill_price.quantize(_FOUR_DP),
            "quantity_entered": quantity,
            "quantity_open": quantity,
            "initial_stop": stop,
            "stop_price": stop,
            "state": "OPEN",
            "simulated": gates.dry_run,
        }
    )
    store.add_fill(
        {
            "position_id": position_id,
            "order_id": order_row,
            "side": "BUY",
            "quantity": quantity,
            "price": fill_price,
            "filled_at": now,
            "simulated": gates.dry_run,
        }
    )
    # The order-to-position link is `vb_order.position_id` (`03` §5), and it is set **here**.
    # Until VB10's drill ran this against a real Postgres it was written the other way round, as
    # a `vb_position.order_id` the schema has never had: the in-memory store the unit tests use
    # accepted any key, so the mistake was invisible to every test that existed.
    store.update_order(
        order_row,
        {
            "state": "FILLED" if complete else "PARTIAL",
            "filled_quantity": quantity,
            "avg_fill_price": fill_price,
            "position_id": position_id,
        },
    )
    line_id = int(line["id"]) if line is not None else _line_for_order(store, order)
    if line_id is not None:
        store.set_line(line_id, state="FILLED" if complete else "SENT", position_id=position_id)
    store.bump_session(entry_date, mode="DRY_RUN" if gates.dry_run else "LIVE", fills=1)
    gtt = await _arm_stop(store, gateway, symbol, position_id, quantity, stop, fill_price, now)
    status = "SIMULATED" if gates.dry_run else ("FILLED" if complete else "SENT")
    return ExecOutcome(status, "", None, gtt, order_row, position_id, gates.dry_run)


def _line_for_order(store: VbtStore, order: dict) -> int | None:
    """The plan line that produced ``order``, when the store can find it by client id."""
    finder = getattr(store, "line_by_client_id", None)
    if finder is None or not order.get("client_id"):
        return None
    line = finder(str(order["client_id"]))
    return None if line is None else int(line["id"])


def naked_positions(store: VbtStore) -> list[dict]:
    """Every ``OPEN`` position with shares and no resting ``gtt_id`` (non-negotiable 4)."""
    return [
        row
        for row in store.open_positions()
        if row.get("gtt_id") is None and int(row.get("quantity_open") or 0) > 0
    ]


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

    if status not in DRY_RUN_STATUSES:
        # LV2 (the review's exit-side defect): a live sell the broker ACCEPTED is not a sell that
        # FILLED. Nothing is booked here — the exit order is recorded, the line is SENT, and
        # :func:`on_order_update` reduces or closes the position from the broker's own filled
        # quantity and average price when the order book reports them.
        exit_id = store.create_exit_order(
            {
                "sleeve": "vbt",
                "position_id": int(position["id"]),
                "line_id": int(line["id"]),
                "symbol": line["symbol"],
                "broker_order_id": result.get("order_id"),
                "client_id": line["client_id"],
                "quantity": quantity,
                "reference_price": reference,
                "state": "SENT",
                "filled_quantity": 0,
                "reason": "EMA_EXIT",
                "simulated": False,
            }
        )
        store.update_position(
            position["id"],
            {"exit_queued_for": plan["session_date"], "exit_reason_queued": "EMA_EXIT"},
        )
        store.set_line(
            line["id"],
            state="SENT",
            journal_ref=str(result.get("order_id") or ""),
            position_id=int(position["id"]),
        )
        store.bump_session(plan["session_date"], mode="LIVE", confirms=1)
        return ExecOutcome(
            "SENT",
            f"{line['symbol']}: sell {quantity} accepted as order {result.get('order_id')} "
            f"(exit {exit_id}); the position is reduced when the broker reports the fill",
            result,
            None,
            None,
            int(position["id"]),
            False,
        )

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


async def on_order_update(
    store: VbtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    payload: dict,
    *,
    now: dt.datetime,
) -> ExecOutcome | None:
    """A broker report on one of this sleeve's orders — a resting buy or a pending exit (LV2).

    ``payload`` is Kite's order-book shape (``order_id``, ``status``, ``filled_quantity``,
    ``average_price``). ``None`` when the order is not this sleeve's or the report adds nothing.
    Idempotent: a repeated, duplicate or out-of-order report writes nothing. Under the session
    lock, a buy that filled (in part or whole) becomes the position, its fill and its one GTT,
    grown and re-sized as more arrives; a sell that filled reduces the position at the broker's
    average for exactly the filled shares, re-sizes the resting GTT to what is left and cancels
    it when nothing is; a dead order closes its line with the broker's reason.
    """
    broker_id = str(payload.get("order_id") or "")
    if not broker_id:
        return None
    status = str(payload.get("status") or "").upper()
    filled = int(payload.get("filled_quantity") or 0)
    raw_price = payload.get("average_price")
    average = Decimal(str(raw_price)) if raw_price is not None else _ZERO
    gates = vbt_gates()
    day = now.astimezone(_IST).date()
    exit_order = store.exit_order_by_broker_id(broker_id)
    if exit_order is not None:
        with store.lock_session_for_update(day):
            return await _apply_exit_update(
                store, gateway, exit_order, status=status, filled=filled, average=average, gates=gates, now=now
            )
    order = store.order_by_broker_id(broker_id)
    if order is None or str(order.get("state")) not in ("SENT", "PARTIAL"):
        return None
    with store.lock_session_for_update(day):
        current = store.order(int(order["id"]))
        if current is None or str(current.get("state")) not in ("SENT", "PARTIAL"):
            return None
        return await _apply_buy_update(
            store, gateway, current, status=status, filled=filled, average=average, gates=gates, now=now, day=day
        )


async def _apply_buy_update(  # noqa: PLR0913 - a report is its order, its size and its price
    store: VbtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    order: dict,
    *,
    status: str,
    filled: int,
    average: Decimal,
    gates: ProductGates,
    now: dt.datetime,
    day: dt.date,
) -> ExecOutcome | None:
    recorded = int(order.get("filled_quantity") or 0)
    position_id = order.get("position_id")
    complete = status == ORDER_COMPLETE
    outcome: ExecOutcome | None = None
    if filled > recorded and average > _ZERO:
        if position_id is None:
            outcome = await _apply_fill(
                store,
                gateway,
                line=None,
                order_row=int(order["id"]),
                quantity=filled,
                fill_price=average,
                entry_date=day,
                gates=gates,
                now=now,
                complete=complete,
            )
        else:
            outcome = await _grow_position(
                store, gateway, order=order, position_id=int(position_id), filled=filled, average=average, gates=gates, now=now, complete=complete
            )
    if status in ORDER_DEAD_STATUSES:
        held = outcome.position_id if outcome is not None else position_id
        line_id = _line_for_order(store, order)
        if held is not None:
            store.update_order(int(order["id"]), {"state": "FILLED"})
            if line_id is not None:
                store.set_line(line_id, state="FILLED", position_id=int(held), note=f"{status}: {max(filled, recorded)} filled, the rest never did")
            return outcome or ExecOutcome("FILLED", f"{status} with shares held", None, None, int(order["id"]), int(held), gates.dry_run)
        reason = str(order.get("cancel_reason") or status)
        store.update_order(
            int(order["id"]),
            {"state": "REJECTED" if status == "REJECTED" else "CANCELLED", "cancelled_on": day, "cancel_reason": "MANUAL" if status != "REJECTED" else None},
        )
        if line_id is not None:
            store.set_line(line_id, state="REJECTED", note=f"{status}: {reason}")
        return ExecOutcome("REJECTED", f"{status}: nothing filled", None, None, int(order["id"]), None, gates.dry_run)
    if complete and outcome is None and position_id is not None:
        store.update_order(int(order["id"]), {"state": "FILLED"})
        line_id = _line_for_order(store, order)
        if line_id is not None:
            store.set_line(line_id, state="FILLED", position_id=int(position_id))
        return ExecOutcome("FILLED", "", None, None, int(order["id"]), int(position_id), gates.dry_run)
    return outcome


async def _grow_position(  # noqa: PLR0913 - a growth is its order, its position and its size
    store: VbtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    *,
    order: dict,
    position_id: int,
    filled: int,
    average: Decimal,
    gates: ProductGates,
    now: dt.datetime,
    complete: bool,
) -> ExecOutcome:
    """More of the same buy filled: grow the position and re-size — never re-arm — its GTT."""
    position = store.position(position_id)
    assert position is not None
    symbol = str(position.get("symbol") or order.get("symbol"))
    entered = int(position["quantity_entered"])
    open_qty = int(position["quantity_open"])
    delta = filled - entered
    if delta <= 0:
        return ExecOutcome("FILLED" if complete else "SENT", "", None, None, int(order["id"]), position_id, gates.dry_run)
    old_avg = Decimal(str(position["entry_avg"]))
    delta_price = ((average * filled - old_avg * entered) / delta).quantize(_FOUR_DP)
    if delta_price <= _ZERO:
        delta_price = average
    new_open = open_qty + delta
    store.add_fill(
        {"position_id": position_id, "order_id": int(order["id"]), "side": "BUY", "quantity": delta, "price": delta_price, "filled_at": now, "simulated": gates.dry_run}
    )
    store.update_position(position_id, {"quantity_entered": filled, "quantity_open": new_open, "entry_avg": average.quantize(_FOUR_DP)})
    store.update_order(int(order["id"]), {"state": "FILLED" if complete else "PARTIAL", "filled_quantity": filled, "avg_fill_price": average})
    line_id = _line_for_order(store, order)
    if line_id is not None:
        store.set_line(line_id, state="FILLED" if complete else "SENT", position_id=position_id)
    stop = Decimal(str(position["stop_price"]))
    ok = await _cover(store, gateway, position, symbol=symbol, qty=new_open, stop=stop, last_price=average, now=now)
    final = "FILLED" if complete else "SENT"
    if ok:
        return ExecOutcome(final, f"{symbol}: +{delta} filled at {delta_price}, GTT now covers {new_open}", None, None, int(order["id"]), position_id, gates.dry_run)
    log.error("%s: +%d filled and the GTT could not cover %d — VBT_POSITION_NAKED", symbol, delta, new_open)
    return ExecOutcome(final, f"{symbol}: +{delta} filled, but the GTT could not be re-sized to {new_open}; it still covers {open_qty}", None, None, int(order["id"]), position_id, gates.dry_run)


async def _cover(  # noqa: PLR0913 - a stop is its instrument, its size and its two prices
    store: VbtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    position: dict,
    *,
    symbol: str,
    qty: int,
    stop: Decimal,
    last_price: Decimal,
    now: dt.datetime,
) -> bool:
    """The one resting GTT covers exactly ``qty``: re-sized when it rests, armed when it does not,
    cancelled when ``qty`` is zero. Every call goes through the gateway; a simulated trigger is
    recorded locally because nothing rests at the exchange under it."""
    gtt_id = position.get("gtt_id")
    position_id = int(position["id"])
    if qty <= 0:
        if gtt_id is None or str(gtt_id).startswith(SIMULATED_GTT_PREFIX):
            store.update_position(position_id, {"gtt_id": None})
            return True
        result = await gateway.delete_gtt(gtt_id=int(gtt_id), symbol=symbol, exchange="NSE", client_id=f"exit:{position_id}:{symbol}:cancel", tenant=_tenant(), plan_tenant=_tenant())
        ok = not str(result.get("status") or "").endswith("BLOCKED") and str(result.get("status") or "") != "ERROR"
        if ok:
            store.update_position(position_id, {"gtt_id": None})
        return ok
    if gtt_id is None:
        result = await _arm_stop(store, gateway, symbol, position_id, qty, stop, last_price, now)
        return result is not None and (str(result.get("status") or "") in GTT_PLACED_STATUSES or str(result.get("status") or "").startswith("DRY_RUN"))
    if str(gtt_id).startswith(SIMULATED_GTT_PREFIX):
        store.update_position(position_id, {"gtt_armed_at": now})
        return True
    result = await gateway.modify_gtt_quantity(
        gtt_id=int(gtt_id), symbol=symbol, qty=int(qty), trigger=float(stop), last_price=float(last_price), exchange="NSE",
        client_id=f"resize:{gtt_id}:{symbol}:{qty}", tenant=_tenant(), plan_tenant=_tenant(), limit_fraction=DEFAULT_VBT_CONFIG.exits.gtt_limit_fraction,
    )
    ok = str(result.get("status") or "") in GTT_MODIFIED_STATUSES or str(result.get("status") or "").startswith("DRY_RUN")
    if ok:
        store.update_position(position_id, {"gtt_armed_at": now})
    return ok


async def _apply_exit_update(  # noqa: PLR0913 - a report is its order, its size and its price
    store: VbtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    exit_order: dict,
    *,
    status: str,
    filled: int,
    average: Decimal,
    gates: ProductGates,
    now: dt.datetime,
) -> ExecOutcome | None:
    """A pending sell's report: the position shrinks by the newly filled shares at the broker's
    average, its GTT follows what is left, and the line closes when the broker is done."""
    if str(exit_order.get("state")) not in ("SENT", "PARTIAL"):
        return None
    position = store.position(int(exit_order["position_id"]))
    if position is None:
        return None
    recorded = int(exit_order.get("filled_quantity") or 0)
    symbol = str(exit_order["symbol"])
    complete = status == ORDER_COMPLETE
    day = now.astimezone(_IST).date()
    wrote = False
    if filled > recorded and average > _ZERO:
        delta = filled - recorded
        old_avg = Decimal(str(exit_order.get("avg_fill_price") or 0))
        delta_price = ((average * filled - old_avg * recorded) / delta).quantize(_FOUR_DP) if recorded else average
        if delta_price <= _ZERO:
            delta_price = average
        remaining = max(0, int(position["quantity_open"]) - delta)
        store.add_fill(
            {"position_id": int(position["id"]), "order_id": None, "side": "SELL", "quantity": delta, "price": delta_price, "filled_at": now, "simulated": False}
        )
        closed = remaining == 0
        store.update_position(
            int(position["id"]),
            {
                "quantity_open": remaining,
                "state": "CLOSED" if closed else "OPEN",
                "closed_on": day if closed else None,
                "close_reason": str(exit_order.get("reason") or "EMA_EXIT") if closed else None,
                "exit_avg": average.quantize(_FOUR_DP) if closed else None,
                "exit_queued_for": None if closed else position.get("exit_queued_for"),
                "exit_reason_queued": None if closed else position.get("exit_reason_queued"),
            },
        )
        store.update_exit_order(int(exit_order["id"]), {"state": "FILLED" if complete else "PARTIAL", "filled_quantity": filled, "avg_fill_price": average})
        await _cover(store, gateway, position, symbol=symbol, qty=remaining, stop=Decimal(str(position["stop_price"])), last_price=average, now=now)
        store.bump_session(day, mode="LIVE", exits=1 if closed else 0)
        wrote = True
    if status in ORDER_DEAD_STATUSES:
        final = "FILLED" if max(filled, recorded) > 0 else ("REJECTED" if status == "REJECTED" else "CANCELLED")
        store.update_exit_order(int(exit_order["id"]), {"state": final})
        if exit_order.get("line_id") is not None:
            store.set_line(int(exit_order["line_id"]), state="FILLED" if final == "FILLED" else "REJECTED", note=f"{status}: {max(filled, recorded)} of {exit_order['quantity']} sold")
        if final != "FILLED":
            store.update_position(int(position["id"]), {"exit_queued_for": None, "exit_reason_queued": None})
        return ExecOutcome(final, f"{symbol}: sell {status}", None, None, None, int(position["id"]), gates.dry_run)
    if complete:
        store.update_exit_order(int(exit_order["id"]), {"state": "FILLED"})
        if exit_order.get("line_id") is not None:
            store.set_line(int(exit_order["line_id"]), state="FILLED", position_id=int(position["id"]))
        return ExecOutcome("FILLED", f"{symbol}: sold {filled} at {average}", None, None, None, int(position["id"]), gates.dry_run)
    if wrote:
        return ExecOutcome("SENT", f"{symbol}: {filled} of {exit_order['quantity']} sold so far", None, None, None, int(position["id"]), gates.dry_run)
    return None


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
        LineKind.BUY_AT_MARKET.value: _buy_at_market,
    }
    with store.lock_session_for_update(plan["session_date"]):
        if line["kind"] == LineKind.SELL_AT_OPEN.value:
            return await _sell_at_open(
                store, gateway, line, plan, gates, stamp, last_price=last_price
            )
        if line["kind"] == LineKind.BUY_AT_MARKET.value:
            return await _buy_at_market(
                store, gateway, line, plan, gates, stamp, last_price=last_price
            )
        return await handlers[line["kind"]](store, gateway, line, plan, gates, stamp)
