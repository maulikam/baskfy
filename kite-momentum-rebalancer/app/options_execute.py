"""Confirming an options plan and closing its position, on paper (`docs/options/06` OP10).

Law 2: `packages/execution` is the only path to an order. Every leg here is an
`OrderGateway.place(...)` — `exchange="NFO"`, `product="MIS"`, `order_type="LIMIT"` — on a gateway
built per sleeve with `app.options_gates.product_gates(sleeve)`. In PAPER that gateway carries
`dry_run=True` (DECISIONS-OP OP2.3), so every leg runs the real guards, risk check, rate limit and
journal and comes back `DRY_RUN` without a broker call; the fill is then simulated by walking the
leg's live depth (`baskfy_core.options.execution.simulate_fill`, `04` §8.4) and written with
`simulated=true` whatever `DRY_RUN` says.

The procedure — longs first, the reprice, the cancel, abandoning an entry and closing what filled,
shorts first on exit, the marketable last attempt — is `baskfy_core.options.executor`'s, which is
pure and property-tested for never-naked. This module is its venue and its bookkeeping:
`op_order` per attempt, `op_fill` per fill, `op_leg` totals, `op_position` on `OPEN`, `op_session`
and `op_plan` states, and the expiry-day slot.

LIVE is refused. A LIVE plan needs all four of Maulik's switches (`02` §3), and a live fill has to be
read back from the broker's order book, which OP10 does not build: `execute_entry` answers
`LIVE_NOT_BUILT` rather than send a real order it could not track (DECISIONS-OP OP10.3).

Nothing here is scheduled. An entry runs only from `POST /nifty-options/execute` with
`confirm=true`; an exit runs from `POST /nifty-options/close` or from the monitor's exit plan
(`run_pending_exits`), which is the entry's confirm at work (PACK.2).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from baskfy_core.options.config import Mode, OptionsConfig, Side, Sleeve
from baskfy_core.options.execution import (
    Attempt,
    LegRole,
    SimFill,
    simulate_fill,
    slot_after_confirm,
)
from baskfy_core.options.chain import Level
from baskfy_core.options.executor import Book, Fill, Outcome, Result, run_entry, run_exit
from baskfy_core.options.structures import Structure

log = logging.getLogger("desk.options_execute")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SCHEMA = "public"
TICK = Decimal("0.05")
DRY_RUN = "DRY_RUN"
FAILED = frozenset({"ERROR", "REJECTED", "BLOCKED", "RISK_BLOCKED"})


class Refused(Exception):
    """A confirm the desk answers with an HTTP status and a reason, before any order."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LegRow:
    id: int
    seq: int
    role: LegRole
    tradingsymbol: str
    instrument_token: int
    side: Side
    quantity: int


@dataclass(frozen=True)
class PlanRow:
    pk: int
    plan_id: str
    session_id: int
    sleeve: Sleeve
    structure: Structure
    kind: str
    status: str
    expires_at: dt.datetime
    lots: int
    lot_size: int
    trade_date: dt.date
    session_state: str
    legs: tuple[LegRow, ...]


@dataclass(frozen=True)
class Quote:
    """A leg's live book: the touch and the depth the simulator walks."""

    bid: Decimal
    ask: Decimal
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]


QuoteSource = Callable[[int], Quote]


@dataclass
class ExecOutcome:
    plan_id: str
    sleeve: str
    outcome: str
    simulated: bool
    session_state: str
    orders: int
    entry_points: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class OptionsStore(Protocol):
    def plan(self, plan_id: str) -> PlanRow | None: ...

    def slot_holder(self, day: dt.date) -> Sleeve | None: ...

    def confirm(self, plan: PlanRow, at: dt.datetime, slot: Sleeve | None) -> None: ...

    def close_orders(self, leg_id: int, side: Side) -> int: ...

    def write_order(self, leg: LegRow, client_id: str, side: Side, quantity: int,
                    limit: Decimal, result: Mapping[str, Any], simulated: bool) -> int: ...

    def write_fill(self, leg: LegRow, order_id: int, at: dt.datetime, fill: SimFill,
                   limit: Decimal) -> None: ...

    def settle_legs(self, plan: PlanRow, result: Result) -> None: ...

    def open_position(self, plan: PlanRow, result: Result, entry_points: Decimal,
                      at: dt.datetime, hard_exit_at: dt.datetime) -> None: ...

    def abandon(self, plan: PlanRow, at: dt.datetime) -> None: ...

    def net_open(self, session_id: int) -> dict[LegRole, int]: ...

    def close_position(self, session_id: int, exit_plan_id: str | None, at: dt.datetime,
                       reason: str) -> None: ...


# --- the venue: one attempt through the gateway, filled from the book -----------------------------


def client_id(plan_id: str, symbol: str, *, closing: bool, number: int) -> str:
    """`plan_id:symbol` for an entry's first attempt, `plan_id:symbol:CLOSE` for a close's, and a
    `:R<n>` suffix for every later attempt (`04` §8.1; the gateway's idempotency map would answer
    a repeated id `DUPLICATE`, so a reprice needs its own — DECISIONS-OP OP10.4)."""
    base = f"{plan_id}:{symbol}:CLOSE" if closing else f"{plan_id}:{symbol}"
    return base if number == 1 else f"{base}:R{number}"


class DeskVenue:
    """`baskfy_core.options.executor.Venue` over the real gateway.

    Runs on a worker thread (`asyncio.to_thread`): `send` schedules `gateway.place(...)` on the
    desk's event loop and waits for its answer, so the gateway's async rate limiter and journal are
    exactly the ones every other order uses.
    """

    def __init__(  # noqa: PLR0913 - every collaborator one attempt needs
        self,
        *,
        gateway: Any,
        loop: asyncio.AbstractEventLoop,
        store: OptionsStore,
        plan: PlanRow,
        quotes: QuoteSource,
        now: Callable[[], dt.datetime],
        options: OptionsConfig,
    ) -> None:
        self.gateway = gateway
        self.loop = loop
        self.store = store
        self.plan = plan
        self.quotes = quotes
        self.now = now
        self.options = options
        self.legs = {lg.role: lg for lg in plan.legs}
        self.orders = 0
        self._close_counts: dict[int, int] = {}

    def quote(self, role: LegRole) -> Book:
        q = self.quotes(self.legs[role].instrument_token)
        return Book(bid=q.bid, ask=q.ask)

    def send(self, role: LegRole, attempt: Attempt, quantity: int, closing: bool) -> Fill:
        leg = self.legs[role]
        number = attempt.number
        if closing:
            number = self._close_counts.get(leg.id, self.store.close_orders(leg.id, attempt.side)) + 1
            self._close_counts[leg.id] = number
        cid = client_id(self.plan.plan_id, leg.tradingsymbol, closing=closing, number=number)
        book = self.quotes(leg.instrument_token)
        future = asyncio.run_coroutine_threadsafe(
            self.gateway.place(
                symbol=leg.tradingsymbol,
                qty=quantity,
                side=attempt.side.value,
                product="MIS",
                exchange="NFO",
                order_type="LIMIT",
                price=float(attempt.price),
                client_id=cid,
                gross_exposure=float(attempt.price) * quantity,
                reference_price=float(book.ask if attempt.side is Side.BUY else book.bid),
            ),
            self.loop,
        )
        result = future.result()
        status = str(result.get("status"))
        order_id = self.store.write_order(
            leg, cid, attempt.side, quantity, attempt.price, result, simulated=status == DRY_RUN
        )
        self.orders += 1
        if status != DRY_RUN:
            # A refusal (guards, risk, product gate) is a leg that did not fill: an entry abandons,
            # an exit retries. A PLACED answer cannot happen — LIVE is refused before any send.
            log.warning("options leg %s %s: %s", cid, status, result.get("error"))
            return Fill(0, None)
        ladder = book.asks if attempt.side is Side.BUY else book.bids
        sim = simulate_fill(
            attempt.side, ladder, quantity, limit_price=attempt.price, tick=TICK,
            config=self.options.execution,
        )  # fmt: skip
        if sim.filled:
            self.store.write_fill(leg, order_id, self.now(), sim, attempt.price)
        return Fill(sim.filled, sim.avg_price)


# --- the confirm --------------------------------------------------------------------------------


def entry_points(structure: Structure, result: Result) -> Decimal:
    """The structure's premium per unit from fills: the condor's credit, the long's price, the
    spread's debit (`04` §12 reads R against it)."""
    total = Decimal(0)
    for role in result.fills:
        price = result.avg_price(role) or Decimal(0)
        if structure is Structure.IRON_CONDOR:
            total += -price if role.is_long else price
        else:
            total += price if role.is_long else -price
    return total.quantize(Decimal("0.01"))


def _validate(plan: PlanRow | None, confirm: bool, now: dt.datetime) -> PlanRow:
    if not confirm:
        raise Refused(400, "CONFIRM_REQUIRED", "confirm must be true")
    if plan is None:
        raise Refused(404, "UNKNOWN_PLAN", "no such plan")
    if plan.kind != "ENTRY":
        raise Refused(400, "NOT_AN_ENTRY", "only an entry plan is confirmed")
    if plan.status != "ISSUED" or plan.session_state != "PLANNED":
        raise Refused(409, "NOT_ISSUED", f"the plan is {plan.status}, its session {plan.session_state}")
    if now >= plan.expires_at:
        raise Refused(410, "PLAN_EXPIRED", "the plan's 30-minute window has passed")
    return plan


async def execute_entry(  # noqa: PLR0913 - the store, the gateway, the book, the plan, the clock
    store: OptionsStore,
    gateway: Any,
    *,
    quotes: QuoteSource,
    plan_id: str,
    confirm: bool,
    mode_of: Callable[[Sleeve], Mode],
    now: Callable[[], dt.datetime] | None = None,
    options: OptionsConfig | None = None,
) -> ExecOutcome:
    """`POST /nifty-options/execute {plan_id, confirm: true}`: refuse, or send the legs in order."""
    clock = now or (lambda: dt.datetime.now(tz=IST))
    cfg = options or OptionsConfig()
    plan = _validate(store.plan(plan_id), confirm, clock())
    if mode_of(plan.sleeve) is Mode.LIVE:
        raise Refused(409, "LIVE_NOT_BUILT", "live execution is not built yet (OP10.3); nothing sent")
    try:
        slot = slot_after_confirm(plan.sleeve, store.slot_holder(plan.trade_date))
    except ValueError as exc:
        raise Refused(409, "SLOT_TAKEN", str(exc)) from exc
    confirmed_at = clock()
    store.confirm(plan, confirmed_at, slot)
    venue = DeskVenue(
        gateway=gateway, loop=asyncio.get_running_loop(), store=store, plan=plan, quotes=quotes,
        now=clock, options=cfg,
    )  # fmt: skip
    quantity = plan.lots * plan.lot_size
    result = await asyncio.to_thread(
        run_entry, venue, [lg.role for lg in plan.legs], quantity, sleeve=plan.sleeve, tick=TICK,
        config=cfg.execution,
    )  # fmt: skip
    store.settle_legs(plan, result)
    if result.outcome is Outcome.OPEN:
        points = entry_points(plan.structure, result)
        hard_exit = dt.datetime.combine(
            plan.trade_date, cfg.hard_exit_time(plan.sleeve), tzinfo=IST
        )
        store.open_position(plan, result, points, clock(), hard_exit)
        return ExecOutcome(plan.plan_id, plan.sleeve.value, "OPEN", True, "OPEN", venue.orders,
                           str(points), {"events": len(result.events)})  # fmt: skip
    store.abandon(plan, clock())
    return ExecOutcome(plan.plan_id, plan.sleeve.value, Outcome.ABANDONED_ENTRY.value, True,
                       "CLOSED", venue.orders,
                       detail={"left_open": {r.value: q for r, q in result.position.items() if q}})


async def execute_exit(  # noqa: PLR0913 - the store, the gateway, the book, the position, the clock
    store: OptionsStore,
    gateway: Any,
    *,
    quotes: QuoteSource,
    plan: PlanRow,
    exit_plan_id: str | None,
    reason: str,
    now: Callable[[], dt.datetime] | None = None,
    options: OptionsConfig | None = None,
) -> ExecOutcome:
    """Close an open position shorts first (`04` §8.3). `FLAT` closes it; `PARTIAL_EXIT` leaves it
    for the next pass, never naked."""
    clock = now or (lambda: dt.datetime.now(tz=IST))
    cfg = options or OptionsConfig()
    position = store.net_open(plan.session_id)
    venue = DeskVenue(
        gateway=gateway, loop=asyncio.get_running_loop(), store=store, plan=plan, quotes=quotes,
        now=clock, options=cfg,
    )  # fmt: skip
    result = await asyncio.to_thread(
        run_exit, venue, position, sleeve=plan.sleeve, tick=TICK, config=cfg.execution,
    )  # fmt: skip
    if result.outcome is Outcome.FLAT:
        store.close_position(plan.session_id, exit_plan_id, clock(), reason)
    return ExecOutcome(plan.plan_id, plan.sleeve.value, result.outcome.value, True,
                       "CLOSED" if result.outcome is Outcome.FLAT else "OPEN", venue.orders,
                       detail={"reason": reason})  # fmt: skip


# --- the store on the op_ tables ------------------------------------------------------------------


def _t(schema: str, table: str) -> str:
    return f"{schema}.{table}" if schema else table


def _aware(at: dt.datetime | str) -> dt.datetime:
    """The desk's adapter hands timestamps back as ISO strings (sqlite's shape); psycopg as
    datetimes. Either way: an aware datetime."""
    value = dt.datetime.fromisoformat(at) if isinstance(at, str) else at
    return value if value.tzinfo is not None else value.replace(tzinfo=IST)


def _day(value: dt.date | str) -> dt.date:
    return dt.date.fromisoformat(value[:10]) if isinstance(value, str) else value


class PgOptionsStore:
    """`OptionsStore` over the screener's `op_` tables through the desk's Postgres adapter."""

    def __init__(self, conn: Any, *, user_id: int, schema: str = SCHEMA) -> None:
        self.conn = conn
        self.user_id = user_id
        self.schema = schema

    def t(self, table: str) -> str:
        return _t(self.schema, table)

    def plan(self, plan_id: str) -> PlanRow | None:
        row = self.conn.execute(
            f"SELECT p.id, p.plan_id, p.session_id, p.sleeve, p.structure, p.kind, p.status, "
            f"p.expires_at, p.lots, p.lot_size, s.trade_date, s.state AS session_state "
            f"FROM {self.t('op_plan')} p JOIN {self.t('op_session')} s ON s.id = p.session_id "
            "WHERE p.user_id = ? AND p.plan_id = ?",
            (self.user_id, plan_id),
        ).fetchone()
        if row is None:
            return None
        legs = self.conn.execute(
            f"SELECT id, seq, role, tradingsymbol, instrument_token, side, quantity "
            f"FROM {self.t('op_leg')} WHERE plan_id = ? ORDER BY seq",
            (row["id"],),
        ).fetchall()
        return PlanRow(
            pk=int(row["id"]), plan_id=str(row["plan_id"]), session_id=int(row["session_id"]),
            sleeve=Sleeve(str(row["sleeve"])), structure=Structure(str(row["structure"])),
            kind=str(row["kind"]), status=str(row["status"]), expires_at=_aware(row["expires_at"]),
            lots=int(row["lots"]), lot_size=int(row["lot_size"]), trade_date=_day(row["trade_date"]),
            session_state=str(row["session_state"]),
            legs=tuple(
                LegRow(int(lg["id"]), int(lg["seq"]), LegRole(str(lg["role"])),
                       str(lg["tradingsymbol"]), int(lg["instrument_token"]), Side(str(lg["side"])),
                       int(lg["quantity"]))
                for lg in legs
            ),
        )  # fmt: skip

    def slot_holder(self, day: dt.date) -> Sleeve | None:
        row = self.conn.execute(
            f"SELECT slot_holder FROM {self.t('op_session')} WHERE user_id = ? AND trade_date = ? "
            "AND slot_holder IS NOT NULL LIMIT 1",
            (self.user_id, day),
        ).fetchone()
        return None if row is None else Sleeve(str(row["slot_holder"]))

    def confirm(self, plan: PlanRow, at: dt.datetime, slot: Sleeve | None) -> None:
        self.conn.execute(
            f"UPDATE {self.t('op_plan')} SET status = 'CONFIRMED', confirmed_at = ? "
            "WHERE id = ? AND status = 'ISSUED'",
            (_aware(at), plan.pk),
        )
        self.conn.execute(
            f"UPDATE {self.t('op_session')} SET state = 'CONFIRMED', slot_holder = ? "
            "WHERE id = ? AND state = 'PLANNED'",
            (slot.value if slot is plan.sleeve else None, plan.session_id),
        )

    def close_orders(self, leg_id: int, side: Side) -> int:
        row = self.conn.execute(
            f"SELECT count(*) AS n FROM {self.t('op_order')} o JOIN {self.t('op_leg')} l "
            "ON l.id = o.leg_id WHERE o.leg_id = ? AND o.side <> l.side",
            (leg_id,),
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    def write_order(self, leg: LegRow, client_id: str, side: Side, quantity: int,
                    limit: Decimal, result: Mapping[str, Any], simulated: bool) -> int:
        row = self.conn.execute(
            f"INSERT INTO {self.t('op_order')} (user_id, leg_id, client_id, side, quantity, "
            "limit_price, broker_order_id, gateway_status, gateway_result, simulated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (self.user_id, leg.id, client_id, side.value, quantity, limit,
             result.get("order_id"), str(result.get("status")), json.dumps(dict(result)),
             simulated),
        ).fetchone()  # fmt: skip
        return int(row["id"])

    def write_fill(self, leg: LegRow, order_id: int, at: dt.datetime, fill: SimFill,
                   limit: Decimal) -> None:
        self.conn.execute(
            f"INSERT INTO {self.t('op_fill')} (user_id, leg_id, order_id, filled_at, quantity, "
            "price, simulated, sim_method, detail) VALUES (?, ?, ?, ?, ?, ?, true, ?, ?)",
            (self.user_id, leg.id, order_id, _aware(at), fill.filled,
             (fill.avg_price or Decimal(0)).quantize(Decimal("0.01")), fill.method.value,
             json.dumps({"limit": str(limit), "requested": fill.requested,
                         "levels_consumed": fill.levels_consumed})),
        )  # fmt: skip

    def settle_legs(self, plan: PlanRow, result: Result) -> None:
        for leg in plan.legs:
            filled = sum(f.quantity for f in result.fills.get(leg.role, []))
            price = result.avg_price(leg.role)
            status = "FILLED" if filled >= leg.quantity else ("PARTIAL" if filled else "CANCELLED")
            self.conn.execute(
                f"UPDATE {self.t('op_leg')} SET filled_qty = ?, avg_price = ?, status = ? "
                "WHERE id = ?",
                (min(filled, leg.quantity), None if price is None else price.quantize(Decimal("0.01")),
                 status, leg.id),
            )  # fmt: skip

    def open_position(self, plan: PlanRow, result: Result, entry_points: Decimal,
                      at: dt.datetime, hard_exit_at: dt.datetime) -> None:
        del result
        quantity = plan.lots * plan.lot_size
        self.conn.execute(
            f"INSERT INTO {self.t('op_position')} (session_id, user_id, leg_ids, entry_points, "
            "entry_inr, lots, opened_at, hard_exit_at, simulated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, true) ON CONFLICT (session_id) DO NOTHING",
            (plan.session_id, self.user_id, [lg.id for lg in plan.legs], entry_points,
             (entry_points * quantity).quantize(Decimal("0.01")), plan.lots, _aware(at),
             hard_exit_at),
        )  # fmt: skip
        self.conn.execute(
            f"UPDATE {self.t('op_session')} SET state = 'OPEN' WHERE id = ? AND state = 'CONFIRMED'",
            (plan.session_id,),
        )

    def abandon(self, plan: PlanRow, at: dt.datetime) -> None:
        del at
        self.conn.execute(
            f"UPDATE {self.t('op_session')} SET state = 'CLOSED', closed_reason = 'ABANDONED_ENTRY', "
            "slot_holder = NULL WHERE id = ? AND state = 'CONFIRMED'",
            (plan.session_id,),
        )

    def net_open(self, session_id: int) -> dict[LegRole, int]:
        rows = self.conn.execute(
            f"SELECT l.role, COALESCE(SUM(CASE WHEN o.side = l.side THEN f.quantity "
            "ELSE -f.quantity END), 0) AS open_qty "
            f"FROM {self.t('op_session')} s JOIN {self.t('op_plan')} p ON p.plan_id = s.plan_id "
            f"JOIN {self.t('op_leg')} l ON l.plan_id = p.id "
            f"LEFT JOIN {self.t('op_fill')} f ON f.leg_id = l.id "
            f"LEFT JOIN {self.t('op_order')} o ON o.id = f.order_id "
            "WHERE s.id = ? AND s.user_id = ? GROUP BY l.role",
            (session_id, self.user_id),
        ).fetchall()
        return {LegRole(str(r["role"])): int(r["open_qty"]) for r in rows}

    def close_position(self, session_id: int, exit_plan_id: str | None, at: dt.datetime,
                       reason: str) -> None:
        self.conn.execute(
            f"UPDATE {self.t('op_position')} SET closed_at = ? WHERE session_id = ? "
            "AND closed_at IS NULL",
            (_aware(at), session_id),
        )
        self.conn.execute(
            f"UPDATE {self.t('op_session')} SET state = 'CLOSED', closed_reason = ? "
            "WHERE id = ? AND state = 'OPEN'",
            (reason[:32], session_id),
        )
        if exit_plan_id is not None:
            self.conn.execute(
                f"UPDATE {self.t('op_plan')} SET status = 'CONFIRMED', confirmed_at = ? "
                "WHERE plan_id = ? AND user_id = ?",
                (_aware(at), exit_plan_id, self.user_id),
            )


# --- the gateway and the book on the desk --------------------------------------------------------


def options_journal_path() -> str:
    """Each sleeve's orders journal beside the desk's own (the vbt/twt pattern)."""
    from .core import gateway as _gateway_module  # noqa: PLC0415 - desk wiring only

    return os.path.join(os.path.dirname(_gateway_module.JOURNAL), "options_orders_journal.jsonl")


_GATEWAYS: dict[Sleeve, Any] = {}


def options_gateway(sleeve: Sleeve, kc: Any, risk: Any) -> Any:
    """A gateway per sleeve whose gates are that sleeve's (`options_gates.product_gates`) — never
    the desk's default gateway, whose config gates refuse NFO. It shares `risk` with the rest of the
    desk (the vbt/twt rule: one account, one daily-loss cap, one order counter — OP10.2)."""
    found = _GATEWAYS.get(sleeve)
    if found is not None:
        return found
    from .core.gateway import OrderGateway  # noqa: PLC0415 - desk wiring only
    from .options_gates import product_gates  # noqa: PLC0415

    built = OrderGateway(
        kc, risk, gates=lambda: product_gates(sleeve), journal_path=options_journal_path()
    )
    _GATEWAYS[sleeve] = built
    return built


def kite_quotes(kite: Any) -> QuoteSource:
    """A leg's book from `quote_raw` (one call per leg; the slot-taking read)."""

    def read(token: int) -> Quote:
        payload = kite.quote_raw([str(token)])
        quote = next(iter(payload.values()), {}) if payload else {}
        depth = quote.get("depth") or {}
        bids = tuple(_levels(depth.get("buy")))
        asks = tuple(_levels(depth.get("sell")))
        if not bids or not asks:
            raise Refused(503, "NO_QUOTE", f"no two-sided quote for {token}")
        return Quote(bids[0].price, asks[0].price, bids, asks)

    return read


def _levels(raw: object) -> list[Level]:
    out: list[Level] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        price = Decimal(str(item.get("price") or 0))
        quantity = int(item.get("quantity") or 0)
        if price > 0 and quantity > 0:
            out.append(Level(price=price, quantity=quantity))
    return out


def entry_plan_for(store: PgOptionsStore, session_id: int) -> PlanRow | None:
    row = store.conn.execute(
        f"SELECT plan_id FROM {store.t('op_session')} WHERE id = ? AND user_id = ?",
        (session_id, store.user_id),
    ).fetchone()
    return None if row is None or row["plan_id"] is None else store.plan(str(row["plan_id"]))


async def run_pending_exits(  # noqa: PLR0913 - the store, the gateways, the book and the clock
    store: PgOptionsStore,
    gateway_for: Callable[[Sleeve], Any],
    *,
    quotes: QuoteSource,
    now: Callable[[], dt.datetime] | None = None,
    options: OptionsConfig | None = None,
) -> list[ExecOutcome]:
    """Every open position the monitor raised an exit for, closed under its entry's confirm (PACK.2).
    A `PARTIAL_EXIT` stays open and is tried again on the next pass."""
    rows = store.conn.execute(
        f"SELECT p.session_id, p.exit_plan_id, x.detail FROM {store.t('op_position')} p "
        f"JOIN {store.t('op_plan')} x ON x.plan_id = p.exit_plan_id "
        "WHERE p.user_id = ? AND p.closed_at IS NULL ORDER BY p.session_id",
        (store.user_id,),
    ).fetchall()
    done: list[ExecOutcome] = []
    for row in rows:
        plan = entry_plan_for(store, int(row["session_id"]))
        if plan is None:
            continue
        detail = row["detail"] if isinstance(row["detail"], dict) else {}
        try:
            done.append(
                await execute_exit(
                    store, gateway_for(plan.sleeve), quotes=quotes, plan=plan,
                    exit_plan_id=str(row["exit_plan_id"]), reason=str(detail.get("code") or "EXIT"),
                    now=now, options=options,
                )  # fmt: skip
            )
        except Refused as exc:  # no quote this pass: the exit waits for the next one
            log.warning("options exit for session %s waits: %s", row["session_id"], exc.message)
    return done


def outcome_json(outcome: ExecOutcome) -> dict[str, Any]:
    return asdict(outcome)
