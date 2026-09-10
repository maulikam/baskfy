"""The volume-breakout sleeve's operator page and its confirm route (``docs/vbt/05`` §3).

Three panels — the plan, the working orders, the book — and one button per line. This module owns
the route and the Postgres store; ``app/vbt_execute.py`` owns what happens after the click.

**It does not poll.** The swing page refreshes every five seconds because its triggers arrive
inside a session; this sleeve's plan is built twice a day and nothing about it fires while the
market is open, so the page refreshes on demand and says when it was built. A five-second poll
here would be motion without information.

Everything on it is read through :class:`PgVbtStore`, which speaks the ``VbtStore`` protocol so
the same rows drive the page and the confirm. A desk whose database has no ``vb_`` tables — a
sqlite desk, a box that has not run ``0037`` — gets a page that says so rather than a 500.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import decimal
import logging
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any, Final

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import config as C

log = logging.getLogger("vbt.desk")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SCHEMA: Final = "public"

router = APIRouter()

#: `02` §3.1's gate, shown on the status bar as "7 of 20".
DRY_RUN_SESSIONS_REQUIRED: Final = 20

#: The order `04` §9.3 renders a plan in: risk coming off before risk going on.
KIND_ORDER: Final[tuple[str, ...]] = (
    "SELL_AT_OPEN",
    "CANCEL_LIMIT",
    "ARM_GTT",
    "PLACE_LIMIT",
)

KIND_LABEL: Final[dict[str, str]] = {
    "PLACE_LIMIT": "VBT BUY LIMIT",
    "SELL_AT_OPEN": "VBT SELL AT OPEN",
    "CANCEL_LIMIT": "VBT CANCEL",
    "ARM_GTT": "VBT ARM STOP",
}


def _in_transaction(conn: Any) -> bool:  # noqa: ANN401 - a DB-API connection
    """Whether sqlite already has a write transaction open. ``True`` for anything else."""
    return bool(getattr(conn, "in_transaction", True))


def _dec(value: Any) -> Decimal | None:  # noqa: ANN401 - a driver value
    if value is None:
        return None
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (decimal.InvalidOperation, ValueError):
        return None


def _date(value: Any) -> dt.date | None:  # noqa: ANN401 - a driver value
    if value is None or isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    return value.date() if isinstance(value, dt.datetime) else dt.date.fromisoformat(str(value))


def _stamp(value: Any) -> dt.datetime | None:  # noqa: ANN401 - a driver value
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=IST)
    return dt.datetime.fromisoformat(str(value)).replace(tzinfo=IST)


def _jsonable(value: Any) -> Any:  # noqa: ANN401 - the whole point is "any value"
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(inner) for key, inner in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(inner) for inner in value]
    return value


class PgVbtStore:
    """One user's ``vb_`` rows, through a sqlite3-shaped connection (the ``VbtStore`` protocol).

    ``schema="public"`` on the desk's Postgres, ``""`` for a sqlite file in a test. Beyond the
    protocol it carries the reads the page needs (``todays_plan``, ``working``, ``book``,
    ``session``, ``config``); those are this module's, and ``app.vbt_execute`` must not depend
    on them.
    """

    def __init__(
        self,
        conn: Any,  # noqa: ANN401 - a DB-API connection
        user_id: int,
        schema: str = SCHEMA,
        *,
        broker_account_id: int | None = None,
    ) -> None:
        self.conn = conn
        self.user_id = int(user_id)
        self.schema = schema
        self.broker_account_id = broker_account_id

    def t(self, table: str) -> str:
        return f"{self.schema}.{table}" if self.schema else table

    # -- VbtStore: plans and lines ---------------------------------------------------------

    def plan(self, plan_id: str) -> dict | None:
        """``None`` for an unknown plan — **including a malformed id**: ``vb_plan.plan_id`` is a
        uuid column on Postgres, and a form post that is not one would otherwise be a driver
        error (a 500) where the contract says 404."""
        try:
            uuid.UUID(str(plan_id))
        except ValueError:
            return None
        row = self.conn.execute(
            f"SELECT id, plan_id, session_date, source, built_at, expires_at, gate, "
            f"sleeve_equity_inr, total_new_exposure_inr FROM {self.t('vb_plan')} "
            "WHERE user_id = ? AND plan_id = ?",
            (self.user_id, str(plan_id)),
        ).fetchone()
        return None if row is None else self._plan_row(row)

    def _plan_row(self, row: Any) -> dict:  # noqa: ANN401 - a driver row
        return {
            "id": int(row["id"]),
            "plan_id": str(row["plan_id"]),
            "session_date": _date(row["session_date"]),
            "source": str(row["source"]),
            "built_at": _stamp(row["built_at"]),
            "expires_at": _stamp(row["expires_at"]),
            "gate": str(row["gate"]),
            "sleeve_equity_inr": _dec(row["sleeve_equity_inr"]),
            "total_new_exposure_inr": _dec(row["total_new_exposure_inr"]),
        }

    _LINE_SELECT = (
        "SELECT l.id, l.plan_id AS plan_pk, p.plan_id, l.kind, l.instrument_id, i.symbol, "
        "l.quantity, l.limit_price, l.stop_price, l.value_inr, l.size_cap, l.reason, l.note, "
        "l.state, l.client_id, l.journal_ref, l.order_id, l.position_id, "
        "p.session_date, p.source AS plan_source, p.expires_at "
    )

    def _line_from(self) -> str:
        return (
            f"FROM {self.t('vb_plan_line')} l "
            f"JOIN {self.t('vb_plan')} p ON p.id = l.plan_id "
            f"JOIN {self.t('instrument')} i ON i.id = l.instrument_id "
        )

    def line(self, line_id: int) -> dict | None:
        row = self.conn.execute(
            self._LINE_SELECT + self._line_from() + "WHERE l.user_id = ? AND l.id = ?",
            (self.user_id, int(line_id)),
        ).fetchone()
        return None if row is None else self._line_row(row)

    def _line_row(self, row: Any) -> dict:  # noqa: ANN401 - a driver row
        return {
            "id": int(row["id"]),
            "plan_pk": int(row["plan_pk"]),
            "plan_id": str(row["plan_id"]),
            "kind": str(row["kind"]),
            "instrument_id": int(row["instrument_id"]),
            "symbol": str(row["symbol"]),
            "quantity": int(row["quantity"] or 0),
            "limit_price": _dec(row["limit_price"]),
            "stop_price": _dec(row["stop_price"]),
            "value_inr": _dec(row["value_inr"]),
            "size_cap": row["size_cap"],
            "reason": row["reason"],
            "note": row["note"],
            "state": str(row["state"]),
            "client_id": row["client_id"],
            "journal_ref": row["journal_ref"],
            "order_id": row["order_id"],
            "position_id": row["position_id"],
            "session_date": _date(row["session_date"]),
            "plan_source": str(row["plan_source"]),
            "expires_at": _stamp(row["expires_at"]),
        }

    def set_line(
        self,
        line_id: int,
        *,
        state: str,
        journal_ref: str | None = None,
        order_id: int | None = None,
        position_id: int | None = None,
        note: str | None = None,
    ) -> None:
        sets = ["state = ?"]
        values: list[Any] = [state]
        for column, value in (
            ("journal_ref", journal_ref),
            ("order_id", order_id),
            ("position_id", position_id),
            ("note", note),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                values.append(value)
        values += [self.user_id, int(line_id)]
        self.conn.execute(
            f"UPDATE {self.t('vb_plan_line')} SET {', '.join(sets)} "
            "WHERE user_id = ? AND id = ?",
            tuple(values),
        )

    # -- VbtStore: the book -----------------------------------------------------------------

    _POSITION_SELECT = (
        "SELECT p.id, p.instrument_id, i.symbol, p.entry_date, p.entry_avg, "
        "p.quantity_entered, p.quantity_open, p.initial_stop, p.stop_price, p.gtt_id, "
        "p.gtt_trigger, p.gtt_armed_at, p.state, p.exit_queued_for, p.exit_reason_queued, "
        "p.closed_on, p.exit_avg, p.close_reason, p.pnl_inr, p.return_pct, p.simulated "
    )

    def _position_from(self) -> str:
        return (
            f"FROM {self.t('vb_position')} p "
            f"JOIN {self.t('instrument')} i ON i.id = p.instrument_id "
        )

    def _position_row(self, row: Any) -> dict:  # noqa: ANN401 - a driver row
        return {
            "id": int(row["id"]),
            "instrument_id": int(row["instrument_id"]),
            "symbol": str(row["symbol"]),
            "entry_date": _date(row["entry_date"]),
            "entry_avg": _dec(row["entry_avg"]),
            "quantity_entered": int(row["quantity_entered"]),
            "quantity_open": int(row["quantity_open"]),
            "initial_stop": _dec(row["initial_stop"]),
            "stop_price": _dec(row["stop_price"]),
            "gtt_id": row["gtt_id"],
            "gtt_trigger": _dec(row["gtt_trigger"]),
            "gtt_armed_at": _stamp(row["gtt_armed_at"]),
            "state": str(row["state"]),
            "exit_queued_for": _date(row["exit_queued_for"]),
            "exit_reason_queued": row["exit_reason_queued"],
            "closed_on": _date(row["closed_on"]),
            "exit_avg": _dec(row["exit_avg"]),
            "close_reason": row["close_reason"],
            "pnl_inr": _dec(row["pnl_inr"]),
            "return_pct": _dec(row["return_pct"]),
            "simulated": bool(row["simulated"]),
        }

    def open_position_for(self, instrument_id: int) -> dict | None:
        row = self.conn.execute(
            self._POSITION_SELECT
            + self._position_from()
            + "WHERE p.user_id = ? AND p.instrument_id = ? AND p.state = 'OPEN' "
            "AND p.quantity_open > 0 ORDER BY p.id LIMIT 1",
            (self.user_id, int(instrument_id)),
        ).fetchone()
        return None if row is None else self._position_row(row)

    def position(self, position_id: int) -> dict | None:
        row = self.conn.execute(
            self._POSITION_SELECT + self._position_from() + "WHERE p.user_id = ? AND p.id = ?",
            (self.user_id, int(position_id)),
        ).fetchone()
        return None if row is None else self._position_row(row)

    def create_position(self, fields: dict) -> int:
        columns = ["user_id", "broker_account_id", *fields]
        values = [self.user_id, self.broker_account_id, *fields.values()]
        marks = ", ".join("?" for _ in columns)
        row = self.conn.execute(
            f"INSERT INTO {self.t('vb_position')} ({', '.join(columns)}) VALUES ({marks}) "
            "RETURNING id",
            tuple(values),
        ).fetchone()
        return int(row["id"])

    def update_position(self, position_id: int, fields: dict) -> None:
        sets = ", ".join(f"{name} = ?" for name in fields)
        self.conn.execute(
            f"UPDATE {self.t('vb_position')} SET {sets} WHERE user_id = ? AND id = ?",
            (*fields.values(), self.user_id, int(position_id)),
        )

    def add_fill(self, fields: dict) -> int:
        columns = ["user_id", *fields]
        values = [self.user_id, *fields.values()]
        marks = ", ".join("?" for _ in columns)
        row = self.conn.execute(
            f"INSERT INTO {self.t('vb_fill')} ({', '.join(columns)}) VALUES ({marks}) "
            "RETURNING id",
            tuple(values),
        ).fetchone()
        return int(row["id"])

    # -- VbtStore: the working orders --------------------------------------------------------

    _ORDER_SELECT = (
        "SELECT o.id, o.instrument_id, i.symbol, o.signal_date, o.limit_price, o.stop_price, "
        "o.quantity, o.state, o.working_from, o.expires_after_session, o.sessions_worked, "
        "o.broker_order_id, o.client_id, o.filled_quantity, o.avg_fill_price, o.position_id, "
        "o.cancelled_on, o.cancel_reason, o.simulated "
    )

    def _order_from(self) -> str:
        return (
            f"FROM {self.t('vb_order')} o "
            f"JOIN {self.t('instrument')} i ON i.id = o.instrument_id "
        )

    def _order_row(self, row: Any) -> dict:  # noqa: ANN401 - a driver row
        return {
            "id": int(row["id"]),
            "instrument_id": int(row["instrument_id"]),
            "symbol": str(row["symbol"]),
            "signal_date": _date(row["signal_date"]),
            "limit_price": _dec(row["limit_price"]),
            "stop_price": _dec(row["stop_price"]),
            "quantity": int(row["quantity"]),
            "state": str(row["state"]),
            "working_from": _date(row["working_from"]),
            "expires_after_session": _date(row["expires_after_session"]),
            "sessions_worked": int(row["sessions_worked"] or 0),
            "broker_order_id": row["broker_order_id"],
            "client_id": row["client_id"],
            "filled_quantity": int(row["filled_quantity"] or 0),
            "avg_fill_price": _dec(row["avg_fill_price"]),
            "position_id": row["position_id"],
            "cancelled_on": _date(row["cancelled_on"]),
            "cancel_reason": row["cancel_reason"],
            "simulated": bool(row["simulated"]),
        }

    def order(self, order_id: int) -> dict | None:
        row = self.conn.execute(
            self._ORDER_SELECT + self._order_from() + "WHERE o.user_id = ? AND o.id = ?",
            (self.user_id, int(order_id)),
        ).fetchone()
        return None if row is None else self._order_row(row)

    def create_order(self, fields: dict) -> int:
        columns = ["user_id", "broker_account_id", *fields]
        values = [self.user_id, self.broker_account_id, *fields.values()]
        marks = ", ".join("?" for _ in columns)
        row = self.conn.execute(
            f"INSERT INTO {self.t('vb_order')} ({', '.join(columns)}) VALUES ({marks}) "
            "RETURNING id",
            tuple(values),
        ).fetchone()
        return int(row["id"])

    def update_order(self, order_id: int, fields: dict) -> None:
        sets = ", ".join(f"{name} = ?" for name in fields)
        self.conn.execute(
            f"UPDATE {self.t('vb_order')} SET {sets} WHERE user_id = ? AND id = ?",
            (*fields.values(), self.user_id, int(order_id)),
        )

    def working_order_for(self, instrument_id: int) -> dict | None:
        row = self.conn.execute(
            self._ORDER_SELECT
            + self._order_from()
            + "WHERE o.user_id = ? AND o.instrument_id = ? AND o.state IN "
            "('PROPOSED', 'CONFIRMED', 'SENT', 'PARTIAL') ORDER BY o.id LIMIT 1",
            (self.user_id, int(instrument_id)),
        ).fetchone()
        return None if row is None else self._order_row(row)

    # -- VbtStore: the session ---------------------------------------------------------------

    def bump_session(
        self, day: dt.date, *, mode: str, confirms: int = 0, fills: int = 0, exits: int = 0
    ) -> None:
        self.conn.execute(
            f"INSERT INTO {self.t('vb_session')} "
            "(user_id, session_date, mode, confirms, fills, exits) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, session_date) DO UPDATE SET mode = EXCLUDED.mode, "
            "confirms = vb_session.confirms + EXCLUDED.confirms, "
            "fills = vb_session.fills + EXCLUDED.fills, "
            "exits = vb_session.exits + EXCLUDED.exits",
            (self.user_id, day, mode, confirms, fills, exits),
        )

    @contextlib.contextmanager
    def lock_session_for_update(self, day: dt.date) -> Iterator[None]:
        """`04` §9.4 — the day's row locked for the whole confirm, so two tabs cannot pass a cap.

        The row is inserted first if absent: ``SELECT … FOR UPDATE`` locks nothing when there is
        nothing to lock, which is precisely the first confirm of a session.
        """
        self.conn.execute(
            f"INSERT INTO {self.t('vb_session')} (user_id, session_date) VALUES (?, ?) "
            "ON CONFLICT (user_id, session_date) DO NOTHING",
            (self.user_id, day),
        )
        if self.schema:
            self.conn.execute(
                f"SELECT 1 FROM {self.t('vb_session')} WHERE user_id = ? AND session_date = ? "
                "FOR UPDATE",
                (self.user_id, day),
            )
        else:
            # sqlite has no row locks and no `FOR UPDATE`. A test file has one writer, so the
            # lock has nothing to protect against; saying so here is better than a statement
            # that would be a syntax error on the backend the tests actually run on.
            self.conn.execute("BEGIN IMMEDIATE") if _in_transaction(self.conn) is False else None
        yield

    def entries_taken(self, day: dt.date) -> int:
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM {self.t('vb_order')} WHERE user_id = ? "
            "AND signal_date = ? AND state IN ('CONFIRMED', 'SENT', 'FILLED', 'PARTIAL')",
            (self.user_id, day),
        ).fetchone()
        return int(row["n"] or 0)

    # -- the page's own reads ----------------------------------------------------------------

    def todays_plan(self, on_or_before: dt.date) -> dict | None:
        """The newest plan at or before the date — the MORNING rebuild wins over the EVENING."""
        row = self.conn.execute(
            f"SELECT id, plan_id, session_date, source, built_at, expires_at, gate, "
            f"sleeve_equity_inr, total_new_exposure_inr FROM {self.t('vb_plan')} "
            "WHERE user_id = ? AND session_date <= ? ORDER BY session_date DESC, built_at DESC "
            "LIMIT 1",
            (self.user_id, on_or_before),
        ).fetchone()
        return None if row is None else self._plan_row(row)

    def lines_for(self, plan_pk: int) -> list[dict]:
        rows = self.conn.execute(
            self._LINE_SELECT + self._line_from() + "WHERE l.user_id = ? AND l.plan_id = ?",
            (self.user_id, int(plan_pk)),
        ).fetchall()
        lines = [self._line_row(row) for row in rows]
        return sorted(lines, key=lambda line: (KIND_ORDER.index(line["kind"]), line["symbol"]))

    def skips_for(self, plan_pk: int) -> list[dict]:
        rows = self.conn.execute(
            f"SELECT s.reason, s.detail, i.symbol FROM {self.t('vb_plan_skip')} s "
            f"JOIN {self.t('instrument')} i ON i.id = s.instrument_id "
            "WHERE s.user_id = ? AND s.plan_id = ? ORDER BY i.symbol",
            (self.user_id, int(plan_pk)),
        ).fetchall()
        return [
            {"reason": str(row["reason"]), "detail": row["detail"], "symbol": str(row["symbol"])}
            for row in rows
        ]

    def working(self) -> list[dict]:
        rows = self.conn.execute(
            self._ORDER_SELECT
            + self._order_from()
            + "WHERE o.user_id = ? AND o.state IN ('PROPOSED', 'CONFIRMED', 'SENT', 'PARTIAL') "
            "ORDER BY o.signal_date, i.symbol",
            (self.user_id,),
        ).fetchall()
        return [self._order_row(row) for row in rows]

    def book(self) -> list[dict]:
        rows = self.conn.execute(
            self._POSITION_SELECT
            + self._position_from()
            + "WHERE p.user_id = ? AND p.state = 'OPEN' AND p.quantity_open > 0 "
            "ORDER BY p.entry_date, i.symbol",
            (self.user_id,),
        ).fetchall()
        return [self._position_row(row) for row in rows]

    def session(self, day: dt.date) -> dict | None:
        row = self.conn.execute(
            f"SELECT session_date, mode, gate, signals, confirms, fills, exits "
            f"FROM {self.t('vb_session')} WHERE user_id = ? AND session_date = ?",
            (self.user_id, day),
        ).fetchone()
        if row is None:
            return None
        return {
            "session_date": _date(row["session_date"]),
            "mode": str(row["mode"]),
            "gate": str(row["gate"]),
            "signals": int(row["signals"] or 0),
            "confirms": int(row["confirms"] or 0),
            "fills": int(row["fills"] or 0),
            "exits": int(row["exits"] or 0),
        }

    def config(self) -> dict:
        row = self.conn.execute(
            f"SELECT sleeve_capital_inr, max_open_positions, max_position_pct, stop_pct, "
            f"dry_run_sessions, first_live_sessions_left FROM {self.t('vb_config')} "
            "WHERE user_id = ?",
            (self.user_id,),
        ).fetchone()
        if row is None:
            return {"sleeve_capital_inr": Decimal(0), "dry_run_sessions": 0}
        return {
            "sleeve_capital_inr": _dec(row["sleeve_capital_inr"]),
            "max_open_positions": int(row["max_open_positions"]),
            "max_position_pct": _dec(row["max_position_pct"]),
            "stop_pct": _dec(row["stop_pct"]),
            "dry_run_sessions": int(row["dry_run_sessions"] or 0),
            "first_live_sessions_left": int(row["first_live_sessions_left"] or 0),
        }

    def breadth(self, on_or_before: dt.date) -> dict | None:
        row = self.conn.execute(
            f"SELECT date, measured_count, above_count, pct_above_dma, gate, thin_session "
            f"FROM {self.t('vb_breadth_daily')} WHERE user_id = ? AND date <= ? "
            "ORDER BY date DESC LIMIT 1",
            (self.user_id, on_or_before),
        ).fetchone()
        if row is None:
            return None
        return {
            "date": _date(row["date"]),
            "measured_count": int(row["measured_count"] or 0),
            "above_count": int(row["above_count"] or 0),
            "pct_above_dma": _dec(row["pct_above_dma"]),
            "gate": str(row["gate"]),
            "thin_session": bool(row["thin_session"]),
        }


# --- the view ---------------------------------------------------------------------------------


def _countdown(expires_at: dt.datetime | None, now: dt.datetime) -> str:
    """How long the plan has left, in words. A plan a person cannot confirm should say so."""
    if expires_at is None:
        return "no expiry"
    left = int((expires_at - now).total_seconds())
    if left <= 0:
        return "expired — rebuild the plan"
    return f"{left // 60}m {left % 60}s left"


def _price(value: Decimal | None) -> str:
    return "—" if value is None else f"₹{value:,.2f}"


def line_view(line: dict, *, now: dt.datetime) -> dict:
    """One confirmable row, as the template renders it.

    A line is confirmable when it is still ``PROPOSED`` **and** its plan has not expired. Both,
    because a page left open for an hour must not offer a button that can only 410.
    """
    expired = line["expires_at"] is not None and now > line["expires_at"]
    return {
        **line,
        "label": KIND_LABEL.get(line["kind"], line["kind"]),
        "confirmable": line["state"] == "PROPOSED" and not expired,
        "expired": expired,
        "limit_text": _price(line["limit_price"]),
        "stop_text": _price(line["stop_price"]),
        "value_text": _price(line["value_inr"]),
    }


def working_view(order: dict, *, valid_sessions: int) -> dict:
    """One resting limit, and whether tonight is its last (``04`` §7.2)."""
    worked = int(order["sessions_worked"])
    return {
        **order,
        "sessions_text": f"{worked} of {valid_sessions}",
        "expires_tonight": worked >= valid_sessions - 1,
        "limit_text": _price(order["limit_price"]),
        "remaining": order["quantity"] - order["filled_quantity"],
    }


def position_view(position: dict) -> dict:
    """One holding, and the one state the method forbids in red."""
    return {
        **position,
        "naked": position["gtt_id"] is None,
        "entry_text": _price(position["entry_avg"]),
        "stop_text": _price(position["stop_price"]),
        "sells_tomorrow": position["exit_queued_for"] is not None,
    }


def build_view(store: PgVbtStore, *, now: dt.datetime) -> dict:
    """Everything ``vbt.html`` needs, in one pass over the store."""
    from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG

    today = now.astimezone(IST).date()
    plan = store.todays_plan(today)
    lines = store.lines_for(plan["id"]) if plan else []
    skips = store.skips_for(plan["id"]) if plan else []
    config = store.config()
    breadth = store.breadth(today)
    session = store.session(plan["session_date"]) if plan else None
    gates = _gates()
    return {
        "available": True,
        "now": now,
        "today": today,
        "plan": plan,
        "countdown": _countdown(plan["expires_at"] if plan else None, now),
        "lines": [line_view(line, now=now) for line in lines],
        "skips": skips,
        "working": [
            working_view(order, valid_sessions=DEFAULT_VBT_CONFIG.entry.valid_sessions)
            for order in store.working()
        ],
        "book": [position_view(position) for position in store.book()],
        "breadth": breadth,
        "config": config,
        "session": session,
        "dry_run": gates["dry_run"],
        "execution_enabled": gates["execution_enabled"],
        "desk_dry_run": gates["desk_dry_run"],
        "dry_run_sessions": config.get("dry_run_sessions", 0),
        "dry_run_sessions_required": DRY_RUN_SESSIONS_REQUIRED,
        "reason": "",
    }


def _gates() -> dict[str, bool]:
    from . import vbt_execute  # noqa: PLC0415 - the sibling module, resolved at call time

    gates = vbt_execute.vbt_gates()
    return {
        "dry_run": bool(gates.dry_run),
        "execution_enabled": bool(C.VBT_EXECUTION_ENABLED),
        "desk_dry_run": bool(C.DRY_RUN),
    }


def unavailable_view(reason: str, *, now: dt.datetime) -> dict:
    """A page that says why it is empty. A desk with no ``vb_`` tables is a state, not a 500."""
    gates = _gates()
    return {
        "available": False,
        "now": now,
        "today": now.astimezone(IST).date(),
        "plan": None,
        "countdown": "",
        "lines": [],
        "skips": [],
        "working": [],
        "book": [],
        "breadth": None,
        "config": {},
        "session": None,
        "dry_run": gates["dry_run"],
        "execution_enabled": gates["execution_enabled"],
        "desk_dry_run": gates["desk_dry_run"],
        "dry_run_sessions": 0,
        "dry_run_sessions_required": DRY_RUN_SESSIONS_REQUIRED,
        "reason": reason,
    }


# --- wiring -----------------------------------------------------------------------------------


@contextlib.contextmanager
def open_store() -> Iterator[PgVbtStore]:
    """The sole user's store over the desk's connection. Tests replace this with their own."""
    from .analytics import db as _db  # noqa: PLC0415 - the desk imports its DB lazily

    with _db.connect() as conn:
        schema = SCHEMA if _db.DB_BACKEND == "postgres" else ""
        yield PgVbtStore(
            conn,
            user_id=C.SOLE_USER_ID,
            schema=schema,
            broker_account_id=C.SOLE_BROKER_ACCOUNT_ID,
        )


_vbt_gateway: Any = None


def vbt_gateway() -> Any:  # noqa: ANN401 - an OrderGateway, built by the execute module
    """This sleeve's gateway, built once and lazily — never at import, because building it needs
    a Kite client and a page must import without one.

    It shares ``app.main``'s risk manager: one account, one daily-loss cap, one order counter, so
    this sleeve cannot spend a limit the weekly book has already used.
    """
    global _vbt_gateway
    if _vbt_gateway is None:
        from . import main as _main  # noqa: PLC0415 - main mounts this router; at call time
        from . import vbt_execute  # noqa: PLC0415 - the sibling module, at call time

        _main.gateway()  # builds `_risk` as a side effect
        _vbt_gateway = vbt_execute.build_vbt_gateway(_main.kite().kc, _main._risk)
    return _vbt_gateway


def last_price(symbol: str) -> Decimal | None:
    """The instrument's last traded price, or ``None`` when the desk has no session.

    A read, not an order. A ``SELL_AT_OPEN`` uses it as the risk layer's reference; without one
    the sell still goes, valued at the entry, because refusing to exit for want of a quote is the
    failure the exit rule exists to prevent.
    """
    try:
        from . import main as _main  # noqa: PLC0415 - main mounts this router; at call time

        value = _main.kite().ltp([symbol]).get(symbol)
    except Exception as exc:  # noqa: BLE001 - no session, no price; the confirm says so
        log.warning("no last price for %s: %s", symbol, exc)
        return None
    return Decimal(str(value)) if value else None


def _templates() -> Any:  # noqa: ANN401 - Jinja2Templates, owned by app.main
    from . import main as _main  # noqa: PLC0415 - one environment, with main's globals

    return _main.templates


def _now() -> dt.datetime:
    return dt.datetime.now(IST)


def _view() -> dict:
    now = _now()
    try:
        with open_store() as store:
            return build_view(store, now=now)
    except Exception as exc:  # noqa: BLE001 - the page reports the failure, it does not 500
        log.warning("vbt view unavailable: %s", exc)
        return unavailable_view(str(exc)[:200], now=now)


# --- routes -----------------------------------------------------------------------------------


@router.get("/vbt", response_class=HTMLResponse)
def vbt_page(request: Request) -> Any:  # noqa: ANN401 - a TemplateResponse
    return _templates().TemplateResponse(request, "vbt.html", {"v": _view()})


@router.get("/vbt/data")
def vbt_data() -> Any:  # noqa: ANN401 - a JSON-able dict
    return _jsonable(_view())


@router.post("/vbt/execute")
async def vbt_execute_line(
    plan_id: str = Form(...), line_id: int = Form(...), confirm: str = Form(...)
) -> Any:  # noqa: ANN401 - a JSON-able dict
    """One line, one click. There is no route that takes more than one line, and no route that
    takes none — ``docs/vbt/02`` Track C §3."""
    from . import vbt_execute as _execute  # noqa: PLC0415 - the sibling module, at call time

    with open_store() as store:
        line = store.line(line_id)
        price = (
            last_price(line["symbol"])
            if line is not None and line["kind"] == "SELL_AT_OPEN"
            else None
        )
        outcome = await _execute.execute_line(
            store,
            vbt_gateway(),
            plan_id=plan_id,
            line_id=line_id,
            confirm=confirm,
            now=_now(),
            last_price=price,
        )
    return _jsonable({**dataclasses.asdict(outcome), "line_id": line_id})
