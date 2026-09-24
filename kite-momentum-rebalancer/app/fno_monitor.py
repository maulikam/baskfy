"""The FO book's desk process: ``python -m app.fno_monitor`` (``docs/fno/06`` FO7).

Goal: plans are raised, exits happen, and positions carry, on paper. Every number it acts on is
``baskfy_core.fno``'s (``monitor``, ``exits``, ``condor``, ``calendar``, ``sizing``); this module is
everything around the arithmetic that touches the world — the scan rows, the live book, the
broker's margin, the bhavcopy — behind small seams so a replay drives it from fixtures.

THE FLAG. ``build_monitor`` returns ``None`` when ``BASKFY_FNO_MONITOR_ENABLED`` is false and the
engine is not constructed; ``main`` returns 0 before importing the database, Kite or a gateway.

WHAT A TICK DOES (every 60 s, 09:15-15:30; ``app.fno_clock``):

* lapses an ``ISSUED`` entry plan past its ``expires_at`` (``LAPSED``; a missed window is not
  retried on a later day, ``04`` §1);
* at 09:20-10:30 raises the day's entry plans from the latest ``fo_scan`` ``CANDIDATE`` rows —
  F1 (``F1N``/``F1B``) on its entry session, re-priced on live quotes, sized under the budget, the
  broker's basket margin a ceiling (``REJECTED_MARGIN``), the cost share ≤ 25 % (``REJECTED_COST``);
  F2 from the previous session's scan, one lot on paper with ``lots_at_ceiling`` recorded. Legs go
  in ``entry_seq`` order, longs first; ``expires_at = min(issued + 30 min, 10:30)``;
* for every open position, on live marks: F1's loss close (cost to close ≥ 2.5 x credit), profit
  take (≤ 50 %), the 15:00 hard exit on ``E - 1`` and ``LATE_EXIT`` at the next open; F2's stop,
  the 40-session time exit, the ``E - 1`` 15:00 roll, a passed roll date (``LATE_EXIT``) and a
  future without its resting stop (``NAKED_FUTURE``). Each becomes an EXIT or ROLL plan **under
  the entry's confirm** (``02`` Track B: exits under a confirm are not entries);
* sends those plans (``fno_execute.run_pending``) through the FO gateway, simulated.

WHAT A NIGHT DOES (after the close, retried until the bhavcopy lands): one ``fo_mark`` per open
position per session at the settle (``fo_contract_daily``; a session whose ingest has not landed is
marked on a later pass, idempotently), and F2's trail — the stop moved up, never lower, and the GTT
modified through the gateway.

NEVER AN ENTRY. Nothing here confirms a plan or sends an entry leg: an entry needs
``fno_execute.execute_entry(..., confirm=True)``, FO8's route. There is no auto-execute flag and
none may be added (``02`` Track B).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from baskfy_core.fno.calendar import (
    hard_exit_session,
    monthly_expiries,
    plan_expires_at,
    roll_session,
    sessions_before,
)
from baskfy_core.fno.condor import (
    ENTRY_SEQUENCE,
    EXIT_SEQUENCE,
    CondorStrikes,
    LegQuote,
    LegRole,
    loss_close_hit,
    loss_close_level,
    profit_take_hit,
    profit_take_level,
)
from baskfy_core.fno.config import (
    DEFAULT_FNO_CEILINGS,
    DEFAULT_FNO_CONFIG,
    FnoCeilings,
    FnoConfig,
    FoSleeve,
    PlanKind,
    PlanState,
    ScanState,
    Structure,
)
from baskfy_core.fno.exits import ExitDecision, ExitReason, f1_exit, f2_time_exit_session
from baskfy_core.fno.monitor import (
    f1_close_cost,
    f1_pnl_inr,
    f2_action,
    f2_pnl_inr,
    f2_trail_step,
    reprice_condor,
    reprice_future,
)
from baskfy_core.fno.sizing import MarginVerdict, margin_check
from baskfy_core.options.config import CostRates, Mode

from . import config as C
from . import fno_execute as X

log = logging.getLogger("desk.fno_monitor")

IST = X.IST
SCHEMA = "public"
SESSION_OPEN = dt.time(9, 15)
SESSION_CLOSE = dt.time(15, 30)
F1_SLEEVES: tuple[FoSleeve, ...] = (FoSleeve.F1N, FoSleeve.F1B)


# --- the seams -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Contract:
    tradingsymbol: str
    instrument_token: int
    lot_size: int
    expiry: dt.date


class Instruments(Protocol):
    """The NFO master: options from ``op_contract``, futures from Kite's ``instruments("NFO")``."""

    def option(self, underlying: str, expiry: dt.date, strike: Decimal,
               option_type: str) -> Contract | None: ...  # fmt: skip

    def future(self, symbol: str, expiry: dt.date) -> Contract | None: ...

    def next_future(self, symbol: str, after: dt.date) -> Contract | None: ...


@dataclass(frozen=True)
class MarginLeg:
    tradingsymbol: str
    side: str
    quantity: int
    price: Decimal


@dataclass(frozen=True)
class MarginQuote:
    required_inr: Decimal
    free_inr: Decimal


#: ``basket_order_margins`` for the plan's legs and the free margin, or ``None`` when unreadable.
MarginSource = Callable[[Sequence[MarginLeg]], MarginQuote | None]


@dataclass(frozen=True)
class Print:
    close: Decimal
    settle: Decimal


#: ``(symbol, expiry, strike or None for a future, 'CE' | 'PE' | 'XX')``.
PrintKey = tuple[str, dt.date, Decimal | None, str]


class Market(Protocol):
    """The exchange calendar and the F&O bhavcopy."""

    def sessions(self) -> Sequence[dt.date]: ...

    def landed(self, day: dt.date) -> bool: ...

    def prints(self, day: dt.date, keys: Sequence[PrintKey]) -> Mapping[PrintKey, Print]: ...


@dataclass(frozen=True)
class ScanRow:
    id: int
    sleeve: str
    trade_date: dt.date
    symbol: str
    state: str
    rv20: Decimal | None
    detail: dict[str, Any]


@dataclass
class TickReport:
    lapsed: list[str] = field(default_factory=list)
    raised: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    done: list[X.FoOutcome] = field(default_factory=list)


@dataclass
class NightReport:
    landed: bool
    marked: int = 0
    trailed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


# --- reading the scan and the sleeve config ------------------------------------------------------


def scan_candidates(store: X.FoStore, sleeves: Sequence[FoSleeve], day: dt.date) -> list[ScanRow]:
    """The latest scan before ``day`` for these sleeves, ``CANDIDATE`` rows only."""
    names = [s.value for s in sleeves]
    rows = store.conn.execute(
        f"SELECT id, sleeve, trade_date, symbol, state, rv20, detail FROM {store.t('fo_scan')} "
        "WHERE user_id = ? AND sleeve::text = ANY(?) AND state = ? AND trade_date = ("
        f"SELECT max(trade_date) FROM {store.t('fo_scan')} WHERE user_id = ? "
        "AND sleeve::text = ANY(?) AND trade_date < ?) ORDER BY sleeve, symbol",
        (store.user_id, names, ScanState.CANDIDATE.value, store.user_id, names, day),
    ).fetchall()
    return [
        ScanRow(int(r["id"]), str(r["sleeve"]), X._day(r["trade_date"]), str(r["symbol"]),
                str(r["state"]), X._dec(r["rv20"]), X._json(r["detail"]))
        for r in rows
    ]  # fmt: skip


def sleeve_capital(store: X.FoStore, group: str) -> Decimal:
    row = store.conn.execute(
        f"SELECT capital_inr FROM {store.t('fo_sleeve_config')} WHERE user_id = ? AND sleeve = ?",
        (store.user_id, group),
    ).fetchone()
    return Decimal(0) if row is None else Decimal(str(row["capital_inr"]))


# --- the engine ----------------------------------------------------------------------------------


def entry_plan_id(sleeve: FoSleeve, day: dt.date, symbol: str, user_id: int) -> str:
    """Deterministic (``03`` §4), and per user: ``fo_plan.plan_id`` is unique across the table,
    so two users' plans for one sleeve, day and underlying must not share it (FO7.6)."""
    return f"{sleeve.value}-{day:%Y%m%d}-{symbol}-u{user_id}"


def _money(value: Decimal) -> Decimal:
    return X.money(value)


def _strikes(detail: Mapping[str, Any]) -> CondorStrikes | None:
    raw = detail.get("strikes")
    if not isinstance(raw, dict):
        return None
    try:
        return CondorStrikes(Decimal(str(raw["LONG_CALL"])), Decimal(str(raw["SHORT_CALL"])),
                             Decimal(str(raw["SHORT_PUT"])), Decimal(str(raw["LONG_PUT"])))
    except (KeyError, ArithmeticError, ValueError):
        return None  # fmt: skip


class FnoMonitor:
    """Raises plans, watches positions, marks the book. Holds no confirm and sends no entry."""

    def __init__(  # noqa: PLR0913 - every seam a replay needs
        self, *, store: X.FoStore, gateway_for: Callable[[FoSleeve], Any],
        quotes: X.QuoteSource, instruments: Instruments, margins: MarginSource, market: Market,
        config: FnoConfig = DEFAULT_FNO_CONFIG, ceilings: FnoCeilings = DEFAULT_FNO_CEILINGS,
        mode_of: Callable[[FoSleeve], Mode] = X.fno_mode,
        alert: Callable[[str], None] = log.error, rates: CostRates | None = None,
    ) -> None:  # fmt: skip
        self.store = store
        self.gateway_for = gateway_for
        self.quotes = quotes
        self.instruments = instruments
        self.margins = margins
        self.market = market
        self.config = config
        self.ceilings = ceilings
        self.mode_of = mode_of
        self.alert = alert
        self.rates = rates or CostRates()

    # the tick ---------------------------------------------------------------------------------

    async def tick(self, now: dt.datetime) -> TickReport:
        report = TickReport()
        report.lapsed = self.store.lapse_expired(now)
        f1 = self.config.f1
        if f1.plan_time <= now.astimezone(IST).time() < f1.entry_window_end:
            report.raised = self.raise_plans(now)
        if SESSION_OPEN <= now.astimezone(IST).time() < SESSION_CLOSE:
            report.actions = self.watch(now)
            report.done = await X.run_pending(
                self.store, self.gateway_for, quotes=self.quotes, now=lambda: now,
                alert=self.alert,
            )  # fmt: skip
        return report

    # raising plans ----------------------------------------------------------------------------

    def raise_plans(self, now: dt.datetime) -> list[str]:
        day = now.astimezone(IST).date()
        raised: list[str] = []
        open_positions = self.store.open_positions()
        if len(open_positions) >= self.config.book.max_open_positions:
            return raised
        for row in scan_candidates(self.store, F1_SLEEVES, day):
            if str(row.detail.get("entry_session")) != day.isoformat():
                continue
            if any(p.symbol == row.symbol for p in open_positions):
                continue  # one F1 structure per underlying; the next waits for the exit
            plan_id = self._raise_f1(row, now)
            if plan_id:
                raised.append(plan_id)
        sessions = list(self.market.sessions())
        previous = sessions_before(sessions, day, 1) if day in sessions else None
        held = {p.symbol for p in open_positions if p.sleeve is FoSleeve.F2}
        for row in scan_candidates(self.store, (FoSleeve.F2,), day):
            if previous is None or row.trade_date != previous or row.symbol in held:
                continue  # F2 trades the previous session's signal only
            if len(held) >= self.config.f2.max_open:
                break
            plan_id = self._raise_f2(row, now, sessions)
            if plan_id:
                raised.append(plan_id)
                held.add(row.symbol)
        return raised

    def _margin(self, legs: Sequence[MarginLeg]) -> tuple[PlanState | None, str, Decimal | None]:
        quote = self.margins(legs)
        if quote is None:
            return (PlanState.REJECTED_MARGIN, "the broker's basket margin could not be read; "
                    "a plan must be shown to fit under it (04 §3)", None)  # fmt: skip
        check = margin_check(quote.required_inr, quote.free_inr)
        if check.verdict is MarginVerdict.REJECTED_MARGIN:
            return PlanState.REJECTED_MARGIN, check.message, quote.required_inr
        return None, check.message, quote.required_inr

    def _raise_f1(self, row: ScanRow, now: dt.datetime) -> str | None:  # noqa: PLR0915
        sleeve = FoSleeve(row.sleeve)
        day = now.astimezone(IST).date()
        plan_id = entry_plan_id(sleeve, day, row.symbol, self.store.user_id)
        if self.store.plan(plan_id) is not None:
            return None
        d = row.detail
        legs_raw = [x for x in d.get("legs") or [] if isinstance(x, dict)]
        by_role = {str(x.get("role")): x for x in legs_raw}
        expiry = X._opt_day(d.get("expiry"))
        reasons: list[str] = []
        contracts: dict[LegRole, Contract] = {}
        strikes_map: dict[str, Decimal] = {}
        for role in LegRole:
            leg = by_role.get(role.value)
            leg_expiry = X._opt_day(leg.get("expiry")) if leg else expiry
            if leg is None or leg_expiry is None:
                reasons.append(f"{role.value}: the scan names no leg")
                continue
            strike = Decimal(str(leg.get("strike")))
            strikes_map[role.value] = strike
            found = self.instruments.option(row.symbol, leg_expiry, strike,
                                            str(leg.get("option_type")))  # fmt: skip
            if found is None:
                reasons.append(f"{role.value} {strike}: not in the NFO master")
            else:
                contracts[role] = found
        issued = now
        expires = plan_expires_at(issued, self.config.f1, self.config.common)
        base: dict[str, Any] = {
            "scan_id": row.id, "scan_date": row.trade_date, "strikes": strikes_map,
            "iv": d.get("iv"), "iv_rv": d.get("iv_rv"), "violations": [],
        }  # fmt: skip
        lot_from_scan = int(d.get("lot_size") or 1)
        if len(contracts) < len(LegRole) or expiry is None:
            self.store.insert_plan(
                self._head(plan_id, sleeve, row.symbol, day, Structure.IRON_CONDOR, issued,
                           expires, lots=1, lot_size=lot_from_scan,
                           status=PlanState.REJECTED_STRUCTURE.value, reason="; ".join(reasons),
                           detail={**base, "reasons": reasons}),
                [],
            )  # fmt: skip
            return plan_id
        strikes = CondorStrikes(strikes_map["LONG_CALL"], strikes_map["SHORT_CALL"],
                                strikes_map["SHORT_PUT"], strikes_map["LONG_PUT"])  # fmt: skip
        lot = contracts[LegRole.SHORT_CALL].lot_size
        books = self.quotes([c.tradingsymbol for c in contracts.values()])
        leg_quotes: dict[LegRole, LegQuote] = {}
        for role, c in contracts.items():
            q = books.get(c.tradingsymbol)
            if q is not None:
                leg_quotes[role] = LegQuote(q.bid, q.ask, (q.oi or 0) // max(lot, 1))
        rep = reprice_condor(
            strikes=strikes, quotes=leg_quotes, lot_size=lot,
            capital_inr=sleeve_capital(self.store, "F1"), mode=self.mode_of(sleeve),
            config=self.config, rates=self.rates, ceilings=self.ceilings,
        )  # fmt: skip
        state, reasons = rep.state, list(rep.reasons)
        lots = rep.sizing.lots if rep.sizing is not None and rep.sizing.lots > 0 else 0
        quantity = max(lots, 1) * lot
        margin_required: Decimal | None = None
        if state is None:
            margin_legs = [
                MarginLeg(contracts[r].tradingsymbol, "BUY" if r.value.startswith("LONG") else
                          "SELL", quantity, rep.mids[r])
                for r in ENTRY_SEQUENCE
            ]  # fmt: skip
            state, message, margin_required = self._margin(margin_legs)
            reasons.append(message)
        hard_exit = X._opt_day(d.get("hard_exit_date")) or hard_exit_session(
            list(self.market.sessions()), expiry, self.config.common.hard_exit_before_expiry)
        credit = rep.credit
        detail: dict[str, Any] = {
            **base, "reasons": reasons, "mids": {r.value: m for r, m in rep.mids.items()},
            "lots_sized": lots, "width_points": strikes.max_width,
            "sizing": None if rep.sizing is None else {
                "lots": rep.sizing.lots, "message": rep.sizing.message,
                "lots_at_ceiling": rep.sizing.lots_at_ceiling},
            "cost": None if rep.cost is None else {
                "round_trip_inr": rep.cost.round_trip_inr,
                "cost_share_pct": rep.cost.cost_share_pct},
            "margin_required_inr": margin_required,
        }  # fmt: skip
        if credit is not None and credit > 0:
            detail["profit_take_points"] = _money(profit_take_level(credit, self.config.f1))
            detail["loss_close_points"] = _money(loss_close_level(credit, strikes, self.config.f1))
        head = self._head(
            plan_id, sleeve, row.symbol, day, Structure.IRON_CONDOR, issued, expires,
            lots=max(lots, 1), lot_size=lot,
            status=(state.value if state is not None else PlanState.ISSUED.value),
            reason=None if state is None else "; ".join(reasons), detail=detail,
        )  # fmt: skip
        head.update({
            "hard_exit_date": hard_exit,
            "credit_points": None if credit is None else _money(credit),
            "width_points": strikes.max_width,
            "risk_per_lot_inr": None if rep.sizing is None else _money(rep.sizing.risk_per_lot_inr),
            "risk_budget_inr": None if rep.sizing is None else _money(rep.sizing.risk_budget_inr),
            "max_loss_inr": None if rep.sizing is None else _money(rep.sizing.max_loss_inr),
            "expected_cost_inr": None if rep.cost is None else rep.cost.round_trip_inr,
            "cost_share": None if rep.cost is None or rep.cost.cost_share_pct is None else
            rep.cost.cost_share_pct,
            "margin_required_inr": margin_required,
            "sizing_mode": (rep.sizing.sizing_mode.value if rep.sizing is not None else "BUDGET"),
        })  # fmt: skip
        legs = []
        for seq, role in enumerate(ENTRY_SEQUENCE, start=1):
            c = contracts[role]
            q = books.get(c.tradingsymbol)
            legs.append({
                "entry_seq": seq, "role": role.value, "tradingsymbol": c.tradingsymbol,
                "instrument_token": c.instrument_token, "expiry": c.expiry,
                "strike": strikes.strike(role), "option_type": str(by_role[role.value].get(
                    "option_type")), "side": "BUY" if role.value.startswith("LONG") else "SELL",
                "quantity": quantity, "reference_price": rep.mids.get(role),
                "bid": None if q is None else q.bid, "ask": None if q is None else q.ask,
            })  # fmt: skip
        self.store.insert_plan(head, legs)
        return plan_id

    def _raise_f2(self, row: ScanRow, now: dt.datetime, sessions: Sequence[dt.date]) -> str | None:
        day = now.astimezone(IST).date()
        plan_id = entry_plan_id(FoSleeve.F2, day, row.symbol, self.store.user_id)
        if self.store.plan(plan_id) is not None:
            return None
        d = row.detail
        expiry = X._opt_day(d.get("contract_expiry"))
        contract = None if expiry is None else self.instruments.future(row.symbol, expiry)
        issued = now
        expires = plan_expires_at(issued, self.config.f1, self.config.common)
        base: dict[str, Any] = {"scan_id": row.id, "scan_date": row.trade_date, "violations": [],
                "atr14": d.get("atr14"), "industry": d.get("industry")}  # fmt: skip
        if contract is None or expiry is None:
            reason = f"{row.symbol}: no future for {d.get('contract_expiry')} in the NFO master"
            self.store.insert_plan(
                self._head(plan_id, FoSleeve.F2, row.symbol, day, Structure.FUTURE, issued,
                           expires, lots=1, lot_size=int(d.get("lot_size") or 1),
                           status=PlanState.REJECTED_STRUCTURE.value, reason=reason,
                           detail={**base, "reasons": [reason]}),
                [],
            )  # fmt: skip
            return plan_id
        book = self.quotes([contract.tradingsymbol]).get(contract.tradingsymbol)
        ann_vol = float(row.rv20) if row.rv20 is not None else 0.0
        rep = reprice_future(
            bid=None if book is None else book.bid, ask=None if book is None else book.ask,
            atr=X._dec(d.get("atr14")) or Decimal(0), ann_vol=ann_vol,
            lot_size=contract.lot_size, capital_inr=sleeve_capital(self.store, "F2"),
            mode=self.mode_of(FoSleeve.F2), config=self.config, ceilings=self.ceilings,
        )  # fmt: skip
        state, reasons = rep.state, list(rep.reasons)
        lots = rep.sizing.lots if rep.sizing is not None and rep.sizing.lots > 0 else 0
        quantity = max(lots, 1) * contract.lot_size
        margin_required: Decimal | None = None
        if state is None and rep.entry is not None:
            state, message, margin_required = self._margin(
                [MarginLeg(contract.tradingsymbol, "BUY", quantity, rep.entry)])
            reasons.append(message)
        detail = {
            **base, "reasons": reasons, "ann_vol": ann_vol, "stop": rep.stop,
            "gtt_trigger": rep.trigger, "entry_reference": rep.entry, "lots_sized": lots,
            "lots_at_ceiling": None if rep.sizing is None else rep.sizing.lots_at_ceiling,
            "time_exit_date": f2_time_exit_session(sessions, day, self.config.f2),
            "roll_date": roll_session(sessions, expiry, self.config.f2.roll_before_expiry),
            "margin_required_inr": margin_required,
        }  # fmt: skip
        head = self._head(
            plan_id, FoSleeve.F2, row.symbol, day, Structure.FUTURE, issued, expires,
            lots=max(lots, 1), lot_size=contract.lot_size,
            status=(state.value if state is not None else PlanState.ISSUED.value),
            reason=None if state is None else "; ".join(reasons), detail=detail,
        )  # fmt: skip
        head.update({
            "debit_points": rep.entry,
            "risk_per_lot_inr": None if rep.sizing is None else _money(rep.sizing.risk_per_lot_inr),
            "risk_budget_inr": None if rep.sizing is None else _money(rep.sizing.risk_budget_inr),
            "max_loss_inr": None if rep.sizing is None else _money(rep.sizing.max_loss_inr),
            "expected_cost_inr": None if rep.cost is None else rep.cost.total,
            "margin_required_inr": margin_required,
            "sizing_mode": (rep.sizing.sizing_mode.value if rep.sizing is not None else "BUDGET"),
        })  # fmt: skip
        leg = {
            "entry_seq": 1, "role": X.FUTURE_ROLE, "tradingsymbol": contract.tradingsymbol,
            "instrument_token": contract.instrument_token, "expiry": contract.expiry,
            "strike": None, "option_type": X.FUTURE_TYPE, "side": "BUY", "quantity": quantity,
            "reference_price": None if book is None else book.mid,
            "bid": None if book is None else book.bid, "ask": None if book is None else book.ask,
        }  # fmt: skip
        self.store.insert_plan(head, [leg])
        return plan_id

    @staticmethod
    def _head(  # noqa: PLR0913, PLR0917 - one fo_plan row
        plan_id: str, sleeve: FoSleeve, symbol: str, day: dt.date, structure: Structure,
        issued: dt.datetime, expires: dt.datetime, *, lots: int, lot_size: int, status: str,
        reason: str | None, detail: Mapping[str, Any], kind: PlanKind = PlanKind.ENTRY,
    ) -> dict[str, Any]:  # fmt: skip
        return {
            "plan_id": plan_id, "sleeve": sleeve.value, "symbol": symbol, "trade_date": day,
            "structure": structure.value, "kind": kind.value, "sizing_mode": "BUDGET",
            "issued_at": issued, "entry_window_end": expires, "expires_at": expires, "lots": lots,
            "lot_size": lot_size, "status": status, "reason": reason, "detail": dict(detail),
        }  # fmt: skip

    # watching positions -----------------------------------------------------------------------

    def watch(self, now: dt.datetime) -> list[str]:
        """Every open position without a pending action, checked on live marks; returns the plans
        raised (EXIT or ROLL)."""
        busy = {str(p.detail.get("position_id")) for p in self.store.pending_actions()}
        raised: list[str] = []
        for position in self.store.open_positions():
            if str(position.id) in busy:
                continue
            if position.structure == Structure.IRON_CONDOR.value:
                decision = self._f1_decision(position, now)
            else:
                decision = self._f2_decision(position, now)
            if decision is None:
                continue
            if decision.reason is ExitReason.ROLL:
                plan_id = self._raise_roll(position, now)
            else:
                plan_id = self._raise_exit(position, decision, now)
            if plan_id:
                raised.append(plan_id)
        return raised

    def _f1_decision(self, position: X.PositionRow, now: dt.datetime) -> ExitDecision | None:
        strikes = _strikes(position.carry)
        if strikes is None or position.hard_exit_date is None or position.entry_credit is None:
            self.alert(f"F1 position {position.id} lacks its strikes or dates; not watched")
            return None
        symbols = {str(x["role"]): str(x["tradingsymbol"]) for x in position.leg_list}
        books = self.quotes(list(symbols.values()))
        marks: dict[LegRole, Decimal] = {}
        for role in LegRole:
            q = books.get(symbols.get(role.value, ""))
            if q is not None and q.mid is not None:
                marks[role] = q.mid
        close_cost = f1_close_cost(strikes, marks)
        return f1_exit(
            now=now.astimezone(IST).replace(tzinfo=None), hard_exit_date=position.hard_exit_date,
            close_cost=close_cost, entry_credit=position.entry_credit, strikes=strikes,
            f1=self.config.f1, common=self.config.common,
        )  # fmt: skip

    def _f2_decision(self, position: X.PositionRow, now: dt.datetime) -> ExitDecision | None:
        legs = position.leg_list
        if not legs:
            return None
        symbol = str(legs[0]["tradingsymbol"])
        q = self.quotes([symbol]).get(symbol)
        price = None if q is None else (q.last or q.mid)
        carry = position.carry
        return f2_action(
            now=now.astimezone(IST).replace(tzinfo=None), price=price,
            trigger=position.stop_price or Decimal(0),
            time_exit_date=X._opt_day(carry.get("time_exit_date")),
            roll_date=position.next_roll_date, naked=position.gtt_id is None,
            common=self.config.common,
        )  # fmt: skip

    def _raise_exit(self, position: X.PositionRow, decision: ExitDecision,
                    now: dt.datetime) -> str | None:  # fmt: skip
        entry = self.store.plan(position.entry_plan_id)
        if entry is None:
            self.alert(f"FO position {position.id}: entry plan {position.entry_plan_id} is gone")
            return None
        exit_id = f"{position.entry_plan_id}-X"
        if self.store.plan(exit_id) is not None:
            return None
        net = self.store.net_by_symbol(position.id)
        by_symbol = {str(x["tradingsymbol"]): x for x in position.leg_list}
        order = [r.value for r in EXIT_SEQUENCE] + [X.FUTURE_ROLE]
        legs: list[dict[str, Any]] = []
        for leg in sorted(by_symbol.values(), key=lambda x: order.index(str(x["role"]))):
            units = net.get(str(leg["tradingsymbol"]), 0)
            if units == 0:
                continue
            legs.append({
                "entry_seq": len(legs) + 1, "role": str(leg["role"]),
                "tradingsymbol": str(leg["tradingsymbol"]),
                "instrument_token": int(leg["instrument_token"]),
                "expiry": X._opt_day(leg["expiry"]), "strike": X._dec(leg.get("strike")),
                "option_type": str(leg["option_type"]), "side": "SELL" if units > 0 else "BUY",
                "quantity": abs(units),
            })  # fmt: skip
        if not legs:
            self.alert(f"FO position {position.id} is open with no units; nothing to close")
            return None
        close = dt.datetime.combine(now.astimezone(IST).date(), SESSION_CLOSE, tzinfo=IST)
        head = self._head(
            exit_id, position.sleeve, position.symbol, now.astimezone(IST).date(),
            Structure(position.structure), now, max(close, now + dt.timedelta(minutes=1)),
            lots=position.lots, lot_size=position.lot_size, status=PlanState.CONFIRMED.value,
            reason=decision.message, kind=PlanKind.EXIT,
            detail={"code": decision.reason.value, "reason": decision.message,
                    "position_id": position.id, "under_confirm_of": entry.plan_id},
        )  # fmt: skip
        head.update({"parent_plan_id": entry.plan_id, "confirmed_at": entry.confirmed_at,
                     "hard_exit_date": position.hard_exit_date, "entry_window_end": None})
        self.store.insert_plan(head, legs)
        if decision.reason is ExitReason.LATE_EXIT:
            self.store.add_violation(entry.plan_id, "LATE_EXIT", decision.message, now)
            self.alert(f"FO {entry.plan_id}: LATE_EXIT — {decision.message}")
        return exit_id

    def _raise_roll(self, position: X.PositionRow, now: dt.datetime) -> str | None:
        entry = self.store.plan(position.entry_plan_id)
        legs_now = position.leg_list
        if entry is None or not legs_now:
            return None
        held = legs_now[0]
        held_expiry = X._opt_day(held.get("expiry"))
        nxt = None if held_expiry is None else self.instruments.next_future(position.symbol,
                                                                             held_expiry)
        if nxt is None:  # nothing to roll into: flat by E-1 all the same
            return self._raise_exit(
                position, ExitDecision(ExitReason.HARD_EXIT, "no next-month future to roll into; "
                                       "flat by E-1 (02 §2.2)"), now)  # fmt: skip
        rolls = int(position.carry.get("rolls") or 0)
        roll_id = f"{position.entry_plan_id}-R{rolls + 1}"
        if self.store.plan(roll_id) is not None:
            return None
        units = self.store.net_by_symbol(position.id).get(str(held["tradingsymbol"]), 0)
        if units <= 0:
            return None
        close = dt.datetime.combine(now.astimezone(IST).date(), SESSION_CLOSE, tzinfo=IST)
        sessions = list(self.market.sessions())
        head = self._head(
            roll_id, position.sleeve, position.symbol, now.astimezone(IST).date(),
            Structure.FUTURE, now, max(close, now + dt.timedelta(minutes=1)),
            lots=position.lots, lot_size=nxt.lot_size, status=PlanState.ISSUED.value,
            reason="15:00 on E-1: the held month sold, the next bought, stop carried",
            kind=PlanKind.ROLL,
            detail={"code": ExitReason.ROLL.value, "position_id": position.id,
                    "under_confirm_of": entry.plan_id, "rolled_from": held["tradingsymbol"],
                    "next_roll_date": roll_session(sessions, nxt.expiry,
                                                   self.config.f2.roll_before_expiry)},
        )  # fmt: skip
        head.update({"parent_plan_id": entry.plan_id, "confirmed_at": entry.confirmed_at,
                     "entry_window_end": None})
        legs = [
            {"entry_seq": 1, "role": X.FUTURE_ROLE, "tradingsymbol": str(held["tradingsymbol"]),
             "instrument_token": int(held["instrument_token"]), "expiry": held_expiry,
             "strike": None, "option_type": X.FUTURE_TYPE, "side": "SELL", "quantity": units},
            {"entry_seq": 2, "role": X.FUTURE_ROLE, "tradingsymbol": nxt.tradingsymbol,
             "instrument_token": nxt.instrument_token, "expiry": nxt.expiry, "strike": None,
             "option_type": X.FUTURE_TYPE, "side": "BUY", "quantity": units},
        ]  # fmt: skip
        self.store.insert_plan(head, legs)
        return roll_id

    # the night --------------------------------------------------------------------------------

    async def nightly(self, day: dt.date) -> NightReport:
        """``fo_mark`` for every open position at each session's settle it has not been marked
        on, and F2's trail on each new mark. Idempotent: a re-run writes nothing new."""
        if not self.market.landed(day):
            return NightReport(landed=False)
        report = NightReport(landed=True)
        sessions = [d for d in self.market.sessions() if d <= day]
        for opened_position in self.store.open_positions():
            position = opened_position
            opened = position.opened_at.astimezone(IST).date()
            done = self.store.marked_days(position.id)
            for session in (d for d in sessions if d >= opened and d not in done):
                if not self.market.landed(session):
                    continue
                if position.structure == Structure.IRON_CONDOR.value:
                    ok = self._mark_f1(position, session)
                else:
                    ok = await self._mark_f2(position, session, report)
                    position = self.store.position(position.id) or position  # the trail moved
                if ok:
                    report.marked += 1
                else:
                    report.missing.append(f"{position.id}@{session.isoformat()}")
        return report

    def _mark_f1(self, position: X.PositionRow, day: dt.date) -> bool:
        strikes = _strikes(position.carry)
        if strikes is None or position.entry_credit is None:
            return False
        keys: dict[LegRole, PrintKey] = {}
        for leg in position.leg_list:
            expiry = X._opt_day(leg.get("expiry"))
            if expiry is None:
                return False
            keys[LegRole(str(leg["role"]))] = (position.symbol, expiry,
                                               X._dec(leg.get("strike")), str(leg["option_type"]))
        prints = self.market.prints(day, list(keys.values()))
        settles = {role: prints[k].settle for role, k in keys.items() if k in prints}
        close_cost = f1_close_cost(strikes, settles)
        if close_cost is None:
            return False
        credit = position.entry_credit
        pnl = f1_pnl_inr(credit, close_cost, position.lots * position.lot_size)
        detail = {
            "settles": {r.value: s for r, s in settles.items()},
            "profit_take_hit": profit_take_hit(close_cost, credit, self.config.f1),
            "loss_close_hit": loss_close_hit(close_cost, credit, strikes, self.config.f1),
            "note": "the settle is a mark; exits fire on the live check (04 §1)",
        }  # fmt: skip
        self.store.write_mark(position.id, day, mark_points=close_cost, pnl_inr=pnl,
                              stop_price=None, detail=detail)  # fmt: skip
        return True

    async def _mark_f2(self, position: X.PositionRow, day: dt.date, report: NightReport) -> bool:
        legs = position.leg_list
        if not legs:
            return False
        leg = legs[0]
        expiry = X._opt_day(leg.get("expiry"))
        symbol = str(leg["tradingsymbol"])
        if expiry is None:
            return False
        key: PrintKey = (position.symbol, expiry, None, X.FUTURE_TYPE)
        found = self.market.prints(day, [key]).get(key)
        if found is None:
            return False
        carry = position.carry
        quantity = int(leg.get("quantity") or position.lots * position.lot_size)
        current_entry = X._dec(leg.get("avg_price")) or position.entry_price or Decimal(0)
        realised = X._dec(carry.get("realised_inr")) or Decimal(0)
        pnl = realised + f2_pnl_inr(current_entry, found.settle, quantity)
        old_trigger = position.stop_price or Decimal(0)
        step = f2_trail_step(
            stop=X._dec(carry.get("trail_stop")) or old_trigger, trigger=old_trigger,
            highest_close=X._dec(carry.get("highest_close")) or current_entry,
            close=found.close, entry=position.entry_price or current_entry,
            atr_at_entry=X._dec(carry.get("atr_at_entry")) or Decimal(0),
            ann_vol=float(carry.get("ann_vol") or 0.0), config=self.config,
        )  # fmt: skip
        fresh = self.store.write_mark(
            position.id, day, mark_points=found.settle, pnl_inr=pnl, stop_price=step.trigger,
            detail={"settle": found.settle, "close": found.close, "trail_stop": step.stop,
                    "highest_close": step.highest_close, "moved": step.moved},
        )  # fmt: skip
        if not fresh:
            return True
        carry.update({"trail_stop": step.stop, "highest_close": step.highest_close})
        self.store.update_position(position.id, legs={"legs": legs, "carry": carry})
        if step.moved and position.gtt_id is not None:
            status = await X.move_future_stop(
                self.store, self.gateway_for(position.sleeve), position=position, symbol=symbol,
                qty=quantity, trigger=step.trigger, floor=old_trigger, last_price=found.close,
            )  # fmt: skip
            report.trailed.append(f"{position.symbol}:{old_trigger}->{step.trigger}:{status}")
        return True


# --- the Postgres and Kite seams -----------------------------------------------------------------


class PgMarket:
    """``trading_day``, ``fo_ingest_day`` and ``fo_contract_daily`` (market data, shared)."""

    def __init__(self, conn: Any, *, schema: str = SCHEMA) -> None:  # noqa: ANN401
        self.conn = conn
        self.schema = schema
        self._sessions: list[dt.date] | None = None

    def sessions(self) -> Sequence[dt.date]:
        if self._sessions is None:
            rows = self.conn.execute(
                f"SELECT date FROM {X._t(self.schema, 'trading_day')} WHERE is_trading_day "
                "AND exchange_id = (SELECT min(exchange_id) FROM "
                f"{X._t(self.schema, 'trading_day')}) ORDER BY date"
            ).fetchall()
            self._sessions = [X._day(r["date"]) for r in rows]
        return self._sessions

    def landed(self, day: dt.date) -> bool:
        row = self.conn.execute(
            f"SELECT status FROM {X._t(self.schema, 'fo_ingest_day')} WHERE trade_date = ?", (day,)
        ).fetchone()
        return row is not None and str(row["status"]) == "INGESTED"

    def prints(self, day: dt.date, keys: Sequence[PrintKey]) -> Mapping[PrintKey, Print]:
        out: dict[PrintKey, Print] = {}
        for key in keys:
            symbol, expiry, strike, kind = key
            row = self.conn.execute(
                f"SELECT close, settle FROM {X._t(self.schema, 'fo_contract_daily')} "
                "WHERE trade_date = ? AND symbol = ? AND expiry = ? AND strike = ? "
                "AND option_type = ?",
                (day, symbol, expiry, strike if strike is not None else Decimal(0), kind),
            ).fetchone()
            if row is not None:
                out[key] = Print(Decimal(str(row["close"])), Decimal(str(row["settle"])))
        return out


class PgKiteInstruments:
    """Options from ``op_contract`` (FO2 widened it to every F&O underlying); futures from Kite's
    NFO dump, read once per process (``op_contract`` holds options only)."""

    def __init__(self, conn: Any, kite: Any, *, schema: str = SCHEMA) -> None:  # noqa: ANN401
        self.conn = conn
        self.kite = kite
        self.schema = schema
        self._futures: list[dict[str, Any]] | None = None

    def option(self, underlying: str, expiry: dt.date, strike: Decimal,
               option_type: str) -> Contract | None:  # fmt: skip
        row = self.conn.execute(
            "SELECT tradingsymbol, instrument_token, lot_size, expiry FROM "
            f"{X._t(self.schema, 'op_contract')} WHERE underlying = ? AND expiry = ? "
            "AND strike = ? AND option_type = ? AND NOT expired",
            (underlying, expiry, strike, option_type),
        ).fetchone()
        return None if row is None else Contract(
            str(row["tradingsymbol"]), int(row["instrument_token"]), int(row["lot_size"]),
            X._day(row["expiry"]))  # fmt: skip

    def _future_rows(self, symbol: str) -> list[Contract]:
        if self._futures is None:
            self._futures = [dict(r) for r in self.kite.instruments("NFO")
                             if str(r.get("instrument_type")) == "FUT"]  # fmt: skip
        out = []
        for r in self._futures:
            if str(r.get("name")) != symbol or r.get("expiry") in (None, ""):
                continue
            out.append(Contract(str(r["tradingsymbol"]), int(r["instrument_token"]),
                                int(r["lot_size"]), X._day(str(r["expiry"]))))  # fmt: skip
        return sorted(out, key=lambda c: c.expiry)

    def future(self, symbol: str, expiry: dt.date) -> Contract | None:
        return next((c for c in self._future_rows(symbol) if c.expiry == expiry), None)

    def next_future(self, symbol: str, after: dt.date) -> Contract | None:
        rows = self._future_rows(symbol)
        monthly = set(monthly_expiries(c.expiry for c in rows))
        return next((c for c in rows if c.expiry > after and c.expiry in monthly), None)


def _levels(raw: object) -> tuple[Any, ...]:
    from baskfy_core.options.chain import Level  # noqa: PLC0415

    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            price, qty = Decimal(str(item.get("price") or 0)), int(item.get("quantity") or 0)
            if price > 0 and qty > 0:
                out.append(Level(price=price, quantity=qty))
    return tuple(out)


def kite_quotes(kite: Any) -> X.QuoteSource:  # noqa: ANN401
    """Books for NFO tradingsymbols from ``quote_raw``, ≤ 500 keys a call."""

    def read(symbols: Sequence[str]) -> Mapping[str, X.FoQuote]:
        out: dict[str, X.FoQuote] = {}
        keys = [f"NFO:{s}" for s in dict.fromkeys(symbols)]
        for i in range(0, len(keys), 500):
            payload = kite.quote_raw(keys[i : i + 500]) or {}
            for key, q in payload.items():
                depth = q.get("depth") or {}
                bids, asks = _levels(depth.get("buy")), _levels(depth.get("sell"))
                out[str(key).removeprefix("NFO:")] = X.FoQuote(
                    bids[0].price if bids else None, asks[0].price if asks else None, bids, asks,
                    X._dec(q.get("last_price")), int(q.get("oi") or 0),
                )  # fmt: skip
        return out

    return read


def kite_margins(kite: Any) -> MarginSource:  # noqa: ANN401
    """``basket_order_margins`` (NRML, the basket's hedge benefit counted) against the equity
    segment's free margin. A read; an unreadable answer is ``None`` and the plan is refused."""

    def read(legs: Sequence[MarginLeg]) -> MarginQuote | None:
        orders = [
            {"exchange": "NFO", "tradingsymbol": leg.tradingsymbol, "transaction_type": leg.side,
             "variety": "regular", "product": "NRML", "order_type": "LIMIT",
             "quantity": leg.quantity, "price": float(leg.price)}
            for leg in legs
        ]  # fmt: skip
        try:
            kite.limits.slot("general")
            basket = kite.kc.basket_order_margins(orders, consider_positions=False)
            free = kite.margins("equity").get("net")
        except Exception:
            log.exception("FO plan: basket_order_margins could not be read")
            return None
        final = basket.get("final") if isinstance(basket, dict) else None
        total = final.get("total") if isinstance(final, dict) else None
        if total is None or free is None:
            return None
        return MarginQuote(Decimal(str(total)), Decimal(str(free)))

    return read


def build_monitor(*, enabled: bool, **seams: Any) -> FnoMonitor | None:  # noqa: ANN401
    """The engine, or ``None`` — with the flag off, not even constructed."""
    if not enabled:
        log.info("BASKFY_FNO_MONITOR_ENABLED is false; the FO monitor is not built")
        return None
    return FnoMonitor(**seams)


def main() -> int:
    """``python -m app.fno_monitor`` — the day's process. Exits 0 and does nothing with the flag
    off, so a scheduler entry can exist before the flag does."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if not C.FNO_MONITOR_ENABLED:
        log.info("BASKFY_FNO_MONITOR_ENABLED is false; exiting without building the monitor")
        return 0
    from . import main as _main  # noqa: PLC0415 - the desk's Kite and risk manager, flag on only
    from .analytics.db import connect  # noqa: PLC0415
    from .fno_clock import run_day  # noqa: PLC0415

    user_id = int(os.environ.get("BASKFY_SOLE_USER_ID", "0") or 0)
    if not user_id:
        log.error("BASKFY_SOLE_USER_ID is not set; fo_ rows are keyed by user")
        return 2
    kite = _main.kite()
    _main.gateway()  # builds the shared risk manager

    def gateway_for(sleeve: FoSleeve) -> Any:  # noqa: ANN401
        return X.fo_gateway(sleeve, kite.kc, _main._risk)

    with connect() as conn:
        monitor = build_monitor(
            enabled=C.FNO_MONITOR_ENABLED, store=X.FoStore(conn, user_id=user_id),
            gateway_for=gateway_for, quotes=kite_quotes(kite),
            instruments=PgKiteInstruments(conn, kite), margins=kite_margins(kite),
            market=PgMarket(conn),
        )  # fmt: skip
        if monitor is None:
            return 0
        report = asyncio.run(run_day(monitor))
        log.info("FO monitor done: %s", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
