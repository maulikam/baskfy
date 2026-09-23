"""The options journal and the risk ledger on the desk (`docs/options/06` OP11).

"Every close writes the record, and the record governs tomorrow."

* **The journal** — `write_journal`: when a position closes FLAT, one `op_journal` row from the
  session's own fills (`baskfy_core.options.ledger.journal_figures`: gross, `04` §6.1's costs over
  every order, net, R, minutes held, MAE/MFE from the monitor's extremes), and the session's
  `pnl_inr` / `pnl_r`. Idempotent on `session_id`.
* **The ledger** — `apply_ledger`: right after the journal row, `04` §9.1 for the sleeve
  (`risk.evaluate_sleeve` over that sleeve's rows of the same kind — paper rows while it is PAPER,
  real rows while LIVE, never both) and §9.3 for the book (`risk.evaluate_book`, once any sleeve has
  capital). A pause is written to `op_sleeve_config` (the sleeve's group) or `op_book_config`, never
  shortened, and audited in `op_config_audit` (`changed_by='ledger'`). A pause that says
  `close_now` — `DAILY_R`, or a book day breach — raises an exit plan for every open position it
  covers, which the monitor's sweep closes (OP10.7).
* **09:00** — `morning_ledger`: the same rules before the session, so a week or month pause that
  a late close earned stands at the open. The monitor runner calls it on start.

A pause is a refusal (§9.4): the worker's plan builders read `paused_until` through `load_context`
and answer `REJECTED_PAUSED`. Nothing here places an order; the closes are the executor's.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal
from typing import Any

from baskfy_core.options.config import (
    Mode,
    OptionsCeilings,
    OptionsConfig,
    Side,
    Sleeve,
    group_of,
)
from baskfy_core.options.exits import ExitVerdict
from baskfy_core.options.ledger import JournalFigures, LegFill, journal_figures, pnl_points
from baskfy_core.options.risk import (
    Pause,
    RealisedTrade,
    book_limits,
    evaluate_book,
    evaluate_sleeve,
)
from baskfy_core.options.structures import Structure

log = logging.getLogger("desk.options_ledger")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _d(value: object) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


def _day(value: object) -> dt.date:
    """The desk adapter returns a `date` as an ISO string (OP10.6); psycopg as a `date`."""
    if isinstance(value, str):
        return dt.date.fromisoformat(value[:10])
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    raise TypeError(f"not a date: {value!r}")


def _aware(at: object) -> dt.datetime:
    value = dt.datetime.fromisoformat(at) if isinstance(at, str) else at
    assert isinstance(value, dt.datetime)
    return value if value.tzinfo is not None else value.replace(tzinfo=IST)


# --- the journal --------------------------------------------------------------------------------


def write_journal(store: Any, session_id: int, *, reason: str) -> JournalFigures | None:  # noqa: ANN401
    """One `op_journal` row for a closed session, from its fills. `None` if it never filled."""
    t = store.t
    head = store.conn.execute(
        f"SELECT s.sleeve, s.trade_date, s.expiry_used, p.plan_id, p.structure, p.sizing_mode, "
        f"p.risk_budget_inr, p.risk_per_lot_inr, p.lots, p.lot_size, p.detail, "
        f"pos.opened_at, pos.closed_at, pos.peak_value, pos.trough_value, pos.simulated "
        f"FROM {t('op_session')} s JOIN {t('op_plan')} p ON p.plan_id = s.plan_id "
        f"JOIN {t('op_position')} pos ON pos.session_id = s.id "
        "WHERE s.id = ? AND s.user_id = ?",
        (session_id, store.user_id),
    ).fetchone()
    if head is None:
        return None
    rows = store.conn.execute(
        f"SELECT f.price, f.quantity, o.side, (o.side <> l.side) AS closing "
        f"FROM {t('op_fill')} f JOIN {t('op_order')} o ON o.id = f.order_id "
        f"JOIN {t('op_leg')} l ON l.id = f.leg_id JOIN {t('op_plan')} p ON p.id = l.plan_id "
        "WHERE p.plan_id = ? ORDER BY f.id",
        (head["plan_id"],),
    ).fetchall()
    if not rows:
        return None
    fills = [
        LegFill(Side(str(r["side"])), _d(r["price"]), int(r["quantity"]), bool(r["closing"]))
        for r in rows
    ]
    paper_one_lot = str(head["sizing_mode"]) == "PAPER_ONE_LOT"
    r_inr = _d(head["risk_per_lot_inr"] if paper_one_lot else head["risk_budget_inr"])
    if r_inr <= 0:
        r_inr = _d(head["risk_budget_inr"]) or _d(head["risk_per_lot_inr"])
    if r_inr <= 0:
        log.error("options journal: session %s has no R; not journalled", session_id)
        return None
    closed = _aware(head["closed_at"]) if head["closed_at"] else dt.datetime.now(IST)
    figures = journal_figures(
        fills, r_inr=r_inr, quantity=int(head["lots"]) * int(head["lot_size"]),
        opened_at=_aware(head["opened_at"]), closed_at=closed,
        peak_points=None if head["peak_value"] is None else _d(head["peak_value"]),
        trough_points=None if head["trough_value"] is None else _d(head["trough_value"]),
        rates=OptionsConfig().costs,
    )  # fmt: skip
    detail = head["detail"] if isinstance(head["detail"], dict) else {}
    c = figures.costs
    store.conn.execute(
        f"INSERT INTO {t('op_journal')} (session_id, user_id, sleeve, trade_date, expiry_used, "
        "structure, entry_inr, exit_inr, gross_pnl_inr, costs_inr, net_pnl_inr, risk_budget_inr, "
        "r_multiple, closed_reason, minutes_held, mae_r, mfe_r, simulated, sizing_mode, half_size, "
        "detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (session_id) DO NOTHING",
        (session_id, store.user_id, head["sleeve"], _day(head["trade_date"]),
         _day(head["expiry_used"] or head["trade_date"]), head["structure"], figures.entry_inr,
         figures.exit_inr, figures.gross_pnl_inr, c.total, figures.net_pnl_inr, figures.r_inr,
         figures.r_multiple, reason[:32], figures.minutes_held, figures.mae_r, figures.mfe_r,
         bool(head["simulated"]), head["sizing_mode"], bool(detail.get("half_size", False)),
         json.dumps({"costs": {"orders": c.orders, "brokerage": str(c.brokerage), "stt": str(c.stt),
                               "exchange_txn": str(c.exchange_txn), "sebi": str(c.sebi),
                               "ipft": str(c.ipft), "stamp": str(c.stamp), "gst": str(c.gst)},
                     "fills": len(fills)})),
    )  # fmt: skip
    store.conn.execute(
        f"UPDATE {t('op_session')} SET pnl_inr = ?, pnl_r = ? WHERE id = ?",
        (figures.net_pnl_inr, figures.r_multiple, session_id),
    )
    return figures


# --- the ledger ---------------------------------------------------------------------------------


def _trading_days(store: Any, today: dt.date) -> list[dt.date]:  # noqa: ANN401
    rows = store.conn.execute(
        f"SELECT date FROM {store.t('trading_day')} WHERE is_trading_day AND date BETWEEN ? AND ?",
        (today, today + dt.timedelta(days=7)),
    ).fetchall()
    days = [_day(r["date"]) for r in rows]
    if days:
        return days
    # No calendar rows (a bare database): weekdays stand in — the rule's own fallback is the day.
    return [today + dt.timedelta(days=i) for i in range(8) if (today + dt.timedelta(days=i)).weekday() < 5]


def _history(store: Any, sleeve: Sleeve, today: dt.date, simulated: bool) -> list[RealisedTrade]:  # noqa: ANN401
    rows = store.conn.execute(
        f"SELECT trade_date, net_pnl_inr, r_multiple FROM {store.t('op_journal')} "
        "WHERE user_id = ? AND sleeve = ? AND simulated = ? AND trade_date BETWEEN ? AND ?",
        (store.user_id, sleeve.value, simulated, today - dt.timedelta(days=40), today),
    ).fetchall()
    return [
        RealisedTrade(sleeve, _day(r["trade_date"]), _d(r["net_pnl_inr"]), _d(r["r_multiple"]))
        for r in rows
    ]


def _write_pause(store: Any, scope: str, pause: Pause) -> bool:  # noqa: ANN401
    """Write a pause (never shortening one already there); audit it. True if anything changed."""
    t = store.t
    table = "op_book_config" if scope == "BOOK" else "op_sleeve_config"
    key_sql = "user_id = ?" if scope == "BOOK" else "user_id = ? AND sleeve = ?"
    key = (store.user_id,) if scope == "BOOK" else (store.user_id, scope)
    row = store.conn.execute(
        f"SELECT paused_until FROM {t(table)} WHERE {key_sql}", key
    ).fetchone()
    old = None if row is None or row["paused_until"] is None else _day(row["paused_until"])
    until = max(pause.paused_until, old) if old is not None else pause.paused_until
    reason = ",".join(r.value for r in pause.reasons)[:32]
    if row is None:
        cols = "user_id, paused_until, paused_reason" if scope == "BOOK" else (
            "user_id, sleeve, paused_until, paused_reason"
        )
        marks = "?, ?, ?" if scope == "BOOK" else "?, ?, ?, ?"
        store.conn.execute(f"INSERT INTO {t(table)} ({cols}) VALUES ({marks})", (*key, until, reason))
    elif old == until:
        return False
    else:
        store.conn.execute(
            f"UPDATE {t(table)} SET paused_until = ?, paused_reason = ?, updated_by = 'ledger' "
            f"WHERE {key_sql}",
            (until, reason, *key),
        )
    store.conn.execute(
        f"INSERT INTO {t('op_config_audit')} (user_id, scope, key, old_value, new_value, "
        "changed_by, note) VALUES (?, ?, 'paused_until', ?, ?, 'ledger', ?)",
        (store.user_id, scope, None if old is None else old.isoformat(), until.isoformat(), reason),
    )
    return True


def _open_marked_inr(store: Any, *, sleeve: Sleeve | None, simulated: bool) -> Decimal:  # noqa: ANN401
    """Today's marked P&L of the open positions (one sleeve's, or every sleeve's)."""
    rows = store.conn.execute(
        f"SELECT pos.entry_points, pos.last_mark_points, pos.lots, p.lot_size, p.structure, s.sleeve "
        f"FROM {store.t('op_position')} pos JOIN {store.t('op_session')} s ON s.id = pos.session_id "
        f"JOIN {store.t('op_plan')} p ON p.plan_id = s.plan_id "
        "WHERE pos.user_id = ? AND pos.closed_at IS NULL AND pos.simulated = ?",
        (store.user_id, simulated),
    ).fetchall()
    total = Decimal(0)
    for r in rows:
        if sleeve is not None and str(r["sleeve"]) != sleeve.value:
            continue
        if r["last_mark_points"] is None:
            continue
        pnl = pnl_points(Structure(str(r["structure"])), _d(r["entry_points"]), _d(r["last_mark_points"]))
        total += pnl * int(r["lots"]) * int(r["lot_size"])
    return total


def _close_open(store: Any, *, sleeve: Sleeve | None, code: str, now: dt.datetime) -> list[int]:  # noqa: ANN401
    """Raise an exit plan (code = the pause's reason) for every open position it covers."""
    from .options_monitor import PgPositionStore  # noqa: PLC0415 - the one exit-plan writer

    positions = PgPositionStore(store.conn, user_id=store.user_id, schema=store.schema)
    raised: list[int] = []
    for tracked in positions.open_positions(now.date()):
        if sleeve is not None and tracked.position.sleeve is not sleeve:
            continue
        if tracked.exit_plan_id is None:
            positions.raise_exit(tracked, ExitVerdict(code, "LEDGER", None, None, False), now)
            raised.append(tracked.session_id)
    return raised


def apply_ledger(  # noqa: PLR0913 - the store, the sleeve, the day and the rules
    store: Any,  # noqa: ANN401 - a PgOptionsStore
    *,
    sleeve: Sleeve,
    today: dt.date,
    now: dt.datetime,
    r_today_inr: Decimal,
    mode: Mode,
    options: OptionsConfig | None = None,
    ceilings: OptionsCeilings | None = None,
) -> list[str]:
    """`04` §9.1 for `sleeve`, then §9.3 for the book. Returns every pause reason applied."""
    cfg = options or OptionsConfig()
    simulated = mode is Mode.PAPER
    applied: list[str] = []
    pause = evaluate_sleeve(
        sleeve=sleeve, today=today, history=_history(store, sleeve, today, simulated),
        today_marked_inr=_open_marked_inr(store, sleeve=sleeve, simulated=simulated),
        r_today_inr=r_today_inr if r_today_inr > 0 else Decimal(1),
        trading_days=_trading_days(store, today), config=cfg.risk,
    )  # fmt: skip
    if pause is not None:
        _write_pause(store, group_of(sleeve).value, pause)
        applied += [r.value for r in pause.reasons]
        if pause.close_now:
            _close_open(store, sleeve=sleeve, code=pause.reasons[0].value, now=now)
    capitals = [
        _d(r["sleeve_capital_inr"])
        for r in store.conn.execute(
            f"SELECT sleeve_capital_inr FROM {store.t('op_sleeve_config')} WHERE user_id = ?",
            (store.user_id,),
        ).fetchall()
    ]
    book = store.conn.execute(
        f"SELECT daily_loss_limit_inr, monthly_pause_inr FROM {store.t('op_book_config')} "
        "WHERE user_id = ?",
        (store.user_id,),
    ).fetchone()
    limits = book_limits(
        sleeve_capitals_inr=capitals,
        daily_loss_limit_inr=_d(book["daily_loss_limit_inr"]) if book else Decimal(0),
        monthly_pause_inr=_d(book["monthly_pause_inr"]) if book else Decimal(0),
        config=cfg.risk, ceilings=ceilings or OptionsCeilings(),
    )  # fmt: skip
    if limits is None:
        return applied
    sums = store.conn.execute(
        f"SELECT COALESCE(SUM(CASE WHEN trade_date = ? THEN net_pnl_inr END), 0) AS today, "
        f"COALESCE(SUM(net_pnl_inr), 0) AS month FROM {store.t('op_journal')} "
        "WHERE user_id = ? AND simulated = ? AND trade_date BETWEEN ? AND ?",
        (today, store.user_id, simulated, today.replace(day=1), today),
    ).fetchone()
    book_pause = evaluate_book(
        today=today,
        today_net_inr=_d(sums["today"]) + _open_marked_inr(store, sleeve=None, simulated=simulated),
        month_realised_inr=_d(sums["month"]),
        limits=limits,
    )
    if book_pause is not None:
        _write_pause(store, "BOOK", book_pause)
        applied += [r.value for r in book_pause.reasons]
        if book_pause.close_now:
            _close_open(store, sleeve=None, code=book_pause.reasons[0].value, now=now)
    return applied


def after_close(store: Any, session_id: int, *, sleeve: Sleeve, reason: str, now: dt.datetime,  # noqa: ANN401
                mode: Mode) -> list[str]:  # fmt: skip
    """The close's record, then the ledger (`06` OP11: "`risk.Ledger` at every close")."""
    figures = write_journal(store, session_id, reason=reason)
    r_today = figures.r_inr if figures is not None else Decimal(0)
    return apply_ledger(store, sleeve=sleeve, today=now.astimezone(IST).date(), now=now,
                        r_today_inr=r_today, mode=mode)  # fmt: skip


def morning_ledger(store: Any, *, now: dt.datetime, mode_of: Any) -> dict[str, list[str]]:  # noqa: ANN401
    """09:00: every sleeve's rules before the open (no position is open yet, so `r_today` is only a
    divisor for a zero)."""
    today = now.astimezone(IST).date()
    return {
        sleeve.value: apply_ledger(store, sleeve=sleeve, today=today, now=now,
                                   r_today_inr=Decimal(1), mode=mode_of(sleeve))  # fmt: skip
        for sleeve in Sleeve
    }
