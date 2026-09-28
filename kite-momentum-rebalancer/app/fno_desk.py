"""The desk's `/fno` page and its one POST (`docs/fno/05` §1, §4; `06` FO8).

The desk console is the only place an FO plan becomes an order (`02` Track C §3: the web app gets
no route that can reach the gateway). This module is that place's HTTP face:

* `GET /fno` — the morning plan card per sleeve (the legs as priced from live quotes at 09:20,
  overlaid with the live book when the desk has a Kite session; credit vs the scan's credit, max
  loss in ₹ and R, lots, basket margin vs free margin, costs, the entry sequence longs first, the
  hard exit date), each confirmable plan with its **Confirm (paper)** and `05` §4's sentence
  verbatim and a countdown to `expires_at`; the open F1 structures and F2 futures (the GTT's
  handle and trigger, a red flag when it is not resting); the exits and rolls the monitor sent
  under a confirm, as they happen; every refusal by name;
* `GET /fno/data` — the same view as JSON;
* `POST /fno/execute` — form `plan_id`, `confirm`; `fno_execute.execute_entry`.

**The confirm is the only field on this page that moves anything.** There is no close button, no
settings field and no second POST: exits and rolls run under the entry's confirm from the monitor
(`fno_execute.run_pending`), and capital, risk and pauses are the settings form's.

Paper only: the executor's FO gateway is pinned to its dry-run branch (DECISIONS-FO FO7.1), so
with every FO flag false — and with `DRY_RUN` false too — every leg is simulated, walking the
live depth into `fo_fill` with `simulated=true`. A LIVE sleeve is refused `LIVE_NOT_BUILT`.

CSRF, the host allowlist and sign-in are the desk's `DeskSecurity` middleware. No scheduler
reaches the POST, and there is no route that confirms without a request.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import logging
import math
import time
from collections.abc import Callable, Iterator, Mapping
from decimal import Decimal
from typing import Any

from baskfy_core.fno.condor import LegRole
from baskfy_core.fno.config import FoSleeve, PlanKind, PlanState, Structure, group_of
from baskfy_core.fno.monitor import f1_close_cost
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import config as C
from . import fno_execute as X
from .fno_monitor import _strikes

log = logging.getLogger("desk.fno_desk")

router = APIRouter()
IST = X.IST
SCHEMA = "public"
SLEEVES: tuple[FoSleeve, ...] = (FoSleeve.F1N, FoSleeve.F1B, FoSleeve.F2, FoSleeve.F3N, FoSleeve.F3B)
#: `05` §4: a credit that drifted more than this from the scan's is shown in amber.
DRIFT_AMBER_PCT = Decimal(20)
#: The index each F1 underlying is read as on NSE (DECISIONS-FO FO5.8).
SPOT_NAMES: dict[str, str] = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}

#: `05` §4, verbatim, under each sleeve's Confirm (paper).
CONFIRM_SENTENCES: dict[str, str] = {
    "F1": (
        "This confirm also authorises this structure's 50 % profit take, its loss close at "
        "1.5× the credit (shorts first), and its E−1 15:00 exit. It places nothing else."  # noqa: RUF001 - 05 §4 verbatim (Maulik, M.3)
    ),
    "F2": (
        "This confirm also authorises this position's trailing GTT stop, its E−1 rolls and its "  # noqa: RUF001 - 05 §4 verbatim
        "40-session time exit"
    ),
    "F3": (
        "This confirm sends the wing, then the short, and nothing else. The exit — a level break, "
        "the cut at 2× the credit, the 80 % decay target, or 15:00 on expiry day — is raised by "
        "the monitor and sent only when BASKFY_FNO_F3_AUTO_EXIT is on; otherwise it waits for "
        "your click here. An add is always a click."
    ),
}
#: The click on an F3 exit the monitor raised (M.5, the flag off) or on its add.
ACTION_SENTENCES: dict[str, str] = {
    "EXIT": "This confirm closes the spread now, short first, then the wing. Nothing else.",
    "ADD": ("This confirm sends the same two legs again, wing first, onto the open spread. It "
            "opens no new position and moves the level nowhere."),
}

#: The live underlying level by F1 symbol (`NIFTY`, `BANKNIFTY`), or ``None``.
SpotSource = Callable[[str], Decimal | None]


def sentence_for(sleeve: str) -> str:
    return CONFIRM_SENTENCES[group_of(FoSleeve(sleeve)).value]


# --- the seams tests replace ---------------------------------------------------------------------


@contextlib.contextmanager
def open_store() -> Iterator[X.FoStore]:
    """The sole user's FO store over the desk's connection. Tests replace this with their own."""
    from .analytics import db as _db  # noqa: PLC0415 - the desk imports its DB lazily

    with _db.connect() as conn:
        yield X.FoStore(conn, user_id=C.SOLE_USER_ID, schema=SCHEMA)


def gateway_for(sleeve: FoSleeve) -> Any:  # noqa: ANN401 - an OrderGateway
    """This sleeve's FO gateway (paper-pinned), with the desk's Kite client and shared risk."""
    from . import main as _main  # noqa: PLC0415 - main mounts this router; at call time

    _main.gateway()  # builds `_risk` as a side effect
    return X.fo_gateway(sleeve, _main.kite().kc, _main._risk)


def quotes() -> X.QuoteSource:
    """NFO books from Kite: what the paper fill walks and the page overlays."""
    from . import main as _main  # noqa: PLC0415
    from .fno_monitor import kite_quotes  # noqa: PLC0415

    return kite_quotes(_main.kite())


def view_sources() -> tuple[X.QuoteSource | None, SpotSource | None, str | None]:
    """The page's live overlay: Kite's books and the index level when the desk has a session;
    otherwise none, and the reason the page says instead."""
    from . import main as _main  # noqa: PLC0415

    try:
        kite = _main.kite()
        if not kite.is_authed():
            return None, None, "no Kite session; legs show the 09:20 pricing"
    except Exception as exc:  # the page renders without a broker
        return None, None, f"Kite unavailable ({type(exc).__name__}); legs show the 09:20 pricing"

    def spot(symbol: str) -> Decimal | None:
        name = SPOT_NAMES.get(symbol)
        if name is None:
            return None
        found = kite.ltp([name], "NSE").get(name)
        return None if found is None else Decimal(str(found))

    return quotes(), spot, None


def mode_of(sleeve: FoSleeve) -> Any:  # noqa: ANN401 - a Mode
    from .fno_gates import fno_gates  # noqa: PLC0415

    return fno_gates(sleeve).mode


def _now() -> dt.datetime:
    return dt.datetime.now(IST)


# --- small formatting ----------------------------------------------------------------------------


def _s(value: object) -> str | None:
    """A stored figure as the string it is stored as (house rule 8); ``None`` stays ``None``."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def _pct(num: Decimal, den: Decimal) -> Decimal | None:
    return None if den == 0 else (num / den * 100).quantize(Decimal("0.01"))


def _spread_pct(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return _pct(ask - bid, (ask + bid) / 2)


def _margin_message(reasons: object) -> str | None:
    """The monitor's `margin_check` sentence ("₹X within free ₹Y" / "margin ₹X > free ₹Y")."""
    if not isinstance(reasons, list):
        return None
    found = [str(r) for r in reasons if "free" in str(r) or "margin" in str(r)]
    return found[-1] if found else None


# --- the view ------------------------------------------------------------------------------------


def _scan_credit(store: X.FoStore, detail: Mapping[str, Any]) -> Decimal | None:
    raw = detail.get("scan_id")
    if raw is None:
        return None
    row = store.conn.execute(
        f"SELECT credit, detail FROM {store.t('fo_scan')} WHERE id = ? AND user_id = ?",
        (int(raw), store.user_id),
    ).fetchone()
    if row is None:
        return None
    found = X._px(row["credit"])
    if found is None:
        found = X._px(X._json(row["detail"]).get("credit_points"))
    return found


def _plan_row(store: X.FoStore, plan_id: str) -> dict[str, Any]:
    row = store.conn.execute(
        f"SELECT * FROM {store.t('fo_plan')} WHERE user_id = ? AND plan_id = ?",
        (store.user_id, plan_id),
    ).fetchone()
    return dict(row) if row is not None else {}


def _leg_view(
    store: X.FoStore, plan: X.FoPlanRow, live: Mapping[str, X.FoQuote]
) -> list[dict[str, Any]]:
    stored = {
        int(r["id"]): r
        for r in store.conn.execute(
            f"SELECT id, bid, ask, reference_price, status, filled_qty, avg_price "
            f"FROM {store.t('fo_leg')} WHERE plan_id = ?",
            (plan.pk,),
        ).fetchall()
    }
    out = []
    for lg in plan.legs:
        row = stored.get(lg.id, {})
        q = live.get(lg.tradingsymbol)
        bid = q.bid if q is not None else X._px(row.get("bid"))
        ask = q.ask if q is not None else X._px(row.get("ask"))
        out.append({
            "seq": lg.entry_seq, "role": lg.role, "symbol": lg.tradingsymbol,
            "side": lg.side, "quantity": lg.quantity, "strike": _s(lg.strike),
            "option_type": lg.option_type, "bid": _s(bid), "ask": _s(ask),
            "spread_pct": _s(_spread_pct(bid, ask)),
            "oi": None if q is None else q.oi, "live": q is not None,
            "reference": _s(X._px(row.get("reference_price"))),
            "status": row.get("status"), "filled": row.get("filled_qty"),
            "avg_price": _s(X._px(row.get("avg_price"))),
        })  # fmt: skip
    return out


def _live_credit(plan: X.FoPlanRow, live: Mapping[str, X.FoQuote]) -> Decimal | None:
    """The condor's credit on live mids now (shorts received, longs paid), or ``None``."""
    total = Decimal(0)
    for lg in plan.legs:
        q = live.get(lg.tradingsymbol)
        if q is None or q.mid is None:
            return None
        total += q.mid if lg.side == "SELL" else -q.mid
    return total.quantize(Decimal("0.01"))


def _plan_view(
    store: X.FoStore, plan: X.FoPlanRow, *, now: dt.datetime, live: Mapping[str, X.FoQuote]
) -> dict[str, Any]:
    head = _plan_row(store, plan.plan_id)
    d = plan.detail
    is_f1 = plan.structure == Structure.IRON_CONDOR.value
    credit = X._px(head.get("credit_points"))
    scan_credit = _scan_credit(store, d) if is_f1 else None
    drift = None
    if credit is not None and scan_credit is not None and scan_credit != 0:
        drift = _pct(credit - scan_credit, scan_credit)
    max_loss = X._px(head.get("max_loss_inr"))
    budget = X._px(head.get("risk_budget_inr"))
    max_loss_r = None
    if max_loss is not None and budget is not None and budget > 0:
        max_loss_r = (max_loss / budget).quantize(Decimal("0.01"))
    expires = plan.expires_at
    confirmable = plan.status == PlanState.ISSUED.value and now < expires
    sequence = " → ".join(f"{lg.role.replace('_', ' ').lower()}" for lg in plan.legs)
    return {
        "plan_id": plan.plan_id, "sleeve": plan.sleeve.value,
        "group": group_of(plan.sleeve).value, "symbol": plan.symbol,
        "structure": plan.structure, "status": plan.status, "reason": head.get("reason"),
        "issued_at": plan.issued_at.isoformat(), "expires_at": expires.isoformat(),
        "expires_epoch_ms": int(expires.timestamp() * 1000),
        "seconds_left": max(0, int((expires - now).total_seconds())),
        "confirmable": confirmable, "paper": mode_of(plan.sleeve).value == "PAPER",
        "sentence": sentence_for(plan.sleeve.value),
        "legs": _leg_view(store, plan, live),
        "entry_sequence": sequence,
        "credit": _s(credit), "live_credit": _s(_live_credit(plan, live)) if is_f1 else None,
        "scan_credit": _s(scan_credit), "credit_drift_pct": _s(drift),
        "credit_drift_amber": drift is not None and abs(drift) > DRIFT_AMBER_PCT,
        "width": _s(X._px(head.get("width_points"))),
        "profit_take": _s(d.get("profit_take_points")),
        "loss_close": _s(d.get("loss_close_points")),
        "entry_reference": _s(X._px(head.get("debit_points"))),
        "stop": _s(d.get("stop")), "gtt_trigger": _s(d.get("gtt_trigger")),
        "time_exit_date": _s(d.get("time_exit_date")), "roll_date": _s(d.get("roll_date")),
        "lots": plan.lots, "lots_sized": d.get("lots_sized"),
        "lots_at_ceiling": d.get("lots_at_ceiling"), "lot_size": plan.lot_size,
        "max_loss_inr": _s(max_loss), "risk_budget_inr": _s(budget), "max_loss_r": _s(max_loss_r),
        "margin_required_inr": _s(X._px(head.get("margin_required_inr"))),
        "margin_message": _margin_message(d.get("reasons")),
        "cost_inr": _s(X._px(head.get("expected_cost_inr"))),
        "cost_share_pct": _s(head.get("cost_share")),
        "hard_exit_date": _s(plan.hard_exit_date),
        "kind": plan.kind,
        "direction": d.get("direction"), "level": _s(X._dec(d.get("level"))),
        "index_last": _s(X._dec(d.get("index_last"))),
        "decay_target": _s(X._dec(d.get("decay_target_mark"))),
        "loss_cut": _s(X._dec(d.get("loss_cut_mark"))), "rule": d.get("rule"),
    }  # fmt: skip


def _sigma(strike: Decimal, spot: Decimal, iv: float, days: int) -> Decimal | None:
    """The underlying's distance to a strike in sigmas over the days left: ``ln(K/S) / (iv √T)``."""
    if spot <= 0 or strike <= 0 or iv <= 0 or days <= 0:
        return None
    width = iv * math.sqrt(days / 365.0)
    return Decimal(str(round(math.log(float(strike / spot)) / width, 2)))


def _f1_view(
    store: X.FoStore, pos: X.PositionRow, *, now: dt.datetime, live: Mapping[str, X.FoQuote],
    spot: SpotSource | None,
) -> dict[str, Any]:  # fmt: skip
    legs = pos.leg_list
    marks: dict[str, Decimal] = {}
    for lg in legs:
        q = live.get(str(lg.get("tradingsymbol")))
        if q is not None and q.mid is not None:
            marks[str(lg.get("role"))] = q.mid
    # The monitor's own arithmetic (each vertical clamped to its width), so the page and the
    # rule that fires can never disagree about the cost to close.
    strikes = _strikes(pos.carry)
    close_cost = None
    if strikes is not None:
        found = f1_close_cost(strikes, {LegRole(r): m for r, m in marks.items()
                                        if r in LegRole.__members__})  # fmt: skip
        close_cost = None if found is None else found.quantize(Decimal("0.01"))
    row = _position_row(store, pos.id)
    profit = X._px(row.get("profit_take_points"))
    loss = X._px(row.get("loss_close_points"))
    level = None
    try:
        level = spot(pos.symbol) if spot is not None else None
    except Exception as exc:  # a missing level is a dash, never a failed page
        log.warning("FO page: the %s level could not be read: %s", pos.symbol, exc)
    iv = _entry_iv(store, pos.entry_plan_id)
    sigmas: dict[str, str | None] = {}
    for lg in legs:
        role = str(lg.get("role"))
        strike = X._dec(lg.get("strike"))
        expiry = X._opt_day(lg.get("expiry"))
        if role.startswith("SHORT") and strike is not None and expiry is not None:
            days = (expiry - now.date()).days
            sigmas[role] = (
                None if level is None or iv is None else _s(_sigma(strike, level, iv, days))
            )
    pnl = None
    if close_cost is not None and pos.entry_credit is not None:
        pnl = ((pos.entry_credit - close_cost) * pos.lots * pos.lot_size).quantize(Decimal("0.01"))
    return {
        "id": pos.id, "sleeve": pos.sleeve.value, "symbol": pos.symbol,
        "entry_plan_id": pos.entry_plan_id, "lots": pos.lots, "lot_size": pos.lot_size,
        "entry_credit": _s(pos.entry_credit), "live_close_cost": _s(close_cost),
        "live_pnl_inr": _s(pnl), "max_loss_inr": _s(pos.max_loss_inr),
        "profit_take": _s(profit), "loss_close": _s(loss),
        "to_profit_take": None if close_cost is None or profit is None else _s(close_cost - profit),
        "to_loss_close": None if close_cost is None or loss is None else _s(loss - close_cost),
        "spot": _s(level), "short_sigma": sigmas, "hard_exit_date": _s(pos.hard_exit_date),
        "next_rule": _f1_next_rule(pos, profit, loss),
        "last_mark": _last_mark(store, pos.id), "simulated": pos.simulated,
        "legs": [{"role": lg.get("role"), "symbol": lg.get("tradingsymbol"),
                  "avg_price": _s(lg.get("avg_price")),
                  "mid": _s(marks.get(str(lg.get("role"))))} for lg in legs],
    }  # fmt: skip


def _f1_next_rule(pos: X.PositionRow, profit: Decimal | None, loss: Decimal | None) -> str:
    when = "—" if pos.hard_exit_date is None else pos.hard_exit_date.isoformat()
    return (f"the E-1 exit at 15:00 on {when}, unless the profit take (close at <= "
            f"{_s(profit) or '—'}) or the loss close (>= {_s(loss) or '—'}) fires "
            "first")  # fmt: skip


def _f3_view(
    store: X.FoStore, pos: X.PositionRow, *, live: Mapping[str, X.FoQuote], spot: SpotSource | None,
) -> dict[str, Any]:  # fmt: skip
    """An open F3 spread: the level against the index, the mark against the target and the cut,
    the next rule and whether the add is spent (04 §11)."""
    legs = pos.leg_list
    carry = pos.carry
    marks: dict[str, Decimal] = {}
    for lg in legs:
        q = live.get(str(lg.get("tradingsymbol")))
        if q is not None and q.mid is not None:
            marks[str(lg.get("role"))] = q.mid
    short_mark = next((m for r, m in marks.items() if r.startswith("SHORT")), None)
    wing_mark = next((m for r, m in marks.items() if r.startswith("LONG")), None)
    mark = None if short_mark is None else (short_mark - (wing_mark or Decimal(0))).quantize(
        Decimal("0.01"))
    level = None
    try:
        level = spot(pos.symbol) if spot is not None else None
    except Exception as exc:  # a missing level is a dash, never a failed page
        log.warning("FO page: the %s level could not be read: %s", pos.symbol, exc)
    row = _position_row(store, pos.id)
    target = X._px(row.get("profit_take_points"))
    cut = X._px(row.get("loss_close_points"))
    pnl = None
    if mark is not None and pos.entry_credit is not None:
        pnl = ((pos.entry_credit - mark) * pos.lots * pos.lot_size).quantize(Decimal("0.01"))
    direction = str(carry.get("direction") or "")
    beyond = "below" if direction == "UP" else "above"
    when = "—" if pos.hard_exit_date is None else pos.hard_exit_date.isoformat()
    return {
        "id": pos.id, "sleeve": pos.sleeve.value, "symbol": pos.symbol,
        "entry_plan_id": pos.entry_plan_id, "lots": pos.lots, "lot_size": pos.lot_size,
        "direction": direction, "level": _s(pos.stop_price), "spot": _s(level),
        "entry_credit": _s(pos.entry_credit), "mark": _s(mark), "live_pnl_inr": _s(pnl),
        "max_loss_inr": _s(pos.max_loss_inr), "decay_target": _s(target), "loss_cut": _s(cut),
        "expiry": when, "added": bool(carry.get("added")),
        "next_rule": (f"out at once if {pos.symbol} trades {beyond} {_s(pos.stop_price) or '—'}; "
                      f"the cut at a mark of {_s(cut) or '—'}; the target at {_s(target) or '—'}; "
                      f"flat at 15:00 on {when}"),
        "last_mark": _last_mark(store, pos.id), "simulated": pos.simulated,
        "legs": [{"role": lg.get("role"), "symbol": lg.get("tradingsymbol"),
                  "quantity": lg.get("quantity"), "avg_price": _s(lg.get("avg_price")),
                  "mid": _s(marks.get(str(lg.get("role"))))} for lg in legs],
    }  # fmt: skip


def _f2_view(
    store: X.FoStore, pos: X.PositionRow, *, live: Mapping[str, X.FoQuote]
) -> dict[str, Any]:
    legs = pos.leg_list
    symbol = str(legs[0].get("tradingsymbol")) if legs else ""
    q = live.get(symbol)
    last = None if q is None else (q.last or q.mid)
    carry = pos.carry
    entry = X._dec(legs[0].get("avg_price")) if legs else None
    entry = entry or pos.entry_price
    pnl = None
    if last is not None and entry is not None:
        qty = int(legs[0].get("quantity") or 0) if legs else 0
        pnl = ((last - entry) * qty).quantize(Decimal("0.01"))
    resting = bool(pos.gtt_id)
    roll = _s(pos.next_roll_date)
    time_exit = _s(carry.get("time_exit_date"))
    return {
        "id": pos.id, "sleeve": pos.sleeve.value, "symbol": pos.symbol, "contract": symbol,
        "entry_plan_id": pos.entry_plan_id, "lots": pos.lots, "lot_size": pos.lot_size,
        "entry_price": _s(entry), "last": _s(last), "live_pnl_inr": _s(pnl),
        "gtt_id": pos.gtt_id, "gtt_trigger": _s(pos.stop_price), "gtt_resting": resting,
        "trail_stop": _s(carry.get("trail_stop")), "rolls": carry.get("rolls"),
        "roll_date": roll, "time_exit_date": time_exit,
        "next_rule": (f"the stop at {_s(pos.stop_price) or '—'}; the E-1 roll at 15:00 on "
                      f"{roll or '—'}; the time exit on {time_exit or '—'}"),
        "last_mark": _last_mark(store, pos.id), "simulated": pos.simulated,
    }  # fmt: skip


def _position_row(store: X.FoStore, position_id: int) -> dict[str, Any]:
    row = store.conn.execute(
        f"SELECT profit_take_points, loss_close_points FROM {store.t('fo_position')} "
        "WHERE id = ? AND user_id = ?",
        (position_id, store.user_id),
    ).fetchone()
    return dict(row) if row is not None else {}


def _entry_iv(store: X.FoStore, plan_id: str) -> float | None:
    plan = store.plan(plan_id)
    if plan is None:
        return None
    raw = plan.detail.get("iv")
    if raw is None:
        raw_scan = plan.detail.get("scan_id")
        if raw_scan is None:
            return None
        row = store.conn.execute(
            f"SELECT iv FROM {store.t('fo_scan')} WHERE id = ?", (int(raw_scan),)
        ).fetchone()
        raw = None if row is None else row["iv"]
    try:
        return None if raw is None else float(raw)
    except (TypeError, ValueError):
        return None


def _last_mark(store: X.FoStore, position_id: int) -> dict[str, Any] | None:
    row = store.conn.execute(
        f"SELECT trade_date, mark_points, pnl_inr FROM {store.t('fo_mark')} "
        "WHERE position_id = ? ORDER BY trade_date DESC LIMIT 1",
        (position_id,),
    ).fetchone()
    if row is None:
        return None
    return {"trade_date": _s(X._opt_day(row["trade_date"])),
            "mark_points": _s(X._px(row["mark_points"])), "pnl_inr": _s(X._px(row["pnl_inr"]))}


def _actions(store: X.FoStore, day: dt.date) -> list[dict[str, Any]]:
    """The exits and rolls the monitor raised under a confirm: today's, and any still running."""
    rows = store.conn.execute(
        f"SELECT plan_id, parent_plan_id, sleeve, symbol, kind, status, reason, issued_at, "
        f"expires_at, detail FROM {store.t('fo_plan')} WHERE user_id = ? AND kind IN "
        "('EXIT', 'ROLL', 'ADD') AND (trade_date = ? OR status IN ('ISSUED', 'CONFIRMED', "
        "'FILLING')) ORDER BY id DESC",
        (store.user_id, day),
    ).fetchall()
    out = []
    for r in rows:
        detail = X._json(r["detail"])
        # An F3 exit the monitor raised for the click (M.5, the flag off), or an F3 add: ISSUED
        # and confirmable until it lapses.
        expires = X._aware(r["expires_at"])
        confirmable = (str(r["status"]) == PlanState.ISSUED.value
                       and str(r["kind"]) in ACTION_SENTENCES and _now() < expires)  # fmt: skip
        out.append({
            "plan_id": r["plan_id"], "parent_plan_id": r["parent_plan_id"], "sleeve": r["sleeve"],
            "symbol": r["symbol"], "kind": r["kind"], "status": r["status"],
            "code": detail.get("code"), "message": detail.get("message"), "reason": r["reason"],
            "issued_at": _s(X._aware(r["issued_at"])), "confirmable": confirmable,
            "awaits_click": bool(detail.get("awaits_click")),
            "sentence": ACTION_SENTENCES.get(str(r["kind"])),
            "expires_epoch_ms": int(expires.timestamp() * 1000),
        })  # fmt: skip
    return out


def _violations(store: X.FoStore) -> list[dict[str, Any]]:
    """Paper-checklist violations on the entry plans of open positions (FO7.12)."""
    out = []
    for pos in store.open_positions():
        plan = store.plan(pos.entry_plan_id)
        for v in (plan.detail.get("violations") if plan is not None else None) or []:
            if isinstance(v, dict):
                out.append({"position_id": pos.id, "symbol": pos.symbol, **v})
    return out


def build_view(
    store: X.FoStore, *, now: dt.datetime, quotes: X.QuoteSource | None = None,
    spot: SpotSource | None = None, live_note: str | None = None,
) -> dict[str, Any]:  # fmt: skip
    """Today's entry plans, the open book and the actions under confirms, as `/fno` shows them."""
    day = now.astimezone(IST).date()
    ids = [
        str(r["plan_id"])
        for r in store.conn.execute(
            f"SELECT plan_id FROM {store.t('fo_plan')} WHERE user_id = ? AND trade_date = ? "
            "AND kind = 'ENTRY' ORDER BY issued_at, id",
            (store.user_id, day),
        ).fetchall()
    ]
    plans = [p for p in (store.plan(i) for i in ids) if p is not None]
    positions = store.open_positions()
    symbols = [lg.tradingsymbol for p in plans if p.status == PlanState.ISSUED.value
               for lg in p.legs]  # fmt: skip
    symbols += [str(lg.get("tradingsymbol")) for pos in positions for lg in pos.leg_list]
    live: Mapping[str, X.FoQuote] = {}
    note = live_note
    if quotes is not None and symbols:
        try:
            live = quotes(symbols)
        except Exception as exc:  # the page says why the overlay is missing
            note = f"live quotes unavailable ({type(exc).__name__}: {exc})"
            live = {}
    return {
        "day": day.isoformat(), "now": now.isoformat(),
        "modes": {s.value: mode_of(s).value for s in SLEEVES},
        "live_note": note,
        "plans": [_plan_view(store, p, now=now, live=live) for p in plans],
        "structures": [_f1_view(store, p, now=now, live=live, spot=spot) for p in positions
                       if p.structure == Structure.IRON_CONDOR.value],
        "futures": [_f2_view(store, p, live=live) for p in positions
                    if p.structure == Structure.FUTURE.value],
        "spreads": [_f3_view(store, p, live=live, spot=spot) for p in positions
                    if p.structure == Structure.CREDIT_SPREAD.value],
        "f3_auto_exit": _f3_auto_exit(),
        "actions": _actions(store, day),
        "violations": _violations(store),
        "badge": badge_of(positions),
    }  # fmt: skip


def _f3_auto_exit() -> bool:
    from .fno_gates import f3_auto_exit_enabled  # noqa: PLC0415 - M.5's flag lives there

    return f3_auto_exit_enabled()


def badge_of(positions: list[X.PositionRow]) -> dict[str, Any]:
    """`05` §1: the open structures and their nearest hard-exit date (F1's `E - 1`; F2's time
    exit, its hard exit — a roll carries the position, FO8.3)."""
    dates: list[dt.date] = []
    for pos in positions:
        if pos.hard_exit_date is not None:
            dates.append(pos.hard_exit_date)
        found = X._opt_day(pos.carry.get("time_exit_date"))
        if found is not None:
            dates.append(found)
    return {"open": len(positions), "nearest": min(dates).isoformat() if dates else None}


# --- the nav badge (every desk page) -------------------------------------------------------------

_BADGE_TTL = 60.0
_badge_cache: dict[str, Any] = {"at": -1e9, "value": None}


def nav_badge() -> dict[str, Any]:
    """The `F&O Overnight` tab's badge and the sleeves' modes, cached a minute; never raises —
    a nav that cannot read the book shows the modes alone."""
    modes = {s.value: mode_of(s).value for s in SLEEVES}
    if time.monotonic() - float(_badge_cache["at"]) < _BADGE_TTL:
        cached = _badge_cache["value"]
        return {**(cached or {"open": None, "nearest": None}), "modes": modes}
    try:
        with open_store() as store:
            value = badge_of(store.open_positions())
    except Exception as exc:  # the nav renders on every page, with or without the fo_ tables
        log.debug("FO nav badge unavailable: %s", exc)
        value = None
    _badge_cache.update(at=time.monotonic(), value=value)
    return {**(value or {"open": None, "nearest": None}), "modes": modes}


# --- the routes ----------------------------------------------------------------------------------


def _view() -> dict[str, Any]:
    try:
        live_quotes, spot, note = view_sources()
        with open_store() as store:
            return {"ok": True, **build_view(store, now=_now(), quotes=live_quotes, spot=spot,
                                             live_note=note)}  # fmt: skip
    except Exception as exc:  # the page must render, and say why it has nothing
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "plans": [],
                "structures": [], "futures": [], "actions": [], "violations": [],
                "modes": {s.value: mode_of(s).value for s in SLEEVES},
                "badge": {"open": None, "nearest": None}}  # fmt: skip


def _templates() -> Any:  # noqa: ANN401 - Jinja2Templates, owned by app.main
    from . import main as _main  # noqa: PLC0415

    return _main.templates


@router.get("/fno", response_class=HTMLResponse)
async def fno_page(request: Request) -> HTMLResponse:
    page: HTMLResponse = _templates().TemplateResponse(request, "fno.html", {"v": _view()})
    return page


@router.get("/fno/data")
async def fno_data() -> JSONResponse:
    return JSONResponse(_view())


def outcome_json(outcome: X.FoOutcome) -> dict[str, Any]:
    out = X._jsonable({
        "plan_id": outcome.plan_id, "sleeve": outcome.sleeve, "kind": outcome.kind,
        "outcome": outcome.outcome, "simulated": outcome.simulated, "orders": outcome.orders,
        "position_id": outcome.position_id, "detail": outcome.detail,
    })  # fmt: skip
    return out if isinstance(out, dict) else {}


@router.post("/fno/execute")
async def fno_execute(plan_id: str = Form(""), confirm: str = Form("")) -> JSONResponse:
    """The one route that moves anything. `confirm` must be the literal `true` and `plan_id` an
    `ISSUED` entry plan inside its 30 minutes; a second post of the same plan is refused
    (`NOT_ISSUED`), and every client id is `plan_id:tradingsymbol`, so nothing is sent twice.
    A missing `confirm` is the same refusal as a false one, by name, before any read."""
    if confirm != "true":
        return JSONResponse({"code": "CONFIRM_REQUIRED", "detail": "confirm must be true"}, 400)
    try:
        with open_store() as store:
            plan = store.plan(plan_id)
            if plan is None:
                return JSONResponse({"code": "UNKNOWN_PLAN", "detail": "no such plan"}, 404)
            if plan.kind == PlanKind.EXIT.value:
                # An F3 exit the monitor raised for the click (M.5): confirmed and closed here.
                outcome = await X.execute_click_exit(
                    store, gateway_for(plan.sleeve), quotes=quotes(), plan_id=plan_id,
                    confirm=True, now=_now,
                )  # fmt: skip
            else:
                outcome = await X.execute_entry(
                    store, gateway_for(plan.sleeve), quotes=quotes(), plan_id=plan_id,
                    confirm=True, mode_of=mode_of, now=_now,
                )  # fmt: skip
    except X.Refused as exc:
        return JSONResponse({"code": exc.code, "detail": exc.message}, status_code=exc.status)
    _badge_cache["at"] = -1e9  # the tab's count changes with this confirm
    body = outcome_json(outcome)
    if outcome.outcome not in (PlanState.OPEN.value, PlanState.CLOSED.value):
        refusals = outcome.detail.get("refusals") or []
        body["code"] = outcome.outcome
        body["detail"] = "; ".join(str(r) for r in refusals) or outcome.outcome
    return JSONResponse(body)
