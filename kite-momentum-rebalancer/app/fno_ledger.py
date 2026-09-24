"""The FO journal and pauses on the desk (``docs/fno/06`` FO10; the options pack's OP11 shape).

"Every close writes the record, and the record governs tomorrow."

* **The journal** — :func:`write_journal`: when a position closes (profit take, loss close, hard
  exit, ``LATE_EXIT``, stop, time exit, ``NAKED_FUTURE``, ``ROLL_INCOMPLETE``), one
  ``fo_journal`` row from the position's own fills (``baskfy_core.fno.journal.journal_figures``):
  entry, exit, gross, costs itemised per order group — each roll with its own costs — net, R
  (the position's planned max loss), sessions held, rolls, MAE/MFE from its ``fo_mark`` rows.
  Idempotent on ``position_id``. An entry abandoned after some legs filled (``ABANDONED_PARTIAL``)
  is journalled too, through a position row opened and closed on its own fills
  (:func:`journal_abandoned`, FO10.4): the money it cost is real.
* **The pauses** — :func:`apply_pauses`: right after the journal row, ``04`` §7 over the closed
  trades of **the same mode only** (``simulated`` as the position was; FO10.1). F2's month pause
  and the book's are written to ``fo_sleeve_config`` (F2) / ``fo_book_config`` with the mode in
  ``paused_reason`` (``PAPER:F2_MONTH_R``), never shortened and never over an active pause, and
  audited in ``fo_config_audit`` (``changed_by='ledger'``). F1's pause is per underlying while
  ``fo_sleeve_config`` is keyed by group, so it is recorded as an audit row (``pause:F1N``) and
  enforced from the journal (FO10.2). :func:`nightly_pauses` runs the same rules each night.
* **The gate** — :func:`entry_block`: the reasons, in words, that stop a new entry of a sleeve in
  a mode — the journal's pauses for that mode plus the stored ones that bind it. The monitor
  asks it before raising a plan (``REJECTED_PAUSED``). A pause stops new entries only; open
  structures run to their own exits (``04`` §7) — nothing here closes or sends anything.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from baskfy_core.fno.config import (
    DEFAULT_FNO_CEILINGS,
    DEFAULT_FNO_CONFIG,
    FnoCeilings,
    FnoConfig,
    FoSleeve,
    PlanKind,
    Structure,
    group_of,
)
from baskfy_core.fno.journal import CloseKind, FoFillRecord, FoJournalFigures, journal_figures
from baskfy_core.fno.ledger import (
    ClosedTrade,
    FoPause,
    column_pause_applies,
    evaluate_pauses,
    pauses_for,
)
from baskfy_core.options.config import CostRates, Side

from . import fno_execute as X

log = logging.getLogger("desk.fno_ledger")

IST = X.IST
LIFT_KEY = "lift:{sleeve}"
PAUSE_KEY = "pause:{sleeve}"
_F1 = (FoSleeve.F1N, FoSleeve.F1B)
SATURDAY = 5


# --- the journal ---------------------------------------------------------------------------------


def _fills(store: X.FoStore, position_id: int) -> list[FoFillRecord]:
    rows = store.conn.execute(
        "SELECT p.plan_id, p.kind, f.side, f.price, f.quantity, f.filled_at "
        f"FROM {store.t('fo_fill')} f JOIN {store.t('fo_leg')} l ON l.id = f.leg_id "
        f"JOIN {store.t('fo_plan')} p ON p.id = l.plan_id "
        "WHERE f.user_id = ? AND f.position_id = ? ORDER BY f.filled_at, f.id",
        (store.user_id, position_id),
    ).fetchall()
    return [
        FoFillRecord(str(r["plan_id"]), PlanKind(str(r["kind"])), Side(str(r["side"])),
                     Decimal(str(r["price"])), int(r["quantity"]), X._aware(r["filled_at"]))
        for r in rows
    ]  # fmt: skip


def _marks(store: X.FoStore, position_id: int) -> list[Decimal]:
    rows = store.conn.execute(
        f"SELECT pnl_inr FROM {store.t('fo_mark')} WHERE user_id = ? AND position_id = ? "
        "ORDER BY trade_date",
        (store.user_id, position_id),
    ).fetchall()
    return [Decimal(str(r["pnl_inr"])) for r in rows]


def sessions_between(store: X.FoStore, first: dt.date, last: dt.date) -> int:
    """Exchange sessions from ``first`` to ``last`` inclusive (``trading_day``; weekdays when the
    calendar has no rows)."""
    row = store.conn.execute(
        f"SELECT count(*) AS n FROM {store.t('trading_day')} WHERE is_trading_day "
        "AND date BETWEEN ? AND ?",
        (first, last),
    ).fetchone()
    n = int(row["n"] or 0) if row is not None else 0
    if n:
        return n
    days = (last - first).days + 1
    every = (first + dt.timedelta(days=i) for i in range(max(days, 0)))
    return sum(1 for day in every if day.weekday() < SATURDAY)


def _risk(store: X.FoStore, position: dict[str, Any], plan: X.FoPlanRow | None) -> Decimal | None:
    """R: the position's planned max loss, else the entry plan's (``RESEARCH.md``)."""
    planned = None if plan is None else _plan_max_loss(store, plan)
    for raw in (position.get("max_loss_inr"), planned):
        value = X._dec(raw)
        if value is not None and value > 0:
            return value
    return None


def _plan_max_loss(store: X.FoStore, plan: X.FoPlanRow) -> object:
    row = store.conn.execute(
        f"SELECT max_loss_inr, risk_per_lot_inr, lots FROM {store.t('fo_plan')} WHERE id = ?",
        (plan.pk,),
    ).fetchone()
    if row is None:
        return None
    if row["max_loss_inr"] is not None:
        return row["max_loss_inr"]
    if row["risk_per_lot_inr"] is not None:
        return Decimal(str(row["risk_per_lot_inr"])) * int(row["lots"])
    return None


def _sizing_mode(store: X.FoStore, plan: X.FoPlanRow | None) -> str:
    if plan is None:
        return "BUDGET"
    row = store.conn.execute(
        f"SELECT sizing_mode FROM {store.t('fo_plan')} WHERE id = ?", (plan.pk,)
    ).fetchone()
    return "BUDGET" if row is None else str(row["sizing_mode"])


def write_journal(
    store: X.FoStore, position_id: int, *, alert: Callable[[str], None] = log.error,
    option_rates: CostRates | None = None, config: FnoConfig = DEFAULT_FNO_CONFIG,
) -> FoJournalFigures | None:  # fmt: skip
    """One ``fo_journal`` row for a closed position; ``None`` (and an alert) when it cannot be
    written — the paper checklist then counts a journal gap rather than a silent hole."""
    pos = store.conn.execute(
        f"SELECT * FROM {store.t('fo_position')} WHERE user_id = ? AND id = ?",
        (store.user_id, position_id),
    ).fetchone()
    if pos is None or pos["closed_at"] is None:
        return None
    position = dict(pos)
    plan = store.plan(str(position["entry_plan_id"]))
    r_inr = _risk(store, position, plan)
    if r_inr is None:
        alert(f"FO position {position_id}: no planned max loss to measure R by; not journalled")
        return None
    fills = _fills(store, position_id)
    structure = Structure(str(position["structure"]))
    figures = journal_figures(
        structure=structure, fills=fills, r_inr=r_inr, marks_inr=_marks(store, position_id),
        option_rates=option_rates or CostRates(), future_rates=config.future_costs,
    )  # fmt: skip
    opened_on = X._aware(position["opened_at"]).astimezone(IST).date()
    closed_on = X._aware(position["closed_at"]).astimezone(IST).date()
    carry = X._json(position["legs"]).get("carry")
    rolls = len(figures.rolls) or int((carry or {}).get("rolls") or 0)
    detail = {
        "costs": figures.cost_detail(),
        "entry_plan_id": position["entry_plan_id"],
        "lots": int(position["lots"]),
        "lot_size": int(position["lot_size"]),
        "violations": [] if plan is None else plan.detail.get("violations") or [],
        "r_source": "fo_position.max_loss_inr",
    }
    row = store.conn.execute(
        f"INSERT INTO {store.t('fo_journal')} (position_id, user_id, sleeve, symbol, structure, "
        "opened_on, closed_on, entry_inr, exit_inr, gross_pnl_inr, costs_inr, net_pnl_inr, "
        "risk_budget_inr, r_multiple, closed_reason, sessions_held, rolls, mae_r, mfe_r, "
        "simulated, sizing_mode, detail) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (position_id) DO NOTHING RETURNING position_id",
        (position_id, store.user_id, str(position["sleeve"]), str(position["symbol"]),
         structure.value, opened_on, closed_on, figures.entry_inr, figures.exit_inr,
         figures.gross_pnl_inr, figures.costs_inr, figures.net_pnl_inr, figures.r_inr,
         figures.r_multiple, str(position["closed_reason"] or "EXIT")[:32],
         sessions_between(store, opened_on, closed_on), rolls, figures.mae_r, figures.mfe_r,
         bool(position["simulated"]), _sizing_mode(store, plan), X.dumps(detail)),
    ).fetchone()  # fmt: skip
    return figures if row is not None else None


def journal_abandoned(store: X.FoStore, plan: X.FoPlanRow, at: dt.datetime) -> int | None:
    """An abandoned entry whose legs filled and were closed at once (FO10.4): a position row
    opened at its first fill and closed at ``at`` (``ABANDONED_PARTIAL``), its fills attached,
    then its journal row. ``None`` when nothing filled (nothing to record)."""
    first = store.conn.execute(
        f"SELECT min(f.filled_at) AS first FROM {store.t('fo_fill')} f "
        f"JOIN {store.t('fo_leg')} l ON l.id = f.leg_id WHERE l.plan_id = ? "
        "AND f.position_id IS NULL",
        (plan.pk,),
    ).fetchone()
    if first is None or first["first"] is None:
        return None
    position_id = store.open_position({
        "sleeve": plan.sleeve.value, "symbol": plan.symbol, "structure": plan.structure,
        "entry_plan_id": plan.plan_id, "legs": {"legs": [], "carry": {"abandoned": True}},
        "lots": plan.lots, "lot_size": plan.lot_size,
        "max_loss_inr": X._dec(_plan_max_loss(store, plan)), "opened_at": X._aware(first["first"]),
        "simulated": True,
    })  # fmt: skip
    store.attach_fills(plan, position_id)
    store.close_position(position_id, CloseKind.ABANDONED_PARTIAL.value, at)
    return position_id


# --- the pauses ----------------------------------------------------------------------------------


def load_trades(store: X.FoStore) -> list[ClosedTrade]:
    rows = store.conn.execute(
        "SELECT j.position_id, j.sleeve, j.symbol, j.simulated, j.closed_on, p.closed_at, "
        "j.net_pnl_inr, j.r_multiple, j.closed_reason, j.rolls "
        f"FROM {store.t('fo_journal')} j JOIN {store.t('fo_position')} p ON p.id = j.position_id "
        "WHERE j.user_id = ? ORDER BY j.closed_on, j.position_id",
        (store.user_id,),
    ).fetchall()
    return [
        ClosedTrade(int(r["position_id"]), FoSleeve(str(r["sleeve"])), str(r["symbol"]),
                    bool(r["simulated"]), X._day(r["closed_on"]),
                    None if r["closed_at"] is None else X._aware(r["closed_at"]),
                    Decimal(str(r["net_pnl_inr"])), Decimal(str(r["r_multiple"])),
                    str(r["closed_reason"]), int(r["rolls"] or 0))
        for r in rows
    ]  # fmt: skip


def load_lifts(store: X.FoStore) -> dict[FoSleeve, dt.datetime]:
    found: dict[FoSleeve, dt.datetime] = {}
    for sleeve in _F1:
        row = store.conn.execute(
            f"SELECT max(changed_at) AS at FROM {store.t('fo_config_audit')} "
            "WHERE user_id = ? AND key = ?",
            (store.user_id, LIFT_KEY.format(sleeve=sleeve.value)),
        ).fetchone()
        if row is not None and row["at"] is not None:
            found[sleeve] = X._aware(row["at"])
    return found


def _book_amount(store: X.FoStore) -> Decimal:
    row = store.conn.execute(
        f"SELECT monthly_pause_inr FROM {store.t('fo_book_config')} WHERE user_id = ?",
        (store.user_id,),
    ).fetchone()
    return Decimal(0) if row is None else Decimal(str(row["monthly_pause_inr"]))


def evaluate(
    store: X.FoStore, *, simulated: bool, day: dt.date,
    config: FnoConfig = DEFAULT_FNO_CONFIG, ceilings: FnoCeilings = DEFAULT_FNO_CEILINGS,
) -> tuple[FoPause, ...]:  # fmt: skip
    """``04`` §7 over this user's journal, one mode, on ``day``."""
    return evaluate_pauses(
        load_trades(store), simulated=simulated, as_of=day, monthly_pause_inr=_book_amount(store),
        lifted_after=load_lifts(store), config=config, ceilings=ceilings,
    )  # fmt: skip


def _audit(  # noqa: PLR0913, PLR0917 - one fo_config_audit row
    store: X.FoStore, scope: str, key: str, old: str | None, new: str | None, note: str
) -> None:
    store.conn.execute(
        f"INSERT INTO {store.t('fo_config_audit')} (user_id, scope, key, old_value, new_value, "
        "changed_by, note) VALUES (?, ?, ?, ?, ?, 'ledger', ?)",
        (store.user_id, scope, key, old, new, note),
    )


def _write_column(store: X.FoStore, pause: FoPause, today: dt.date) -> bool:
    """F2's or the book's pause into its row: never shortened, never over another active pause
    (which stands; the journal's own pause is enforced by :func:`entry_block` and the scan all
    the same). True when written."""
    book = pause.scope == "BOOK"
    table = "fo_book_config" if book else "fo_sleeve_config"
    where = "user_id = ?" if book else "user_id = ? AND sleeve = ?"
    key: tuple[object, ...] = (store.user_id,) if book else (store.user_id, pause.scope)
    until = pause.paused_until
    if until is None:
        return False
    row = store.conn.execute(
        f"SELECT paused_until, paused_reason FROM {store.t(table)} WHERE {where}", key
    ).fetchone()
    old = None if row is None else X._opt_day(row["paused_until"])
    old_reason = None if row is None else row["paused_reason"]
    if old is not None and old >= today and (old >= until or old_reason != pause.reason):
        return False  # already recorded, or another active pause stands (the journal enforces)
    if row is None:
        cols = "user_id, paused_until, paused_reason, updated_by" if book else (
            "user_id, sleeve, paused_until, paused_reason, updated_by")  # fmt: skip
        marks = ", ".join("?" for _ in range(len(key) + 3))
        store.conn.execute(f"INSERT INTO {store.t(table)} ({cols}) VALUES ({marks})",
                           (*key, until, pause.reason, "ledger"))  # fmt: skip
    else:
        store.conn.execute(
            f"UPDATE {store.t(table)} SET paused_until = ?, paused_reason = ?, "
            f"updated_by = 'ledger', updated_at = now() WHERE {where}",
            (until, pause.reason, *key),
        )
    _audit(store, pause.scope, "paused_until", None if old is None else old.isoformat(),
           until.isoformat(), f"{pause.reason}: {pause.message}")  # fmt: skip
    return True


def _record_f1(store: X.FoStore, pause: FoPause, lifts: dict[FoSleeve, dt.datetime]) -> bool:
    """F1's per-underlying pause: one audit row per run (after the latest lift)."""
    key = PAUSE_KEY.format(sleeve=pause.scope)
    since = lifts.get(FoSleeve(pause.scope))
    row = store.conn.execute(
        f"SELECT count(*) AS n FROM {store.t('fo_config_audit')} WHERE user_id = ? AND key = ? "
        "AND new_value = ? AND (?::timestamptz IS NULL OR changed_at > ?::timestamptz)",
        (store.user_id, key, pause.reason, since, since),
    ).fetchone()
    if row is not None and int(row["n"] or 0) > 0:
        return False
    _audit(store, group_of(FoSleeve(pause.scope)).value, key, None, pause.reason, pause.message)
    return True


def apply_pauses(
    store: X.FoStore, *, simulated: bool, today: dt.date,
    config: FnoConfig = DEFAULT_FNO_CONFIG, ceilings: FnoCeilings = DEFAULT_FNO_CEILINGS,
) -> list[FoPause]:  # fmt: skip
    """``04`` §7 for one mode on ``today``, recorded. Returns every pause now in force."""
    found = evaluate(store, simulated=simulated, day=today, config=config, ceilings=ceilings)
    lifts = load_lifts(store)
    for pause in found:
        if pause.scope in {s.value for s in _F1}:
            _record_f1(store, pause, lifts)
        else:
            _write_column(store, pause, today)
    return list(found)


def after_close(store: X.FoStore, position_id: int, *, now: dt.datetime,
                alert: Callable[[str], None] = log.error) -> list[FoPause]:  # fmt: skip
    """The close's record, then ``04`` §7 in the position's own mode (``06`` FO10: "at every
    close"). A failure is alerted, never raised into the exit that has already happened."""
    try:
        write_journal(store, position_id, alert=alert)
        row = store.conn.execute(
            f"SELECT simulated FROM {store.t('fo_position')} WHERE id = ?", (position_id,)
        ).fetchone()
        simulated = True if row is None else bool(row["simulated"])
        return apply_pauses(store, simulated=simulated, today=now.astimezone(IST).date())
    except Exception as exc:  # the exit is done; the gap is alerted, logged and counted
        alert(f"FO position {position_id}: the journal or the pauses failed after the close: "
              f"{exc!r}; the paper checklist counts a journal gap")  # fmt: skip
        log.exception("fo journal after close %s", position_id)
        return []


def nightly_pauses(store: X.FoStore, *, today: dt.date) -> list[FoPause]:
    """Both modes' rules, each over its own rows (the night's pass, ``06`` FO10)."""
    return [*apply_pauses(store, simulated=True, today=today),
            *apply_pauses(store, simulated=False, today=today)]  # fmt: skip


# --- the gate the monitor asks -------------------------------------------------------------------


def entry_block(  # noqa: PLR0913 - the store, the sleeve, the day, the mode and the rules
    store: X.FoStore, sleeve: FoSleeve, *, day: dt.date, simulated: bool,
    config: FnoConfig = DEFAULT_FNO_CONFIG, ceilings: FnoCeilings = DEFAULT_FNO_CEILINGS,
) -> list[str]:  # fmt: skip
    """Why a new ``sleeve`` entry in this mode may not be raised on ``day`` (empty: it may)."""
    reasons: list[str] = []
    group = group_of(sleeve).value
    book = store.conn.execute(
        f"SELECT paused_until, paused_reason FROM {store.t('fo_book_config')} WHERE user_id = ?",
        (store.user_id,),
    ).fetchone()
    own = store.conn.execute(
        f"SELECT paused_until, paused_reason FROM {store.t('fo_sleeve_config')} "
        "WHERE user_id = ? AND sleeve = ?",
        (store.user_id, group),
    ).fetchone()
    stored = (("the FO book", book), (f"sleeve {group}", own))
    for name, row in stored:
        if row is None:
            continue
        until = X._opt_day(row["paused_until"])
        if column_pause_applies(until, row["paused_reason"], simulated=simulated, day=day):
            reasons.append(f"{name} is paused until {until} "
                           f"({row['paused_reason'] or 'no reason recorded'})")  # fmt: skip
    found = evaluate(store, simulated=simulated, day=day, config=config, ceilings=ceilings)
    reasons.extend(p.message for p in pauses_for(found, sleeve))
    return reasons


__all__ = [
    "after_close",
    "apply_pauses",
    "entry_block",
    "journal_abandoned",
    "nightly_pauses",
    "write_journal",
]
