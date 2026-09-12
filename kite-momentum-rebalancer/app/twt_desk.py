"""TW6 — the three-weeks-tight sleeve's operator page and the six routes the runbook needs.

Four panels in ``docs/twt/05`` §2's order — the session strip, **the exits**, the entries with
their skips, and the book with a Re-arm button per naked line — and one Confirm button per line.
This module owns the routes and the Postgres store; ``app/twt_execute.py`` owns what happens
after the click.

**Exits are first and that is an argument, not a layout.** A morning that runs out of attention
should have armed the stops: what protects a position that has none, then what tightens a stop
that has one, and only then what commits new money. The swing desk puts them first for the same
reason.

**It does not poll.** This sleeve's plan is built twice a day and nothing about it fires while
the market is open (``02`` Track C §3), so the page refreshes on demand and says when it was
built. A five-second poll would be motion without information.

THE SIX ROUTES, AND WHY THEY EXIST
----------------------------------
``FIRST-LIVE-MORNING.md`` was written **before** this code, deliberately, and writing the first
live morning down turned up six commands a person needs that no module was planned to build:

* ``POST /twt/execute`` — one line, one click. ``04`` §10.4.
* ``POST /twt/halt`` — **the stop-the-sleeve command**, and the most important route in this
  file. It zeroes the capital, expires every live plan, and **never touches protection**.
* ``POST /twt/rearm`` — ``05`` §2's per-line Re-arm button, which named no route.
* ``POST /twt/sweep`` — the 15:15 chore, so it is checkable from a phone.
* ``POST /twt/reconcile`` — attach a hand-armed GTT to a position. Without it a line Maulik
  protects by hand reads naked forever, and the sweep keeps shouting about a covered line.
* ``GET /twt`` and ``GET /twt/data`` — the page and its JSON.

**Every POST here needs an ``Origin`` header** (``app/core/websec.py::DeskSecurity``). A browser
sends one; ``curl`` does not unless told, and the 403 reads exactly like a permissions problem.
It is not: it is the defence that stops a form on another site posting to this desk.

Everything on the page is read through :class:`PgTwtStore`, which speaks the ``TwtStore``
protocol so the same rows drive the page and the confirm. A desk whose database has no ``tw_``
tables — a sqlite desk, a box that has not run ``0041`` — gets a page that says so rather than a
500.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import decimal
import json
import logging
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any, Final

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from baskfy_core.twt.config import DEFAULT_TWT_CONFIG
from baskfy_core.twt.plan import LINE_ORDER

from . import config as C
from .core.guards import UntouchableInstrumentError

log = logging.getLogger("twt.desk")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SCHEMA: Final = "public"

router = APIRouter()

#: ``05`` §2's order down the page, and ``04`` §10.3's order in the plan: risk coming off before
#: risk going on. Imported from the engine rather than retyped — the page and the planner
#: disagreeing about which line comes first is exactly the bug this ordering exists to prevent.
KIND_ORDER: Final[tuple[str, ...]] = tuple(kind.value for kind in LINE_ORDER)

KIND_LABEL: Final[dict[str, str]] = {
    "BUY_AT_OPEN": "TWT BUY AT OPEN",
    "ARM_GTT": "TWT ARM STOP",
    "RAISE_GTT_STOP": "TWT RAISE STOP",
    "SELL_AT_OPEN": "TWT SELL AT OPEN",
}

#: ``04`` §10.4 — restated on the page so an expired plan explains itself.
PLAN_TTL_MINUTES: Final = 30

#: The 15:15 strip (``05`` §2). Before this the page shows the book; after it, a red band naming
#: every open line without a resting GTT, and it stays until the sweep is clean.
SWEEP_AT: Final = dt.time(15, 15)

OPEN_STATE: Final = "OPEN"
CLOSED_STATE: Final = "CLOSED"
ENTERED_ORDER_STATES: Final[tuple[str, ...]] = ("CONFIRMED", "SENT", "PARTIAL", "FILLED")

#: TW12's four states, and they are the swing book's and VBT-1's, in that order.
SCAN_IN_FLIGHT: Final[tuple[str, ...]] = ("QUEUED", "RUNNING")
#: TW12's two refusal windows, mirrored from ``baskfy_worker.tasks.twt_scan`` — the desk cannot
#: import the worker (different venv, no Celery), so the numbers are copied and
#: ``tests/test_twt_scan_desk.py`` asserts the two copies agree. VB12 made the same copy for the
#: same reason; a third sleeve inventing its own numbers would be three answers to one question.
SCAN_STALE_AFTER_SECONDS: Final = 600
SCAN_MIN_INTERVAL_SECONDS: Final = 60


class ScanRefused(Exception):
    """:meth:`PgTwtStore.request_scan`'s two refusals, carrying the status the route answers with.

    409 while one is in flight, 429 inside the minute — the contract's vocabulary, and the swing
    desk's (`app/swing_desk.py`). VBT-1's ``/vbt/rescan`` answers 200 with ``accepted: false``
    instead; that shape predates the contract and is not copied here, because a caller that has to
    read the body to find out whether it was refused is a caller that will forget to.
    """

    def __init__(self, status: int, reason: str, *, run_id: int, retry_after: int | None = None):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.run_id = run_id
        self.retry_after = retry_after


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
    if value is None or (isinstance(value, dt.date) and not isinstance(value, dt.datetime)):
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


class PgTwtStore:
    """One user's ``tw_`` rows, through a sqlite3-shaped connection (the ``TwtStore`` protocol).

    ``schema="public"`` on the desk's Postgres, ``""`` for a sqlite file in a test. Beyond the
    protocol it carries the reads the page needs (``todays_plan``, ``book``, ``session``,
    ``config``); those are this module's, and ``app.twt_execute`` must not depend on them.
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

    # -- TwtStore: plans and lines -----------------------------------------------------------

    def plan(self, plan_id: str) -> dict | None:
        """``None`` for an unknown plan — **including a malformed id**: ``tw_plan.plan_id`` is a
        uuid column on Postgres, and a form post that is not one would otherwise be a driver
        error (a 500) where the contract says 404."""
        try:
            uuid.UUID(str(plan_id))
        except ValueError:
            return None
        row = self.conn.execute(
            f"SELECT id, plan_id, session_date, source, built_at, expires_at, gate, "
            f"sleeve_equity_inr, total_new_exposure_inr FROM {self.t('tw_plan')} "
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
        "l.quantity, l.stop_price, l.value_inr, l.high_since, l.previous_trigger, l.note, "
        "l.state, l.client_id, l.journal_ref, l.order_id, l.position_id, "
        "p.session_date, p.source AS plan_source, p.expires_at, s.turnover_avg_20 "
    )

    def _line_from(self) -> str:
        """The line, its plan, its symbol — **and the signal row's turnover**.

        The last one is a LEFT JOIN and it is load-bearing: ``04`` §10.5 says the confirm
        re-sizes "through §6 against the same caps", and the 1 %-of-turnover cap is one of them.
        Without this column the re-size would silently omit the cap that binds hardest on this
        book at ₹25 lakh, and would sometimes send *more* than the page showed.
        """
        return (
            f"FROM {self.t('tw_plan_line')} l "
            f"JOIN {self.t('tw_plan')} p ON p.id = l.plan_id "
            f"JOIN {self.t('instrument')} i ON i.id = l.instrument_id "
            f"LEFT JOIN {self.t('tw_signal_daily')} s ON s.user_id = l.user_id "
            "AND s.instrument_id = l.instrument_id AND s.date = p.session_date "
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
            "stop_price": _dec(row["stop_price"]),
            "value_inr": _dec(row["value_inr"]),
            "high_since": _dec(row["high_since"]),
            "previous_trigger": _dec(row["previous_trigger"]),
            "note": row["note"],
            "state": str(row["state"]),
            "client_id": row["client_id"],
            "journal_ref": row["journal_ref"],
            "order_id": row["order_id"],
            "position_id": row["position_id"],
            "session_date": _date(row["session_date"]),
            "plan_source": str(row["plan_source"]),
            "expires_at": _stamp(row["expires_at"]),
            "turnover_avg_inr": _dec(row["turnover_avg_20"]),
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
            f"UPDATE {self.t('tw_plan_line')} SET {', '.join(sets)} WHERE user_id = ? AND id = ?",
            tuple(values),
        )

    # -- TwtStore: the book ------------------------------------------------------------------

    _POSITION_SELECT = (
        "SELECT p.id, p.instrument_id, i.symbol, p.signal_date, p.entry_date, p.entry_avg, "
        "p.entry_adj_factor, p.quantity_entered, p.quantity_open, p.initial_stop, p.stop_price, "
        "p.high_since, p.high_since_date, p.gtt_id, p.gtt_trigger, p.gtt_armed_at, "
        "p.next_trigger, p.next_trigger_for, p.state, p.closed_on, p.exit_avg, p.close_reason, "
        "p.pnl_inr, p.return_pct, p.simulated, p.half_size "
    )

    def _position_from(self) -> str:
        return (
            f"FROM {self.t('tw_position')} p "
            f"JOIN {self.t('instrument')} i ON i.id = p.instrument_id "
        )

    def _position_row(self, row: Any) -> dict:  # noqa: ANN401 - a driver row
        return {
            "id": int(row["id"]),
            "instrument_id": int(row["instrument_id"]),
            "symbol": str(row["symbol"]),
            "signal_date": _date(row["signal_date"]),
            "entry_date": _date(row["entry_date"]),
            "entry_avg": _dec(row["entry_avg"]),
            "entry_adj_factor": _dec(row["entry_adj_factor"]),
            "quantity_entered": int(row["quantity_entered"]),
            "quantity_open": int(row["quantity_open"]),
            "initial_stop": _dec(row["initial_stop"]),
            "stop_price": _dec(row["stop_price"]),
            "high_since": _dec(row["high_since"]),
            "high_since_date": _date(row["high_since_date"]),
            "gtt_id": row["gtt_id"],
            "gtt_trigger": _dec(row["gtt_trigger"]),
            "gtt_armed_at": _stamp(row["gtt_armed_at"]),
            "next_trigger": _dec(row["next_trigger"]),
            "next_trigger_for": _date(row["next_trigger_for"]),
            "state": str(row["state"]),
            "closed_on": _date(row["closed_on"]),
            "exit_avg": _dec(row["exit_avg"]),
            "close_reason": row["close_reason"],
            "pnl_inr": _dec(row["pnl_inr"]),
            "return_pct": _dec(row["return_pct"]),
            "simulated": bool(row["simulated"]),
            "half_size": bool(row["half_size"]),
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

    def open_positions(self) -> list[dict]:
        """**The sleeve's own book, and nothing else** (``02`` Track C §5, ``04`` §9.4).

        A name in the broker's account that is not a ``tw_position`` row is the weekly book's,
        the swing book's, VBT-1's or Maulik's, and this sleeve cannot see it — which is what
        stops it ever selling a holding it did not buy.
        """
        rows = self.conn.execute(
            self._POSITION_SELECT
            + self._position_from()
            + "WHERE p.user_id = ? AND p.state = 'OPEN' AND p.quantity_open > 0 "
            "ORDER BY p.entry_date, i.symbol",
            (self.user_id,),
        ).fetchall()
        return [self._position_row(row) for row in rows]

    def create_position(self, fields: dict) -> int:
        columns = ["user_id", "broker_account_id", *fields]
        values = [self.user_id, self.broker_account_id, *fields.values()]
        marks = ", ".join("?" for _ in columns)
        row = self.conn.execute(
            f"INSERT INTO {self.t('tw_position')} ({', '.join(columns)}) VALUES ({marks}) "
            "RETURNING id",
            tuple(values),
        ).fetchone()
        return int(row["id"])

    def update_position(self, position_id: int, fields: dict) -> None:
        sets = ", ".join(f"{name} = ?" for name in fields)
        self.conn.execute(
            f"UPDATE {self.t('tw_position')} SET {sets} WHERE user_id = ? AND id = ?",
            (*fields.values(), self.user_id, int(position_id)),
        )

    def add_fill(self, fields: dict) -> int:
        columns = ["user_id", *fields]
        values = [self.user_id, *fields.values()]
        marks = ", ".join("?" for _ in columns)
        row = self.conn.execute(
            f"INSERT INTO {self.t('tw_fill')} ({', '.join(columns)}) VALUES ({marks}) "
            "RETURNING id",
            tuple(values),
        ).fetchone()
        return int(row["id"])

    # -- TwtStore: the orders ----------------------------------------------------------------

    _ORDER_SELECT = (
        "SELECT o.id, o.instrument_id, i.symbol, o.signal_date, o.side, o.quantity, "
        "o.stop_price, o.state, o.broker_order_id, o.client_id, o.filled_quantity, "
        "o.avg_fill_price, o.position_id, o.simulated "
    )

    def _order_from(self) -> str:
        return (
            f"FROM {self.t('tw_order')} o "
            f"JOIN {self.t('instrument')} i ON i.id = o.instrument_id "
        )

    def _order_row(self, row: Any) -> dict:  # noqa: ANN401 - a driver row
        return {
            "id": int(row["id"]),
            "instrument_id": int(row["instrument_id"]),
            "symbol": str(row["symbol"]),
            "signal_date": _date(row["signal_date"]),
            "side": str(row["side"]),
            "quantity": int(row["quantity"]),
            "stop_price": _dec(row["stop_price"]),
            "state": str(row["state"]),
            "broker_order_id": row["broker_order_id"],
            "client_id": row["client_id"],
            "filled_quantity": int(row["filled_quantity"] or 0),
            "avg_fill_price": _dec(row["avg_fill_price"]),
            "position_id": row["position_id"],
            "simulated": bool(row["simulated"]),
        }

    def order(self, order_id: int) -> dict | None:
        row = self.conn.execute(
            self._ORDER_SELECT + self._order_from() + "WHERE o.user_id = ? AND o.id = ?",
            (self.user_id, int(order_id)),
        ).fetchone()
        return None if row is None else self._order_row(row)

    def order_by_broker_id(self, broker_order_id: str) -> dict | None:
        row = self.conn.execute(
            self._ORDER_SELECT
            + self._order_from()
            + "WHERE o.user_id = ? AND o.broker_order_id = ? ORDER BY o.id DESC LIMIT 1",
            (self.user_id, str(broker_order_id)),
        ).fetchone()
        return None if row is None else self._order_row(row)

    def create_order(self, fields: dict) -> int:
        columns = ["user_id", "broker_account_id", *fields]
        values = [self.user_id, self.broker_account_id, *fields.values()]
        marks = ", ".join("?" for _ in columns)
        row = self.conn.execute(
            f"INSERT INTO {self.t('tw_order')} ({', '.join(columns)}) VALUES ({marks}) "
            "RETURNING id",
            tuple(values),
        ).fetchone()
        return int(row["id"])

    def update_order(self, order_id: int, fields: dict) -> None:
        sets = ", ".join(f"{name} = ?" for name in fields)
        self.conn.execute(
            f"UPDATE {self.t('tw_order')} SET {sets} WHERE user_id = ? AND id = ?",
            (*fields.values(), self.user_id, int(order_id)),
        )

    def entries_taken(self, day: dt.date) -> int:
        """``04`` §6.3 — **whatever plan they came from**, so a fourth confirm is a refusal."""
        marks = ", ".join("?" for _ in ENTERED_ORDER_STATES)
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM {self.t('tw_order')} WHERE user_id = ? "
            f"AND signal_date = ? AND side = 'BUY' AND state IN ({marks})",
            (self.user_id, day, *ENTERED_ORDER_STATES),
        ).fetchone()
        return int(row["n"] or 0)

    # -- TwtStore: the session ---------------------------------------------------------------

    def bump_session(  # noqa: PLR0913 - the session row is its counters
        self,
        day: dt.date,
        *,
        mode: str,
        confirms: int = 0,
        fills: int = 0,
        ratchets: int = 0,
        exits: int = 0,
    ) -> None:
        self.conn.execute(
            f"INSERT INTO {self.t('tw_session')} "
            "(user_id, session_date, mode, confirms, fills, ratchets, exits) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, session_date) DO UPDATE SET mode = EXCLUDED.mode, "
            "confirms = tw_session.confirms + EXCLUDED.confirms, "
            "fills = tw_session.fills + EXCLUDED.fills, "
            "ratchets = tw_session.ratchets + EXCLUDED.ratchets, "
            "exits = tw_session.exits + EXCLUDED.exits",
            (self.user_id, day, mode, confirms, fills, ratchets, exits),
        )

    def set_naked_count(self, day: dt.date, *, mode: str, naked: int) -> None:
        """``naked_at_1515`` is **set**, not incremented: it is a state, not an event, and the
        sweep is idempotent and keyed on the day (TW7)."""
        self.conn.execute(
            f"INSERT INTO {self.t('tw_session')} (user_id, session_date, mode, naked_at_1515) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (user_id, session_date) DO UPDATE SET "
            "naked_at_1515 = EXCLUDED.naked_at_1515",
            (self.user_id, day, mode, int(naked)),
        )

    @contextlib.contextmanager
    def lock_session_for_update(self, day: dt.date) -> Iterator[None]:
        """``04`` §10.5 — the day's row locked for the whole confirm, so two tabs cannot pass a
        cap between them.

        The row is inserted first if absent: ``SELECT … FOR UPDATE`` locks nothing when there is
        nothing to lock, which is precisely the first confirm of a session.
        """
        self.conn.execute(
            f"INSERT INTO {self.t('tw_session')} (user_id, session_date) VALUES (?, ?) "
            "ON CONFLICT (user_id, session_date) DO NOTHING",
            (self.user_id, day),
        )
        if self.schema:
            self.conn.execute(
                f"SELECT 1 FROM {self.t('tw_session')} WHERE user_id = ? AND session_date = ? "
                "FOR UPDATE",
                (self.user_id, day),
            )
        elif _in_transaction(self.conn) is False:
            # sqlite has no row locks and no `FOR UPDATE`. A test file has one writer, so the
            # lock has nothing to protect against; saying so here is better than a statement
            # that would be a syntax error on the backend the tests actually run on.
            self.conn.execute("BEGIN IMMEDIATE")
        yield

    def session(self, day: dt.date) -> dict | None:
        row = self.conn.execute(
            f"SELECT session_date, mode, gate, states, signals, confirms, fills, ratchets, "
            f"exits, naked_at_1515, first_live_entries_counted FROM {self.t('tw_session')} "
            "WHERE user_id = ? AND session_date = ?",
            (self.user_id, day),
        ).fetchone()
        if row is None:
            return None
        return {
            "session_date": _date(row["session_date"]),
            "mode": str(row["mode"]),
            "gate": str(row["gate"]),
            "states": int(row["states"] or 0),
            "signals": int(row["signals"] or 0),
            "confirms": int(row["confirms"] or 0),
            "fills": int(row["fills"] or 0),
            "ratchets": int(row["ratchets"] or 0),
            "exits": int(row["exits"] or 0),
            "naked_at_1515": int(row["naked_at_1515"] or 0),
            "first_live_entries_counted": int(row["first_live_entries_counted"] or 0),
        }

    # -- TwtStore: the money -----------------------------------------------------------------

    def config(self) -> dict:
        row = self.conn.execute(
            f"SELECT sleeve_capital_inr, max_open_positions, max_position_pct, stop_pct, "
            f"trail_pct, first_live_entries_left, dry_run_sessions, updated_at, updated_by "
            f"FROM {self.t('tw_config')} WHERE user_id = ?",
            (self.user_id,),
        ).fetchone()
        if row is None:
            # An unseeded sleeve is ₹0, not an error: the two states produce the same plan —
            # one with every signal skipped `NO_SLEEVE_CAPITAL` (TW5.1).
            return {"sleeve_capital_inr": Decimal(0), "first_live_entries_left": 0}
        return {
            "sleeve_capital_inr": _dec(row["sleeve_capital_inr"]) or Decimal(0),
            "max_open_positions": int(row["max_open_positions"] or 10),
            "max_position_pct": _dec(row["max_position_pct"]),
            "stop_pct": _dec(row["stop_pct"]),
            "trail_pct": _dec(row["trail_pct"]),
            "first_live_entries_left": int(row["first_live_entries_left"] or 0),
            "dry_run_sessions": int(row["dry_run_sessions"] or 0),
            "updated_at": _stamp(row["updated_at"]),
            "updated_by": row["updated_by"],
        }

    def realised_pnl(self, as_of: dt.date) -> Decimal:
        row = self.conn.execute(
            f"SELECT COALESCE(SUM(pnl_inr), 0) AS total FROM {self.t('tw_position')} "
            "WHERE user_id = ? AND state = 'CLOSED' AND closed_on <= ?",
            (self.user_id, as_of),
        ).fetchone()
        return _dec(row["total"]) or Decimal(0)

    def marks(self, instrument_ids: list[int], as_of: dt.date) -> dict[int, Decimal]:
        """The latest published close **on or before** ``as_of``, per instrument (``04`` §9.1).

        On or before, never the latest row: a position marked at a bar from after the session
        being reported would be a look-ahead in the one place it would flatter the book — its
        own value (house rule 5).
        """
        if not instrument_ids:
            return {}
        marks = ", ".join("?" for _ in instrument_ids)
        rows = self.conn.execute(
            f"SELECT o.instrument_id, o.close FROM {self.t('ohlcv_daily')} o "
            f"JOIN (SELECT instrument_id, MAX(date) AS date FROM {self.t('ohlcv_daily')} "
            f"WHERE instrument_id IN ({marks}) AND date <= ? GROUP BY instrument_id) latest "
            "ON latest.instrument_id = o.instrument_id AND latest.date = o.date",
            (*instrument_ids, as_of),
        ).fetchall()
        return {int(row["instrument_id"]): _dec(row["close"]) or Decimal(0) for row in rows}

    def adj_factor(self, instrument_id: int, as_of: dt.date) -> Decimal:
        """The bar's own factor, so ``04`` §7.3 can tell a split under a hold later."""
        row = self.conn.execute(
            f"SELECT adj_factor FROM {self.t('ohlcv_daily')} WHERE instrument_id = ? "
            "AND date <= ? ORDER BY date DESC LIMIT 1",
            (int(instrument_id), as_of),
        ).fetchone()
        return (_dec(row["adj_factor"]) if row is not None else None) or Decimal(1)

    def sleeve_money(self, as_of: dt.date) -> Any:  # noqa: ANN401 - a SleeveMoney
        from . import twt_execute as _execute  # noqa: PLC0415 - the sibling, at call time

        positions = self.open_positions()
        return _execute.sleeve_money_from_rows(
            self.config(),
            positions,
            self.marks([int(row["instrument_id"]) for row in positions], as_of),
            self.realised_pnl(as_of),
        )

    # -- TwtStore: the two writes only the halt and the fill make ---------------------------

    def count_first_live_entry(
        self, position_id: int, *, session_date: dt.date, now: dt.datetime, live: bool
    ) -> bool:
        """``04`` §6.4 and TW5.3's three refusals, against the same two columns.

        * **Not on a proposed line** — the parameter is a ``position_id``, and a line nobody
          confirmed has no ``tw_position`` row to have an id.
        * **Not on a simulated fill** — half size is a live-money discipline, and a paper plan
          that is not the plan is not a rehearsal.
        * **Not twice for the same fill** — ``tw_position.half_size`` is the record of which
          entries the countdown applied to, so a row already marked is already counted.

        The write is audited into ``tw_config_audit`` like TW5's, because "who counted that
        entry" has to have an answer. It is not reimplemented for fun: this process has no async
        Postgres driver and cannot call :func:`baskfy_api.twt_sleeve.count_first_live_entry` at
        all. DECISIONS-TW **TW6.3**.
        """
        position = self.position(position_id)
        if position is None or position.get("half_size"):
            return False
        if not live or position.get("simulated"):
            return False
        config = self.config()
        left = int(config.get("first_live_entries_left") or 0)
        if left <= 0:
            return False
        self._audit("first_live_entries_left", str(left), str(left - 1), now, "twt-fill")
        self.conn.execute(
            f"UPDATE {self.t('tw_config')} SET first_live_entries_left = ?, updated_by = ? "
            "WHERE user_id = ?",
            (left - 1, "twt-fill", self.user_id),
        )
        self.conn.execute(
            f"UPDATE {self.t('tw_position')} SET half_size = ? WHERE user_id = ? AND id = ?",
            (True, self.user_id, int(position_id)),
        )
        self.conn.execute(
            f"INSERT INTO {self.t('tw_session')} "
            "(user_id, session_date, mode, first_live_entries_counted) VALUES (?, ?, 'LIVE', 1) "
            "ON CONFLICT (user_id, session_date) DO UPDATE SET "
            "first_live_entries_counted = tw_session.first_live_entries_counted + 1",
            (self.user_id, session_date),
        )
        return True

    def _audit(  # noqa: PLR0913 - one argument per column of the audit row
        self,
        key: str,
        old: str | None,
        new: str | None,
        now: dt.datetime,
        changed_by: str,
        note: str | None = None,
    ) -> None:
        self.conn.execute(
            f"INSERT INTO {self.t('tw_config_audit')} "
            "(user_id, key, old_value, new_value, changed_at, changed_by, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.user_id, key, old, new, now, changed_by, note),
        )

    def zero_capital(self, *, now: dt.datetime, changed_by: str) -> Decimal | None:
        """The halt's first behaviour: capital to ₹0, **audited with the previous value**.

        The previous value is in ``tw_config_audit`` so the sleeve can be restored by reading a
        row rather than by remembering a number at 09:20 on a bad morning.
        """
        config = self.config()
        before = _dec(config.get("sleeve_capital_inr"))
        row = self.conn.execute(
            f"SELECT 1 AS present FROM {self.t('tw_config')} WHERE user_id = ?",
            (self.user_id,),
        ).fetchone()
        if row is None:
            return None
        self._audit(
            "sleeve_capital_inr",
            None if before is None else str(before),
            "0",
            now,
            changed_by,
            note="halted from the desk: the sleeve cannot size a line at zero",
        )
        self.conn.execute(
            f"UPDATE {self.t('tw_config')} SET sleeve_capital_inr = 0, updated_by = ? "
            "WHERE user_id = ?",
            (changed_by, self.user_id),
        )
        return before

    def expire_plans(self, now: dt.datetime) -> int:
        """The halt's second behaviour: no ``plan_id`` in anybody's browser can still be
        confirmed.

        The plan rows are **kept** and their ``expires_at`` is moved back, rather than deleted:
        the record of what was proposed on the morning somebody pressed the stop button is worth
        more than the rows are worth reclaiming, and ``/twt/execute`` answers 410 either way.
        """
        cursor = self.conn.execute(
            f"UPDATE {self.t('tw_plan')} SET expires_at = ? "
            "WHERE user_id = ? AND expires_at > ?",
            (now, self.user_id, now),
        )
        return int(getattr(cursor, "rowcount", 0) or 0)

    # -- the page's own reads ----------------------------------------------------------------

    def todays_plan(self, on_or_before: dt.date) -> dict | None:
        """The newest plan at or before the date — the MORNING rebuild wins over the EVENING."""
        row = self.conn.execute(
            f"SELECT id, plan_id, session_date, source, built_at, expires_at, gate, "
            f"sleeve_equity_inr, total_new_exposure_inr FROM {self.t('tw_plan')} "
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
            f"SELECT s.reason, s.detail, i.symbol FROM {self.t('tw_plan_skip')} s "
            f"JOIN {self.t('instrument')} i ON i.id = s.instrument_id "
            "WHERE s.user_id = ? AND s.plan_id = ? ORDER BY i.symbol",
            (self.user_id, int(plan_pk)),
        ).fetchall()
        return [
            {"reason": str(row["reason"]), "detail": row["detail"], "symbol": str(row["symbol"])}
            for row in rows
        ]

    # -- TW12: the "Scan now" button's one table -------------------------------------------

    def _scan_run_row(self, row: Any) -> dict:  # noqa: ANN401 - a driver row
        detail = row["detail"]
        return {
            "id": int(row["id"]),
            "requested_at": _stamp(row["requested_at"]),
            "started_at": _stamp(row["started_at"]),
            "finished_at": _stamp(row["finished_at"]),
            "session_date": _date(row["session_date"]),
            "status": str(row["status"]),
            "source": str(row["source"]),
            "detail": json.loads(detail) if isinstance(detail, str) else detail,
            "error": row["error"],
            "task_id": row["task_id"],
        }

    _SCAN_SELECT: Final = (
        "SELECT id, requested_at, started_at, finished_at, session_date, status, source, "
        "detail, error, task_id FROM "
    )

    def newest_scan(self) -> dict | None:
        """This user's most recent **Scan now** request, whatever state it is in (TW12).

        Both refusals are answered from this row rather than from a cache, so the rule holds on a
        box with no Redis and is testable against the database alone — the argument SW15.1 made
        for the swing book and VB12 repeated for VBT-1.
        """
        row = self.conn.execute(
            f"{self._SCAN_SELECT}{self.t('tw_scan_run')} "
            "WHERE user_id = ? ORDER BY requested_at DESC, id DESC LIMIT 1",
            (self.user_id,),
        ).fetchone()
        return None if row is None else self._scan_run_row(row)

    def scan_run(self, run_id: int) -> dict | None:
        """One run by id, scoped to this user. ``None`` for somebody else's — the page must not
        be able to poll a run it did not ask for, even on a desk with one tenant."""
        row = self.conn.execute(
            f"{self._SCAN_SELECT}{self.t('tw_scan_run')} WHERE user_id = ? AND id = ?",
            (self.user_id, int(run_id)),
        ).fetchone()
        return None if row is None else self._scan_run_row(row)

    def request_scan(
        self,
        *,
        now: dt.datetime,
        min_interval: dt.timedelta,
        stale_after: dt.timedelta,
        source: str = "desk",
    ) -> int:
        """Insert one ``QUEUED`` run for the worker's sweep to publish, or refuse.

        **The desk has no Celery client**, so this is the whole of the button's write: a row.
        Raises :class:`ScanRefused` with the contract's two statuses — 409 while a scan younger
        than ``stale_after`` is ``QUEUED``/``RUNNING``, 429 when the newest request of any state
        is inside ``min_interval``.

        The order of the two checks matters and is the swing desk's: in-flight first, so a second
        press two seconds after the first is told *what is happening* ("scan 4 is queued") rather
        than *how long to wait*, which is the less useful of the two true answers.

        It writes one row. It cannot size, place or cancel anything.
        """
        newest = self.newest_scan()
        if newest is not None and newest["requested_at"] is not None:
            requested_at = newest["requested_at"]
            in_flight = newest["status"] in SCAN_IN_FLIGHT
            if in_flight and requested_at > now - stale_after:
                raise ScanRefused(
                    409,
                    f"Scan {newest['id']} is {str(newest['status']).lower()}; its result is on "
                    f"its way.",
                    run_id=newest["id"],
                )
            if requested_at > now - min_interval:
                wait = min_interval - (now - requested_at)
                seconds = max(1, int(wait.total_seconds() + 0.999))
                ago = int((now - requested_at).total_seconds())
                raise ScanRefused(
                    429,
                    f"A scan was requested {ago} s ago; one a minute is the limit. "
                    f"Try again in {seconds} s.",
                    run_id=newest["id"],
                    retry_after=seconds,
                )
        row = self.conn.execute(
            f"INSERT INTO {self.t('tw_scan_run')} (user_id, requested_at, status, source) "
            "VALUES (?, ?, 'QUEUED', ?) RETURNING id",
            (self.user_id, now, str(source)),
        ).fetchone()
        return int(row["id"])

    def breadth(self, on: dt.date) -> dict | None:
        row = self.conn.execute(
            f"SELECT date, measured_count, above_count, pct_above_dma, gate, thin_session "
            f"FROM {self.t('tw_breadth_daily')} WHERE user_id = ? AND date = ?",
            (self.user_id, on),
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


def _gates() -> dict[str, bool]:
    from . import twt_execute  # noqa: PLC0415 - the sibling module, resolved at call time

    gates = twt_execute.twt_gates()
    return {
        "dry_run": bool(gates.dry_run),
        "execution_enabled": bool(C.TWT_EXECUTION_ENABLED),
        "desk_dry_run": bool(C.DRY_RUN),
    }


def _countdown(plan: dict | None, now: dt.datetime) -> str:
    """How long the plan has left, in words. **An expired plan's buttons are gone, not
    disabled** (``05`` §2), and this is the sentence that says why."""
    if plan is None or plan["expires_at"] is None:
        return ""
    left = (plan["expires_at"] - now).total_seconds()
    if left <= 0:
        return (
            f"expired at {plan['expires_at'].astimezone(IST).strftime('%H:%M')} — rebuild it "
            f"(make twt-plan SOURCE=MORNING) and read it again"
        )
    return f"{int(left // 60)} min {int(left % 60)} s left"


def _decorated(line: dict, *, expired: bool) -> dict:
    """The three things the row needs that are not columns.

    ``confirmable`` is what decides whether a Confirm button is rendered **at all**: ``05`` §2
    says an expired plan's buttons are *gone*, not disabled, because a disabled button is a
    thing a person tries to press.
    """
    return {
        **line,
        "label": KIND_LABEL.get(line["kind"], line["kind"]),
        "expired": expired,
        "confirmable": (not expired) and line["state"] == "PROPOSED",
    }


def build_view(store: PgTwtStore, *, now: dt.datetime) -> dict:
    """Everything ``05`` §2's page shows, as one dict. Reads only; places nothing."""
    from . import twt_execute  # noqa: PLC0415 - the sibling module, at call time

    today = now.astimezone(IST).date()
    plan = store.todays_plan(today)
    expired = bool(plan and plan["expires_at"] and plan["expires_at"] <= now)
    lines = [
        _decorated(line, expired=expired) for line in (store.lines_for(plan["id"]) if plan else [])
    ]
    skips = store.skips_for(plan["id"]) if plan else []
    book = store.open_positions()
    naked = twt_execute.naked_positions(store)
    session_date = plan["session_date"] if plan else today
    gates = _gates()
    config = store.config()
    return {
        "available": True,
        "now": now,
        "today": today,
        "plan": plan,
        "countdown": _countdown(plan, now),
        "expired": expired,
        "lines": lines,
        "exits": [line for line in lines if line["kind"] in ("ARM_GTT", "RAISE_GTT_STOP")],
        "entries": [line for line in lines if line["kind"] == "BUY_AT_OPEN"],
        "labels": KIND_LABEL,
        "skips": skips,
        "book": book,
        "naked": [row["symbol"] for row in naked],
        "breadth": store.breadth(session_date),
        "session": store.session(session_date),
        "config": config,
        "money": dataclasses.asdict(store.sleeve_money(session_date)),
        "first_live_entries_left": config.get("first_live_entries_left", 0),
        "max_new_entries_per_session": DEFAULT_TWT_CONFIG.sizing.max_new_entries_per_session,
        "sweep_due": now.astimezone(IST).time() >= SWEEP_AT,
        "plan_ttl_minutes": PLAN_TTL_MINUTES,
        **gates,
        "reason": "",
    }


def unavailable_view(reason: str, *, now: dt.datetime) -> dict:
    """A page that says why it is empty. A desk with no ``tw_`` tables is a state, not a 500."""
    gates = _gates()
    return {
        "available": False,
        "now": now,
        "today": now.astimezone(IST).date(),
        "plan": None,
        "countdown": "",
        "expired": False,
        "lines": [],
        "exits": [],
        "entries": [],
        "labels": KIND_LABEL,
        "skips": [],
        "book": [],
        "naked": [],
        "breadth": None,
        "session": None,
        "config": {},
        "money": {},
        "first_live_entries_left": 0,
        "max_new_entries_per_session": DEFAULT_TWT_CONFIG.sizing.max_new_entries_per_session,
        "sweep_due": False,
        "plan_ttl_minutes": PLAN_TTL_MINUTES,
        **gates,
        "reason": reason,
    }


# --- wiring -------------------------------------------------------------------------------


@contextlib.contextmanager
def open_store() -> Iterator[PgTwtStore]:
    """The sole user's store over the desk's connection. Tests replace this with their own."""
    from .analytics import db as _db  # noqa: PLC0415 - the desk imports its DB lazily

    with _db.connect() as conn:
        schema = SCHEMA if _db.DB_BACKEND == "postgres" else ""
        yield PgTwtStore(
            conn,
            user_id=C.SOLE_USER_ID,
            schema=schema,
            broker_account_id=C.SOLE_BROKER_ACCOUNT_ID,
        )


_twt_gateway: Any = None


def twt_gateway() -> Any:  # noqa: ANN401 - an OrderGateway, built by the execute module
    """This sleeve's gateway, built once and lazily — never at import, because building it needs
    a Kite client and a page must import without one.

    It shares ``app.main``'s risk manager: one account, one daily-loss cap, one order counter, so
    this sleeve cannot spend a limit another book has already used.
    """
    global _twt_gateway
    if _twt_gateway is None:
        from . import main as _main  # noqa: PLC0415 - main mounts this router; at call time
        from . import twt_execute  # noqa: PLC0415 - the sibling module, at call time

        _main.gateway()  # builds `_risk` as a side effect
        _twt_gateway = twt_execute.build_twt_gateway(_main.kite().kc, _main._risk)
    return _twt_gateway


def last_price(symbol: str) -> Decimal | None:
    """The instrument's last traded price, or ``None`` when the desk has no session.

    **A read, not an order.** Every executable kind needs it: a ``BUY_AT_OPEN`` is a MARKET order
    whose risk valuation has no price of its own, and both GTT kinds are refused without one
    because a trigger at or above the last price fires the moment it is armed.
    """
    try:
        from . import main as _main  # noqa: PLC0415 - main mounts this router; at call time

        value = _main.kite().ltp([symbol]).get(symbol)
    except Exception as exc:  # noqa: BLE001 - no session, no price; the confirm says so
        log.warning("no last price for %s: %s", symbol, exc)
        return None
    return Decimal(str(value)) if value else None


class KiteGtts:
    """The broker's resting triggers, as a **read**. Never a write.

    ``/twt/reconcile`` uses it to attach a hand-armed GTT to the position it protects. There is
    no method here that could cancel one: if Baskfy and Kite disagree about whether a GTT exists,
    the safe direction is always *more* protection.
    """

    def __init__(self, kite: Any) -> None:  # noqa: ANN401 - the desk's Kite wrapper
        self.kite = kite

    def list_gtts(self) -> list[dict]:
        triggers = self.kite.kc.get_gtts()
        out: list[dict] = []
        for trigger in triggers or []:
            condition = trigger.get("condition") or {}
            values = condition.get("trigger_values") or []
            out.append(
                {
                    "gtt_id": trigger.get("id"),
                    "symbol": condition.get("tradingsymbol"),
                    "trigger": values[0] if values else None,
                    "status": trigger.get("status"),
                }
            )
        return out


def gtt_source() -> Any:  # noqa: ANN401 - a GttSource, or None without a session
    try:
        from . import main as _main  # noqa: PLC0415

        return KiteGtts(_main.kite())
    except Exception as exc:  # noqa: BLE001 - reported, not fatal: the reconcile is a pull
        log.warning("no GTT source: %s", exc)
        return None


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
        log.warning("twt view unavailable: %s", exc)
        return unavailable_view(str(exc)[:200], now=now)


def _outcome_json(outcome: Any, **extra: Any) -> dict:  # noqa: ANN401 - an ExecOutcome
    body = dataclasses.asdict(outcome) if dataclasses.is_dataclass(outcome) else dict(vars(outcome))
    return _jsonable({**body, **extra})


def _refused_json(exc: Exception, **extra: Any) -> dict:
    """A guard's refusal in the outcome vocabulary — non-negotiable 7, as an answer.

    ``guards.assert_tradeable`` **raises** on an SGB, a G-sec or an ``EXCLUDED_SYMBOLS`` name,
    at the lowest layer and before any network call. That is the right behaviour and the wrong
    *shape* for a route: a 500 tells a person nothing at 09:15. The swing desk answers the same
    way, and this sleeve should never see one at all — its universe has no such instrument — but
    "should never" is exactly the condition worth having an answer for.
    """
    from . import twt_execute as _execute  # noqa: PLC0415 - the sibling module, at call time

    return _jsonable(
        {
            "status": "BLOCKED",
            "reason": str(exc),
            "order": None,
            "gtt": None,
            "order_row_id": None,
            "position_id": None,
            "simulated": _execute.twt_gates().dry_run,
            "naked": False,
            **extra,
        }
    )


def _prices_for(book: list[dict]) -> dict[str, Decimal]:
    prices: dict[str, Decimal] = {}
    for row in book:
        found = last_price(str(row["symbol"]))
        if found is not None:
            prices[str(row["symbol"])] = found
    return prices


# --- routes -------------------------------------------------------------------------------


@router.get("/twt", response_class=HTMLResponse)
def twt_page(request: Request) -> Any:  # noqa: ANN401 - a TemplateResponse
    return _templates().TemplateResponse(request, "twt.html", {"v": _view()})


@router.get("/twt/data")
def twt_data() -> Any:  # noqa: ANN401 - a JSON-able dict
    return _jsonable(_view())


@router.post("/twt/scan", status_code=202)
def twt_scan_now() -> Any:  # noqa: ANN401 - a JSON-able dict
    """TW12: ask for the latest **published** session to be detected again. Places nothing.

    The night the chain's step was skipped because the quality gate refused the day, and the
    morning after a threshold changed, this is the way to get the funnel recomputed without
    waiting for 21:00 or reaching for a shell.

    **It is not the swing book's "Scan now", although it is spelled like it.** That one scans
    *today* from live quotes and labels its rows provisional. This one cannot and should not:
    ``04`` §2 reads three *weekly* ranges that have closed, a monthly low, and a sessions-out
    count over closed sessions. A bar built from a quote at 13:42 would change the answer without
    making it truer. So there is no provisional path and no provisional column — the same
    conclusion VB12 reached from different arithmetic (DECISIONS-TW **TW12.2**).

    **202** with the run to poll; **409** while one is in flight, naming it; **429** inside the
    minute, with ``Retry-After``. Both refusals are answered from ``tw_scan_run`` rather than from
    a cache, so the rule holds on a box with no Redis and is testable against the database alone.

    It writes one row, and the worker's sweep publishes it. Nothing on this path can size, place
    or cancel anything: the task it queues calls the nightly's own detector and names no broker,
    and ``packages/core/tests/test_twt_safety_properties.py`` asserts that over this route.
    """
    now = _now()
    with open_store() as store:
        try:
            run_id = store.request_scan(
                now=now,
                min_interval=dt.timedelta(seconds=SCAN_MIN_INTERVAL_SECONDS),
                stale_after=dt.timedelta(seconds=SCAN_STALE_AFTER_SECONDS),
            )
        except ScanRefused as exc:
            headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
            raise HTTPException(exc.status, exc.reason, headers=headers) from exc
    return _jsonable({"run_id": run_id, "status": "QUEUED", "requested_at": now})


@router.get("/twt/scan/{run_id}")
def twt_scan_status(run_id: int) -> Any:  # noqa: ANN401 - a JSON-able dict
    """One scan's state: ``QUEUED`` -> ``RUNNING`` -> ``DONE`` | ``FAILED``.

    ``session_date`` is null until the worker has decided which session it is detecting, because
    the caller asks for "the latest" and only the worker knows which that is. ``detail`` carries
    the funnel on ``DONE`` and ``error`` the reason on ``FAILED``. A run id this user did not
    request is a **404**, not somebody else's row.
    """
    with open_store() as store:
        run = store.scan_run(run_id)
    if run is None:
        raise HTTPException(404, f"No scan {run_id}.")
    return _jsonable(run)


@router.post("/twt/execute")
async def twt_execute_line(
    plan_id: str = Form(...), line_id: int = Form(...), confirm: str = Form(...)
) -> Any:  # noqa: ANN401 - a JSON-able dict
    """One line, one click. **There is no route that takes more than one line, and no route that
    takes none** (``02`` Track C §3).

    The gates — ``confirm``, the plan, its thirty-minute expiry, the line's kind and its state —
    are ``execute_line``'s and raise through here as 400 / 404 / 410 / 409.
    """
    from . import twt_execute as _execute  # noqa: PLC0415 - the sibling module, at call time

    with open_store() as store:
        line = store.line(line_id)
        # EVERY EXECUTABLE KIND NEEDS THE MARKET NOW. The entry is a MARKET order, so the live
        # price is what the risk layer values it at; both GTT kinds are refused without one,
        # because a trigger at or above the last price fires the moment it is armed. Read here,
        # after the cheap refusals inside `execute_line` would have run — a 400/404/410/409 on
        # an unknown line still costs no broker read, because `line` is None and this is skipped.
        price = last_price(line["symbol"]) if line is not None else None
        try:
            outcome = await _execute.execute_line(
                store,
                twt_gateway(),
                plan_id=plan_id,
                line_id=line_id,
                confirm=confirm,
                now=_now(),
                last_price=price,
            )
        except UntouchableInstrumentError as exc:
            return _refused_json(exc, line_id=line_id)
    return _outcome_json(outcome, line_id=line_id)


@router.post("/twt/halt")
def twt_halt(confirm: str = Form(...)) -> Any:  # noqa: ANN401 - a JSON-able dict
    """**Stop the sleeve.** One line, runnable from a phone, and it never removes protection.

    ``curl -fsS -X POST $DESK/twt/halt -u desk:$DESK_PW -H "Origin: $DESK" -d confirm=true``

    It zeroes the sleeve's capital (audited, with the previous value) and expires every live
    plan. **Every resting GTT stays exactly where it is**, and ``ARM_GTT``, ``RAISE_GTT_STOP``,
    ``/twt/rearm`` and ``/twt/sweep`` all keep working: a halted sleeve is a sleeve that cannot
    *buy*, and everything it already holds keeps its stop and keeps ratcheting.

    It touches this sleeve and nothing else — the weekly book, the swing book and VBT-1 are not
    reachable from here.
    """
    from . import twt_execute as _execute  # noqa: PLC0415 - the sibling module, at call time

    with open_store() as store:
        report = _execute.halt_sleeve(store, confirm=confirm, now=_now())
    return _jsonable(
        {
            "halted": report.halted,
            "sleeve_capital_inr_before": report.sleeve_capital_inr_before,
            "plans_expired": report.plans_expired,
            "protection_untouched": True,
            "message": report.line(),
        }
    )


@router.post("/twt/rearm")
async def twt_rearm(position_id: int = Form(...), confirm: str = Form(...)) -> Any:  # noqa: ANN401
    """``05`` §2's Re-arm button: one naked position, one stop. **It is a stop, not a buy.**"""
    from . import twt_execute as _execute  # noqa: PLC0415 - the sibling module, at call time

    with open_store() as store:
        position = store.position(position_id)
        price = last_price(str(position["symbol"])) if position is not None else None
        try:
            outcome = await _execute.rearm_gtt(
                store,
                twt_gateway(),
                position_id=position_id,
                confirm=confirm,
                now=_now(),
                last_price=price,
            )
        except UntouchableInstrumentError as exc:
            return _refused_json(exc, position_id=position_id)
    return _outcome_json(outcome, position_id=position_id)


@router.post("/twt/sweep")
async def twt_sweep(confirm: str = Form(...)) -> Any:  # noqa: ANN401 - a JSON-able dict
    """The 15:15 chore as a route, so it is checkable from a phone. Expect ``naked: 0``.

    Idempotent and keyed on the day. It re-arms what it finds naked and says what is still
    naked afterwards; it places no buy and cancels nothing, which is why it is safe to run at
    any hour and why a halted sleeve still runs it.
    """
    from . import twt_execute as _execute  # noqa: PLC0415 - the sibling module, at call time

    if str(confirm).lower() != "true":
        raise HTTPException(400, "The 15:15 sweep requires explicit confirmation.")
    with open_store() as store:
        report = await _execute.sweep_naked(
            store,
            twt_gateway(),
            now=_now(),
            prices=_prices_for(_execute.naked_positions(store)),
        )
    return _jsonable(
        {
            "naked_before": report.naked_before,
            "rearmed": report.rearmed,
            "naked": report.naked,
            "outcomes": [_outcome_json(one) for one in report.outcomes],
        }
    )


@router.post("/twt/reconcile")
def twt_reconcile(confirm: str = Form(...)) -> Any:  # noqa: ANN401 - a JSON-able dict
    """Attach a **hand-armed** GTT to the position it protects (FIRST-LIVE-MORNING §9.2 step 3).

    Without it a line Maulik protects by hand in the Kite app reads naked forever, and the 15:15
    sweep keeps shouting about a line that is in fact covered — and would try to arm a second
    trigger on it. A read of the broker and a write of our own book: it places nothing, cancels
    nothing and moves no stop.
    """
    from . import twt_execute as _execute  # noqa: PLC0415 - the sibling module, at call time

    if str(confirm).lower() != "true":
        raise HTTPException(400, "Reconciling stops requires explicit confirmation.")
    with open_store() as store:
        report = _execute.reconcile_gtts(store, gtt_source(), now=_now())
    return _jsonable(
        {
            "attached": report.attached,
            "unmatched": report.unmatched,
            "positions": list(report.positions),
        }
    )
